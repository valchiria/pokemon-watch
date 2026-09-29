"""Costo di spedizione letto dal carrello dei negozi, come fa un cliente vero.

Molti negozi non scrivono il costo di spedizione da nessuna parte: si vede solo alla cassa.
Una volta a settimana per ogni negozio il robot mette nel carrello un prodotto economico e chiede
le tariffe per un indirizzo di Milano:
- Shopify: /products/<handle>.js → /cart/add.js → /cart/shipping_rates.json (o la versione "async");
- WooCommerce: Store API /wc/store/v1/cart/add-item → /cart/update-customer.
Prende la tariffa di consegna a domicilio più economica (esclusi ritiro in negozio e simili).
Il carrello è di una sessione usa e getta: non si crea nessun ordine e non si paga niente.
"""
import re
import time
from datetime import date, timedelta
from urllib.parse import urlparse

import requests

REFRESH_DAYS = 7
ADDRESS = {"country": "IT", "postcode": "20121", "city": "Milano", "state": "MI"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; robot-pokemon/3)", "Accept": "application/json",
           "Accept-Language": "it-IT,it;q=0.9"}
PICKUP = re.compile(r"ritir|pickup|pick up|negozio|in store|locker|punto|fermo", re.I)


def _cheapest(rates):
    """[(nome, prezzo)] → prezzo della consegna a domicilio più economica."""
    home = [p for n, p in rates if p is not None and not PICKUP.search(n or "")]
    return min(home) if home else None


def probe_shopify(session, site, product_url):
    handle = urlparse(product_url).path.rstrip("/").split("/products/")[-1]
    prod = session.get(f"{site}/products/{handle}.js", headers=HEADERS, timeout=20).json()
    variant = next((v for v in prod.get("variants", []) if v.get("available")), None)
    if not variant:
        return None
    r = session.post(f"{site}/cart/add.js", json={"items": [{"id": variant["id"], "quantity": 1}]},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    q = (f"shipping_address[zip]={ADDRESS['postcode']}&shipping_address[country]=Italy"
         f"&shipping_address[province]={ADDRESS['state']}")
    data = None
    r = session.get(f"{site}/cart/shipping_rates.json?{q}", headers=HEADERS, timeout=20)
    if r.status_code == 200:
        data = r.json()
    else:  # alcuni negozi calcolano le tariffe in modo asincrono
        session.post(f"{site}/cart/prepare_shipping_rates.json?{q}", headers=HEADERS, timeout=20)
        for _ in range(6):
            time.sleep(1.5)
            r = session.get(f"{site}/cart/async_shipping_rates.json?{q}", headers=HEADERS, timeout=20)
            if r.status_code == 200 and r.json():
                data = r.json()
                break
    rates = [(x.get("name"), float(x["price"])) for x in (data or {}).get("shipping_rates", [])
             if x.get("price") not in (None, "")]
    return _cheapest(rates), variant.get("price", 0) / 100 if isinstance(variant.get("price"), int) else None


def probe_woo(session, site):
    base = f"{site}/wp-json/wc/store/v1"
    items = session.get(f"{base}/products?search=pokemon&per_page=40", headers=HEADERS, timeout=20).json()
    cand = []
    for p in items if isinstance(items, list) else []:
        pr = p.get("prices") or {}
        try:
            price = int(pr.get("price")) / 10 ** int(pr.get("currency_minor_unit", 2))
        except (TypeError, ValueError):
            continue
        if p.get("is_purchasable") and p.get("is_in_stock") and 8 <= price <= 40:
            cand.append((price, p["id"]))
    if not cand:
        return None
    price, pid = sorted(cand)[0]
    r = session.get(f"{base}/cart", headers=HEADERS, timeout=20)
    h = dict(HEADERS)
    for k in ("Nonce", "X-WC-Store-API-Nonce"):
        if r.headers.get(k):
            h["Nonce"] = r.headers[k]
    if r.headers.get("Cart-Token"):
        h["Cart-Token"] = r.headers["Cart-Token"]
    r = session.post(f"{base}/cart/add-item", json={"id": pid, "quantity": 1}, headers=h, timeout=20)
    r.raise_for_status()
    r = session.post(f"{base}/cart/update-customer",
                     json={"shipping_address": {**ADDRESS, "first_name": "Mario", "last_name": "Rossi",
                                                "address_1": "Via Roma 1"}}, headers=h, timeout=20)
    r.raise_for_status()
    rates = []
    for pkg in r.json().get("shipping_rates", []):
        for x in pkg.get("shipping_rates", []):
            try:
                rates.append((x.get("name"), int(x["price"]) / 10 ** int(x.get("currency_minor_unit", 2))))
            except (TypeError, ValueError, KeyError):
                continue
    return _cheapest(rates), price


def refresh(cfg, cache, offers, today, session_factory=None):
    """Aggiorna una volta a settimana il costo di spedizione di ogni negozio. Non solleva mai eccezioni."""
    cache = dict(cache or {})
    session_factory = session_factory or requests.Session
    for shop in cfg["negozi"]:
        name = shop["nome"]
        old = cache.get(name)
        if old and date.fromisoformat(old["data"]) > today - timedelta(days=REFRESH_DAYS):
            continue
        threshold = shop.get("gratis_da") or 1e9
        try:
            if shop["tipo"] == "shopify":
                cheap = sorted((o["prezzo"], o["url"]) for o in offers.values()
                               if o["negozio"] == name and o.get("comprabile") and o.get("prezzo")
                               and 8 <= o["prezzo"] < min(threshold, 60))
                res = probe_shopify(session_factory(), shop["sito"], cheap[0][1]) if cheap else None
            else:
                res = probe_woo(session_factory(), shop["sito"])
        except Exception as e:
            print(f"Spedizione {name}: non letta ({str(e)[:80]})")
            res = None
        if res and res[0] is not None:
            cache[name] = {"costo": round(res[0], 2), "data": today.isoformat(), "carrello": res[1]}
            print(f"Spedizione {name}: {res[0]:.2f} € (letta dal carrello)")
        else:  # non letto: si tiene il valore vecchio (se c'è) e si riprova domani
            retry = (today - timedelta(days=REFRESH_DAYS - 1)).isoformat()
            cache[name] = {**(old or {"costo": None}), "data": retry}
    return cache
