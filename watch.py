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
import subprocess
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

import dashboard
import interest
import market
import sources

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"
NAMES_FILE = ROOT / "pokemon_names.txt"
DOCS_DIR = ROOT / "docs"
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
STANDARD_PRICE_TYPES = {"Box 36 buste", "Set Allenatore", "Bundle 6 buste", "Blister", "Ultra Premium"}
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
    if re.search(r" bundle \d+ (tin|collezion|box|set) ", t) or re.search(r" \d+ ?x | x ?\d+ ", t) \
            or re.search(r" set (di |da )?\d+ (mini tin|tin|artwork)", t) or "collezione completa" in t:
        return None  # confezioni multiple
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


def pokemon_keys(card_name):
    """ "Mega Zygarde ex" -> " mega zygarde " (quello che si cerca nel titolo di un prodotto)."""
    k = norm(card_name)
    return k[:-3] if k.endswith(" ex ") else k


def make_guesser(names_by_set):
    """Funzione titolo -> nome del set, se il Pokémon protagonista (ex o Mega) è di un solo set seguito.
    Restituisce anche la chiave trovata (es. "mega zygarde") per cercarla poi in tutti i negozi."""
    keys = {}
    for set_name, v in (names_by_set or {}).items():
        for n in v.get("nomi", []):
            k = pokemon_keys(n)
            # solo le Mega: sono legate a un'espansione precisa, i Pokémon "ex" si ripetono tra i set
            if k.startswith(" mega ") and len(k.strip()) >= 8:
                keys.setdefault(k, set()).add(set_name)

    def guess(title):
        t = norm(title)
        hits = {}
        for k, sets_ in keys.items():
            if k in t:
                for sn in sets_:
                    hits.setdefault(sn, []).append(k.strip())
        if len(hits) == 1:
            sn, ks = next(iter(hits.items()))
            return sn, max(ks, key=len)
        return None, None
    return guess


QUERIES_SHOPIFY = ["{s}", "{s} box", "{s} set allenatore", "{s} bundle", "{s} collezione"]


def scan_shop(shop, sets, today, guess=None):
    """Tutte le offerte di un negozio per i set seguiti. Restituisce (offerte, ok, novità).
    Oltre al nome del set cerca i Pokémon protagonisti già scoperti (s["extra"], es. "mega zygarde")."""
    session = requests.Session()
    offers, found_any, errors = {}, False, 0

    def search(q, extra=""):
        if shop["tipo"] == "woo":
            return search_woo(session, shop["sito"], q, today, extra)
        return search_shopify(session, shop["sito"], q, today)

    for s in sets:
        queries = [s["cerca"]] if shop["tipo"] == "woo" else [q.format(s=s["cerca"]) for q in QUERIES_SHOPIFY]
        queries += [f"pokemon {x}" for x in s.get("extra", [])]
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
                if not matches_set(r["titolo"], s) and not (
                        guess and not any(matches_set(r["titolo"], x) for x in sets)
                        and guess(r["titolo"])[0] == s["nome"]):
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


