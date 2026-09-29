"""Cruscotto del mattino: una pagina HTML (pubblicata su GitHub Pages) e la sua immagine per Telegram.

Stessi dati, due formati:
- mode="image": colonna stretta da telefono, liste brevi, senza link → foto nel messaggio del mattino;
- mode="page": pagina completa con tutti i link, aggiornata a ogni giro.
L'immagine si ottiene fotografando la pagina con Chrome (già installato sui server di GitHub).
Niente emoji nella grafica: sui server mancano i font a colori, si usano forme e testo.
"""
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CSS = """
:root{--bg:#0f1419;--card:#18212b;--line:#26323f;--ink:#f2f4f6;--ink2:#b7c0ca;--ink3:#7d8a97;
--blue:#3987e5;--orange:#d95926;--green:#2fa36b;--yellow:#c98500;--red:#e66767;--gray:#5b6773}
*{box-sizing:border-box;margin:0;padding:0}
body{overflow-x:hidden;background:var(--bg);color:var(--ink);font:15px/1.45 Inter,"DejaVu Sans",Arial,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:560px;margin:0 auto;padding:18px 16px 26px}
a{color:inherit;text-decoration:none}
.page a.lk{color:#8cc2ff}
header{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:14px}
h1{font-size:22px;font-weight:800;letter-spacing:.2px}
h1 small{display:block;font-size:12px;font-weight:600;color:var(--ink3);letter-spacing:1.4px;text-transform:uppercase}
.meta{font-size:12px;color:var(--ink3);text-align:right}
.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:14px}
.kpi{background:var(--card);border-radius:12px;padding:10px 12px}
.kpi b{display:block;font-size:22px;font-weight:800;line-height:1.1}
.kpi span{font-size:11px;color:var(--ink3);text-transform:uppercase;letter-spacing:.6px}
.kpi i{display:block;font-style:normal;font-size:11.5px;color:var(--ink2);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
section{background:var(--card);border-radius:14px;padding:12px 14px;margin-bottom:12px}
h2{font-size:12px;font-weight:800;letter-spacing:1.3px;text-transform:uppercase;color:var(--ink2);
margin-bottom:8px;display:flex;align-items:center;gap:8px}
h2 .dot{width:8px;height:8px;border-radius:50%}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.l{min-width:0}.l b{font-weight:700}.l .sub{font-size:12px;color:var(--ink3)}
.r{text-align:right;white-space:nowrap}.r b{font-size:16px;font-weight:800}.r .sub{font-size:12px;color:var(--ink3)}
.chip{display:inline-block;font-size:10.5px;font-weight:800;letter-spacing:.6px;text-transform:uppercase;
border-radius:6px;padding:2px 6px;margin-left:4px;vertical-align:2px}
.alta{background:rgba(230,103,103,.18);color:#ff9b9b}.media{background:rgba(201,133,0,.2);color:#f0b94a}
.bassa{background:rgba(91,103,115,.25);color:var(--ink2)}.pre{background:rgba(217,89,38,.2);color:#ff9a6e}
.ok{background:rgba(47,163,107,.2);color:#6fd8a2}.est{background:rgba(91,103,115,.25);color:var(--ink2)}
.focus .l{flex:1}.meter{height:6px;border-radius:3px;background:var(--line);margin-top:6px;overflow:hidden}
.meter i{display:block;height:100%;border-radius:3px}
.focus .why{font-size:12px;color:var(--ink2);margin-top:3px}
.date{flex:0 0 52px;text-align:center;background:#101820;border-radius:10px;padding:4px 0}
.date b{display:block;font-size:17px;font-weight:800;line-height:1.1}.date span{font-size:10px;color:var(--ink3);text-transform:uppercase}
.cal .row{justify-content:flex-start}
.bars .bar{display:grid;grid-template-columns:118px 1fr 44px;align-items:center;gap:8px;padding:5px 0}
.bars .track{position:relative;height:12px;background:#101820;border-radius:4px}
.bars .fill{position:absolute;left:0;top:0;bottom:0;background:var(--blue);border-radius:0 4px 4px 0}
.bars .ref{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink3)}
.bars .nm{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bars .v{font-size:13px;font-weight:700;text-align:right}
.note{font-size:11.5px;color:var(--ink3);margin-top:6px}
.news .row{display:block}.news .sub{font-size:11.5px;color:var(--ink3)}
footer{font-size:11.5px;color:var(--ink3);text-align:center;margin-top:6px}
"""


def e(s):
    return html.escape(str(s))


LEVEL_TEXT = {"alta": "interesse alto", "media": "interesse medio", "bassa": "interesse basso"}


def _chip(level):
    return f'<span class="chip {level}">{LEVEL_TEXT.get(level, level)}</span>'


