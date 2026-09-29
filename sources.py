"""Notizie verificate e scoperta automatica dei set nuovi.

Fonti lette una volta ogni 6 ore:
- pokemon.com/it (UFFICIALE): titoli tipo "L'espansione Megaevoluzione—Dominio Delta del GCC Pokémon
  arriva il 6 novembre 2026" → nome del set + data certa;
- Pokémon Millennium (sito italiano specializzato, feed RSS): annunci di espansioni;
- i titoli dei prodotti nei negozi ("Megaevoluzione Dominio Delta – Box 36 buste").

Regola anti-bufale: un set nuovo viene seguito solo se è confermato da
  la fonte ufficiale, OPPURE Pokémon Millennium + almeno un negozio, OPPURE almeno 3 negozi diversi.
Le voci con meno conferme restano "candidati" e non generano avvisi.
"""
import html
import re
import unicodedata
from datetime import date, datetime, timedelta

import requests

OFFICIAL = ("pokemon.com", "https://www.pokemon.com/it/novita")
FEEDS = [("Pokémon Millennium", "https://www.pokemonmillennium.net/feed/")]
REFRESH = timedelta(hours=6)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; robot-pokemon/3)", "Accept-Language": "it-IT,it;q=0.9"}

ERA_PREFIXES = ["megaevoluzione"]  # quando cambierà era basta aggiungere il nuovo prefisso
STOP = {"gcc", "pokemon", "pokémon", "set", "carte", "italiano", "italiana", "ita", "it", "espansione",
        "nuova", "del", "della", "il", "la", "le", "di", "e", "gioco", "collezionabili", "carte collezionabili"}
TYPE_WORDS = r"(box|display|set allenatore|allenatore|bundle|blister|collezione|busta|buste|bustine|mini tin|tin|" \
             r"pacchetto|starter|kit|pack|presentazione|evoluzione|mega|destino|lotto|" \
             r"confezione|etb|elite|booster|del gcc|gcc|ita|it|pre ?ordine|preordine|prevendita|arriva|in arrivo|" \
             r"uscita|scopri|tutti|tutte|annunciata|annunciato|carte|mazzo|mazzi|lotta|sfida)"
MONTHS = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
          "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}


