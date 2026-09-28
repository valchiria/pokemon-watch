"""Robot Pokémon v3 — cerca i prodotti Pokémon sigillati in italiano nei negozi e scrive su Telegram.

Cosa fa a ogni giro (ogni ora)
- Per ogni set in config.json cerca il nome del set in ogni negozio con la ricerca ufficiale del
  negozio (Shopify o WooCommerce), che dice se un prodotto si può comprare e a che prezzo.
- Riconosce il prodotto dal titolo (box 36, set allenatore, Ultra Premium Umbreon...) e scarta
  inglese, giapponese, case da più box, carte singole e gadget.
- Capisce se un prodotto è in preordine: dal titolo/descrizione ("preordine", "uscita prevista
  il...") oppure perché il set non è ancora uscito.
- Avvisa quando:
    📅 si apre un preordine (con TUTTI i negozi dove è prenotabile e quelli non ancora aperti)
    👀 compare la scheda di un prodotto non ancora prenotabile
    🟢 un prodotto torna comprabile al prezzo giusto, 🆕 compare, 💶 scende di prezzo
    ⏰ mancano 6 settimane, 1 settimana o 1 giorno a un'uscita del calendario
- Il prezzo considera la spedizione di ogni negozio e ricorda il minimo mai visto.
- Ogni mattina dalle 9 manda il riepilogo; il lunedì anche il valore delle carte di ogni set.
"""
import html
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

