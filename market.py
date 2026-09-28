"""Valore delle carte di un set, dai prezzi Cardmarket pubblicati gratis da TCGdex.

Per ogni set seguito (con il nome inglese in config.json) scarica tutte le carte, le divide
per rarità e calcola il valore medio delle carte che trovi in una busta:
    somma su ogni rarità di  (quante carte di quella rarità escono per busta)
                           × (prezzo medio delle carte di quella rarità)
Le probabilità di uscita non sono ufficiali: sono stime della community (vedi config.json).
Le buste rovesce (reverse holo) non sono contate, quindi il valore è un po' prudente.
I dati si aggiornano una volta al giorno e restano salvati nello stato.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

API = "https://api.tcgdex.net/v2/en"
REFRESH = timedelta(hours=20)
TIME_BUDGET = 240  # secondi massimi per aggiornare tutti i set

# carte per busta, stime della community per le espansioni Megaevoluzione (fonte: ThePriceDex)
DEFAULT_PULL = {"C": 4, "U": 3, "R": 1 / 1.4, "DR": 1 / 5, "IR": 1 / 9, "UR": 1 / 12,
                "SIR": 1 / 101, "HR": 1 / 1260}
NAMES = {"C": "Comune", "U": "Non comune", "R": "Rara", "DR": "Doppia rara", "IR": "Illustrazione rara",
         "UR": "Ultra rara", "SIR": "Illustrazione speciale", "HR": "Iper rara", "ACE": "Asso Tattico"}


def _norm(s):
    return " ".join(str(s or "").lower().replace("-", " ").split())


def bucket(rarity):
    r = _norm(rarity)
    if not r:
        return None
    if "special illustration" in r:
        return "SIR"
    if "hyper" in r:
        return "HR"
    if "illustration" in r:
        return "IR"
    if "ultra" in r:
        return "UR"
    if "double" in r:
        return "DR"
    if "ace" in r:
        return "ACE"
    if r in ("rare", "rare holo", "holo rare"):
        return "R"
    if "uncommon" in r:
        return "U"
    if "common" in r:
        return "C"
    return None


def card_price(card):
    cm = ((card or {}).get("pricing") or {}).get("cardmarket") or {}
    for k in ("trend", "avg", "trend-holo", "avg-holo", "avg30"):
        v = cm.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _get(session, url):
    r = session.get(url, timeout=20, headers={"User-Agent": "robot-pokemon/3"})
    r.raise_for_status()
    return r.json()


def set_value(session, set_id, pull, deadline):
    info = _get(session, f"{API}/sets/{set_id}")
    ids = [c["id"] for c in info.get("cards", []) if c.get("id")]

    def one(cid):
        if time.time() > deadline:
            return None
        try:
            return _get(session, f"{API}/cards/{cid}")
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        cards = [c for c in ex.map(one, ids) if c]
    if len(cards) < max(10, len(ids) * 0.8):
        return None  # troppo pochi dati: meglio niente che un numero sbagliato

    by_bucket = {}
    top = []
    for c in cards:
        p = card_price(c)
        b = bucket(c.get("rarity"))
        if p is None or b is None:
            continue
        by_bucket.setdefault(b, []).append(p)
        top.append((p, c.get("name", ""), b))
    if not any(b in by_bucket for b in ("IR", "SIR", "UR", "DR")):
        return None  # set non ancora uscito o senza prezzi

    ev = sum(pull.get(b, 0) * (sum(ps) / len(ps)) for b, ps in by_bucket.items())
    top.sort(reverse=True)
    return {"ev_busta": round(ev, 2),
            "top": [{"nome": n, "prezzo": round(p, 2), "rarita": NAMES.get(b, b)} for p, n, b in top[:5]],
            "carte": len(cards)}


def refresh(cfg, cache, now, session=None):
    """Aggiorna cache {nome set: valori} per i set che hanno 'en'. Non solleva mai eccezioni."""
    cache = dict(cache or {})
    todo = []
    for s in cfg["set"]:
        if not s.get("en") or (s.get("speciale") and not s.get("pull")):
            continue  # i set speciali hanno buste diverse: senza probabilità dedicate non stimiamo
        old = cache.get(s["nome"])
        if old and now - datetime.fromisoformat(old["aggiornato"]) < REFRESH:
            continue
        todo.append(s)
    if not todo:
        return cache
    session = session or requests.Session()
    deadline = time.time() + TIME_BUDGET
    try:
        sets = _get(session, f"{API}/sets")
        if not isinstance(sets, list):
            raise ValueError("risposta inattesa")
    except Exception as e:
        print("TCGdex non raggiungibile:", e)
        return cache
    for s in todo:
        match = next((x for x in sets if _norm(x.get("name")) == _norm(s["en"])), None)
        if not match:
            print(f"TCGdex: set '{s['en']}' non trovato")
            continue
        try:
            v = set_value(session, match["id"], {**DEFAULT_PULL, **(s.get("pull") or {})}, deadline)
        except Exception as e:
            print(f"TCGdex: errore su {s['en']}: {e}")
            v = None
        if v:
            v["aggiornato"] = now.isoformat(timespec="minutes")
            v["stima_generica"] = not s.get("pull")
            cache[s["nome"]] = v
            print(f"TCGdex: {s['nome']} → {v['ev_busta']} €/busta su {v['carte']} carte")
        elif not (cache.get(s["nome"]) or {}).get("ev_busta"):
            # set senza prezzi (non ancora uscito): riproviamo domani, non a ogni giro
            cache[s["nome"]] = {"aggiornato": now.isoformat(timespec="minutes")}
    return cache


def values(cache):
    """Solo i set con un valore calcolato."""
    return {k: v for k, v in (cache or {}).items() if v.get("ev_busta")}
