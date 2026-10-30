#!/usr/bin/env python3
"""
Rapport surf à partir de Windguru (vent, houle, météo) + marées (Open-Meteo).

    python windguru_surf.py                       # rapport réel -> rapport.html
    python windguru_surf.py --out docs/index.html # pour GitHub Pages
    python windguru_surf.py --demo                # aperçu avec données fictives
    python windguru_surf.py --debug               # détail des modèles reçus
"""
import argparse
import html
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

# =============================================================== RÉGLAGES DU SPOT
SPOT = {
    "id": 48481,
    "name": "Sainte-Barbe",
    "lat": 47.588, "lon": -3.165,     # point en mer devant la plage (marée, eau)
    "facing": 250,                    # direction vers le large (°)
    "swell_window": (220, 310),       # directions de houle qui rentrent bien (°)
    "min_period": 8, "ideal_period": 12,
    "height": (0.8, 2.5),             # m
    "max_wind_kn": 22,
    "hours": (7, 20),                 # heures affichées (heure locale)
    "tz": "Europe/Paris",
}
BREST = (48.30, -4.80)                # référence officielle des coefficients
U_BREST = 6.10                        # marnage (m) correspondant au coef 100

WG_API = "https://www.windguru.cz/int/iapi.php"
MARINE_API = "https://marine-api.open-meteo.com/v1/marine"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.windguru.cz/",
}

# =============================================================== WINDGURU