def _meter(score):
    color = "#e66767" if score >= 65 else ("#c98500" if score >= 40 else "#5b6773")
    return f'<div class="meter"><i style="width:{score}%;background:{color}"></i></div>'


def _a(url, inner, page):
    return f'<a class="lk" href="{e(url)}">{inner}</a>' if page and url else inner


def render(d, mode="page"):
    page = mode == "page"
    lim = (lambda n: None) if page else (lambda n: n)
    out = ['<!doctype html><html lang="it"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>Cruscotto Pokémon</title>'
           + ('<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" '
              'rel="stylesheet">' if page else "") +
           f'<style>{CSS}</style></head><body class="{mode}"><div class="wrap">']
    out.append(f'<header><h1><small>Pokémon · mercato</small>{e(d["data"])}</h1>'
               f'<div class="meta">{d["negozi"]} negozi<br>ore {e(d["ora"])}</div></header>')

    out.append('<div class="kpis">')
    for k in d["kpi"]:
        out.append(f'<div class="kpi"><span>{e(k["label"])}</span><b>{e(k["value"])}</b>'
                   f'{"<i>" + e(k["sub"]) + "</i>" if k.get("sub") else ""}</div>')
    out.append('</div>')

    if d["focus"]:
        out.append('<section class="focus"><h2><span class="dot" style="background:#e66767"></span>In primo piano</h2>')
        for f in d["focus"][:lim(3)]:
            verdict = ""
            if f.get("giudizio"):
                cls, txt = {"sotto": ("ok", f'{f["scost"]}% sul listino'),
                            "allineato": ("bassa", "a listino"),
                            "accettabile": ("media", f'+{f["scost"]}% accettabile'),
                            "gonfiato": ("alta", f'+{f["scost"]}% gonfiato')}[f["giudizio"]]
                verdict = f'<div class="sub" style="margin-top:4px"><span class="chip {cls}" style="margin-left:0">{e(txt)}</span>' + \
                    (f' obiettivo {e(f["obiettivo"])}' if f.get("obiettivo") else "") + '</div>'
            out.append(f'<div class="row"><div class="l"><b>{e(f["label"])}</b>{_chip(f["level"])}'
                       f'<div class="sub">{e(f["set"])} · {e(f["stato"])}</div>'
                       f'<div class="why">{e(f["why"])}</div>{verdict}{_meter(f["score"])}</div>'
                       f'<div class="r">{_a(f.get("url"), "<b>" + e(f["prezzo"]) + "</b>", page)}'
                       f'<div class="sub">{e(f["dove"])}</div></div></div>')
        out.append('</section>')

    if d["uscite"]:
        out.append('<section class="cal"><h2><span class="dot" style="background:#3987e5"></span>Prossime uscite</h2>')
        for u in d["uscite"][:lim(4)]:
            chips = (' <span class="chip est">stima</span>' if u["stima"] else "") + \
                    (f' <span class="chip pre">{u["n_pre"]} in preordine</span>' if u["n_pre"] else "")
            out.append(f'<div class="row"><div class="date"><b>{e(u["giorno"])}</b><span>{e(u["mese"])}</span></div>'
                       f'<div class="l"><b>{e(u["set"])}</b>{chips}<div class="sub">{e(u["cosa"])} · '
                       f'{e(u["tra"])}</div></div></div>')
        out.append('</section>')

    if d["preordini"]:
        out.append('<section><h2><span class="dot" style="background:#d95926"></span>Preordini aperti</h2>')
        for p in d["preordini"][:lim(4)]:
            warn = f' <span class="chip {p["giudizio"][0]}">{e(p["giudizio"][1])}</span>' if p.get("giudizio") else ""
            out.append(f'<div class="row"><div class="l"><b>{e(p["label"])}</b>{_chip(p["level"])}'
                       f'<div class="sub">{e(p["set"])} · {p["n"]} {"negozio" if p["n"] == 1 else "negozi"}</div></div>'
                       f'<div class="r">{_a(p["url"], "<b>" + e(p["prezzo"]) + "</b>", page)}{warn}'
                       f'<div class="sub">{e(p["negozio"])}</div></div></div>')
        if not page and len(d["preordini"]) > 4:
            out.append(f'<div class="note">+ altri {len(d["preordini"]) - 4} nel cruscotto completo</div>')
        out.append('</section>')

    out.append('<section><h2><span class="dot" style="background:#2fa36b"></span>Migliori occasioni</h2>')
    if d["occasioni"]:
        for o in d["occasioni"][:lim(4)]:
            tags = "".join(f' <span class="chip {c}">{e(t)}</span>' for c, t in o["tags"])
            out.append(f'<div class="row"><div class="l"><b>{e(o["label"])}</b>{tags}'
                       f'<div class="sub">{e(o["set"])}{" · " + e(o["busta"]) if o["busta"] else ""}</div></div>'
                       f'<div class="r">{_a(o["url"], "<b>" + e(o["prezzo"]) + "</b>", page)}'
                       f'<div class="sub">{e(o["negozio"])}</div></div></div>')
        if len(d["occasioni"]) > 4 and not page:
            out.append(f'<div class="note">+ altre {len(d["occasioni"]) - 4} nel cruscotto completo</div>')
    else:
        out.append('<div class="sub">Oggi niente al prezzo giusto.</div>')
    out.append('</section>')

    if d["mercato"]:
        out.append('<section class="bars"><h2><span class="dot" style="background:#3987e5"></span>'
                   'Quanto rende un box in carte</h2>')
        scale = max([1.0] + [m["rende"] for m in d["mercato"] if m["rende"]]) * 1.05
        for m in d["mercato"]:
            if m["rende"] is None:
                continue
            w = m["rende"] / scale * 100
            out.append(f'<div class="bar"><div class="nm">{e(m["set"])}</div>'
                       f'<div class="track"><div class="fill" style="width:{w:.1f}%"></div>'
                       f'<div class="ref" style="left:{100 / scale:.1f}%"></div></div>'
                       f'<div class="v">{round(m["rende"] * 100)}%</div></div>')
        out.append('<div class="note">Valore medio delle carte (Cardmarket) sul prezzo del box più economico. '
                   'Linea = 100%: oltre, aprire ripaga. Stima prudente.</div>')
        tops = [m for m in d["mercato"] if m.get("top")]
        if tops and page:
            out.append('<div class="note" style="margin-top:8px">Carte top: ' + " · ".join(
                f'{e(m["set"])}: {e(m["top"][0][0])} {e(m["top"][0][1])}' for m in tops[:lim(4)]) + '</div>')
        out.append('</section>')

    if d["notizie"]:
        out.append('<section class="news"><h2><span class="dot" style="background:#b7c0ca"></span>Notizie verificate</h2>')
        for n in d["notizie"][:lim(1) if not page else 8]:
            out.append(f'<div class="row">{_a(n["url"], "<b>" + e(n["titolo"]) + "</b>", page)}'
                       f'<div class="sub">{e(n["fonte"])}{" · ufficiale" if n["ufficiale"] else ""}</div></div>')
        out.append('</section>')

    if page and (d.get("archiviati") or d.get("seguiti")):
        out.append('<section><h2><span class="dot" style="background:#5b6773"></span>Set seguiti</h2>'
                   f'<div class="sub">{e(", ".join(d.get("seguiti", [])))}</div>')
        if d.get("archiviati"):
            out.append(f'<div class="note">Archiviati (mercato raffreddato, solo riepilogo): '
                       f'{e(", ".join(d["archiviati"]))}</div>')
        out.append('</section>')

    out.append(f'<footer>Robot Pokémon · {"aggiornato ogni ora" if page else "cruscotto completo col pulsante qui sotto"}'
               f'</footer></div></body></html>')
    return "".join(out)


