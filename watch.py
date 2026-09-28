"""Robot Pokémon v2 — cerca i prodotti Pokémon in italiano nei negozi e scrive su Telegram.

Come funziona
- Per ogni set in config.json cerca il nome del set in ogni negozio usando la ricerca
  ufficiale del negozio (Shopify o WooCommerce), che dice esattamente se un prodotto
  si può comprare. Niente link fissi: se un negozio aggiunge un prodotto, lo trova da solo.
- Riconosce il tipo di prodotto dal titolo (box 36, set allenatore, bundle...) e scarta
  inglese, giapponese, case da 6 box e carte singole.
- Avvisa solo quando qualcosa diventa comprabile al prezzo giusto o scende di prezzo.
- Ogni mattina dalle 9 manda il riepilogo: la migliore offerta per ogni prodotto.
- I negozi che non rispondono vengono ignorati in silenzio.
"""
import html
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"
TZ = ZoneInfo("Europe/Rome")
SUMMARY_HOUR = 9
DEFAULT_CHAT_ID = "875856621"
STATE_VERSION = 2
MAX_ALERTS = 8

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


FOREIGN = ["eng", "english", "inglese", "en", "jap", "jpn", "japanese", "giapponese", "giappone",
           "kor", "korean", "coreano", "chinese", "cinese", "chn", "de", "deutsch", "tedesco",
           "fr", "francese", "francais", "es", "spagnolo", "espanol"]
BULK = ["case", "cassa", "x6", "6x", "x2", "2x", "x3", "3x", "factory sealed", "sealed case", "lotto", "set di"]

TYPES_PACKS = {"Box 36 buste": 36, "Set Allenatore": 9, "Bundle 6 buste": 6, "Ultra Premium": 30,
               "Collezione Premium": 8, "Tin": 4, "Mini Tin": 2}


def classify(title):
    """Tipo di prodotto sigillato dal titolo, oppure None se va scartato."""
    t = norm(title)
    if has(t, *FOREIGN) or any(f" {b} " in t for b in BULK):
        return None
    if has(t, "carta", "card", "singola", "promo") and not has(t, "box", "display", "bundle", "blister",
                                                               "collezione", "set", "tin"):
        return None
    if has(t, "mazzo", "mazzi", "deck", "portfolio", "raccoglitore", "album", "bustine protettive",
           "sleeves", "toolkit"):
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


# ------------------------------------------------------------------ negozi

def get_json(session, url):
    r = session.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


def search_shopify(session, site, query):
    url = (f"{site}/search/suggest.json?q={quote(query)}&resources[type]=product"
           "&resources[limit]=10&resources[options][unavailable_products]=last")
    data = get_json(session, url)
    out = []
    for p in data.get("resources", {}).get("results", {}).get("products", []):
        link = p.get("url", "")
        link = link if link.startswith("http") else site + link.split("?")[0]
        out.append({"titolo": p.get("title", ""), "url": link,
                    "comprabile": bool(p.get("available")),
                    "prezzo": parse_price(p.get("price") or p.get("price_min"))})
    return out


def search_woo(session, site, query, extra=""):
    url = f"{site}/wp-json/wc/store/v1/products?search={quote(query)}&per_page=50{extra}"
    data = get_json(session, url)
    out = []
    for p in data if isinstance(data, list) else []:
        prices = p.get("prices") or {}
        minor = int(prices.get("currency_minor_unit", 2) or 2)
        raw = prices.get("price")
        price = int(raw) / 10 ** minor if raw not in (None, "") and str(raw).isdigit() else parse_price(raw)
        buy = bool(p.get("is_purchasable", True)) and bool(p.get("is_in_stock"))
        out.append({"titolo": html.unescape(re.sub("<[^>]+>", "", p.get("name", ""))),
                    "url": p.get("permalink", ""), "comprabile": buy,
                    "preordine": bool(p.get("is_on_backorder")), "prezzo": price,
                    "data": p.get("date_created") or ""})
    return out


