"""Indice di interesse (0-100): quanto un prodotto è "da collezionista" e quanto sta andando a ruba.

Ragiona come un collezionista con segnali misurabili:
- tipo di prodotto: Set Allenatore di un set speciale, Ultra Premium, esclusive Pokémon Center
  partono alti; blister e buste sfuse bassi;
- scarsità: quanti negozi che lo hanno in catalogo sono esauriti o non ancora aperti;
- ricarico: quanto i negozi lo vendono sopra listino (+95% sul Set Allenatore del 30° = va a ruba);
- velocità: quanto è calata la disponibilità negli ultimi 7 giorni (dallo storico giornaliero).
Più l'indice è alto, più il robot insiste: preordini "PRIORITÀ", avvisi anche sopra listino,
prima posizione nel riepilogo.
"""
from datetime import date, timedelta

HIGH, MEDIUM = 65, 40
PRIOR_TYPE = {"Ultra Premium": 25, "Set Allenatore": 20, "Box 36 buste": 15, "Collezione Premium": 12,
              "Bundle 6 buste": 8, "Tin": 6, "Collezione": 5, "Mini Tin": 4, "Blister": 3}
HISTORY_DAYS = 60


def level(score):
    return "alta" if score >= HIGH else ("media" if score >= MEDIUM else "bassa")


def snapshot(group, soglia):
    """Fotografia del prodotto oggi: negozi in catalogo, disponibili, prezzo mediano, ricarico."""
    listed = len({o["negozio"] for o in group})
    avail = [o for o in group if o["stato"] in ("disponibile", "preordine") and o.get("prezzo")]
    prices = sorted(o["prezzo"] for o in (avail or [o for o in group if o.get("prezzo")]))
    med = prices[len(prices) // 2] if prices else None
    return {"tot": listed, "disp": len({o["negozio"] for o in avail}),
            "med": med, "ricarico": round(med / soglia - 1, 3) if med and soglia else None}


def record(history, key, snap, today):
    """Aggiunge/aggiorna la riga di oggi nello storico di un prodotto (una riga al giorno)."""
    rows = history.setdefault(key, [])
    row = {"d": today.isoformat(), **snap}
    if rows and rows[-1]["d"] == row["d"]:
        rows[-1] = row
    else:
        rows.append(row)
    cutoff = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    history[key] = [r for r in rows if r["d"] >= cutoff]


def score(o, group, soglia, collector_flags, history_rows, today):
    """(punteggio 0-100, motivi) per il prodotto rappresentato da `group` (offerte di tutti i negozi)."""
    special, center = collector_flags
    reasons = []
    s = PRIOR_TYPE.get(o["tipo"], 5)
    if special:
        s += 20
        reasons.append("set da collezione")
    if center:
        s += 20
        reasons.append("esclusiva")
    snap = snapshot(group, soglia)
    if snap["tot"] >= 3:
        gone = 1 - snap["disp"] / snap["tot"]
        s += 25 * gone
        if gone >= 0.6:
            reasons.append(f"non disponibile in {snap['tot'] - snap['disp']} negozi su {snap['tot']}")
    if snap["ricarico"] is not None and snap["ricarico"] > 0.05:
        s += 25 * min(1.0, snap["ricarico"])
        reasons.append(f"venduto a +{round(snap['ricarico'] * 100)}% sul listino")
    week_ago = (today - timedelta(days=7)).isoformat()
    old = next((r for r in history_rows or [] if r["d"] >= week_ago), None)
    if old and old["tot"] and snap["tot"]:
        drop = old["disp"] / old["tot"] - snap["disp"] / snap["tot"]
        if drop > 0.1:
            s += 10 * min(1.0, drop * 2)
            reasons.append("disponibilità in calo questa settimana")
    return max(0, min(100, round(s))), reasons


def set_snapshot(groups_of_set, soglia_of, card_value):
    """Fotografia di un set: quota di prodotti comprabili a listino, ricarico mediano, valore carte top."""
    ok, markups = 0, []
    for g in groups_of_set:
        sg = soglia_of(g[0])
        avail = [o for o in g if o["stato"] == "disponibile" and o.get("prezzo")]
        if sg and any(o["prezzo"] <= sg for o in avail):
            ok += 1
        if sg and avail:
            markups.append(min(o["prezzo"] for o in avail) / sg - 1)
    markups.sort()
    return {"ok": round(ok / len(groups_of_set), 3) if groups_of_set else 0,
            "ricarico": round(markups[len(markups) // 2], 3) if markups else None,
            "carte": card_value}


def cooling(rows, today, days=21):
    """Il set si è raffreddato? Tutti gli ultimi `days` giorni: >=60% dei prodotti a listino,
    ricarico <=5%, e (se abbiamo i prezzi) carte top in calo di almeno il 5%."""
    since = (today - timedelta(days=days)).isoformat()
    window = [r for r in rows if r["d"] >= since]
    if len(window) < days * 0.8 or not rows or rows[0]["d"] > since:
        return False  # storico troppo corto
    if not all(r["ok"] >= 0.6 and (r["ricarico"] is None or r["ricarico"] <= 0.05) for r in window):
        return False
    cards = [r["carte"] for r in window if r.get("carte")]
    if len(cards) >= 2:
        return cards[-1] <= cards[0] * 0.95
    return True


def heating(row):
    """Segnali che un set archiviato è tornato caldo."""
    return row["ok"] < 0.4 or (row["ricarico"] is not None and row["ricarico"] > 0.2)


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days