def wg_get(params):
    r = requests.get(WG_API, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    d = r.json()
    if isinstance(d, dict) and d.get("return") == "error":
        raise RuntimeError(d.get("message", d))
    return d


def find_model_ids(obj, found=None):
    found = [] if found is None else found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "id_model" and str(v).isdigit():
                if int(v) not in found:
                    found.append(int(v))
            else:
                find_model_ids(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_model_ids(v, found)
    return found


def fetch_windguru(spot_id, debug=False):
    models = find_model_ids(wg_get({"q": "forecast_spot", "id_spot": spot_id}))
    if debug:
        print(f"Modèles Windguru : {models}")
    out = {}
    for m in models:
        try:
            d = wg_get({"q": "forecast", "id_model": m, "id_spot": spot_id})
            fc = d.get("fcst", d)
            if "hours" in fc:
                out[m] = fc
                if debug:
                    print(f"  {m}: {len(fc['hours'])} pas, "
                          f"{[k for k, v in fc.items() if isinstance(v, list)]}")
        except Exception as e:
            if debug:
                print(f"  {m}: échec ({e})")
    return out


def to_series(fc):
    init, hrs = fc["initstamp"], fc["hours"]
    out = {}
    for i, h in enumerate(hrs):
        t = datetime.fromtimestamp(init + h * 3600, tz=timezone.utc)
        out[t] = {k: v[i] for k, v in fc.items()
                  if isinstance(v, list) and len(v) == len(hrs) and v[i] is not None}
    return out


WAVE_KEYS = ("HTSGW", "PERPW", "DIRPW", "SWELL", "SWPER", "SWDIR", "WV")


def merge(forecasts):
    """Vent/météo du premier modèle atmosphérique, houle du premier modèle de vagues."""
    merged = defaultdict(dict)
    wind_done = wave_done = False
    for fc in forecasts.values():
        for t, row in to_series(fc).items():
            for k, v in row.items():
                wave = k.startswith(WAVE_KEYS)
                if (wave and not wave_done) or (not wave and not wind_done):
                    merged[t].setdefault(k, v)
        wind_done |= "WINDSPD" in fc
        wave_done |= "HTSGW" in fc or "SWELL1" in fc
    return dict(sorted(merged.items()))

# =============================================================== MARÉES


def fetch_marine(lat, lon, extra=""):
    r = requests.get(MARINE_API, timeout=20, params={
        "latitude": lat, "longitude": lon, "timezone": "GMT", "forecast_days": 8, "past_days": 1,
        "hourly": "sea_level_height_msl" + extra})
    r.raise_for_status()
    h = r.json()["hourly"]
    times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h["time"]]
    return times, h


def extremes(times, levels):
    ex = []
    for i in range(1, len(levels) - 1):
        a, b, c = levels[i - 1], levels[i], levels[i + 1]
        if None in (a, b, c):
            continue
        kind = "PM" if b > a and b >= c else "BM" if b < a and b <= c else None
        if not kind:
            continue
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den else 0.0
        t_ex = times[i] + timedelta(hours=off)
        b = b - 0.25 * (a - c) * off
        if ex and ex[-1][2] == kind:      # deux extrêmes du même type : on garde le plus marqué
            if (kind == "PM") == (b > ex[-1][1]):
                ex[-1] = (t_ex, b, kind)
        else:
            ex.append((t_ex, b, kind))
    return ex


def tide_state(t, level, ex):
    prev = next((e for e in reversed(ex) if e[0] <= t), None)
    nxt = next((e for e in ex if e[0] > t), None)
    if not prev or not nxt or level is None:
        return None
    rising = nxt[2] == "PM"
    low, high = (prev[1], nxt[1]) if rising else (nxt[1], prev[1])
    f = min(1, max(0, (level - low) / (high - low))) if high > low else 0.5
    if f < 0.2:
        label = "basse mer"
    elif f > 0.8:
        label = "pleine mer"
    else:
        label = "mi-marée"
    return {"rising": rising, "f": f, "label": label}


def coefficients(times, levels):
    """Coefficient approximatif de chaque pleine mer, à partir du marnage à Brest."""
    ex = extremes(times, levels)
    out = []
    for i, (t, h, kind) in enumerate(ex):
        if kind != "PM":
            continue
        lows = [e[1] for e in (ex[i - 1] if i else None, ex[i + 1] if i + 1 < len(ex) else None)
                if e and e[2] == "BM"]
        if lows:
            out.append((t, round(100 * (h - sum(lows) / len(lows)) / U_BREST)))
    return out


def coef_at(t, coefs):
    """Coef de la pleine mer la plus proche de l’instant t."""
    if not coefs:
        return None
    pm, c = min(coefs, key=lambda x: abs((x[0] - t).total_seconds()))
    return c if abs((pm - t).total_seconds()) <= 7 * 3600 else None


def get_tides(s, tz, debug=False):
    try:
        times, h = fetch_marine(s["lat"], s["lon"], ",sea_surface_temperature")
        levels = h["sea_level_height_msl"]
        local = {t: (lv, sst) for t, lv, sst in zip(times, levels, h.get("sea_surface_temperature", [None] * len(times)))}
        ex = extremes(times, levels)
        bt, bh = fetch_marine(*BREST)
        coefs = coefficients(bt, bh["sea_level_height_msl"])
        return local, ex, coefs
    except Exception as e:
        if debug:
            print(f"Marées indisponibles : {e}")
        return {}, [], []

# =============================================================== NOTATION


def angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def wind_kind(wd, s=SPOT):
    rel = angle_diff(wd, s["facing"])
    return "offshore" if rel >= 135 else "side-shore" if rel >= 60 else "onshore"


def score_hour(r, tide, s=SPOT):
    h = r.get("SWELL1", r.get("HTSGW"))
    p = r.get("SWPER1", r.get("PERPW"))
    d = r.get("SWDIR1", r.get("DIRPW"))
    ws, wd = r.get("WINDSPD"), r.get("WINDDIR")
    if h is None or p is None or ws is None:
        return None
    sc = 0.0
    lo, hi = s["height"]                                   # hauteur : 2,5 pts
    if lo <= h <= hi:
        sc += 2.5
    elif h < lo:
        sc += 2.5 * max(0, (h / lo - 0.3) / 0.7)
    else:
        sc += max(0, 2.5 - (h - hi) * 1.7)
    if p >= s["ideal_period"]:                             # période : 2 pts
        sc += 2
    elif p >= s["min_period"]:
        sc += 0.8 + 1.2 * (p - s["min_period"]) / (s["ideal_period"] - s["min_period"])
    if d is not None:                                      # direction houle : 1,5 pt
        w0, w1 = s["swell_window"]
        if w0 <= d <= w1:
            sc += 1.5
        else:
            sc += max(0, 1.5 - min(angle_diff(d, w0), angle_diff(d, w1)) / 30)
    if wd is not None:                                     # vent : 2,5 pts
        k = wind_kind(wd, s)
        if ws <= 5:
            sc += 2.5
        elif k == "offshore":
            sc += 2.5 if ws <= 15 else 1.7
        elif k == "side-shore":
            sc += max(0, 1.7 - (ws - 5) / 8)
        else:
            sc += max(0, 0.8 - (ws - 5) / 6)
    if ws > s["max_wind_kn"]:
        sc -= 2
    if tide:                                               # marée : 1,5 pt
        sc += 1.0 * (1 - abs(tide["f"] - 0.5) * 2) + (0.5 if tide["rising"] else 0)
    else:                                                  # pas de marée : note ramenée sur 10
        sc *= 10 / 8.5
    return round(max(0, min(10, sc)), 1)


def verdict(sc):
    return "top" if sc >= 8.5 else "go" if sc >= 7 else "ok" if sc >= 5 else "nul"

# =============================================================== ASSEMBLAGE


def build_days(merged, tides, s=SPOT):
    tz = ZoneInfo(s["tz"])
    local, ex, coefs = tides
    days = defaultdict(list)
    for t, r in merged.items():
        lt = t.astimezone(tz)
        if not s["hours"][0] <= lt.hour <= s["hours"][1]:
            continue
        lv, sst = local.get(t.replace(minute=0, second=0), (None, None))
        tide = tide_state(t, lv, ex) if ex else None
        sc = score_hour(r, tide, s)
        if sc is None:
            continue
        days[lt.date()].append({"t": lt, "r": r, "tide": tide, "sst": sst,
                                "coef": coef_at(t, coefs), "score": sc, "v": verdict(sc)})
    tides_by_day = defaultdict(list)
    for t, h, kind in ex:
        lt = t.astimezone(tz)
        tides_by_day[lt.date()].append((lt, kind, coef_at(t, coefs) if kind == "PM" else None))
    return dict(sorted(days.items())), tides_by_day


def best_window(hours):
    """Plus longue suite d'heures au meilleur verdict du jour."""
    rank = {"top": 3, "go": 2, "ok": 1, "nul": 0}
    top = max(rank[h["v"]] for h in hours)
    if top == 0:
        return "nul", None
    best, cur = [], []
    for h in hours:
        if rank[h["v"]] == top:
            cur.append(h)
            if len(cur) > len(best):
                best = list(cur)
        else:
            cur = []
    v = {3: "top", 2: "go", 1: "ok"}[top]
    a, b = best[0]["t"], best[-1]["t"]
    return v, f"{a:%H}h" if a == b else f"{a:%H}h–{b:%H}h"

# =============================================================== RAPPORT HTML

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_C = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
          "sept.", "oct.", "nov.", "déc."]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