QUERIES_SHOPIFY = ["{s}", "{s} box", "{s} set allenatore", "{s} bundle", "{s} collezione"]


def scan_shop(shop, sets):
    """Tutte le offerte di un negozio per i set seguiti. Restituisce (offerte, ok, novità)."""
    session = requests.Session()
    offers, found_any, errors = {}, False, 0
    for s in sets:
        queries = [s["cerca"]] if shop["tipo"] == "woo" else [q.format(s=s["cerca"]) for q in QUERIES_SHOPIFY]
        for q in queries:
            try:
                res = search_woo(session, shop["sito"], q) if shop["tipo"] == "woo" \
                    else search_shopify(session, shop["sito"], q)
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
    if shop["tipo"] == "woo" and found_any:
        try:
            for r in search_woo(session, shop["sito"], "pokemon", "&orderby=date&order=desc&per_page=30"):
                kind = classify(r["titolo"])
                if kind and not any(matches_set(r["titolo"], s) for s in sets):
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


# ------------------------------------------------------------------ messaggi

def euro(p):
    return "—" if p is None else f"{p:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def per_pack(o):
    n = packs(o["tipo"], o["titolo"])
    return f" · {euro(o['prezzo'] / n)}/busta" if n and o.get("prezzo") else ""


def esc(s):
    return html.escape(str(s))


def soglia_for(cfg, set_name, kind):
    s = next((x for x in cfg["set"] if x["nome"] == set_name), {})
    return (s.get("soglie") or {}).get(kind, cfg["soglie"].get(kind))


def good(o, cfg):
    sg = soglia_for(cfg, o["set"], o["tipo"])
    return o["comprabile"] and o["prezzo"] is not None and (sg is None or o["prezzo"] <= sg)


def alert_text(kind, o, old_price=None):
    label = "prenotabile" if o.get("preordine") else "disponibile"
    head = {
        "back": f"🟢 <b>Di nuovo {label}</b>",
        "new": f"🆕 <b>Appena comparso</b>",
        "drop": f"💶 <b>Prezzo sceso</b>",
    }[kind]
    price = f"<b>{euro(o['prezzo'])}</b>"
    if kind == "drop":
        price = f"<s>{euro(old_price)}</s> → <b>{euro(o['prezzo'])}</b>"
    return (f"{head}\n<b>{esc(o['set'])}</b> · {esc(o['tipo'])}\n"
            f"{price}{per_pack(o)}\nsu {esc(o['negozio'])}")