def find_chrome():
    for c in (os.environ.get("CHROME_BIN"), "google-chrome", "google-chrome-stable", "chromium",
              "chromium-browser"):
        if c and (shutil.which(c) or Path(c).exists()):
            return shutil.which(c) or c
    return None


def screenshot(html_text, png_path, width=540):
    """Foto della pagina in modalità immagine. Restituisce (riuscita, motivo)."""
    chrome = find_chrome()
    if not chrome:
        return False, "Chrome non trovato sul server"
    png = Path(png_path)
    png.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "c.html"
        src.write_text(html_text, encoding="utf-8")
        cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
               "--hide-scrollbars", "--no-first-run", f"--user-data-dir={tmp}/profilo",
               "--force-device-scale-factor=2", f"--window-size={width},3200", "--virtual-time-budget=3000",
               f"--screenshot={png}", src.as_uri()]
        try:
            r = subprocess.run(cmd, timeout=120, capture_output=True, text=True)
        except Exception as ex:
            return False, f"Chrome bloccato: {ex}"
        if not png.exists() or png.stat().st_size < 1000:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            return False, f"Chrome ({Path(chrome).name}) non ha creato l'immagine, codice {r.returncode}: " + \
                " | ".join(err[-3:])[:300]
    try:
        from PIL import Image
        im = Image.open(png).convert("RGB")
        bg = im.getpixel((2, im.height - 2))
        px = im.load()
        bottom = im.height - 1
        while bottom > 0 and all(px[x, bottom] == bg for x in range(0, im.width, 7)):
            bottom -= 1
        im.crop((0, 0, im.width, min(im.height, bottom + 24))).save(png, optimize=True)
    except Exception as ex:
        print("Ritaglio immagine non riuscito (la mando intera):", ex)
    return True, f"ok ({Path(chrome).name}, {png.stat().st_size // 1024} KB)"