ICON = {
    "top": '<svg class="ic top" viewBox="0 0 40 40" aria-label="Top"><path d="M5 27c3.5 0 5-3 8.5-3s5 3 8.5 3 5-3 8.5-3 4 2 5 2.5"/><path d="M7 21c0-8 6-14 14-14 5 0 9 3 9 7-3-2-7-1.5-8 1.5-1.2 3.5 2 6 6 5.5"/></svg>',
    "go": '<svg class="ic go" viewBox="0 0 40 40" aria-label="GO"><path d="M5 27c3.5 0 5-3 8.5-3s5 3 8.5 3 5-3 8.5-3 4 2 5 2.5"/><path d="M7 21c0-8 6-14 14-14 5 0 9 3 9 7-3-2-7-1.5-8 1.5-1.2 3.5 2 6 6 5.5"/></svg>',
    "ok": '<svg class="ic ok" viewBox="0 0 40 40" aria-label="Correct"><path d="M5 27c3.5 0 5-3 8.5-3s5 3 8.5 3 5-3 8.5-3 4 2 5 2.5"/><path d="M7 21c0-8 6-14 14-14 5 0 9 3 9 7-3-2-7-1.5-8 1.5-1.2 3.5 2 6 6 5.5"/></svg>',
    "nul": '<svg class="ic nul" viewBox="0 0 40 40" aria-label="Nul"><path d="M11 11l18 18M29 11L11 29"/></svg>',
}
LABEL = {"top": "Top", "go": "GO", "ok": "Correct", "nul": "Nul"}

