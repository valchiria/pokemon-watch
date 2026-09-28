"""Robot Pokémon: controlla i negozi e scrive su Telegram quando qualcosa cambia.

Gira su GitHub Actions ogni ora. Legge products.json, confronta con state.json
(l'ultima situazione vista) e manda un messaggio solo per le novità.
Una volta al giorno (dalle 9 in poi, ora italiana) manda anche il riepilogo completo.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
PRODUCTS_FILE = ROOT / "products.json"
STATE_FILE = ROOT / "state.json"
TZ = ZoneInfo("Europe/Rome")
SUMMARY_HOUR = 9
DEFAULT_CHAT_ID = "875856621"  # chat Telegram di Riccardo

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Accept-Language": "it-IT,it;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

DISPONIBILE, PREORDINE, ESAURITO, SCONOSCIUTO = "disponibile", "preordine", "esaurito", "non leggibile"
COMPRABILE = {DISPONIBILE, PREORDINE}

OUT_WORDS = [
    "prodotto esaurito", "esaurito", "sold out", "non disponibile", "out of stock",
    "avvisami quando", "informami quando", "inviami un'e-mail quando", "inviami un’e-mail quando",
]
PRE_WORDS = ["preordina", "pre-ordina", "prenota ora", "in preordine"]
IN_WORDS = ["aggiungi al carrello", "acquista ora", "add to cart"]


# ---------------------------------------------------------------- lettura pagine

def parse_price(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("€", "").replace("EUR", "").replace("\xa0", "").strip()
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


def read_shopify(url, session):
    """Negozi Shopify: il file .js del prodotto dice esattamente se si compra."""
    r = session.get(url.split("?")[0].rstrip("/") + ".js", headers=HEADERS, timeout=25)
    r.raise_for_status()
    data = r.json()
    variants = data.get("variants") or []
    available = bool(data.get("available")) or any(v.get("available") for v in variants)
    prices = [v.get("price") for v in variants if v.get("price") is not None] or [data.get("price")]
    prices = [p for p in prices if p is not None]
    price = min(prices) / 100 if prices else None
    status = DISPONIBILE if available else ESAURITO
    return status, price, "dati del negozio (Shopify)"


def _iter_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                if "@graph" in item:
                    stack.extend(item["@graph"] if isinstance(item["@graph"], list) else [item["@graph"]])


def read_jsonld(soup):
    for item in _iter_jsonld(soup):
        types = item.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue
        offers = item.get("offers")
        if isinstance(offers, dict) and "offers" in offers:
            offers = offers["offers"]
        offers = offers if isinstance(offers, list) else [offers] if offers else []
        for off in offers:
            if not isinstance(off, dict):
                continue
            avail = str(off.get("availability", "")).lower()
            price = parse_price(off.get("price") or off.get("lowPrice") or
                                (off.get("priceSpecification") or {}).get("price"))
            if "preorder" in avail or "presale" in avail:
                return PREORDINE, price
            if "instock" in avail or "limitedavailability" in avail or "onlineonly" in avail:
                return DISPONIBILE, price
            if any(k in avail for k in ("outofstock", "soldout", "discontinued")):
                return ESAURITO, price
            if avail == "" and price is not None:
                return None, price
    return None, None


def read_text(soup):
    """Parole vicino al pulsante d'acquisto. Restituisce (stato, preciso?).

    'preciso' è True solo se abbiamo trovato il blocco del carrello del prodotto:
    altrove la pagina può contenere prodotti correlati con scritte 'Esaurito'.
    """
    area, precise = None, False
    for sel in ["form.cart", ".summary .stock", "p.stock", ".product-form", "form[action*='/cart/add']",
                ".single_add_to_cart_button", ".product-info-main", ".product-add-form"]:
        el = soup.select_one(sel)
        if el is not None:
            area, precise = (el.find_parent() or el), True
            break
    if area is None:
        area = soup.select_one(".product .summary") or soup.select_one("main") or soup.body or soup
    text = " ".join(area.get_text(" ", strip=True).lower().split())
    btn = soup.select_one("button.single_add_to_cart_button, button[name='add-to-cart'], button[name='add']")
    if btn is not None and (btn.has_attr("disabled") or "disabled" in (btn.get("class") or [])):
        return ESAURITO, True
    if any(w in text for w in OUT_WORDS):
        return ESAURITO, precise
    if any(w in text for w in PRE_WORDS):
        return PREORDINE, precise
    if any(w in text for w in IN_WORDS):
        return DISPONIBILE, precise
    return None, False


def read_price_meta(soup):
    for sel, attr in [("meta[property='product:price:amount']", "content"),
                      ("meta[itemprop='price']", "content"), ("[itemprop='price']", "content")]:
        el = soup.select_one(sel)
        if el is not None and el.get(attr):
            p = parse_price(el.get(attr))
            if p:
                return p
    el = soup.select_one(".summary .price ins .amount, .summary .price .amount, p.price .amount")
    if el is not None:
        return parse_price(el.get_text())
    return None


def read_html(url, session):
    r = session.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    status, price = read_jsonld(soup)
    text_status, precise = read_text(soup)
    # "Esaurito" scritto accanto al pulsante vince sui dati strutturati
    if text_status == ESAURITO and precise:
        status = ESAURITO
    elif status is None:
        status = text_status
    if price is None:
        price = read_price_meta(soup)
    return status or SCONOSCIUTO, price, "pagina del negozio"


def check(product, session):
    url = product["url"]
    last_err = None
    for attempt in range(2):
        try:
            if "/products/" in url:
                try:
                    return read_shopify(url, session)
                except Exception:
                    return read_html(url, session)
            return read_html(url, session)
        except Exception as e:  # rete, 403, 404...
            last_err = e
            time.sleep(3)
    return SCONOSCIUTO, None, f"errore: {type(last_err).__name__}"


# ---------------------------------------------------------------- Telegram

def tg(method, token, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=25)
    r.raise_for_status()
    return r.json()


def find_chat_id(token, state):
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    if state.get("chat_id"):
        return state["chat_id"]
    if DEFAULT_CHAT_ID:
        return DEFAULT_CHAT_ID
    updates = tg("getUpdates", token).get("result", [])
    for upd in reversed(updates):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            state["chat_id"] = str(chat["id"])
            return state["chat_id"]
    return None


def send(token, chat_id, text):
    # Telegram accetta al massimo 4096 caratteri per messaggio
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        tg("sendMessage", token, chat_id=chat_id, text=c, parse_mode="HTML",
           disable_web_page_preview="true")


# ---------------------------------------------------------------- messaggi

def euro(p):
    return "—" if p is None else f"{p:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


ICON = {DISPONIBILE: "🟢", PREORDINE: "🔵", ESAURITO: "🔴", SCONOSCIUTO: "⚪️"}


def link(p):
    return f'<a href="{html.escape(p["url"])}">{html.escape(p["nome"])}</a>'


def diff(product, old, new):
    """Restituisce (priorità, riga) se la novità merita un messaggio, altrimenti None."""
    st, price = new["status"], new["price"]
    soglia = product.get("soglia")
    ok_soglia = soglia is None or price is None or price <= soglia
    ost, oprice = (old or {}).get("status"), (old or {}).get("price")
    who = f'{link(product)} · {html.escape(product["negozio"])}'

    if st in COMPRABILE and ost not in COMPRABILE and ost is not None:
        if not ok_soglia:
            return (3, f"🟡 {who}\n   è tornato {st}, ma a {euro(price)} (sopra la tua soglia di {euro(soglia)})")
        verb = "PRENOTABILE" if st == PREORDINE else "COMPRABILE"
        return (0, f"🚨 {who}\n   ora è <b>{verb}</b> a <b>{euro(price)}</b> (era {ost})")
    if st == ESAURITO and ost in COMPRABILE:
        return (2, f"🔴 {who}\n   ora è esaurito (prima {ost} a {euro(oprice)})")
    if st in COMPRABILE and ost in COMPRABILE and price is not None and oprice is not None and price < oprice - 0.009:
        tag = " — <b>sotto la tua soglia</b>" if soglia and price <= soglia and oprice > soglia else ""
        return (1, f"💶 {who}\n   prezzo sceso: {euro(oprice)} → <b>{euro(price)}</b>{tag}")
    return None


def summary(products, results, now):
    lines = [f"📋 <b>Riepilogo Pokémon</b> · {now:%d/%m %H:%M}", ""]
    groups = [(DISPONIBILE, "Comprabili"), (PREORDINE, "Prenotabili"), (ESAURITO, "Esauriti"),
              (SCONOSCIUTO, "Non leggibili")]
    for st, title in groups:
        rows = [(p, r) for p, r in zip(products, results) if r["status"] == st]
        if not rows:
            continue
        lines.append(f"{ICON[st]} <b>{title}</b> ({len(rows)})")
        for p, r in sorted(rows, key=lambda x: (x[1]["price"] or 9e9)):
            extra = ""
            if st in COMPRABILE and p.get("soglia") and r["price"] and r["price"] <= p["soglia"]:
                extra = " 🎯"
            lines.append(f"• {link(p)} · {html.escape(p['negozio'])} · {euro(r['price'])}{extra}")
        lines.append("")
    ok = sum(1 for r in results if r["status"] != SCONOSCIUTO)
    lines.append(f"Letti {ok} prodotti su {len(results)}. 🎯 = sotto la tua soglia.")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    force_summary = os.environ.get("FORCE_SUMMARY", "") == "true"
    dry = not token
    products = json.loads(PRODUCTS_FILE.read_text())["prodotti"]
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    seen = state.setdefault("prodotti", {})
    first_run = not seen

    session = requests.Session()
    results = []
    for p in products:
        status, price, how = check(p, session)
        results.append({"status": status, "price": price, "how": how})
        print(f"{ICON[status]} {p['nome']} · {p['negozio']}: {status} {euro(price)} [{how}]")

    now = datetime.now(TZ)
    alerts = []
    for p, r in zip(products, results):
        key = p["url"]
        old = seen.get(key)
        if r["status"] == SCONOSCIUTO:
            # pagina non letta: teniamo l'ultima situazione nota, contiamo i fallimenti
            if old:
                old["fails"] = old.get("fails", 0) + 1
            continue
        d = diff(p, old, r)
        if d:
            alerts.append(d)
        seen[key] = {"status": r["status"], "price": r["price"], "fails": 0,
                     "visto": now.isoformat(timespec="minutes")}

    # tieni in memoria solo i prodotti ancora in lista
    for key in list(seen):
        if key not in {p["url"] for p in products}:
            del seen[key]

    msgs = []
    if alerts:
        alerts.sort()
        head = f"🔔 <b>Pokémon: {len(alerts)} novità</b>"
        msgs.append(head + "\n\n" + "\n\n".join(a[1] for a in alerts))

    today = now.date().isoformat()
    if first_run or force_summary or (now.hour >= SUMMARY_HOUR and state.get("ultimo_riepilogo") != today):
        text = summary(products, results, now)
        if first_run:
            text = "🤖 <b>Robot Pokémon attivo!</b> Ti scrivo appena qualcosa cambia, più un riepilogo ogni mattina.\n\n" + text
        msgs.append(text)
        state["ultimo_riepilogo"] = today

    unreadable = sum(1 for r in results if r["status"] == SCONOSCIUTO)
    if unreadable > len(results) / 2 and state.get("avviso_blocco") != today:
        msgs.append(f"⚠️ Non sono riuscito a leggere {unreadable} negozi su {len(results)}. "
                    "Se succede di nuovo domani, scrivilo a Claude.")
        state["avviso_blocco"] = today

    state["ultimo_controllo"] = now.isoformat(timespec="minutes")

    if dry:
        print("\n--- TELEGRAM_TOKEN mancante: messaggi non inviati ---")
        for m in msgs:
            print(m, "\n")
    elif msgs:
        chat_id = find_chat_id(token, state)
        if not chat_id:
            print("Nessuna chat trovata: apri il bot su Telegram e premi Avvia, poi rilancia.")
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            sys.exit(1)
        for m in msgs:
            send(token, chat_id, m)
        print(f"Inviati {len(msgs)} messaggi.")

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