def send(token, chat, text, buttons=None):
    """buttons: lista di (testo, link), uno per riga sotto il messaggio."""
    params = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if buttons:
        params["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t[:60], "url": u}] for t, u in buttons]})
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
        self.interest = {}  # chiave prodotto -> (punteggio 0-100, motivi)
        self.first_prices = {}  # chiave prodotto -> {negozio: primo prezzo visto}

    def interest_of(self, o):
        return self.interest.get(product_key(o), (0, []))

    def hot(self, o):
        return self.interest_of(o)[0] >= interest.HIGH

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

    # ---- giudizio sul prezzo: sotto listino / allineato / accettabile / gonfiato

    def reference(self, o):
        """(prezzo di riferimento, fonte). 1) listino ufficiale in config, 2) per i prodotti con listino
        standard (box, set allenatore...) il listino tipico, 3) per collezioni e tin il prezzo più basso
        con cui i negozi l'hanno messo in vendita la prima volta, 4) il listino tipico del tipo."""
        s = self.sets.get(o["set"], {})
        lst = s.get("listini") or {}
        label = product_label(o)
        if label in lst:
            return lst[label], "ufficiale"
        if o["tipo"] in lst:
            return lst[o["tipo"]], "ufficiale"
        sg = self.soglia(o)
        if sg is None:
            return None, None
        if o["tipo"] in STANDARD_PRICE_TYPES:
            return sg, "tipico"
        first = sorted(p for p in self.first_prices.get(product_key(o), {}).values() if p)
        if first:
            # prezzo di lancio: il più basso, ma con 3+ negozi il secondo (un errore di prezzo non lo falsa)
            launch = first[1] if len(first) >= 3 else first[0]
            return round(min(sg, launch), 2), "negozi"
        return sg, "tipico"

    def tolerance(self, o):
        t = self.cfg.get("tolleranza") or {}
        return t.get("richiesto", 0.30) if self.hot(o) else t.get("normale", 0.15)

    def verdict(self, o):
        """(fascia, scostamento, riferimento): fascia = sotto | allineato | accettabile | gonfiato."""
        ref, _ = self.reference(o)
        if not ref or o.get("prezzo") is None:
            return "allineato", 0.0, ref
        pct = o["prezzo"] / ref - 1
        if pct <= -0.05:
            band = "sotto"
        elif pct <= 0.05:
            band = "allineato"
        elif pct <= self.tolerance(o) + 0.0001:
            band = "accettabile"
        else:
            band = "gonfiato"
        return band, pct, ref

    def target(self, o):
        ref, _ = self.reference(o)
        return round(ref * (1 + self.tolerance(o)), 2) if ref else None

    def verdict_text(self, o, group=()):
        band, pct, ref = self.verdict(o)
        if not ref:
            return ""
        p = round(abs(pct) * 100)
        src = {"ufficiale": "listino", "tipico": "listino tipico", "negozi": "prezzo di lancio"}[self.reference(o)[1]]
        txt = {"sotto": f"🟢 {p}% sotto il {src} ({euro(ref)})",
               "allineato": f"⚪ In linea col {src} ({euro(ref)})",
               "accettabile": f"🟡 +{p}% sul {src} ({euro(ref)}): accettabile",
               "gonfiato": f"🔴 +{p}% sul {src} ({euro(ref)}): troppo caro, obiettivo {euro(self.target(o))}"}[band]
        others = sorted(x["prezzo"] for x in group if x is not o and x.get("prezzo")
                        and x["stato"] in ("disponibile", "preordine"))
        if len(others) >= 2 and o.get("prezzo"):
            med = others[len(others) // 2]
            if o["prezzo"] <= med * 0.85:
                txt += f" · {round((1 - o['prezzo'] / med) * 100)}% sotto gli altri negozi"
        return txt

    def good(self, o):
        """Da segnalare come acquisto: prezzo non gonfiato rispetto al riferimento, oppure buste il cui
        valore medio in carte ripaga il prezzo."""
        if not o.get("comprabile") or o.get("prezzo") is None:
            return False
        if self.verdict(o)[0] != "gonfiato":
            return True
        r = self.ev_ratio(o)
        return r is not None and r >= self.cfg.get("affare_buste", 1.0)

    def over_text(self, o):
        """Non vuoto se il prezzo è gonfiato (usato per i segnali ⚠️/🔴)."""
        return self.verdict_text(o) if self.verdict(o)[0] == "gonfiato" else ""

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
#
# Due tipi di messaggio:
# - SCHEDA: una per ogni occasione (preordine aperto, prezzo sceso, di nuovo disponibile...),
#   breve, con sotto un pulsante per ogni negozio dove comprare/prenotare.
# - RIEPILOGO del mattino: un unico messaggio con prossime uscite, preordini aperti,
#   migliori occasioni e mercato, separati da linee.

SEP = "━━━━━━━━━━━━━━━"
SHORT = {"Box 36 buste": "Box 36", "Bundle 6 buste": "Bundle 6", "Set Allenatore": "Set Allenatore"}
MAX_BUTTONS = 6


def link(o):
    return f"<a href=\"{esc(o['url'])}\">{esc(o['negozio'])}</a>"


def short_label(o):
    v = variant(o["titolo"], o["tipo"])
    return f"{SHORT.get(o['tipo'], o['tipo'])} {v}".strip()


def fmt_short(d):
    return f"{DAYS[d.weekday()]} {d.day}/{d.month}"


def button(o, prefix="🛒 ", suffix=""):
    return (f"{prefix}{o['negozio']} · {euro(o['prezzo'])}{suffix}", o["url"])


def card(head, o, collector=False):
    title = f"{head}{'  💎' if collector else ''}"
    return [title, f"<b>{esc(o['set'])}</b> · {esc(product_label(o))}"]


def hot_line(ctx, o):
    sc, why = ctx.interest_of(o)
    if sc < interest.HIGH:
        return ""
    return f"🔥 Interesse alto ({sc}/100): {esc(', '.join(why[:2]) or 'prodotto da collezione')}"


def alert_text(ctx, kind, o, old_price=None, others=(), group=()):
    """Scheda per un prodotto disponibile a un prezzo sensato. Restituisce (testo, pulsanti)."""
    head = {"back": "🟢 <b>DI NUOVO DISPONIBILE</b>", "new": "🆕 <b>APPENA COMPARSO</b>",
            "drop": "💶 <b>PREZZO SCESO</b>", "target": "🎯 <b>PREZZO OBIETTIVO RAGGIUNTO</b>"}[kind]
    lines = card(head, o, ctx.collector(o))
    lines.append(SEP)
    price = f"<b>{euro(o['prezzo'])}</b>"
    if kind in ("drop", "target") and old_price:
        price = f"<s>{euro(old_price)}</s> → <b>{euro(o['prezzo'])}</b>"
    lines.append(f"{price}{ctx.per_pack(o) if o['tipo'] in PACK_TYPES else ''}")
    lines += [x for x in (ctx.verdict_text(o, group), f"🚚 {ctx.ship_text(o)}", hot_line(ctx, o),
                          ctx.min_line(o), ctx.value_line(o)) if x]
    n_others = len(others)
    others = sorted(others, key=ctx.total)[:2]  # il migliore + le 2 alternative più convenienti
    if n_others:
        lines.append(f"<i>A un prezzo sensato anche in altri {n_others} "
                     f"{'negozio' if n_others == 1 else 'negozi'}</i>")
    buttons = [button(o)] + [button(x, prefix="") for x in others]
    return "\n".join(lines), buttons


def preorder_text(ctx, group, new_urls, dropped):
    """Scheda per un preordine: un pulsante per ogni negozio dove è prenotabile a un prezzo sensato."""
    first = group[0]
    opened = sorted([o for o in group if o["stato"] == "preordine"], key=ctx.total)
    fair = [o for o in opened if ctx.verdict(o)[0] != "gonfiato"]
    pricey = [o for o in opened if ctx.verdict(o)[0] == "gonfiato"]
    waiting = sorted({o["negozio"] for o in group if o["stato"] == "in_arrivo"} -
                     {o["negozio"] for o in opened})
    head = "📅 <b>PREORDINE APERTO</b>" if new_urls else "💶 <b>PREORDINE · PREZZO SCESO</b>"
    if ctx.hot(first) and new_urls and fair:
        head = "🔥 <b>PRIORITÀ · PREORDINE APERTO</b>"
    lines = card(head, first, ctx.collector(first))
    rel = fmt_release(ctx, opened + group)
    if rel:
        lines.append(f"🗓 {rel}")
    lines.append(SEP)
    n = len(opened)
    lines.append(f"Prenotabile in <b>{n}</b> {'negozio' if n == 1 else 'negozi'}"
                 f"{' · ⭐ appena aperto' if new_urls else ''}")
    if ctx.hot(first):
        lines.append(hot_line(ctx, first))
    if fair:
        best = fair[0]
        lines.append(f"Il migliore: <b>{euro(best['prezzo'])}</b> su {esc(best['negozio'])} · {ctx.ship_text(best)}")
        lines.append(ctx.verdict_text(best, group))
        if ctx.hot(first):
            lines.append("<b>Tende a sparire: se ti interessa, prenotalo subito.</b>")
    else:
        best = opened[0]
        lines.append(f"🔴 Per ora solo prezzi gonfiati (da {euro(best['prezzo'])}): "
                     f"ti avviso se qualcuno lo mette sotto {euro(ctx.target(best))}.")
    vl = ctx.value_line(fair[0] if fair else best)
    if vl:
        lines.append(vl)
    if pricey and fair:
        lines.append("🔴 Troppo cari: " + ", ".join(f"{esc(o['negozio'])} {euro(o['prezzo'])}" for o in pricey[:4]))
    if waiting:
        lines.append(f"⏳ Non ancora prenotabile: {esc(', '.join(waiting))}")
    extra = fair[MAX_BUTTONS:]
    if extra:
        lines.append("Anche su: " + ", ".join(f"{link(o)} {euro(o['prezzo'])}" for o in extra))
    buttons = []
    for o in fair[:MAX_BUTTONS]:
        mark = "⭐ " if o["url"] in new_urls else ("⬇️ " if o["url"] in dropped else "")
        band = ctx.verdict(o)[0]
        buttons.append(button(o, prefix=mark, suffix={"sotto": " 🟢", "accettabile": " 🟡"}.get(band, "")))
    return "\n".join(lines), buttons


def upcoming_text(ctx, group):
    first = group[0]
    lines = card("👀 <b>IN ARRIVO</b>", first, ctx.collector(first))
    rel = fmt_release(ctx, group)
    if rel:
        lines.append(f"🗓 {rel}")
    lines += [SEP, "Scheda online, non ancora prenotabile.", "Ti scrivo appena apre il preordine."]
    buttons = [(f"👀 {o['negozio']}", o["url"]) for o in sorted(group, key=lambda o: o["negozio"])[:MAX_BUTTONS]]
    return "\n".join(lines), buttons


def news_text(items):
    """Un prodotto di un set non seguito, con un pulsante per ogni negozio che lo ha."""
    items = sorted(items, key=lambda n: n.get("prezzo") or 0)
    n = items[0]
    lines = ["🆕 <b>NUOVO PRODOTTO</b> · set non seguito", esc(n["titolo"]), SEP,
             f"da <b>{euro(n['prezzo'])}</b>{' · in preordine' if any(x.get('preordine') for x in items) else ''}",
             "<i>Se il set si conferma da più fonti, inizio a seguirlo da solo.</i>"]
    return "\n".join(lines), [button(x, prefix="🛒 " if i == 0 else "") for i, x in enumerate(items[:MAX_BUTTONS])]


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
    return f"Uscita {fmt_day(d)}{' (data stimata)' if est else ''}" if d else ""


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
    cosa = ev["cosa"]
    if cosa == f"Uscita {ev.get('set', '')}":
        cosa = "Uscita"
    lines = [f"⏰ <b>{days_label(days).upper()}</b>",
             f"<b>{esc(ev.get('set', ''))}</b> · {esc(cosa)}",
             f"🗓 {fmt_day(d)}{' (data stimata)' if ev.get('stima') else ''}", SEP]
    buttons = []
    if "prerelease" in norm(ev["cosa"]):
        lines.append("Tornei con buste in anteprima nei negozi aderenti.")
        return "\n".join(lines), buttons
    st = preorders_of_event(ctx, offers, ev)
    if st:
        lines.append("Già prenotabile:")
        for label, n, best in st:
            lines.append(f"• {esc(label)} da <b>{euro(best['prezzo'])}</b> ({n} {'negozio' if n == 1 else 'negozi'})")
            if len(buttons) < MAX_BUTTONS:
                buttons.append((f"🛒 {SHORT.get(best['tipo'], label)} · {best['negozio']} {euro(best['prezzo'])}",
                                best["url"]))
    elif days > REMINDERS[1]:
        lines.append("Di solito i preordini aprono in queste settimane: ti avviso appena si aprono.")
    else:
        lines.append("Nessun negozio ha ancora aperto i preordini.")
    return "\n".join(lines), buttons


KEY_TYPES = {"Box 36 buste", "Set Allenatore", "Ultra Premium", "Collezione Premium", "Bundle 6 buste"}


def deal_score(ctx, o, key, group):
    """Più basso = occasione migliore. Conta quanto costa meno rispetto agli altri negozi e al
    listino, con bonus per nuovi minimi, buste che rendono e prodotti principali."""
    prices = sorted(x["prezzo"] for x in group if x.get("prezzo") and x["stato"] in ("disponibile", "esaurito"))
    median = prices[len(prices) // 2] if prices else o["prezzo"]
    _, pct, _ = ctx.verdict(o)
    score = 0.5 * (o["prezzo"] / median) + 0.5 * (1 + pct)  # metà rispetto agli altri negozi, metà al listino
    m = ctx.minimi.get(key)
    if m and o["prezzo"] < m["prezzo"] - 0.009:
        score -= 0.10  # nuovo minimo
    r = ctx.ev_ratio(o)
    if r is not None and r >= ctx.cfg.get("affare_buste", 1.0):
        score -= 0.30
    if o["tipo"] in KEY_TYPES:
        score -= 0.05
    return score


def summary_text(ctx, offers, shops_ok, news, now, weekly):
    """Riepilogo del mattino: un solo messaggio, sintetico."""
    today = now.date()
    lines = [f"☀️ <b>POKÉMON · {fmt_day(today).upper()}</b>"]

    # 📅 prossime uscite (45 giorni)
    soon = [e for e in calendar_events(ctx.cfg)
            if today <= date.fromisoformat(e["data"]) <= today + timedelta(days=45)]
    if soon:
        lines += [SEP, "📅 <b>PROSSIME USCITE</b>"]
        for e in soon[:6]:
            d = date.fromisoformat(e["data"])
            cosa = "Uscita" if e["cosa"] == f"Uscita {e['set']}" else e["cosa"]
            n_pre = len({o["negozio"] for o in offers.values() if o["stato"] == "preordine" and matches_event(o, e)})
            pre = f" · 🟠 prenotabile in {n_pre}" if n_pre else ""
            lines.append(f"• <b>{fmt_short(d)}</b> {esc(e['set'])} — {esc(cosa)}"
                         f"{' <i>(stima)</i>' if e.get('stima') else ''}{pre}")

    groups = {}
    for o in offers.values():
        groups.setdefault(product_key(o), []).append(o)

    # 🟠 preordini aperti
    pre_rows = []
    for k, g in groups.items():
        pre = sorted([o for o in g if o["stato"] == "preordine"], key=ctx.total)
        if pre:
            pre_rows.append((pre[0]["set"], k, pre))
    if pre_rows:
        lines += [SEP, "🟠 <b>PREORDINI APERTI</b>"]
        order = {x["nome"]: i for i, x in enumerate(ctx.cfg["set"])}
        for _, k, pre in sorted(pre_rows, key=lambda r: (order.get(r[0], 99), r[1]))[:6]:
            b = pre[0]
            lines.append(f"• {esc(b['set'])} · {esc(short_label(b))} — da <b>{euro(b['prezzo'])}</b> "
                         f"({link(b)}) · {len(pre)} {'negozio' if len(pre) == 1 else 'negozi'}")
        if len(pre_rows) > 6:
            lines.append(f"<i>+ altri {len(pre_rows) - 6} prodotti in preordine</i>")

    # 🔥 migliori occasioni: il migliore per ogni tipo di prodotto, ordinati per convenienza
    best_by_type = {}
    for k, g in groups.items():
        good = [o for o in g if o["stato"] == "disponibile" and ctx.good(o)]
        if not good:
            continue
        b = min(good, key=ctx.total)
        t = (b["set"], b["tipo"])
        cand = (deal_score(ctx, b, k, g), b, k)
        if t not in best_by_type or cand[0] < best_by_type[t][0]:
            best_by_type[t] = cand
    deals = sorted(best_by_type.values(), key=lambda x: x[0])
    lines += [SEP, "🔥 <b>MIGLIORI OCCASIONI</b>"]
    if deals:
        for score, b, k in deals[:6]:
            tags = []
            m = ctx.minimi.get(k)
            if m and b["prezzo"] < m["prezzo"] - 0.009:
                tags.append("📉 minimo")
            if ctx.collector(b):
                tags.append("💎")
            r = ctx.ev_ratio(b)
            if r is not None and r >= ctx.cfg.get("affare_buste", 1.0):
                tags.append("🔥 rende")
            pp = ctx.per_pack(b) if b["tipo"] in PACK_TYPES else ""
            lines.append(f"• {esc(b['set'])} · {esc(short_label(b))} — <b>{euro(b['prezzo'])}</b>{pp} "
                         f"({link(b)}){' ' + ' '.join(tags) if tags else ''}")
        if len(deals) > 6:
            lines.append(f"<i>+ altri {len(deals) - 6} prodotti al prezzo giusto</i>")
    else:
        lines.append("Oggi niente al prezzo giusto.")

    # 📊 mercato: valore medio delle carte e carte top
    rows = []
    for s in ctx.cfg["set"]:
        mv = ctx.market.get(s["nome"])
        if not mv:
            continue
        box = [o for o in offers.values() if o["set"] == s["nome"] and o["tipo"] == "Box 36 buste"
               and o["stato"] == "disponibile" and o.get("prezzo")]
        rend = ""
        if box:
            b = min(box, key=lambda o: o["prezzo"])
            rend = f" · il box rende il {round(ctx.ev_ratio(b) * 100)}%"
        rows.append(f"• <b>{esc(s['nome'])}</b>: carte ~{euro(mv['ev_busta'])}/busta{rend}")
        if weekly and mv.get("top"):
            rows.append("   " + " · ".join(f"{esc(t['nome'])} {euro(t['prezzo'])}" for t in mv["top"][:2]))
    if rows:
        lines += [SEP, "📊 <b>MERCATO</b> <i>(prezzi Cardmarket, stime)</i>"] + rows[:12]

    if news:
        lines += [SEP, f"🆕 <b>NOVITÀ</b> · {len(news)} prodotti di set non seguiti"]
        for n in news[:3]:
            lines.append(f"• <a href=\"{esc(n['url'])}\">{esc(n['titolo'][:60])}</a> {euro(n['prezzo'])}")

    lines += [SEP, f"<i>{shops_ok} negozi controllati · {now.strftime('%H:%M')}</i>"]
    return "\n".join(lines)


# ------------------------------------------------------------------ cruscotto


MONTH_SHORT = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]


def best_deals(ctx, groups):
    """Migliore offerta per ogni (set, tipo), ordinate dalla più conveniente: [(punteggio, offerta, chiave)]."""
    best = {}
    for k, g in groups.items():
        good = [o for o in g if o["stato"] == "disponibile" and ctx.good(o)]
        if not good:
            continue
        b = min(good, key=ctx.total)
        t = (b["set"], b["tipo"])
        cand = (deal_score(ctx, b, k, g) - (0.15 if ctx.hot(b) else 0), b, k)
        if t not in best or cand[0] < best[t][0]:
            best[t] = cand
    return sorted(best.values(), key=lambda x: x[0])


def dashboard_data(ctx, offers, groups, shops_ok, now, news_items, archived):
    today = now.date()
    d = {"data": fmt_day(today), "ora": now.strftime("%H:%M"), "negozi": shops_ok}

    # in primo piano: i prodotti con l'indice di interesse più alto
    focus = []
    for k, g in groups.items():
        sc, why = ctx.interest.get(k, (0, []))
        if sc < interest.MEDIUM or g[0]["set"] in archived:
            continue
        buy = sorted([o for o in g if o["stato"] in ("preordine", "disponibile") and o.get("prezzo")], key=ctx.total)
        o = buy[0] if buy else g[0]
        st = {"preordine": "preordine aperto", "disponibile": "disponibile"}.get(
            o["stato"], "in arrivo" if o["stato"] == "in_arrivo" else "esaurito ovunque")
        band, pct, ref = ctx.verdict(o)
        if not buy:
            band = None
        focus.append({"score": sc, "level": interest.level(sc), "label": product_label(o), "set": o["set"],
                      "stato": st, "why": ", ".join(why[:2]) or "prodotto da collezione",
                      "prezzo": euro(o["prezzo"]) if buy else "—",
                      "dove": o["negozio"] if buy else f"listino {euro(ref)}",
                      "url": o["url"] if buy and band != "gonfiato" else None,
                      "giudizio": band, "scost": round(pct * 100) if buy else None,
                      "obiettivo": euro(ctx.target(o)) if band == "gonfiato" else None})
    focus.sort(key=lambda f: -f["score"])
    d["focus"] = focus[:6]

    events = [e for e in calendar_events(ctx.cfg) if today <= date.fromisoformat(e["data"]) <= today + timedelta(days=60)]
    d["uscite"] = []
    for e in events:
        dt = date.fromisoformat(e["data"])
        n_pre = len({o["negozio"] for o in offers.values() if o["stato"] == "preordine" and matches_event(o, e)})
        days = (dt - today).days
        d["uscite"].append({"giorno": str(dt.day), "mese": MONTH_SHORT[dt.month - 1], "set": e["set"],
                            "cosa": "Uscita" if e["cosa"] == f"Uscita {e['set']}" else e["cosa"],
                            "stima": bool(e.get("stima")), "n_pre": n_pre,
                            "tra": "oggi" if days == 0 else ("domani" if days == 1 else f"tra {days} giorni")})

    pre = []
    for k, g in groups.items():
        op = sorted([o for o in g if o["stato"] == "preordine"], key=ctx.total)
        if op and op[0]["set"] not in archived:
            b = op[0]
            sc = ctx.interest.get(k, (0, []))[0]
            band, pct, _ = ctx.verdict(b)
            chip = {"sotto": ("ok", f"{round(pct * 100)}%"), "allineato": ("bassa", "a listino"),
                    "accettabile": ("media", f"+{round(pct * 100)}%")}.get(band, ("alta", f"+{round(pct * 100)}% gonfiato"))
            pre.append((-sc, {"label": product_label(b), "set": b["set"], "prezzo": euro(b["prezzo"]),
                              "negozio": b["negozio"], "url": b["url"] if band != "gonfiato" else None, "n": len(op),
                              "level": interest.level(sc), "sopra": band == "gonfiato", "giudizio": chip}))
    d["preordini"] = [p for _, p in sorted(pre, key=lambda x: x[0])]

    d["occasioni"] = []
    for _, b, k in best_deals(ctx, groups):
        if b["set"] in archived:
            continue
        tags = []
        band, pct, _ = ctx.verdict(b)
        tags.append({"sotto": ("ok", f"{round(pct * 100)}%"), "allineato": ("bassa", "a listino"),
                     "accettabile": ("media", f"+{round(pct * 100)}%")}.get(band, ("alta", f"+{round(pct * 100)}%")))
        m = ctx.minimi.get(k)
        if m and b["prezzo"] < m["prezzo"] - 0.009:
            tags.append(("ok", "minimo"))
        if ctx.hot(b):
            tags.append(("alta", "richiesto"))
        elif ctx.collector(b):
            tags.append(("media", "collezione"))
        n = packs(b["tipo"], b["titolo"]) if b["tipo"] in PACK_TYPES else None
        d["occasioni"].append({"label": product_label(b), "set": b["set"], "prezzo": euro(b["prezzo"]),
                               "negozio": b["negozio"], "url": b["url"], "tags": tags,
                               "busta": f"{euro(b['prezzo'] / n)}/busta" if n else ""})

    d["mercato"] = []
    for s in ctx.cfg["set"]:
        mv = ctx.market.get(s["nome"])
        if not mv:
            continue
        box = [o for o in offers.values() if o["set"] == s["nome"] and o["tipo"] == "Box 36 buste"
               and o["stato"] == "disponibile" and o.get("prezzo")]
        rende = ctx.ev_ratio(min(box, key=lambda o: o["prezzo"])) if box else None
        d["mercato"].append({"set": s["nome"], "rende": rende,
                             "top": [(t["nome"], euro(t["prezzo"])) for t in mv.get("top", [])[:2]]})
    d["mercato"].sort(key=lambda m: -(m["rende"] or 0))

    d["notizie"] = [{"titolo": n["titolo"][:110], "fonte": n["fonte"], "url": n["url"], "ufficiale": n.get("ufficiale")}
                    for n in news_items if sources.relevant(n["titolo"])][:8]
    d["seguiti"] = [s["nome"] for s in ctx.cfg["set"] if s["nome"] not in archived]
    d["archiviati"] = sorted(archived)
    d["kpi"] = [{"label": "Preordini", "value": str(len(d["preordini"])),
                 "sub": f"{sum(1 for p in d['preordini'] if p['level'] == 'alta')} ad alto interesse"},
                {"label": "Occasioni", "value": str(len(d["occasioni"])), "sub": "al prezzo giusto"}]
    nxt = next((u for u in d["uscite"]), None)
    d["kpi"].append({"label": "Prossima uscita", "value": f"{nxt['giorno']} {nxt['mese']}" if nxt else "—",
                     "sub": f"{nxt['set']} · {nxt['tra']}" if nxt else ""})
    return d


def send_photo(token, chat, png, caption, buttons=None, as_document=False):
    params = {"chat_id": chat, "caption": caption[:1000], "parse_mode": "HTML"}
    if buttons:
        params["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t[:60], "url": u}] for t, u in buttons]})
    kind = "document" if as_document else "photo"
    with open(png, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{token}/send{kind.title()}", data=params,
                          files={kind: ("riepilogo.png", f, "image/png")}, timeout=60)
    r.raise_for_status()


# ------------------------------------------------------------------ cervello: set nuovi e set raffreddati


def discover_sets(state, cfg_sets, shop_names, raw_news, offers, news_items, today, baseline):
    """Aggiorna i candidati con notizie e titoli dei negozi; restituisce i set appena confermati."""
    def st(n):
        if n.get("preordine"):
            return "preordine" if n.get("comprabile") else "in_arrivo"
        return "disponibile" if n.get("comprabile") else "esaurito"
    shop_titles = [(n["negozio"], n["titolo"], {"stato": st(n), "uscita": n.get("uscita")}) for n in raw_news]
    cand = sources.update_candidates(state.setdefault("candidati", {}), cfg_sets, news_items, shop_titles, today)
    added = []
    for name, c in list(cand.items()):
        if sources.known(name, list(cfg_sets) + added):
            del cand[name]
            continue
        if sources.confirmed(c, shop_names) and c["futuro"]:
            d, certain = sources.best_date(c)
            s = {"nome": name.title(), "cerca": name, "auto": True, "scoperto": today.isoformat(),
                 "fonti": c["fonti"][:6], "primo_giro": True}
            if d:
                s.update(uscita=d, certo=certain)
            state.setdefault("set_auto", []).append(s)
            added.append(s)
            del cand[name]
    # i candidati mai confermati si dimenticano dopo 60 voci
    if len(cand) > 60:
        for k in list(cand)[:-60]:
            del cand[k]
    return [] if baseline else added


def new_set_text(s):
    lines = ["🆕 <b>NUOVO SET · LO SEGUO DA ORA</b>", f"<b>{esc(s['nome'])}</b>"]
    if s.get("uscita"):
        lines.append(f"🗓 Uscita {fmt_day(date.fromisoformat(s['uscita']))}{'' if s.get('certo') else ' (stima)'}")
    lines += [SEP, "Confermato da: " + esc(", ".join(s.get("fonti", []))),
              "Dal prossimo giro lo cerco in tutti i negozi e ti avviso dei preordini."]
    return "\n".join(lines), []


def update_archive(state, ctx, groups, today, baseline):
    """Storico giornaliero per set; archivia i set raffreddati e riattiva quelli tornati caldi."""
    hist = state.setdefault("storia_set", {})
    archived = set(state.get("archiviati", []))
    msgs = []
    by_set = {}
    for g in groups.values():
        by_set.setdefault(g[0]["set"], []).append(g)
    for s in ctx.cfg["set"]:
        name = s["nome"]
        if name not in by_set:
            continue
        mv = ctx.market.get(name) or {}
        cards = round(sum(t["prezzo"] for t in mv.get("top", [])[:5]), 2) or None
        row = {"d": today.isoformat(), **interest.set_snapshot(by_set[name], ctx.soglia, cards)}
        rows = hist.setdefault(name, [])
        if rows and rows[-1]["d"] == row["d"]:
            rows[-1] = row
        else:
            rows.append(row)
        hist[name] = rows[-90:]
        released = s.get("uscita") and date.fromisoformat(s["uscita"]) < today - timedelta(days=60)
        revived = state.setdefault("riattivati", {})
        if name in archived and interest.heating(row):
            archived.discard(name)
            revived[name] = today.isoformat()
            msgs.append(("🔥 <b>SET TORNATO CALDO</b>", name,
                         "Sparisce dagli scaffali o i prezzi salgono: torno ad avvisarti per questo set."))
        elif name not in archived and released and interest.cooling(hist[name], today) and not (
                revived.get(name) and (today - date.fromisoformat(revived[name])).days < 21):
            archived.add(name)
            msgs.append(("🧊 <b>SET ARCHIVIATO</b>", name,
                         "Da 3 settimane si trova ovunque a listino e le carte top calano: "
                         "niente più avvisi, resta solo nel riepilogo. Se torna caldo te lo dico."))
    state["archiviati"] = sorted(archived)
    if baseline:
        return archived, []
    return archived, [(f"{h}\n<b>{esc(n)}</b>\n{SEP}\n{t}", []) for h, n, t in msgs]



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
    shop_names = {s["nome"] for s in cfg["negozi"]}

    # 📰 notizie verificate (ogni 6 ore) e set seguiti = config + set scoperti da soli
    state["fonti"] = sources.refresh_news(state.get("fonti", {}), now)
    news_items = state["fonti"].get("notizie", [])
    all_sets = [dict(s) for s in cfg["set"]] + [dict(s) for s in state.get("set_auto", [])]
    for name, d in sources.official_dates(news_items, all_sets, today).items():
        for s in all_sets:
            if s["nome"] == name and (s.get("uscita") != d or not s.get("certo")):
                s.update(uscita=d, certo=True)  # la data ufficiale vince sempre
    cfg = {**cfg, "set": all_sets}

    # nomi delle carte ex/Mega di ogni set: servono a riconoscere i prodotti senza nome del set
    state["nomi_set"] = market.refresh_names(cfg, state.get("nomi_set", {}), now)
    guess = make_guesser(state["nomi_set"])
    first_guess = "cerca_extra" not in state  # primo giro con il riconoscimento: niente avvisi arretrati
    extra = state.setdefault("cerca_extra", {})
    quiet = set()
    sets = [{**s, "extra": extra.get(s["nome"], [])} for s in cfg["set"]]

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda sh: (sh, *scan_shop(sh, sets, today, guess)), cfg["negozi"]))

    offers, news, raw_news, shops_ok = {}, [], [], 0
    for shop, off, ok, nw in results:
        print(f"{'✅' if ok else '⛔️'} {shop['nome']}: {len(off)} prodotti")
        if ok:
            shops_ok += 1
            offers.update(off)
            raw_news += nw
            for n in nw:
                set_name, key = guess(n["titolo"])
                if set_name:  # es. "Collezione Premium Mega Zygarde ex" → Equilibrio Perfetto
                    offers.setdefault(n["url"], {**n, "set": set_name})
                    if first_guess:
                        quiet.add(n["url"])
                    ks = extra.setdefault(set_name, [])
                    if key not in ks:
                        ks.append(key)
                        del ks[:-4]  # al massimo 4 ricerche extra per set
                else:
                    news.append(n)
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

    groups = {}
    for o in offers.values():
        groups.setdefault(product_key(o), []).append(o)

    # 💶 primo prezzo con cui ogni negozio ha messo in vendita ogni prodotto (serve per il "prezzo di lancio")
    firsts = state.setdefault("primi_prezzi", {})
    for k, g in groups.items():
        fp = firsts.setdefault(k, {})
        for o in g:
            if o.get("prezzo") and o["negozio"] not in fp:
                fp[o["negozio"]] = o["prezzo"]
    ctx.first_prices = firsts

    # 🔥 indice di interesse di ogni prodotto + storico giornaliero della domanda
    hist = state.setdefault("storia", {})
    for k, g in groups.items():
        o0 = g[0]
        ref = ctx.reference(o0)[0]
        flags = (bool(ctx.sets.get(o0["set"], {}).get("speciale")),
                 any("pokemon center" in norm(o["titolo"]) or has(norm(o["titolo"]), "esclusiva", "esclusivo")
                     for o in g))
        ctx.interest[k] = interest.score(o0, g, ref, flags, hist.get(k), today)
        interest.record(hist, k, interest.snapshot(g, ref), today)
    for k in [k for k in hist if k not in groups and hist[k][-1]["d"] < (today - timedelta(days=30)).isoformat()]:
        del hist[k]

    for o in sorted(offers.values(), key=lambda o: (o["set"], product_key(o), o["prezzo"] or 0)):
        print(f"   {o['stato'][:5]:5} {ctx.interest_of(o)[0]:3} {o['set']} · {product_label(o)} · {o['negozio']} "
              f"{euro(o['prezzo'])} — {o['titolo']}")

    # 🧊 set raffreddati / tornati caldi, 🆕 set nuovi confermati dalle fonti
    archived, arch_msgs = update_archive(state, ctx, groups, today, baseline)
    new_sets = discover_sets(state, cfg["set"], shop_names, raw_news, offers, news_items, today, baseline)

    # set scoperti al giro prima: primo passaggio nei negozi, solo gli avvisi di preordine
    first_scan = {s["nome"] for s in state.get("set_auto", []) if s.get("primo_giro") and s not in new_sets}
    for s in state.get("set_auto", []):
        if s["nome"] in first_scan:
            s["primo_giro"] = False

    alerts = []  # (priorità, testo, pulsanti)
    alerts += [(0, *new_set_text(s)) for s in new_sets]
    alerts += [(1, t, b) for t, b in arch_msgs]
    seen_products = set(state.get("prodotti_visti", []))
    if not baseline:
        pre_new, pre_drop = {}, {}
        for url, o in offers.items():
            old = prev.get(url) or ghosts.get(url)
            if o["negozio"] not in known_shops or url in quiet or o["set"] in archived:
                continue  # prima lettura di un negozio, prodotto appena riconosciuto o set archiviato
            k = product_key(o)
            if o["stato"] == "preordine":
                if old is None or old.get("stato") != "preordine":
                    pre_new.setdefault(k, set()).add(url)
                elif old.get("prezzo") and o["prezzo"] and o["prezzo"] < old["prezzo"] - 0.009:
                    pre_drop.setdefault(k, set()).add(url)
                continue
            if o["stato"] != "disponibile" or not ctx.good(o) or o["set"] in first_scan:
                continue
            if old is not None and old.get("stato") == "preordine":
                continue  # uscito: era già prenotabile, lo sapevi
            others = [x for x in groups[k] if x is not o and x["stato"] == "disponibile" and ctx.good(x)]
            boost = -1 if ctx.hot(o) else 0  # i prodotti molto richiesti passano davanti
            g = groups[k]
            if old is None:
                alerts.append((2 + boost, *alert_text(ctx, "new", o, others=others, group=g)))
            elif old.get("stato") == "disponibile" and old.get("prezzo") and not ctx.good(old):
                # era in vendita ma a prezzo gonfiato, ora è sceso a un prezzo sensato
                alerts.append((0 + boost, *alert_text(ctx, "target", o, old["prezzo"], others=others, group=g)))
            elif not (old.get("stato") == "disponibile" and ctx.good(old)):
                alerts.append((1 + boost, *alert_text(ctx, "back", o, others=others, group=g)))
            elif old.get("prezzo") and o["prezzo"] < old["prezzo"] - 0.009:
                alerts.append((3 + boost, *alert_text(ctx, "drop", o, old["prezzo"], others=others, group=g)))

        for k in set(pre_new) | set(pre_drop):
            if k not in pre_new and not any(ctx.verdict(o)[0] != "gonfiato"
                                            for o in groups[k] if o["stato"] == "preordine"):
                continue  # è sceso ma resta gonfiato: niente avviso
            alerts.append((0 if k in pre_new else 3,
                           *preorder_text(ctx, groups[k], pre_new.get(k, set()), pre_drop.get(k, set()))))

        # 👀 prodotti mai visti prima, presenti solo come scheda non prenotabile
        for k, g in groups.items():
            if k not in seen_products and g[0]["set"] not in archived | first_scan and \
                    all(o["stato"] == "in_arrivo" for o in g) and any(o["negozio"] in known_shops for o in g):
                alerts.append((1, *upcoming_text(ctx, g)))
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
                alerts.append((0, *reminder_text(ctx, ev, days, offers)))
                sent.add(key)
    state["promemoria"] = sorted(sent)[-300:]

    seen_news = set(state.get("novita_viste", []))
    news = list({n["url"]: n for n in news}.values())
    fresh_news = [n for n in news if n["url"] not in seen_news]
    state["novita_viste"] = sorted(seen_news | {n["url"] for n in news})[-800:]
    new_names = {s["cerca"] for s in new_sets}
    grouped = {}
    for n in ([] if baseline else fresh_news):
        if new_names & set(sources.extract_set_names(n["titolo"])):
            continue  # set appena scoperto: i suoi prodotti arrivano come preordini dal prossimo giro
        grouped.setdefault(norm(n["titolo"]), []).append(n)
    for items in list(grouped.values())[:3]:
        alerts.append((4, *news_text(items)))

    msgs = []  # {"text", "buttons", "summary", "photo"}
    alerts.sort(key=lambda a: a[0])
    msgs += [{"text": t, "buttons": b, "summary": False} for _, t, b in alerts[:MAX_ALERTS]]
    if len(alerts) > MAX_ALERTS:
        msgs.append({"text": f"…e altre {len(alerts) - MAX_ALERTS} novità: le trovi nel cruscotto.",
                     "buttons": None, "summary": False})

    # 📊 cruscotto: la pagina web si aggiorna a ogni giro, l'immagine parte la mattina
    diag = state["diagnostica"] = {"ora": now.isoformat(timespec="minutes"), "chrome": dashboard.find_chrome()}
    data = dashboard_data(ctx, offers, groups, shops_ok, now, news_items, archived)
    try:
        DOCS_DIR.mkdir(exist_ok=True)
        (DOCS_DIR / "index.html").write_text(dashboard.render(data, "page"), encoding="utf-8")
        (DOCS_DIR / ".nojekyll").write_text("")  # GitHub Pages: pubblica la pagina così com'è
        diag["pagina"] = str((DOCS_DIR / "index.html").resolve())
        for name, cmd in (("git_ignore", ["git", "check-ignore", "-v", "docs/index.html"]),
                          ("git_status", ["git", "status", "--porcelain", "--ignored", "docs"])):
            try:  # per capire perché la pagina non finisce nel repository
                r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=20)
                diag[name] = (r.stdout + r.stderr).strip()[:300] or "(niente)"
            except Exception as e:
                diag[name] = f"errore: {e}"
    except Exception as e:
        diag["pagina"] = f"errore: {e}"
        print("Pagina del cruscotto non scritta:", e)

    # l'immagine si disegna a ogni giro (pochi secondi): così la pagina web la mostra sempre aggiornata
    # e la diagnostica dice subito se sul server funziona, senza aspettare la mattina
    png = DOCS_DIR / "riepilogo.png"
    try:
        ok, why = dashboard.screenshot(dashboard.render(data, "image"), str(png))
    except Exception as e:
        ok, why = False, f"errore nel disegno: {e}"
    diag["immagine"] = why
    print("Immagine del mattino:", why)

    today_s = today.isoformat()
    did_summary = False
    if baseline or force or (now.hour >= SUMMARY_HOUR and state.get("ultimo_riepilogo") != today_s):
        did_summary = True
        text = summary_text(ctx, offers, shops_ok, [] if baseline else fresh_news, now, today.weekday() == 0)
        buttons = summary_buttons(ctx, cfg, groups, archived)
        if ok:
            msgs.append({"text": summary_caption(ctx, data, groups), "buttons": buttons,
                         "summary": True, "photo": str(png), "fallback": split_message(text)})
        else:  # niente immagine: riepilogo testuale, con il motivo e i pulsanti
            parts = split_message(f"⚠️ <i>Immagine non creata: {esc(why)[:200]}</i>\n\n" + text)
            msgs += [{"text": part, "buttons": buttons if i == len(parts) - 1 else None, "summary": True}
                     for i, part in enumerate(parts)]

    failed_summary = False
    if token:
        for i, m in enumerate(msgs):
            for attempt in (1, 2):
                try:
                    if m.get("photo") and attempt == 1:
                        send_photo(token, chat, m["photo"], m["text"], m["buttons"])
                    elif m.get("photo"):  # la foto non passa: come documento, poi in testo
                        try:
                            send_photo(token, chat, m["photo"], m["text"], m["buttons"], as_document=True)
                        except Exception as e2:
                            diag["invio_foto"] = str(e2)[:300]
                            for part in m["fallback"]:
                                send(token, chat, part, None)
                    else:
                        # secondo tentativo in testo semplice, nel caso Telegram rifiuti HTML o foto
                        send(token, chat, m["text"] if attempt == 1 else
                             html.unescape(re.sub(r"<[^>]+>", "", m["text"])), m["buttons"])
                    break
                except Exception as e:
                    print(f"Invio non riuscito (tentativo {attempt}):", e)
                    detail = getattr(getattr(e, "response", None), "text", "") or ""
                    diag.setdefault("errori_invio", []).append(f"{e} {detail}"[:300])
                    if attempt == 1:
                        time.sleep(5)
                    elif m["summary"]:
                        failed_summary = True
            if i + 1 < len(msgs):
                time.sleep(1)  # Telegram limita i messaggi troppo ravvicinati
        print(f"Inviati {len(msgs)} messaggi.")
    else:
        print("\n--- TELEGRAM_TOKEN mancante: anteprima ---")
        for m in msgs:
            print(("[FOTO] " if m.get("photo") else "") + m["text"],
                  "".join(f"\n[{b[0]}]" for b in m["buttons"] or []), "\n")
    if did_summary and not failed_summary:
        state["ultimo_riepilogo"] = today_s

    state["offerte"] = offers
    state["minimi"] = minimi
    state["ultimo_controllo"] = now.isoformat(timespec="minutes")
    state["negozi_letti"] = shops_ok
    state["negozi_visti"] = sorted(known_shops | {sh["nome"] for sh, _, ok, _ in results if ok})
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")))