CSS = """
:root{--top:#7b3fd4;--bg:#eef4f6;--card:#ffffff;--ink:#0f2533;--muted:#5d7380;--line:#d3e0e6;
--go:#1767d1;--ok:#23955a;--off:#23955a;--on:#c2412d;--side:#b07a12;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--top:#b48cff;--bg:#0b1820;--card:#112430;
--ink:#e4eef2;--muted:#8fa6b2;--line:#21394a;--go:#5aa2ff;--ok:#46c483;--off:#46c483;--on:#ff7a63;--side:#e2b04a;color-scheme:dark}}
:root[data-theme="dark"]{--top:#b48cff;--bg:#0b1820;--card:#112430;--ink:#e4eef2;--muted:#8fa6b2;--line:#21394a;
--go:#5aa2ff;--ok:#46c483;--off:#46c483;--on:#ff7a63;--side:#e2b04a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 "Barlow",system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 18px 60px}
h1{font:600 2.4rem/1 "Barlow Condensed","Arial Narrow",sans-serif;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 22px}
.demo{display:inline-block;background:var(--on);color:#fff;padding:2px 8px;border-radius:4px;font-size:.85rem;margin-left:8px}
.ic{width:34px;height:34px;fill:none;stroke-width:3.2;stroke-linecap:round;stroke-linejoin:round}
.ic.top{stroke:var(--top)}.ic.go{stroke:var(--go)}.ic.ok{stroke:var(--ok)}.ic.nul{stroke:var(--ink)}
.week{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:8px;margin-bottom:34px}
.day{display:block;text-decoration:none;color:inherit;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px 12px 10px}
.day:focus-visible,.day:hover{outline:2px solid var(--go);outline-offset:1px}
.day .d{font:600 1.15rem/1 "Barlow Condensed","Arial Narrow",sans-serif;text-transform:capitalize}
.day .n{color:var(--muted);font-size:.85rem}
.day .ic{width:46px;height:46px;margin:8px 0 2px}
.day .c{color:var(--muted);font-size:.85rem;margin-top:2px}
.day .w{font-weight:600}.day .w.none{color:var(--muted);font-weight:400}
.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:.9rem;margin:-18px 0 30px}
.legend span{display:flex;align-items:center;gap:6px}.legend .ic{width:22px;height:22px}
section{margin-bottom:34px;scroll-margin-top:12px}
section h2{font:600 1.6rem/1.1 "Barlow Condensed","Arial Narrow",sans-serif;margin:0 0 4px;
display:flex;align-items:center;gap:10px}
section .meta{color:var(--muted);margin:0 0 10px}
section .meta:has(+ .tides){margin-bottom:2px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;min-width:100%}
th,td{padding:6px 8px;text-align:center;white-space:nowrap;border-bottom:1px solid var(--line)}
tr:last-child th,tr:last-child td{border-bottom:0}
th{text-align:left;font-weight:500;color:var(--muted);position:sticky;left:0;background:var(--card);font-size:.88rem}
thead td{font-weight:600}
tr.v td{padding:8px 6px 2px}
tr.sc td{font-size:.8rem;color:var(--muted);padding-top:0}
td.top{background:color-mix(in srgb,var(--top) 14%,transparent)}
td.go{background:color-mix(in srgb,var(--go) 12%,transparent)}
td.ok{background:color-mix(in srgb,var(--ok) 12%,transparent)}
.ar{display:inline-block;font-size:1rem;line-height:1}
.off{color:var(--off)}.on{color:var(--on)}.side-shore{color:var(--side)}
small{color:var(--muted)}
footer{color:var(--muted);font-size:.85rem;margin-top:40px}
"""


def arrow(deg):
    """Flèche dans le sens où va le vent/la houle (la direction météo indique d'où il vient)."""
    if deg is None:
        return "–"
    return f'<span class="ar" style="transform:rotate({deg}deg)">↓</span>'


def fmt(v, f="{:.0f}"):
    return "–" if v is None else f.format(v)