import market

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"
NAMES_FILE = ROOT / "pokemon_names.txt"
TZ = ZoneInfo("Europe/Rome")
SUMMARY_HOUR = 9
DEFAULT_CHAT_ID = "875856621"
STATE_VERSION = 3
MAX_ALERTS = 10
REMINDERS = [42, 7, 1]  # giorni prima di un'uscita in cui arriva il promemoria
TG_LIMIT = 3900

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Accept": "application/json,text/html;q=0.8,*/*;q=0.5",
    "Accept-Language": "it-IT,it;q=0.9",
}

# ------------------------------------------------------------------ testo


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = s.replace("°", " ").replace("º", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return f" {s.strip()} "


def has(t, *words):
    return any(f" {w} " in t for w in words)


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))


FOREIGN = ["eng", "english", "inglese", "en", "jap", "jpn", "japanese", "giapponese", "giappone",
           "kor", "korean", "coreano", "chinese", "cinese", "chn", "de", "deutsch", "tedesco",
           "fr", "francese", "francais", "es", "spagnolo", "espanol"]
BULK = ["case", "cassa", "x6", "6x", "x2", "2x", "x3", "3x", "factory sealed", "sealed case", "lotto", "set di"]
NOT_SEALED = ["mazzo", "mazzi", "deck", "portfolio", "raccoglitore vuoto", "album", "bustine protettive",
              "sleeves", "toolkit", "funko", "peluche", "plush", "gadget", "tappetino", "playmat"]

TYPES_PACKS = {"Box 36 buste": 36, "Set Allenatore": 9, "Bundle 6 buste": 6, "Ultra Premium": 18,
               "Collezione Premium": 8, "Tin": 4, "Mini Tin": 2}
PACK_TYPES = {"Box 36 buste", "Set Allenatore", "Bundle 6 buste", "Blister"}
COLLECTOR_TYPES = {"Ultra Premium", "Collezione Premium"}
FEATURES = [("pokemon center", "Pokémon Center"), ("poster", "Poster"), ("adesivi", "Adesivi"),
            ("sticker", "Adesivi"), ("raccoglitore", "Raccoglitore"), ("binder", "Raccoglitore"),
            ("figura", "Figura"), ("spilla", "Spille"), ("spille", "Spille"), ("moneta", "Moneta")]


def load_names():
    try:
        return [n.strip() for n in NAMES_FILE.read_text().splitlines() if len(n.strip()) >= 3]
    except OSError:
        return []


POKEMON = load_names()


def classify(title):
    """Tipo di prodotto sigillato dal titolo, oppure None se va scartato."""
    t = norm(title)
    if has(t, *FOREIGN) or any(f" {b} " in t for b in BULK):
        return None
    if has(t, "carta", "card", "singola", "promo") and not has(t, "box", "display", "bundle", "blister",
                                                               "collezione", "set", "tin"):
        return None
    if any(f" {w} " in t for w in NOT_SEALED):
        return None
    if has(t, "checklane", "paper", "sleeve", "sleeved") or "busta blister" in t or "bustina blister" in t:
        return None  # buste singole: non sono prodotti da collezione
    if "collezionista" in t and has(t, "bundle", "lotto", "pacchetto"):
        return None  # "bundle del collezionista": pacchetti fatti dal negozio
    if re.search(r" bundle \d+ (tin|collezion|box|set) ", t):
        return None
    if "ultra premium" in t:
        return "Ultra Premium"
    if has(t, "set allenatore", "etb", "elite trainer", "allenatore fuoriclasse"):
        return "Set Allenatore"
    if "premium" in t:
        return "Collezione Premium"
    if has(t, "mini tin"):
        return "Mini Tin"
    if has(t, "tin"):
        return "Tin"
    if has(t, "box", "display", "booster box") and (" 36 " in t or "36 bust" in t):
        return "Box 36 buste"
    if has(t, "bundle", "booster bundle") or re.search(r" (confezione|pack|set) (da |di )?6 bust", t) \
            or re.search(r" 6 (buste|bustine) ", t):
        return "Bundle 6 buste"
    if has(t, "blister"):
        return "Blister"
    if has(t, "collezione", "collection"):
        return "Collezione"
    return None


@lru_cache(maxsize=4096)
def variant(title, kind):
    """Cosa distingue prodotti diversi dello stesso tipo (Ultra Premium Umbreon vs Espeon)."""
    t = norm(title)
    pc = "pokemon center" in t
    if kind in ("Box 36 buste", "Bundle 6 buste", "Blister"):
        return ""
    if kind == "Set Allenatore":
        return "Pokémon Center" if pc else ""
    feats = [label for kw, label in FEATURES if f" {kw} " in t]
    if feats and kind in ("Collezione", "Collezione Premium"):
        return feats[0]
    names = [n for n in POKEMON if f" {n} " in t]
    names = [n for n in names if not any(n != m and n in m for m in names)]
    out = " ".join(n.title() for n in sorted(names))
    return ("Pokémon Center " + out).strip() if pc else out


def packs(kind, title):
    if kind == "Blister":
        return 3 if re.search(r" 3 (buste|bustine|pack)", norm(title)) else 2
    return TYPES_PACKS.get(kind)


def matches_set(title, s):
    t = norm(title)
    if norm(s["cerca"]).strip() in t:
        return True
    # gli alias (es. "30th") sono usati anche in inglese: li accettiamo solo se il titolo dice ITA
    return any(norm(k).strip() and norm(k).strip() in t for k in s.get("alias", [])) and \
        has(t, "it", "ita", "italiano", "italiana", "italian")


def parse_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("€", "").replace("\xa0", "").strip()
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ preordini e date

PRE_WORDS = re.compile(r"pre ?-?ordin|preorder|pre order|prevendit|prenotazion|prenotabil|"
                       r"uscita prevista|in uscita|disponibile dal|disponibile a partire", re.I)
# solo parole di uscita: le date vicino a "spedizione", "consegna", "offerta valida" non contano
DATE_CONTEXT = re.compile(r"uscit|rilasci|disponibil|arriv|preordin|pre-ordin|prevendit|release|"
                          r"a partire", re.I)
MONTHS = {"gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6, "lug": 7, "ago": 8,
          "set": 9, "ott": 10, "nov": 11, "dic": 12}
RE_NUM_DATE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")
RE_SHORT_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b(?![/.\-]\d)")
RE_TXT_DATE = re.compile(r"\b(\d{1,2})\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-z]*\.?"
                         r"(?:\s+(\d{4}))?", re.I)


def find_release_date(text, today):
    """Prima data plausibile di uscita/spedizione citata nel testo (vicino a 'uscita', 'disponibile'...)."""
    if not text:
        return None
    low = strip_html(text).lower()
    found = []
    for m in RE_NUM_DATE.finditer(low):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        found.append((m.start(), d, mo, y))
    for m in RE_SHORT_DATE.finditer(low):
        if len(m.group(1)) + len(m.group(2)) < 3:
            continue  # "1/2" è più probabilmente una frazione che una data
        found.append((m.start(), int(m.group(1)), int(m.group(2)), None))
    for m in RE_TXT_DATE.finditer(low):
        d, mo = int(m.group(1)), MONTHS[m.group(2).lower()[:3]]
        found.append((m.start(), d, mo, int(m.group(3)) if m.group(3) else None))
    for pos, d, mo, y in sorted(found):
        if not DATE_CONTEXT.search(low[max(0, pos - 45):pos]):
            continue
        try:
            if y:
                dt = date(y, mo, d)
            else:
                # senza anno: quest'anno se è recente o futura; l'anno prossimo solo se la data di
                # quest'anno è di oltre 8 mesi fa (es. "15 gennaio" scritto a settembre)
                dt = date(today.year, mo, d)
                if dt < today - timedelta(days=240):
                    dt = date(today.year + 1, mo, d)
        except ValueError:
            continue
        if today - timedelta(days=10) <= dt <= today + timedelta(days=400):
            return dt
    return None


def release_of(o, cfg):
    """Data di uscita nota per l'offerta: dal testo del negozio o dalla data ufficiale del set."""
    if o.get("uscita"):
        return date.fromisoformat(o["uscita"])
    s = next((x for x in cfg["set"] if x["nome"] == o.get("set")), None)
    if s and s.get("uscita") and s.get("certo"):
        return date.fromisoformat(s["uscita"])
    return None


def status(o, cfg, today):
    """preordine / in_arrivo / disponibile / esaurito."""
    flag = bool(o.get("preordine"))
    s = next((x for x in cfg["set"] if x["nome"] == o.get("set")), {})
    if o.get("uscita"):  # data scritta dal negozio: la più precisa
        pre = date.fromisoformat(o["uscita"]) > today
    elif s.get("uscita") and s.get("certo"):
        # set già uscito: la parola "preordine" rimasta nel titolo non conta, tranne nei set
        # speciali che escono a ondate, se il calendario ha un'uscita futura per quel prodotto
        # (es. Ultra Premium del 30° mesi dopo il set)
        wave = flag and bool(s.get("speciale")) and any(
            date.fromisoformat(ev["data"]) > today and matches_event(o, ev) for ev in cfg.get("calendario", []))
        pre = date.fromisoformat(s["uscita"]) > today or wave
    else:
        pre = flag
    if o.get("comprabile"):
        return "preordine" if pre else "disponibile"
    return "in_arrivo" if pre else "esaurito"


# ------------------------------------------------------------------ negozi


def get_json(session, url):
    r = session.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


def search_shopify(session, site, query, today):
    url = (f"{site}/search/suggest.json?q={quote(query)}&resources[type]=product"
           "&resources[limit]=10&resources[options][unavailable_products]=last")
    data = get_json(session, url)
    out = []
    for p in data.get("resources", {}).get("results", {}).get("products", []):
        link = p.get("url", "")
        link = link if link.startswith("http") else site + link.split("?")[0]
        title = p.get("title", "")
        tags = " ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else str(p.get("tags") or "")
        body = strip_html(p.get("body", ""))[:3000]
        rel = find_release_date(title, today) or find_release_date(body, today)
        head = bool(PRE_WORDS.search(title) or PRE_WORDS.search(tags))
        rel = rel if head or PRE_WORDS.search(body) else None  # una data da sola non basta
        pre = head or bool(rel)  # nel testo lungo "preordine" conta solo insieme a una data
        out.append({"titolo": title, "url": link, "comprabile": bool(p.get("available")),
                    "prezzo": parse_price(p.get("price") or p.get("price_min")),
                    "preordine": pre, "uscita": rel.isoformat() if rel else None})
    return out


def search_woo(session, site, query, today, extra=""):
    url = f"{site}/wp-json/wc/store/v1/products?search={quote(query)}&per_page=50{extra}"
    data = get_json(session, url)
    out = []
    for p in data if isinstance(data, list) else []:
        prices = p.get("prices") or {}
        minor = prices.get("currency_minor_unit")
        minor = 2 if minor in (None, "") else int(minor)
        raw = prices.get("price")
        price = int(raw) / 10 ** minor if raw not in (None, "") and str(raw).isdigit() else parse_price(raw)
        title = strip_html(p.get("name", "")).strip()
        body = strip_html((p.get("short_description") or "") + " " + (p.get("description") or ""))[:3000]
        rel = find_release_date(title, today) or find_release_date(body, today)
        words = bool(PRE_WORDS.search(title) or PRE_WORDS.search(body))
        rel = rel if words else None  # una data senza parole da preordine non basta
        pre = bool(p.get("is_on_backorder")) or bool(PRE_WORDS.search(title)) or bool(rel)
        buy = bool(p.get("is_purchasable", True)) and bool(p.get("is_in_stock"))
        out.append({"titolo": title, "url": p.get("permalink", ""), "comprabile": buy,
                    "preordine": pre, "prezzo": price, "uscita": rel.isoformat() if rel else None})
    return out


QUERIES_SHOPIFY = ["{s}", "{s} box", "{s} set allenatore", "{s} bundle", "{s} collezione"]


def scan_shop(shop, sets, today):
    """Tutte le offerte di un negozio per i set seguiti. Restituisce (offerte, ok, novità)."""
    session = requests.Session()
    offers, found_any, errors = {}, False, 0

    def search(q, extra=""):
        if shop["tipo"] == "woo":
            return search_woo(session, shop["sito"], q, today, extra)
        return search_shopify(session, shop["sito"], q, today)

    for s in sets:
        queries = [s["cerca"]] if shop["tipo"] == "woo" else [q.format(s=s["cerca"]) for q in QUERIES_SHOPIFY]
        for q in queries:
            try:
                res = search(q)
                found_any = True
            except Exception:
                errors += 1
                if errors >= 3 and not found_any:
                    return {}, False, []
                continue
            for r in res:
                if not matches_set(r["titolo"], s):
                    continue
                kind = classify(r["titolo"])
                if not kind:
                    continue
                offers[r["url"]] = {**r, "set": s["nome"], "tipo": kind, "negozio": shop["nome"]}
            time.sleep(0.4)
    news = []
    if found_any:
        queries = [("pokemon", "&orderby=date&order=desc&per_page=30")] if shop["tipo"] == "woo" \
            else [("pokemon preordine", ""), ("pokemon novita", "")]
        for q, extra in queries:
            try:
                for r in search(q, extra):
                    kind = classify(r["titolo"])
                    if kind and " pokemon " in norm(r["titolo"]) and \
                            not any(matches_set(r["titolo"], s) for s in sets):
                        news.append({**r, "tipo": kind, "negozio": shop["nome"]})
            except Exception:
                pass
    return offers, found_any, news


# ------------------------------------------------------------------ Telegram


def tg(token, method, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=25)
    r.raise_for_status()
    return r.json()


def send(token, chat, text, button=None):
    params = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if button:
        params["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": button[0], "url": button[1]}]]})
    tg(token, "sendMessage", **params)


def split_message(text, limit=TG_LIMIT):
    """Telegram accetta al massimo 4096 caratteri: divide ai paragrafi (poi alle righe)."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        if cur and len(cur) + 2 + len(block) > limit:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    return parts


# ------------------------------------------------------------------ prezzi e valore


def euro(p):
    return "—" if p is None else f"{p:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def esc(s):
    return html.escape(str(s))


DAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MONTH_NAMES = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
               "settembre", "ottobre", "novembre", "dicembre"]


def fmt_day(d):
    return f"{DAYS[d.weekday()]} {d.day} {MONTH_NAMES[d.month - 1]}"


def product_key(o):
    return f"{o['set']}|{o['tipo']}|{variant(o['titolo'], o['tipo'])}"


def product_label(o):
    return f"{o['tipo']} {variant(o['titolo'], o['tipo'])}".strip()


class Ctx:
    """Configurazione + dati di mercato + minimi storici, usati per valutare le offerte."""

    def __init__(self, cfg, market_values, minimi, today):
        self.cfg, self.market, self.minimi, self.today = cfg, market_values or {}, minimi or {}, today
        self.shops = {s["nome"]: s for s in cfg["negozi"]}
        self.sets = {s["nome"]: s for s in cfg["set"]}

    def shipping(self, o):
        sh = self.shops.get(o["negozio"], {})
        if o.get("prezzo") is None:
            return None
        if sh.get("gratis_da") is not None and o["prezzo"] >= sh["gratis_da"]:
            return 0.0
        return sh.get("spedizione")

    def total(self, o):
        """Prezzo + spedizione (se il negozio non la dichiara, una stima media) per ordinare i negozi."""
        if o.get("prezzo") is None:
            return float("inf")
        sp = self.shipping(o)
        return o["prezzo"] + (self.cfg.get("spedizione_stimata", 6.9) if sp is None else sp)

    def ship_text(self, o):
        sh = self.shops.get(o["negozio"], {})
        sp = self.shipping(o)
        free = f", gratis da {euro(sh['gratis_da'])}" if sh.get("gratis_da") else ""
        if sp == 0:
            return "spedizione gratis"
        if sp is None:
            return "spedizione al checkout" + free
        return f"+ {euro(sp)} spedizione{free}"

    def soglia(self, o):
        s = self.sets.get(o["set"], {})
        return (s.get("soglie") or {}).get(o["tipo"], self.cfg["soglie"].get(o["tipo"]))

    def collector(self, o):
        t = norm(o["titolo"])
        return o["tipo"] in COLLECTOR_TYPES or bool(self.sets.get(o["set"], {}).get("speciale")) or \
            "pokemon center" in t or has(t, "esclusiva", "esclusivo", "limitata", "limited", "numerata")

    def ev_ratio(self, o):
        """Valore medio delle carte contenute / prezzo (solo prodotti con buste e set con dati)."""
        mv = self.market.get(o["set"])
        n = packs(o["tipo"], o["titolo"])
        if not mv or not n or not o.get("prezzo") or o["tipo"] not in PACK_TYPES:
            return None
        return mv["ev_busta"] * n / o["prezzo"]

    def good(self, o):
        """Da segnalare come acquisto: sotto listino, oppure da collezione entro tolleranza,
        oppure buste il cui valore medio in carte ripaga il prezzo."""
        if not o.get("comprabile") or o.get("prezzo") is None:
            return False
        sg = self.soglia(o)
        if sg is None or o["prezzo"] <= sg:
            return True
        if self.collector(o) and o["prezzo"] <= sg * self.cfg.get("tolleranza_collezione", 1.5):
            return True
        r = self.ev_ratio(o)
        return r is not None and r >= self.cfg.get("affare_buste", 1.0)

    def over_text(self, o):
        sg = self.soglia(o)
        if sg and o.get("prezzo") and o["prezzo"] > sg + 0.009:
            return f"⚠️ Sopra listino ({euro(sg)}) del {max(1, round((o['prezzo'] / sg - 1) * 100))}%"
        return ""

    def per_pack(self, o):
        n = packs(o["tipo"], o["titolo"])
        return f" · {euro(o['prezzo'] / n)}/busta" if n and o.get("prezzo") else ""

    def value_line(self, o):
        mv = self.market.get(o["set"])
        r = self.ev_ratio(o)
        if r is None:
            return ""
        mark = "🔥" if r >= self.cfg.get("affare_buste", 1.0) else "🎲"
        return f"{mark} Carte in media {euro(mv['ev_busta'])}/busta: rende il {round(r * 100)}% del prezzo"

    def min_line(self, o):
        m = self.minimi.get(product_key(o))
        if not m or o.get("prezzo") is None:
            return ""
        if o["prezzo"] < m["prezzo"] - 0.009:
            return f"📉 Prezzo più basso mai visto (prima {euro(m['prezzo'])})"
        if o["prezzo"] <= m["prezzo"] + 0.009:
            return "📉 Uguale al minimo mai visto"
        d = date.fromisoformat(m["data"])
        return f"📉 Minimo visto: {euro(m['prezzo'])} su {esc(m['negozio'])} il {d.day}/{d.month}"


def update_minimi(minimi, offers, today):
    """Aggiorna il prezzo minimo mai visto per ogni prodotto; restituisce i minimi di prima."""
    before = {k: dict(v) for k, v in minimi.items()}
    for o in offers.values():
        if not o.get("comprabile") or not o.get("prezzo"):
            continue
        k = product_key(o)
        m = minimi.get(k)
        if m is None or o["prezzo"] < m["prezzo"] - 0.009:
            minimi[k] = {"prezzo": o["prezzo"], "negozio": o["negozio"], "data": today.isoformat()}
    return before


# ------------------------------------------------------------------ messaggi


def link(o):
    return f"<a href=\"{esc(o['url'])}\">{esc(o['negozio'])}</a>"


def alert_text(ctx, kind, o, old_price=None, others=()):
    head = {"back": "🟢 <b>Di nuovo disponibile</b>", "new": "🆕 <b>Appena comparso</b>",
            "drop": "💶 <b>Prezzo sceso</b>"}[kind]
    if ctx.collector(o):
        head += " · 💎 da collezione"
    price = f"<b>{euro(o['prezzo'])}</b>"
    if kind == "drop" and old_price:
        price = f"<s>{euro(old_price)}</s> → <b>{euro(o['prezzo'])}</b>"
    lines = [head, f"<b>{esc(o['set'])}</b> · {esc(product_label(o))}",
             f"{price} {ctx.ship_text(o)}{ctx.per_pack(o)}"]
    lines += [x for x in (ctx.over_text(o), ctx.value_line(o), ctx.min_line(o)) if x]
    lines.append(f"su {link(o)}")
    others = sorted(others, key=ctx.total)[:4]
    if others:
        lines.append("Anche su: " + ", ".join(f"{link(x)} {euro(x['prezzo'])}" for x in others))
    return "\n".join(lines)


def preorder_text(ctx, group, new_urls, dropped):
    """Un messaggio per prodotto con tutti i negozi dove è prenotabile."""
    first = group[0]
    opened = sorted([o for o in group if o["stato"] == "preordine"], key=ctx.total)
    waiting = sorted({o["negozio"] for o in group if o["stato"] == "in_arrivo"} -
                     {o["negozio"] for o in opened})
    rel = fmt_release(ctx, opened + group)
    head = "📅 <b>Preordine aperto</b>" if new_urls else "💶 <b>Preordine: prezzo sceso</b>"
    if ctx.collector(first):
        head += " · 💎 da collezione"
    lines = [head, f"<b>{esc(first['set'])}</b> · {esc(product_label(first))}"]
    if rel:
        lines.append(rel)
    lines.append(f"\nPrenotabile in {len(opened)} {'negozio' if len(opened) == 1 else 'negozi'}:")
    for o in opened:
        tag = " ⭐ <i>appena aperto</i>" if o["url"] in new_urls else \
            (" ⬇️ <i>sceso</i>" if o["url"] in dropped else "")
        warn = " ⚠️" if ctx.over_text(o) else ""
        lines.append(f"• {link(o)} <b>{euro(o['prezzo'])}</b>{warn} — {ctx.ship_text(o)}{tag}")
    best = opened[0]
    if any(ctx.over_text(o) for o in opened):
        lines.append(f"⚠️ = sopra listino ({euro(ctx.soglia(best))})")
    vl = ctx.value_line(best)
    if vl:
        lines.append(vl)
    if waiting:
        lines.append(f"\n⏳ Scheda online ma non prenotabile (non aperto o già esaurito): {esc(', '.join(waiting))}")
    have = {o["negozio"] for o in group}
    missing = [s["nome"] for s in ctx.cfg["negozi"] if s["nome"] not in have]
    if missing:
        lines.append(f"🔎 Non ancora in catalogo in {len(missing)} "
                     f"{'altro negozio' if len(missing) == 1 else 'altri negozi'}: controllo ogni ora.")
    return "\n".join(lines), best


def upcoming_text(ctx, group):
    first = group[0]
    rel = fmt_release(ctx, group)
    shops = ", ".join(link(o) for o in sorted(group, key=lambda o: o["negozio"]))
    lines = ["👀 <b>In arrivo</b> · scheda comparsa, non ancora prenotabile",
             f"<b>{esc(first['set'])}</b> · {esc(product_label(first))}"]
    if rel:
        lines.append(rel)
    lines += [f"Su: {shops}", "Ti scrivo appena apre il preordine."]
    return "\n".join(lines)


def matches_event(o, ev):
    """L'evento del calendario parla di questo prodotto? (es. "Ultra Premium Umbreon e Espeon")"""
    if ev.get("set") != o.get("set"):
        return False
    if ev["cosa"].startswith("Uscita"):
        return True
    c = norm(ev["cosa"])
    words = [w for w in norm(product_label(o)).split() if len(w) > 2]
    return norm(o["tipo"]).strip() in c and all(f" {w} " in c for w in words)


def shown_release(ctx, group):
    """(data, stimata) da mostrare nei messaggi: data del negozio, calendario, o uscita del set."""
    for o in group:
        if o.get("uscita") and date.fromisoformat(o["uscita"]) > ctx.today:
            return date.fromisoformat(o["uscita"]), False
    for ev in ctx.cfg.get("calendario", []):
        d = date.fromisoformat(ev["data"])
        if d > ctx.today and not ev["cosa"].startswith("Uscita") and matches_event(group[0], ev):
            return d, bool(ev.get("stima"))
    rel = next((release_of(o, ctx.cfg) for o in group if release_of(o, ctx.cfg)), None)
    return (rel, False) if rel and rel > ctx.today else (None, False)


def fmt_release(ctx, group):
    d, est = shown_release(ctx, group)
    return f"Uscita: {fmt_day(d)}{' (data stimata)' if est else ''}" if d else ""


def calendar_events(cfg):
    """Eventi del calendario + uscite ufficiali dei set."""
    ev = [{"data": s["uscita"], "set": s["nome"], "cosa": f"Uscita {s['nome']}", "stima": not s.get("certo")}
          for s in cfg["set"] if s.get("uscita")]
    ev += [dict(e) for e in cfg.get("calendario", [])]
    return sorted(ev, key=lambda e: e["data"])


def preorders_of_event(ctx, offers, ev):
    """Preordini aperti dei prodotti di un evento: [(etichetta, n negozi, migliore offerta)]."""
    groups = {}
    for o in offers.values():
        if o["stato"] == "preordine" and matches_event(o, ev):
            groups.setdefault(product_label(o), []).append(o)
    return [(label, len(g), min(g, key=ctx.total)) for label, g in sorted(groups.items())]


def days_label(days):
    if days <= 1:
        return "Domani" if days == 1 else "Oggi"
    if days >= 14:
        return f"Tra {round(days / 7)} settimane"
    return "Tra una settimana" if days == 7 else f"Tra {days} giorni"


def reminder_text(ctx, ev, days, offers):
    d = date.fromisoformat(ev["data"])
    lines = [f"⏰ <b>{days_label(days)}</b>: {esc(ev['cosa'])}",
             f"<b>{esc(ev.get('set', ''))}</b> · {fmt_day(d)}{' (data stimata)' if ev.get('stima') else ''}"]
    st = preorders_of_event(ctx, offers, ev)
    if "prerelease" in norm(ev["cosa"]):
        return "\n".join(lines)
    if st:
        lines.append("\nGià prenotabile:")
        for label, n, best in st:
            lines.append(f"• {esc(label)}: {n} {'negozio' if n == 1 else 'negozi'}, "
                         f"da <b>{euro(best['prezzo'])}</b> su {link(best)}")
    elif days > REMINDERS[1]:
        lines.append("Di solito i preordini aprono in queste settimane: ti avviso appena si aprono.")
    else:
        lines.append("Nessun negozio ha ancora aperto i preordini.")
    return "\n".join(lines)


def summary_text(ctx, offers, shops_ok, news, now, weekly):
    today = now.date()
    lines = [f"☀️ <b>Pokémon · {fmt_day(today)}</b>", f"<i>{shops_ok} negozi controllati</i>", ""]

    soon = [e for e in calendar_events(ctx.cfg)
            if today <= date.fromisoformat(e["data"]) <= today + timedelta(days=60)]
    if soon:
        lines.append("📅 <b>Prossime uscite</b>")
        for e in soon:
            d = date.fromisoformat(e["data"])
            n_pre = len({o["negozio"] for o in offers.values() if o["set"] == e["set"] and o["stato"] == "preordine"})
            pre = f" — prenotabile in {n_pre} {'negozio' if n_pre == 1 else 'negozi'}" \
                if n_pre and e["cosa"].startswith("Uscita") else ""
            lines.append(f"• {fmt_day(d)}{' (stima)' if e.get('stima') else ''}: "
                         f"<b>{esc(e['set'])}</b> · {esc(e['cosa'])}{pre}")
        lines.append("")

    groups = {}
    for o in offers.values():
        groups.setdefault(product_key(o), []).append(o)
    order = {s["nome"]: i for i, s in enumerate(ctx.cfg["set"])}

    pre_lines, out_lines = [], {}
    buy = {}  # set -> tipo -> [migliore offerta di ogni variante, n negozi]
    for k in sorted(groups, key=lambda k: (order.get(groups[k][0]["set"], 99), k)):
        g = groups[k]
        o0 = g[0]
        pre = sorted([o for o in g if o["stato"] == "preordine"], key=ctx.total)
        if pre:
            shops = ", ".join(f"{link(o)} {euro(o['prezzo'])}{' ⚠️' if ctx.over_text(o) else ''}" for o in pre[:4])
            more = f" +{len(pre) - 4}" if len(pre) > 4 else ""
            pre_lines.append(f"• <b>{esc(o0['set'])}</b> · {esc(product_label(o0))}: {shops}{more}")
            continue
        good = sorted([o for o in g if o["stato"] == "disponibile" and ctx.good(o)], key=ctx.total)
        if good:
            buy.setdefault(o0["set"], {}).setdefault(o0["tipo"], []).append((good[0], len(good)))
        elif any(o["stato"] in ("disponibile", "esaurito") for o in g):
            out_lines.setdefault(o0["set"], []).append(product_label(o0))

    buy_lines = []
    for s in ctx.cfg["set"]:
        if s["nome"] not in buy:
            continue
        buy_lines.append(f"<b>{esc(s['nome'])}</b>{' 💎' if s.get('speciale') else ''}")
        for tipo, items in sorted(buy[s["nome"]].items(), key=lambda kv: -(packs(kv[0], "") or 0)):
            b, n = min(items, key=lambda x: ctx.total(x[0]))
            name = esc(product_label(b)) if len(items) == 1 else f"{esc(tipo)} ({len(items)} versioni)"
            gem = " 💎" if not s.get("speciale") and ctx.collector(b) else ""
            warn = " ⚠️" if ctx.over_text(b) else ""
            shops = f" +{n - 1}" if n > 1 else ""
            line = (f"• {name}{gem} {'da ' if len(items) > 1 else ''}<b>{euro(b['prezzo'])}</b>{warn}"
                    f"{ctx.per_pack(b) if tipo in PACK_TYPES else ''} — {link(b)}{shops}")
            extra = [x for x in (ctx.value_line(b) if tipo == "Box 36 buste" else "",
                                 ctx.min_line(b) if "più basso" in ctx.min_line(b) else "") if x]
            buy_lines.append(line + "".join(f"\n   {x}" for x in extra))

    if pre_lines:
        lines += ["🟠 <b>Preordini aperti</b>"] + pre_lines + [""]
    if buy_lines:
        lines += ["🟢 <b>Da prendere ora</b> <i>(⚠️ sopra listino · 💎 da collezione · +N altri negozi)</i>"] \
            + buy_lines + [""]
    elif not pre_lines:
        lines += ["Oggi niente di comprabile al prezzo giusto.", ""]

    if weekly and ctx.market:
        rows = []
        for s in ctx.cfg["set"]:
            mv = ctx.market.get(s["nome"])
            if not mv:
                continue
            best_pp = min((o["prezzo"] / packs(o["tipo"], o["titolo"]) for o in offers.values()
                           if o["set"] == s["nome"] and o["tipo"] in PACK_TYPES and o["stato"] == "disponibile"
                           and o.get("prezzo")), default=None)
            ratio = f" · rende il {round(mv['ev_busta'] / best_pp * 100)}% (busta migliore {euro(best_pp)})" \
                if best_pp else ""
            top = ", ".join(f"{esc(t['nome'])} ({esc(t.get('rarita', ''))}) {euro(t['prezzo'])}"
                            for t in mv["top"][:3])
            rows.append(f"<b>{esc(s['nome'])}</b>: carte in media {euro(mv['ev_busta'])}/busta{ratio}\n"
                        f"   Top: {top}")
        if rows:
            lines += ["💎 <b>Valore delle carte per set</b> <i>(prezzi Cardmarket, stime)</i>"] + rows + [""]

    if out_lines:
        lines.append("🔴 <b>Esauriti o sopra prezzo</b>")
        for s in ctx.cfg["set"]:
            if s["nome"] in out_lines:
                lines.append(f"<b>{esc(s['nome'])}</b>: {esc(', '.join(sorted(set(out_lines[s['nome']]))))}")
        lines.append("")

    if news:
        lines.append("🆕 <b>Nuovi in catalogo</b> (set non ancora seguiti)")
        for n in news[:8]:
            lines.append(f"• <a href=\"{esc(n['url'])}\">{esc(n['titolo'])}</a> — {esc(n['negozio'])}"
                         f" {euro(n['prezzo'])}")
        lines.append("")
    return "\n".join(lines).strip()


# ------------------------------------------------------------------ main


def main():
    cfg = json.loads(CONFIG_FILE.read_text())
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or DEFAULT_CHAT_ID
    force = os.environ.get("FORCE_SUMMARY", "") == "true"
    now = datetime.now(TZ)
    today = now.date()

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    fresh_install = "offerte" not in state
    upgraded = not fresh_install and state.get("versione") != STATE_VERSION
    baseline = fresh_install or upgraded  # primo giro: si registra tutto, niente avvisi
    if upgraded:
        state = {k: state[k] for k in ("offerte", "negozi_visti", "novita_viste", "ultimo_riepilogo") if k in state}
    state["versione"] = STATE_VERSION
    prev = state.get("offerte", {})
    known_shops = set(state.get("negozi_visti", []))

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda sh: (sh, *scan_shop(sh, cfg["set"], today)), cfg["negozi"]))

    offers, news, shops_ok = {}, [], 0
    for shop, off, ok, nw in results:
        print(f"{'✅' if ok else '⛔️'} {shop['nome']}: {len(off)} prodotti")
        if ok:
            shops_ok += 1
            offers.update(off)
            news += nw
        else:
            # negozio muto: teniamo quello che sapevamo, senza avvisare
            offers.update({u: o for u, o in prev.items() if o.get("negozio") == shop["nome"]})

    # prodotti spariti per un giro (ricerca che salta, limite di risultati): li ricordiamo 7 giorni,
    # così se ricompaiono non sembrano "nuovi" e non mandano di nuovo lo stesso avviso
    ok_shops = {sh["nome"] for sh, _, ok, _ in results if ok}
    ghosts = {u: o for u, o in state.get("spariti", {}).items()
              if (today - date.fromisoformat(o["sparito"])).days <= 7}
    state["spariti"] = {u: o for u, o in ghosts.items() if u not in offers}
    for u, o in prev.items():
        if u not in offers and o.get("negozio") in ok_shops and u not in state["spariti"]:
            state["spariti"][u] = {**o, "sparito": today.isoformat()}

    state["mercato"] = market.refresh(cfg, state.get("mercato", {}), now)
    minimi = state.setdefault("minimi", {})
    for o in list(offers.values()) + list(prev.values()) + list(ghosts.values()):
        o.setdefault("preordine", False)
        o["stato"] = status(o, cfg, today)
    old_minimi = update_minimi(minimi, offers, today)
    ctx = Ctx(cfg, market.values(state["mercato"]), old_minimi, today)  # nei messaggi: il minimo di PRIMA di questo giro

    for o in sorted(offers.values(), key=lambda o: (o["set"], product_key(o), o["prezzo"] or 0)):
        print(f"   {o['stato'][:5]:5} {o['set']} · {product_label(o)} · {o['negozio']} "
              f"{euro(o['prezzo'])} — {o['titolo']}")

    groups = {}
    for o in offers.values():
        groups.setdefault(product_key(o), []).append(o)

    alerts = []  # (priorità, testo, bottone)
    seen_products = set(state.get("prodotti_visti", []))
    if not baseline:
        pre_new, pre_drop, singles = {}, {}, []
        for url, o in offers.items():
            old = prev.get(url) or ghosts.get(url)
            if o["negozio"] not in known_shops:
                continue  # la prima lettura di un negozio fa da base
            k = product_key(o)
            if o["stato"] == "preordine":
                if old is None or old.get("stato") != "preordine":
                    pre_new.setdefault(k, set()).add(url)
                elif old.get("prezzo") and o["prezzo"] and o["prezzo"] < old["prezzo"] - 0.009:
                    pre_drop.setdefault(k, set()).add(url)
                continue
            if o["stato"] != "disponibile" or not ctx.good(o):
                continue
            if old is not None and old.get("stato") == "preordine":
                continue  # uscito: era già prenotabile, lo sapevi
            others = [x for x in groups[k] if x is not o and x["stato"] == "disponibile" and ctx.good(x)]
            if old is None:
                singles.append((2, alert_text(ctx, "new", o, others=others), o))
            elif not (old.get("stato") == "disponibile" and ctx.good(old)):
                singles.append((1, alert_text(ctx, "back", o, others=others), o))
            elif old.get("prezzo") and o["prezzo"] < old["prezzo"] - 0.009:
                singles.append((3, alert_text(ctx, "drop", o, old["prezzo"], others=others), o))

        for k in set(pre_new) | set(pre_drop):
            text, best = preorder_text(ctx, groups[k], pre_new.get(k, set()), pre_drop.get(k, set()))
            alerts.append((0 if k in pre_new else 3, text, (f"🛒 Prenota su {best['negozio']}", best["url"])))
        for p, text, o in singles:
            alerts.append((p, text, (f"🛒 Apri {o['negozio']}", o["url"])))

        # 👀 prodotti mai visti prima, presenti solo come scheda non prenotabile
        for k, g in groups.items():
            if k not in seen_products and all(o["stato"] == "in_arrivo" for o in g) and \
                    any(o["negozio"] in known_shops for o in g):
                alerts.append((1, upcoming_text(ctx, g), None))
    state["prodotti_visti"] = sorted(seen_products | set(groups))[-3000:]

    # ⏰ promemoria del calendario
    sent = set(state.get("promemoria", []))
    for ev in calendar_events(cfg):
        days = (date.fromisoformat(ev["data"]) - today).days
        for i, limit in enumerate(REMINDERS):
            nxt = REMINDERS[i + 1] if i + 1 < len(REMINDERS) else 0
            key = f"{ev['data']}|{ev.get('set', '')}|{ev['cosa']}|{limit}"
            if key in sent:
                continue
            if days <= nxt:
                sent.add(key)  # finestra già passata: non la recuperiamo
            elif days <= limit and not baseline and now.hour >= 8:  # niente promemoria di notte
                alerts.append((0, reminder_text(ctx, ev, days, offers), None))
                sent.add(key)
    state["promemoria"] = sorted(sent)[-300:]

    seen_news = set(state.get("novita_viste", []))
    news = list({n["url"]: n for n in news}.values())
    fresh_news = [n for n in news if n["url"] not in seen_news]
    state["novita_viste"] = sorted(seen_news | {n["url"] for n in news})[-800:]
    for n in ([] if baseline else fresh_news):
        pre = " · in preordine" if n.get("preordine") else ""
        alerts.append((4, f"🆕 <b>Nuovo prodotto Pokémon</b>{pre}\n{esc(n['titolo'])}\n"
                          f"<b>{euro(n['prezzo'])}</b> su {esc(n['negozio'])}\n"
                          f"<i>Set non ancora seguito: se ti interessa, aggiungilo in config.json</i>",
                       (f"🛒 Apri {n['negozio']}", n["url"])))

    msgs = []
    alerts.sort(key=lambda a: a[0])
    msgs += [(text, btn, False) for _, text, btn in alerts[:MAX_ALERTS]]
    if len(alerts) > MAX_ALERTS:
        msgs.append((f"…e altre {len(alerts) - MAX_ALERTS} novità: le trovi nel riepilogo di domattina.",
                     None, False))

    today_s = today.isoformat()
    did_summary = False
    if baseline or force or (now.hour >= SUMMARY_HOUR and state.get("ultimo_riepilogo") != today_s):
        weekly = baseline or force or today.weekday() == 0
        text = summary_text(ctx, offers, shops_ok, [] if baseline else fresh_news, now, weekly)
        if baseline:
            text = ("🤖 <b>Robot Pokémon v3!</b> Ora seguo i preordini in tutti i negozi, il calendario "
                    "delle uscite, la spedizione, i prezzi minimi e il valore delle carte.\n\n" + text)
        msgs += [(part, None, True) for part in split_message(text)]
        did_summary = True

    failed_summary = False
    if token:
        for i, (text, btn, is_summary) in enumerate(msgs):
            for attempt in (1, 2):
                try:
                    # secondo tentativo in testo semplice, nel caso Telegram rifiuti l'HTML
                    send(token, chat, text if attempt == 1 else html.unescape(re.sub(r"<[^>]+>", "", text)), btn)
                    break
                except Exception as e:
                    print(f"Invio non riuscito (tentativo {attempt}):", e)
                    if attempt == 1:
                        time.sleep(5)
                    elif is_summary:
                        failed_summary = True
            if i + 1 < len(msgs):
                time.sleep(1)  # Telegram limita i messaggi troppo ravvicinati
        print(f"Inviati {len(msgs)} messaggi.")
    else:
        print("\n--- TELEGRAM_TOKEN mancante: anteprima ---")
        for text, btn, _ in msgs:
            print(text, f"\n[{btn[0]}]" if btn else "", "\n")
    if did_summary and not failed_summary:
        state["ultimo_riepilogo"] = today_s

    state["offerte"] = offers
    state["minimi"] = minimi
    state["ultimo_controllo"] = now.isoformat(timespec="minutes")
    state["negozi_letti"] = shops_ok
    state["negozi_visti"] = sorted(known_shops | {sh["nome"] for sh, _, ok, _ in results if ok})
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