def summary_text(cfg, offers, shops_ok, news, now):
    days = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
    months = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
              "settembre", "ottobre", "novembre", "dicembre"]
    lines = [f"☀️ <b>Pokémon · {days[now.weekday()]} {now.day} {months[now.month - 1]}</b>",
             f"<i>{shops_ok} negozi controllati</i>", ""]

    best, seen_kinds = {}, {}
    for o in offers.values():
        key = (o["set"], o["tipo"])
        seen_kinds.setdefault(o["set"], set()).add(o["tipo"])
        if good(o, cfg) and (key not in best or o["prezzo"] < best[key]["prezzo"]):
            best[key] = o

    order = [s["nome"] for s in cfg["set"]]
    if best:
        lines.append("🟢 <b>Da prendere ora</b>")
        for set_name in order:
            items = sorted([o for (sn, _), o in best.items() if sn == set_name], key=lambda o: o["prezzo"])
            if not items:
                continue
            lines.append(f"\n<b>{esc(set_name)}</b>")
            for o in items:
                others = sum(1 for x in offers.values()
                             if x["set"] == set_name and x["tipo"] == o["tipo"] and good(x, cfg)) - 1
                more = (f" (anche in altri {others} negozi)" if others > 1 else " (anche in un altro negozio)") if others > 0 else ""
                lines.append(f"• {esc(o['tipo'])} — <b>{euro(o['prezzo'])}</b>{per_pack(o)}\n"
                             f"   <a href=\"{esc(o['url'])}\">{esc(o['negozio'])}</a>{more}")
        lines.append("")
    else:
        lines += ["Oggi niente di comprabile al prezzo giusto.", ""]

    out_lines = []
    for set_name in order:
        missing = sorted(k for k in seen_kinds.get(set_name, set()) if (set_name, k) not in best)
        if missing:
            out_lines.append(f"<b>{esc(set_name)}</b>: {esc(', '.join(missing))}")
    if out_lines:
        lines.append("🔴 <b>Esauriti o sopra prezzo</b>")
        lines += out_lines
        lines.append("")

    if news:
        lines.append("🆕 <b>Nuovi in catalogo</b> (set non ancora seguiti)")
        for n in news[:6]:
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

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if state.get("versione") != STATE_VERSION:
        state = {"versione": STATE_VERSION}
    first = "offerte" not in state
    prev = state.get("offerte", {})
    known_shops = set(state.get("negozi_visti", []))

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda sh: (sh, *scan_shop(sh, cfg["set"])), cfg["negozi"]))

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

    for o in sorted(offers.values(), key=lambda o: (o["set"], o["tipo"], o["prezzo"] or 0)):
        print(f"   {'🟢' if o['comprabile'] else '🔴'} {o['set']} · {o['tipo']} · {o['negozio']} "
              f"{euro(o['prezzo'])} — {o['titolo']}")

    now = datetime.now(TZ)
    alerts = []
    if not first:
        for url, o in offers.items():
            old = prev.get(url)
            if not good(o, cfg):
                continue
            if old is None:
                if o["negozio"] in known_shops:  # la prima lettura di un negozio fa da base, niente avvisi
                    alerts.append((1, alert_text("new", o), o))
            elif not good(old, cfg):
                alerts.append((0, alert_text("back", o), o))
            elif old.get("prezzo") and o["prezzo"] < old["prezzo"] - 0.009:
                alerts.append((2, alert_text("drop", o, old["prezzo"]), o))

    seen_news = set(state.get("novita_viste", []))
    fresh_news = [n for n in news if n["url"] not in seen_news]
    state["novita_viste"] = sorted(seen_news | {n["url"] for n in news})[-500:]
    for n in ([] if first else fresh_news):
        alerts.append((3, f"🆕 <b>Nuovo prodotto Pokémon</b>\n{esc(n['titolo'])}\n"
                          f"<b>{euro(n['prezzo'])}</b> su {esc(n['negozio'])}", {**n}))

    msgs = []
    alerts.sort(key=lambda a: a[0])
    for _, text, o in alerts[:MAX_ALERTS]:
        msgs.append((text, (f"🛒 Apri {o['negozio']}", o["url"])))
    if len(alerts) > MAX_ALERTS:
        msgs.append((f"…e altre {len(alerts) - MAX_ALERTS} novità: le trovi nel riepilogo di domattina.", None))

    today = now.date().isoformat()
    if first or force or (now.hour >= SUMMARY_HOUR and state.get("ultimo_riepilogo") != today):
        text = summary_text(cfg, offers, shops_ok, fresh_news if not first else [], now)
        if first:
            text = "🤖 <b>Robot Pokémon aggiornato!</b> Ora cerco da solo in tutti i negozi.\n\n" + text
        msgs.append((text, None))
        state["ultimo_riepilogo"] = today

    if token:
        for text, btn in msgs:
            send(token, chat, text, btn)
        print(f"Inviati {len(msgs)} messaggi.")
    else:
        print("\n--- TELEGRAM_TOKEN mancante: anteprima ---")
        for text, btn in msgs:
            print(text, f"\n[{btn[0]}]" if btn else "", "\n")

    state["offerte"] = offers
    state["ultimo_controllo"] = now.isoformat(timespec="minutes")
    state["negozi_letti"] = shops_ok
    state["negozi_visti"] = sorted(known_shops | {sh["nome"] for sh, _, ok, _ in results if ok})
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()