def render_html(days, tides_by_day, s=SPOT, demo=False):
    tz = ZoneInfo(s["tz"])
    now = datetime.now(tz)
    e = html.escape
    out = ['<!doctype html><html lang="fr"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>Surf {e(s['name'])}</title>",
           '<link rel="preconnect" href="https://fonts.googleapis.com">',
           '<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@600&display=swap" rel="stylesheet">',
           f"<style>{CSS}</style></head><body><main>",
           f"<h1>{e(s['name'])}</h1>",
           f'<p class="sub">Mis à jour le {JOURS[now.weekday()]} {now.day} {MOIS[now.month - 1]} à {now:%H:%M}'
           + ('<span class="demo">données fictives</span>' if demo else "") + "</p>",
           '<div class="week">']
    summary = {}
    for day, hours in days.items():
        v, win = best_window(hours)
        summary[day] = (v, win)
        cs = [c for _, k, c in tides_by_day.get(day, []) if k == "PM" and c]
        ctxt = f'<div class="c">coef {"/".join(map(str, cs))}</div>' if cs else ""
        out.append(f'<a class="day" href="#j{day:%m%d}"><div class="d">{JOURS[day.weekday()][:3]}.</div>'
                   f'<div class="n">{day.day} {MOIS_C[day.month - 1]}</div>{ICON[v]}'
                   + (f'<div class="w">{win}</div>' if win else '<div class="w none">rien de bon</div>') + ctxt + "</a>")
    out.append("</div>")
    out.append('<div class="legend">'
               f'<span>{ICON["top"]} Top (8,5/10 et plus)</span>'
               f'<span>{ICON["go"]} GO (7 à 8,5)</span>'
               f'<span>{ICON["ok"]} Correct (5 à 7)</span>'
               f'<span>{ICON["nul"]} Nul (moins de 5)</span></div>')

    for day, hours in days.items():
        v, win = summary[day]
        tl = tides_by_day.get(day, [])
        meta = []
        if win:
            meta.append(f"{LABEL[v]} de {win}" if "–" in win else f"{LABEL[v]} à {win}")
        pms = [f"{t:%H}h{t:%M}" + (f" (coef {c})" if c else "") for t, k, c in tl if k == "PM"]
        bms = [f"{t:%H}h{t:%M}" for t, k, c in tl if k == "BM"]
        tide_line = ""
        if pms:
            tide_line += "Pleine mer " + " et ".join(pms)
        if bms:
            tide_line += (", basse mer " if pms else "Basse mer ") + " et ".join(bms)
        sst = next((h["sst"] for h in hours if h["sst"] is not None), None)
        if sst is not None:
            meta.append(f"eau {sst:.0f} °C")
        out.append(f'<section id="j{day:%m%d}"><h2>{ICON[v]}{JOURS[day.weekday()].capitalize()} {day.day} {MOIS[day.month - 1]}</h2>'
                   f'<p class="meta">{", ".join(meta) if meta else "Pas de créneau correct"}</p>'
                   + (f'<p class="meta tides">{tide_line}</p>' if tide_line else "")
                   + '<div class="scroll"><table><thead><tr><th></th>')
        out += [f'<td>{h["t"]:%H}h</td>' for h in hours]
        out.append("</tr></thead><tbody>")

        def row(label, cells, cls=""):
            out.append(f'<tr class="{cls}"><th>{label}</th>' + "".join(cells) + "</tr>")

        row("", [f'<td class="{h["v"]}">{ICON[h["v"]]}</td>' for h in hours], "v")
        row("Note", [f'<td class="{h["v"]}">{h["score"]:.1f}</td>' for h in hours], "sc")
        R = [h["r"] for h in hours]
        row("Houle (m)", [f"<td>{fmt(r.get('SWELL1', r.get('HTSGW')), '{:.1f}')}</td>" for r in R])
        row("Période (s)", [f"<td>{fmt(r.get('SWPER1', r.get('PERPW')))}</td>" for r in R])
        row("Dir. houle", [f"<td>{arrow(r.get('SWDIR1', r.get('DIRPW')))} <small>{fmt(r.get('SWDIR1', r.get('DIRPW')))}°</small></td>" for r in R])
        row("Vent (nds)", [f"<td>{fmt(r.get('WINDSPD'))} <small>{fmt(r.get('GUST'))}</small></td>" for r in R])
        cells = []
        for r in R:
            wd = r.get("WINDDIR")
            k = wind_kind(wd) if wd is not None else ""
            cls = {"offshore": "off", "onshore": "on"}.get(k, k)
            cells.append(f'<td class="{cls}">{arrow(wd)} <small class="{cls}">{k.replace("-shore", "")}</small></td>')
        row("Dir. vent", cells)
        row("Marée", [f"<td>{('↗ ' if h['tide']['rising'] else '↘ ') + h['tide']['label']}</td>" if h["tide"] else "<td>–</td>" for h in hours])
        row("Coef", [f"<td>{h['coef'] if h['coef'] else '–'}</td>" for h in hours])
        row("Air (°C)", [f"<td>{fmt(r.get('TMP'))}</td>" for r in R])
        row("Nuages (%)", [f"<td>{fmt(r.get('TCDC'))}</td>" for r in R])
        out.append("</tbody></table></div></section>")

    out.append("<footer>Vent, houle et météo : Windguru. Marées, coefficients (calculés depuis Brest) "
               "et température de l’eau : Open-Meteo, modèles DWD, valeurs indicatives.</footer>"
               "</main></body></html>")
    return "\n".join(out), summary