def _plain(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    s = re.sub(r"[\u2010-\u2015|:_/]", " ", s)  # trattini lunghi e separatori: non devono unire le parole
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", s).split())


def extract_set_names(text):
    """Nomi di set dopo il prefisso dell'era: "Megaevoluzione Dominio Delta – Box" → ["dominio delta"]."""
    t = f" {_plain(text)} "
    out = []
    for p in ERA_PREFIXES:
        for m in re.finditer(rf" {p} ((?:[a-z']+ ){{1,4}}?)(?={TYPE_WORDS} |\d|$)", t):
            words = [w for w in m.group(1).split() if w]
            while words and words[-1] in STOP:
                words.pop()
            while words and words[0] in STOP:
                words.pop(0)
            if not words or re.fullmatch(TYPE_WORDS, words[0]):
                continue  # "Mega Lucario ex", "Pacchetto starter"...: non sono set
            if 1 <= len(words) <= 4 and not all(w in STOP for w in words) and len(" ".join(words)) >= 5:
                out.append(" ".join(words))
    return out


def extract_date(text, today):
    """ "arriva il 6 novembre 2026" → date(2026, 11, 6)."""
    t = _plain(text)
    m = re.search(r"\b(\d{1,2}) (" + "|".join(MONTHS) + r") (\d{4})\b", t)
    if not m:
        return None
    try:
        d = date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None
    return d if today - timedelta(days=60) <= d <= today + timedelta(days=500) else None


def _get(session, url):
    r = session.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def fetch_official(session, today):
    """Titoli e link dalla pagina novità di pokemon.com/it."""
    page = _get(session, OFFICIAL[1])
    items = []
    for m in re.finditer(r'href="(/it/novita/[^"#?]+)"', page):
        slug = m.group(1)
        title = slug.rsplit("/", 1)[-1].replace("-", " ")
        if "gcc" not in title and "espansione" not in title:
            continue
        items.append({"fonte": OFFICIAL[0], "titolo": title, "url": "https://www.pokemon.com" + slug,
                      "ufficiale": True})
    return items


def fetch_feed(session, name, url):
    xml = _get(session, url)
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        block = m.group(1)
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        link = re.search(r"<link>(.*?)</link>", block, re.S)
        href = link.group(1).strip() if link else ""
        if title:
            items.append({"fonte": name, "titolo": html.unescape(title.group(1).strip()),
                          "url": href if href.startswith(("https://", "http://")) else "", "ufficiale": False})
    return items


def refresh_news(cache, now, session=None):
    """Aggiorna al massimo ogni 6 ore l'elenco delle notizie GCC. Non solleva mai eccezioni."""
    cache = dict(cache or {})
    last = cache.get("aggiornato")
    if last and now - datetime.fromisoformat(last) < REFRESH:
        return cache
    session = session or requests.Session()
    items, ok = [], 0
    try:
        items += fetch_official(session, now.date())
        ok += 1
    except Exception as e:
        print("pokemon.com/it non raggiungibile:", e)
    for name, url in FEEDS:
        try:
            items += [i for i in fetch_feed(session, name, url)
                      if re.search(r"gcc|espansion|carte|megaevoluzione", _plain(i["titolo"]))]
            ok += 1
        except Exception as e:
            print(f"{name} non raggiungibile:", e)
    if ok:
        seen = {i["url"] for i in cache.get("notizie", [])}
        fresh = [dict(i, visto=now.date().isoformat()) for i in items if i["url"] not in seen]
        cache["notizie"] = (fresh + cache.get("notizie", []))[:60]
        cache["aggiornato"] = now.isoformat(timespec="minutes")
    return cache


def known(name, sets):
    n = _plain(name)
    return any(n == _plain(s["cerca"]) or n == _plain(s["nome"]) or n in _plain(s["cerca"])
               or _plain(s["cerca"]) in n for s in sets)


def _cand(cand, name):
    c = cand.setdefault(name, {"fonti": [], "date": {}, "futuro": False, "notizie": []})
    if isinstance(c.get("date"), list):  # vecchio formato
        c["date"] = {}
    return c


def update_candidates(cand, sets, news, shop_titles, today):
    """Raccoglie le conferme per ogni nome di set non ancora seguito.
    shop_titles: [(negozio, titolo, info)] dove info ha stato/uscita del prodotto."""
    for n in news:
        for name in extract_set_names(n["titolo"]):
            if known(name, sets):
                continue
            c = _cand(cand, name)
            if n["fonte"] not in c["fonti"]:
                c["fonti"].append(n["fonte"])
            d = extract_date(n["titolo"], today) if re.search(r"arriv|uscit|disponibil", _plain(n["titolo"])) else None
            if d:
                c["date"]["ufficiale" if n.get("ufficiale") else n["fonte"]] = d.isoformat()
                c["futuro"] = c["futuro"] or d >= today - timedelta(days=30)
            if n["url"] not in c["notizie"]:
                c["notizie"] = (c["notizie"] + [n["url"]])[-5:]
    for name in [k for k in cand if known(k, sets)]:
        del cand[name]  # nel frattempo è diventato un set seguito
    for shop, title, info in shop_titles:
        for name in extract_set_names(title):
            if known(name, sets):
                continue
            c = _cand(cand, name)
            if shop not in c["fonti"]:
                c["fonti"].append(shop)
            if info.get("uscita"):
                c["date"][shop] = info["uscita"]
            if info.get("stato") in ("preordine", "in_arrivo") or (
                    info.get("uscita") and info["uscita"] >= today.isoformat()):
                c["futuro"] = True
    return cand


def confirmed(c, shop_names):
    shops = [f for f in c["fonti"] if f in shop_names]
    news = [f for f in c["fonti"] if f not in shop_names]
    if OFFICIAL[0] in c["fonti"]:
        return True
    return (bool(news) and len(shops) >= 1) or len(shops) >= 3


def best_date(c):
    """Data ufficiale se c'è, altrimenti la più citata dai negozi. (data, certa)"""
    if c["date"].get("ufficiale"):
        return c["date"]["ufficiale"], True
    others = list(c["date"].values())
    if not others:
        return None, False
    return max(set(others), key=others.count), False


def official_dates(news, sets, today):
    """Date ufficiali per i set già seguiti (per confermarle o correggerle)."""
    out = {}
    for n in news:
        if not n.get("ufficiale"):
            continue
        d = extract_date(n["titolo"], today) if re.search(r"arriv|uscit", _plain(n["titolo"])) else None
        if not d:
            continue
        for name in extract_set_names(n["titolo"]):
            for s in sets:
                if _plain(name) == _plain(s["cerca"]) or _plain(name) == _plain(s["nome"]):
                    out[s["nome"]] = d.isoformat()
    return out