def summary_caption(ctx, d, groups):
    n_hot = sum(1 for p in d["preordini"] if p["level"] == "alta")
    lines = [f"☀️ <b>POKÉMON · {esc(d['data'].upper())}</b>",
             f"🟠 {len(d['preordini'])} preordini aperti{f' ({n_hot} ad alto interesse)' if n_hot else ''} · "
             f"🔥 {len(d['occasioni'])} occasioni"]
    if d["uscite"]:
        u = d["uscite"][0]
        lines.append(f"📅 Prossima: {esc(u['set'])} — {esc(u['cosa'])}, {esc(u['tra'])}")
    if d["focus"]:
        f = d["focus"][0]
        price = ""
        if f.get("giudizio") == "gonfiato":
            price = f", {esc(f['prezzo'])} = +{f['scost']}%: aspetta, obiettivo {esc(f['obiettivo'])}"
        elif f.get("giudizio"):
            price = f", {esc(f['prezzo'])} su {esc(f['dove'])}"
        lines.append(f"💎 In primo piano: {esc(f['label'])} {esc(f['set'])} ({esc(f['stato'])}{price})")
    return "\n".join(lines)


def summary_buttons(ctx, cfg, groups, archived):
    """Le 3 cose da guardare subito: prima i prodotti richiesti comprabili, poi le occasioni migliori."""
    picks, seen = [], set()
    hot = sorted(((ctx.interest.get(k, (0, []))[0], k) for k in groups), reverse=True)
    for sc, k in hot:
        if sc < interest.HIGH:
            break
        if groups[k][0]["set"] in archived:
            continue
        buy = sorted([o for o in groups[k] if o["stato"] in ("preordine", "disponibile") and o.get("prezzo")
                      and ctx.verdict(o)[0] != "gonfiato"], key=ctx.total)
        if buy and k not in seen:  # solo se si trova a un prezzo sensato
            picks.append(("🔥", buy[0]))
            seen.add(k)
    for _, b, k in best_deals(ctx, groups):
        if k not in seen and b["set"] not in archived:
            picks.append(("🛒", b))
            seen.add(k)
    marks = {"sotto": "🟢", "allineato": "⚪", "accettabile": "🟡"}
    buttons = [(f"{ic} {short_label(o)} {o['set']} · {euro(o['prezzo'])} {marks.get(ctx.verdict(o)[0], '')}".strip(),
                o["url"]) for ic, o in picks[:3]]
    if cfg.get("cruscotto_url"):
        buttons.append(("📊 Cruscotto completo", cfg["cruscotto_url"]))
    return buttons


if __name__ == "__main__":
    main()