def text_summary(summary, s=SPOT):
    emo = {"top": "💜 Top", "go": "🌊 GO", "ok": "🟢 Correct", "nul": "✖️ Nul"}
    lines = [f"Surf {s['name']}"]
    for day, (v, win) in summary.items():
        lines.append(f"{JOURS[day.weekday()][:3]} {day.day} : {emo[v]}" + (f" ({win})" if win else ""))
    return "\n".join(lines)


def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)

# =============================================================== DÉMO


def make_demo(s=SPOT):
    tz = ZoneInfo(s["tz"])
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    n = 7 * 24
    fc = {"initstamp": int(start.timestamp()), "hours": list(range(n))}
    hrs = range(n)
    day = [i // 24 for i in hrs]
    prof = [(1.4, 12, 6, 70), (1.0, 9, 14, 250), (0.5, 7, 8, 240), (1.8, 13, 9, 90),
            (2.0, 11, 18, 280), (1.3, 11, 7, 160), (0.9, 10, 5, 60)]
    fc["SWELL1"] = [prof[d][0] + 0.2 * math.sin(i / 6) for i, d in zip(hrs, day)]
    fc["SWPER1"] = [prof[d][1] + math.sin(i / 8) for i, d in zip(hrs, day)]
    fc["SWDIR1"] = [275 + 15 * math.sin(i / 20) for i in hrs]
    fc["WINDSPD"] = [max(2, prof[d][2] + 5 * math.sin((i % 24 - 8) / 24 * 2 * math.pi)) for i, d in zip(hrs, day)]
    fc["WINDDIR"] = [(prof[d][3] + 4 * (i % 24)) % 360 for i, d in zip(hrs, day)]
    fc["GUST"] = [w * 1.4 for w in fc["WINDSPD"]]
    fc["WINDDIR"] = [(70 + 180 * (1 + math.sin(i / 26)) / 2) % 360 for i in hrs]
    fc["TMP"] = [12 + 4 * math.sin((i % 24 - 9) / 24 * 2 * math.pi) for i in hrs]
    fc["TCDC"] = [int(50 + 45 * math.sin(i / 11)) for i in hrs]
    times = [start + timedelta(hours=i) for i in range(-12, n + 6)]
    def lvl(i, amp):
        return amp * (0.75 + 0.25 * math.cos(i / 177)) * math.cos(2 * math.pi * i / 12.42)
    idx = range(-12, n + 6)
    local = {t: (lvl(i, 2.2), 14.5) for i, t in zip(idx, times)}
    ex = extremes(times, [lvl(i, 2.2) for i in idx])
    coefs = coefficients(times, [lvl(i, 2.6) for i in idx])
    return {0: fc}, (local, ex, coefs)

# =============================================================== MAIN


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", type=int, default=SPOT["id"])
    ap.add_argument("--out", default="rapport.html")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    tz = ZoneInfo(SPOT["tz"])

    if a.demo:
        forecasts, tides = make_demo()
    else:
        try:
            forecasts = fetch_windguru(a.spot, a.debug)
        except Exception as e:
            sys.exit(f"Impossible de joindre Windguru : {e}")
        if not forecasts:
            sys.exit("Aucune prévision Windguru reçue (relance avec --debug).")
        tides = get_tides(SPOT, tz, a.debug)

    days, tides_by_day = build_days(merge(forecasts), tides)
    page, summary = render_html(days, tides_by_day, demo=a.demo)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(page)
    txt = text_summary(summary)
    print(txt + f"\n\nRapport : {a.out}")
    if not a.demo:
        send_telegram(txt)


if __name__ == "__main__":
    main()
