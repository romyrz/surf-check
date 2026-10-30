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
import re
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
    # Règles éliminatoires (croix directe, quelle que soit la note)
    "veto_min_waves": 0.6,       # vagues strictement inférieures (m)
    "veto_min_period": 8,        # période strictement inférieure (s)
    "veto_max_wind": 20,         # vent strictement supérieur (nœuds)
    "star_score": 9,             # étoile au-delà de cette note
    "hours": (7, 20),                 # heures affichées (heure locale)
    "tz": "Europe/Paris",
    # Modèles Windguru : par nom (tel qu’affiché dans le journal) ou par numéro id_model.
    "main_model": 100,           # WG, le tableau affiché par défaut sur Windguru
    "wind_first": [52],          # AROME-FR 1.3 pour le vent tant qu’il couvre l’heure ([] = vent WG partout)
    "exclude": [3],              # GFS 13, jamais utilisé
    "marc_zone": "7032_2",            # zone MARC Ifremer Quiberon / Ria d’Etel
    "marc_days": 6,                   # cartes MARC disponibles sur 6 jours
}
MARC_URL = ("https://marc.ifremer.fr/resultats/vagues/modeles_atlantique_nord/(variable)/hs/"
            "(typevisu)/map/(zoneid)/{zone}/(date)/{date}/(type)/canvas#appTop")


def marc_link(lt, s=SPOT):
    """Lien vers la carte MARC (hauteur de vagues) à l’heure locale lt."""
    return MARC_URL.format(zone=s["marc_zone"], date=f"{lt:%Y%m%d_%H}0000")
BREST = (48.30, -4.80)                # référence officielle des coefficients
U_BREST = 6.10                        # marnage (m) correspondant au coef 100

WG_API = "https://www.windguru.cz/int/iapi.php"
WG_PAGE = "https://www.windguru.cz/{spot}"
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


NAME_KEYS = ("model_name", "model_longname", "name", "model", "label", "title", "nm")


def model_names(obj, names=None):
    """Associe chaque id_model à son nom quand Windguru le fournit."""
    names = {} if names is None else names
    if isinstance(obj, dict):
        for k, v in obj.items():                     # forme {"3": {"model_name": ...}}
            if str(k).isdigit() and isinstance(v, dict):
                for nk in NAME_KEYS:
                    if isinstance(v.get(nk), str) and v[nk].strip():
                        names.setdefault(int(k), v[nk].strip())
                        break
        mid = obj.get("id_model")
        if str(mid).isdigit():
            for k in NAME_KEYS:
                if isinstance(obj.get(k), str) and obj[k].strip():
                    names.setdefault(int(mid), obj[k].strip())
                    break
        for v in obj.values():
            model_names(v, names)
    elif isinstance(obj, list):
        for v in obj:
            model_names(v, names)
    return names


MODEL_NAMES = {100: "WG", 3: "GFS 13", 84: "GFS-Wave 16", 117: "IFS-HRES 9", 118: "IFS-WAM 9",
               52: "AROME-FR 1.3", 109: "HARM-DK 2", 101: "UKV 2", 64: "Zephr-HD 2.6", 21: "WRF 9",
               57: "HARMONIE 5", 43: "ICON 7", 46: "EWAM 5", 45: "ICON 13", 59: "GDPS 15",
               60: "GDWPS 25"}


def model_info(obj, info=None):
    """Champs simples associés à chaque id_model dans la réponse forecast_spot."""
    info = {} if info is None else info
    if isinstance(obj, dict):
        mid = obj.get("id_model")
        if str(mid).isdigit():
            d = info.setdefault(int(mid), {})
            for k, v in obj.items():
                if isinstance(v, (str, int, float)) and k != "id_model":
                    d.setdefault(k, v)
        for k, v in obj.items():
            if str(k).isdigit() and isinstance(v, dict):
                d = info.setdefault(int(k), {})
                for kk, vv in v.items():
                    if isinstance(vv, (str, int, float)):
                        d.setdefault(kk, vv)
            model_info(v, info)
    elif isinstance(obj, list):
        for v in obj:
            model_info(v, info)
    return info


def model_matches(m, key):
    """key = numéro id_model ou nom (« WG », « AROME », « GFS 13 »...)."""
    if isinstance(key, int) or str(key).isdigit():
        return m == int(key)
    name = MODEL_NAMES.get(m, "")
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(str(key))}(?![A-Za-z0-9])", name, re.I))


ROLES = {"main": [], "wind": []}      # modèles retenus pour chaque rôle, dans l’ordre


def fetch_windguru(spot_id, debug=False, s=SPOT):
    spot = wg_get({"q": "forecast_spot", "id_spot": spot_id})
    models = find_model_ids(spot)
    MODEL_NAMES.update(model_names(spot))
    info = model_info(spot)
    tabs = spot.get("tabs") if isinstance(spot, dict) else None
    if debug:
        os.makedirs("site", exist_ok=True)
        with open("site/windguru_spot.json", "w", encoding="utf-8") as f:
            json.dump(spot, f, ensure_ascii=False, indent=1)
        print(f"Clés de la réponse Windguru : {list(spot) if isinstance(spot, dict) else type(spot)}")
        if isinstance(tabs, list):
            for i, tab in enumerate(tabs[:3]):
                print(f"  onglet {i} (extrait) : {json.dumps(tab, ensure_ascii=False)[:600]}")
        print("Modèles Windguru disponibles :")
        for m in models:
            print(f"  {m} : {MODEL_NAMES.get(m, '(nom inconnu)')}  {json.dumps(info.get(m, {}), ensure_ascii=False)[:300]}")
        if isinstance(tabs, list):
            for i, tab in enumerate(tabs):
                print(f"  onglet {i} : {find_model_ids(tab)}")

    excluded = [m for m in models if any(model_matches(m, k) for k in s.get("exclude", []))]
    for k in s.get("exclude", []):
        if not any(model_matches(m, k) for m in models):
            print(f"Attention : modèle à exclure « {k} » introuvable par son nom "
                  f"(indique plutôt son numéro dans \"exclude\").")
    usable = [m for m in models if m not in excluded]

    main = [m for m in usable if model_matches(m, s.get("main_model", "WG"))]
    if not main and isinstance(tabs, list) and tabs:      # nom introuvable : onglet par défaut
        main = [m for m in find_model_ids(tabs[0]) if m in usable]
        print(f"« {s.get('main_model')} » introuvable par son nom : onglet par défaut utilisé {main}.")
    wind = [m for k in s.get("wind_first", []) for m in usable if model_matches(m, k)]
    for k in s.get("wind_first", []):
        if not any(model_matches(m, k) for m in usable):
            print(f"Attention : modèle de vent « {k} » introuvable (indique son numéro dans \"wind_first\").")
    # vagues : on prend aussi les modèles de vagues de l’onglet principal s’ils sont séparés
    if isinstance(tabs, list):
        for tab in tabs:
            ids = find_model_ids(tab)
            if any(m in main for m in ids):
                main += [m for m in ids if m in usable and m not in main]
    ROLES["main"], ROLES["wind"] = main, wind
    if debug:
        names = lambda ms: ", ".join(f"{m} {MODEL_NAMES.get(m, '')}".strip() for m in ms) or "aucun"
        print(f"Exclus : {names(excluded)}")
        print(f"Vent prioritaire : {names(wind)}")
        print(f"Modèle principal : {names(main)}")

    out = {}
    for m in dict.fromkeys(wind + main):
        try:
            params = {"q": "forecast", "id_model": m, "id_spot": spot_id}
            for k in ("rundef", "initstr", "id_model_arr", "cachefix"):
                if k in info.get(m, {}):
                    params[k] = info[m][k]
            try:
                d = wg_get(params)
            except requests.HTTPError:
                if len(params) == 3:
                    raise
                d = wg_get({"q": "forecast", "id_model": m, "id_spot": spot_id})
            fc = d.get("fcst", d)
            if "hours" in fc:
                out[m] = fc
                if debug:
                    print(f"  {m}: {len(fc['hours'])} pas, "
                          f"{[k for k, v in fc.items() if isinstance(v, list)]}")
        except Exception as e:
            print(f"  {m}: échec ({e})")
            if m in main[:1]:                             # WG : on passe par la page
                fc = fetch_via_browser(spot_id, m, debug)
                if fc and "hours" in fc:
                    out[m] = fc
                    print(f"  {m}: {len(fc['hours'])} pas (via la page), "
                          f"{[k for k, v in fc.items() if isinstance(v, list)]}")
    return out


def fetch_via_browser(spot_id, model, debug=False):
    """Ouvre la page Windguru du spot comme un visiteur et récupère la prévision du modèle
    demandé au moment où la page la charge (c’est ainsi que le tableau WG arrive à l’écran)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright absent : impossible de lire WG via la page.")
        return None
    got = {}

    def on_response(resp):
        u = resp.url
        if ("iapi.php" in u and re.search(r"[?&]q=forecast(&|$)", u)
                and re.search(rf"[?&]id_model={model}(&|$)", u) and "data" not in got):
            try:
                got["data"], got["url"] = resp.json(), u
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="fr-FR", user_agent=HEADERS["User-Agent"],
                                viewport={"width": 1400, "height": 1000})
        page.on("response", on_response)
        try:
            page.goto(WG_PAGE.format(spot=spot_id), wait_until="domcontentloaded", timeout=60000)
            for _ in range(80):
                if "data" in got:
                    break
                page.wait_for_timeout(500)
        except Exception as e:
            print(f"  page Windguru : échec ({str(e).splitlines()[0][:120]})")
        browser.close()
    if "data" not in got:
        print(f"  modèle {model} introuvable dans les échanges de la page Windguru")
        return None
    if debug:
        print(f"  modèle {model} lu via la page : {got['url']}")
    d = got["data"]
    return d.get("fcst", d) if isinstance(d, dict) else None


def to_series(fc):
    init, hrs = fc["initstamp"], fc["hours"]
    out = {}
    for i, h in enumerate(hrs):
        t = datetime.fromtimestamp(init + h * 3600, tz=timezone.utc)
        out[t] = {k: v[i] for k, v in fc.items()
                  if isinstance(v, list) and len(v) == len(hrs) and v[i] is not None}
    return out


WAVE_KEYS = ("HTSGW", "PERPW", "DIRPW", "SWELL", "SWPER", "SWDIR", "WV")


WIND_KEYS = ("WINDSPD", "GUST", "WINDDIR")


def merge(forecasts, debug=False):
    """Assemble heure par heure :
    - vent, rafales, direction : premier modèle de « wind_first » (AROME) qui couvre l’heure,
      sinon le modèle principal (WG) ;
    - vagues, température, nuages, pluie : modèle principal (WG).
    Les heures du tableau sont celles du modèle principal, comme sur Windguru."""
    main = [m for m in ROLES["main"] if m in forecasts] or list(forecasts)
    wind = [m for m in ROLES["wind"] if m in forecasts]
    ser = {m: to_series(forecasts[m]) for m in forecasts}
    main_atmo = [m for m in main if "WINDSPD" in forecasts[m]]
    main_wave = [m for m in main if "HTSGW" in forecasts[m] or "SWELL1" in forecasts[m]]
    if not main_atmo:
        return {}
    hours = sorted(ser[main_atmo[0]])                         # colonnes = celles de WG
    if len(main_atmo) > 1 and hours:                          # au-delà de WG : relais
        hours += sorted(t for m in main_atmo[1:2] for t in ser[m] if t > hours[-1])
    merged, used = {}, defaultdict(lambda: [set(), set()])
    for t in hours:
        row, wind_src, wave_src = {}, None, None
        for m in main_atmo:                                   # base WG
            r = ser[m].get(t)
            if r and "WINDSPD" in r:
                row.update({k: v for k, v in r.items() if not k.startswith(WAVE_KEYS)})
                wind_src = m
                break
        for m in wind:                                        # vent AROME s’il couvre l’heure
            r = ser[m].get(t)
            if r and "WINDSPD" in r:
                row.update({k: r[k] for k in WIND_KEYS if k in r})
                wind_src = m
                break
        for m in main_wave:                                   # vagues WG (heure la plus proche, ±1h30)
            near = [u for u in ser[m] if abs((u - t).total_seconds()) <= 5400]
            r = ser[m][min(near, key=lambda u: abs((u - t).total_seconds()))] if near else None
            if r and ("HTSGW" in r or "SWELL1" in r):
                row.update({k: v for k, v in r.items() if k.startswith(WAVE_KEYS)})
                wave_src = m
                break
        if row:
            row["_wind_model"], row["_wave_model"] = wind_src, wave_src
            merged[t] = row
            if wind_src is not None:
                used[t.date()][0].add(wind_src)
            if wave_src is not None:
                used[t.date()][1].add(wave_src)
    if debug:
        for day, (w, v) in sorted(used.items()):
            fw = ", ".join(f"{m} {MODEL_NAMES.get(m, '')}".strip() for m in sorted(w))
            fv = ", ".join(f"{m} {MODEL_NAMES.get(m, '')}".strip() for m in sorted(v))
            print(f"  {day} : vent = {fw} ; vagues = {fv}")
    return merged


# =============================================================== CARTES MARC (IFREMER)


def capture_marc(slots, out_dir, s=SPOT, debug=False):
    """Ouvre la page MARC de chaque heure dans un navigateur sans écran et
    photographie la carte (dessinée dans un <canvas>). Renvoie {heure locale: fichier}."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright absent : cartes MARC ignorées (pip install playwright).")
        return {}
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):                       # on repart de zéro à chaque rapport
        if f.endswith((".jpg", ".png")):
            os.remove(os.path.join(out_dir, f))
    done, failures = {}, 0
    biggest = """() => { const c = [...document.querySelectorAll('canvas')]
        .filter(x => x.width > 300 && x.height > 200)
        .sort((a, b) => b.width * b.height - a.width * a.height); return c[0] || null; }"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1100},
                                locale="fr-FR", timezone_id=s["tz"])
        for lt in slots:
            name = f"{lt:%Y%m%d_%H}.jpg"
            path = os.path.join(out_dir, name)
            try:
                page.goto(marc_link(lt, s), wait_until="load", timeout=60000)
                page.wait_for_function(f"({biggest})() !== null", timeout=30000)
                page.wait_for_timeout(2500)              # laisse le temps au dessin de se terminer
                page.evaluate_handle(biggest).as_element().screenshot(
                    path=path, type="jpeg", quality=80)
                if os.path.getsize(path) < 8000:          # carte vide ou pas encore dessinée
                    os.remove(path)
                    raise RuntimeError("carte vide")
                done[lt] = name
                if debug:
                    print(f"  MARC {lt:%d/%m %Hh} : ok")
            except Exception as e:
                failures += 1
                print(f"  MARC {lt:%d/%m %Hh} : échec ({str(e).splitlines()[0][:120]})")
                if failures == 1:                         # capture de diagnostic de la page entière
                    try:
                        page.screenshot(path=os.path.join(out_dir, "_diagnostic.png"), full_page=True)
                    except Exception:
                        pass
                if failures >= 5 and not done:            # le site ne répond pas : on arrête
                    print("  MARC indisponible, cartes ignorées pour ce rapport.")
                    break
        browser.close()
    print(f"Cartes MARC : {len(done)}/{len(slots)} récupérées")
    return done

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
    h = r.get("HTSGW", r.get("SWELL1"))
    p = r.get("PERPW", r.get("SWPER1"))
    d = r.get("DIRPW", r.get("SWDIR1"))
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


def veto(r, s=SPOT):
    """Raison éliminatoire (texte court) ou None. Comparé aux valeurs affichées (arrondies)."""
    h = r.get("HTSGW", r.get("SWELL1"))
    p = r.get("PERPW", r.get("SWPER1"))
    ws = r.get("WINDSPD")
    if h is not None and round(h, 1) < s["veto_min_waves"]:
        return f"vagues < {s['veto_min_waves']:g} m".replace(".", ",")
    if p is not None and round(p) < s["veto_min_period"]:
        return f"période < {s['veto_min_period']} s"
    if ws is not None and round(ws) > s["veto_max_wind"]:
        return f"vent > {s['veto_max_wind']} nds"
    return None


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
                                "coef": coef_at(t, coefs), "score": sc, "veto": (vt := veto(r, s)),
                                "v": "nul" if vt else verdict(sc),
                                "star": not vt and sc > s["star_score"]})
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
MAP_ICON = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linejoin="round"><path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z"/><path d="M9 4v14M15 6v14"/></svg>')
VIEWER_JS = """<script>
document.querySelectorAll('.mbtn').forEach(function (b) {
  b.addEventListener('click', function () {
    var f = document.getElementById(b.dataset.target);
    if (!f) return;
    f.querySelector('img').src = b.dataset.src;
    f.querySelector('.mh').textContent = b.dataset.h;
    f.querySelector('a').href = b.dataset.href;
    document.querySelectorAll('.mbtn[data-target="' + b.dataset.target + '"]').forEach(function (x) {
      x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
    });
    f.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  });
});
</script>"""
STAR = ('<svg class="star" viewBox="0 0 24 24" aria-label="Exceptionnel"><path d="M12 2.5l2.9 6.1 6.6.8-4.9 4.6 '
        '1.3 6.6L12 17.3l-5.9 3.3 1.3-6.6-4.9-4.6 6.6-.8z"/></svg>')
LABEL = {"top": "Top", "go": "GO", "ok": "Correct", "nul": "Nul"}

CSS = """
:root{--star:#e0a100;--top:#7b3fd4;--bg:#eef4f6;--card:#ffffff;--ink:#0f2533;--muted:#5d7380;--line:#d3e0e6;
--go:#1767d1;--ok:#23955a;--off:#23955a;--on:#c2412d;--side:#b07a12;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--star:#ffd24a;--top:#b48cff;--bg:#0b1820;--card:#112430;
--ink:#e4eef2;--muted:#8fa6b2;--line:#21394a;--go:#5aa2ff;--ok:#46c483;--off:#46c483;--on:#ff7a63;--side:#e2b04a;color-scheme:dark}}
:root[data-theme="dark"]{--star:#ffd24a;--top:#b48cff;--bg:#0b1820;--card:#112430;--ink:#e4eef2;--muted:#8fa6b2;--line:#21394a;
--go:#5aa2ff;--ok:#46c483;--off:#46c483;--on:#ff7a63;--side:#e2b04a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 "Barlow",system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1500px;margin:0 auto;padding:28px 18px 60px}
h1{font:600 2.4rem/1 "Barlow Condensed","Arial Narrow",sans-serif;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 22px}
.demo{display:inline-block;background:var(--on);color:#fff;padding:2px 8px;border-radius:4px;font-size:.85rem;margin-left:8px}
.ic{width:34px;height:34px;fill:none;stroke-width:3.2;stroke-linecap:round;stroke-linejoin:round}
.ic.top{stroke:var(--top)}
.vi{position:relative;display:inline-block;line-height:0}
.star{position:absolute;top:-6px;right:-10px;width:16px;height:16px;fill:var(--star);stroke:var(--card);stroke-width:1.5}
.day .star{width:20px;height:20px;right:-12px}
.legend .vi .star{position:static;width:18px;height:18px}
td.veto{font-size:.72rem;white-space:nowrap}.ic.go{stroke:var(--go)}.ic.ok{stroke:var(--ok)}.ic.nul{stroke:var(--ink)}
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
.marc{color:var(--go);display:inline-flex;padding:4px;border-radius:6px;border:0;background:none;cursor:pointer;font:inherit}
.marc:hover,.marc:focus-visible{background:color-mix(in srgb,var(--go) 15%,transparent);outline:none}
.marc[aria-pressed="true"]{background:var(--go);color:var(--card)}
.daygrid{display:grid;grid-template-columns:minmax(300px,520px) minmax(0,1fr);gap:14px;align-items:start}
.daygrid .scroll{min-width:0}
@media (max-width:900px){.daygrid{grid-template-columns:minmax(0,1fr)}}
.marcview{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;
position:sticky;top:calc(env(safe-area-inset-top,0px) + 10px)}
@media (max-width:900px){.marcview{position:static}}
.marcview img{display:block;width:100%;height:auto;border-radius:4px;background:#fff}
.marcview figcaption{color:var(--muted);font-size:.9rem;margin-top:8px}
.marcview figcaption a{color:var(--go)}
.ar{display:inline-block;font-size:1rem;line-height:1}
.off{color:var(--off)}.on{color:var(--on)}.side-shore{color:var(--side)}
small{color:var(--muted)}
td.hs,td.ws{font-weight:600}
footer{color:var(--muted);font-size:.85rem;margin-top:40px}
"""


# Échelle de couleurs des cartes MARC (Ifremer) : (hauteur minimale en m, couleur)
HOULE_SCALE = [
    (0.0, "#efe5d8"), (0.1, "#ddcbb5"), (0.25, "#c4ac90"), (0.5, "#c7baf6"),
    (0.75, "#8f78e8"), (1.0, "#4c30b4"), (1.25, "#7ff2f4"), (1.5, "#3fc4f2"),
    (1.75, "#1f8fe6"), (2.0, "#1450d0"), (2.25, "#0b2aa3"), (2.5, "#0a7a33"),
    (2.75, "#15a043"), (3.0, "#2fc45a"), (3.25, "#6fdd66"), (3.5, "#a8ec6b"),
    (3.75, "#dcf56d"), (4.0, "#ffff66"), (4.5, "#ffe04a"), (5.0, "#ffbf1f"),
    (5.5, "#ff9500"), (6.0, "#ffc9c9"), (7.0, "#ff9a9a"), (8.0, "#ff3d3d"),
    (9.0, "#e00000"), (10.0, "#9c5a2e"), (11.0, "#b0002b"), (12.0, "#86002f"),
    (13.0, "#c2007c"), (14.0, "#ff00c8"), (15.0, "#8e0088"),
]


def houle_cell(h):
    """Cellule colorée comme sur MARC, texte clair ou foncé selon le fond."""
    if h is None:
        return "<td>–</td>"
    h = round(h, 1)                 # la couleur correspond à la valeur affichée
    color = HOULE_SCALE[0][1]
    for lo, c in HOULE_SCALE:
        if h >= lo:
            color = c
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    ink = "#10202b" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"
    return f'<td class="hs" style="background:{color};color:{ink}">{h:.1f}</td>'


# Dégradé de couleurs du vent façon Windguru : (nœuds, couleur), interpolé entre les points
WIND_SCALE = [
    (0, "#ffffff"), (5, "#ffffff"), (8.5, "#67f7f1"), (13.5, "#00ff00"),
    (18.5, "#f0ff00"), (24.5, "#ff3200"), (31, "#dc00dc"), (38.5, "#9600ff"),
    (50, "#4b0082"),
]


def _ink(r, g, b):
    return "#10202b" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


def wind_cell(kn):
    if kn is None:
        return "<td>–</td>"
    kn = round(kn)
    pts = [(v, tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))) for v, c in WIND_SCALE]
    rgb = pts[-1][1]
    for (v0, c0), (v1, c1) in zip(pts, pts[1:]):
        if kn <= v1:
            t = 0 if v1 == v0 else max(0, (kn - v0) / (v1 - v0))
            rgb = tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
            break
    return (f'<td class="ws" style="background:rgb{rgb};color:{_ink(*rgb)}">{kn}</td>')


def arrow(deg):
    """Flèche dans le sens où va le vent/la houle (la direction météo indique d'où il vient)."""
    if deg is None:
        return "–"
    return f'<span class="ar" style="transform:rotate({deg}deg)">↓</span>'


def fmt(v, f="{:.0f}"):
    return "–" if v is None else f.format(v)


def render_html(days, tides_by_day, s=SPOT, demo=False, marc=None, marc_dir="marc"):
    marc = marc or {}
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
        star = any(h["star"] for h in hours)
        summary[day] = (v, win, star)
        cs = [c for _, k, c in tides_by_day.get(day, []) if k == "PM" and c]
        ctxt = f'<div class="c">coef {"/".join(map(str, cs))}</div>' if cs else ""
        out.append(f'<a class="day" href="#j{day:%m%d}"><div class="d">{JOURS[day.weekday()][:3]}.</div>'
                   f'<div class="n">{day.day} {MOIS_C[day.month - 1]}</div><span class="vi">{ICON[v]}{STAR if star else ""}</span>'
                   + (f'<div class="w">{win}</div>' if win else '<div class="w none">rien de bon</div>') + ctxt + "</a>")
    out.append("</div>")
    out.append('<div class="legend">'
               f'<span>{ICON["top"]} Top (8,5/10 et plus)</span>'
               f'<span>{ICON["go"]} GO (7 à 8,5)</span>'
               f'<span>{ICON["ok"]} Correct (5 à 7)</span>'
               f'<span>{ICON["nul"]} Nul (moins de 5, ou vagues &lt; 0,6 m, période &lt; 8 s, vent &gt; 20 nds)</span>'
               f'<span><span class="vi">{STAR}</span> Exceptionnel (plus de 9/10)</span></div>')

    for day, hours in days.items():
        v, win, _ = summary[day]
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
                   )
        grid_at = len(out)                    # la carte sera insérée ici, à gauche du tableau
        out.append('<div class="scroll"><table><thead><tr><th></th>')
        out += [f'<td>{h["t"]:%H}h</td>' for h in hours]
        out.append("</tr></thead><tbody>")

        def row(label, cells, cls=""):
            out.append(f'<tr class="{cls}"><th>{label}</th>' + "".join(cells) + "</tr>")

        row("", [f'<td class="{h["v"]}"><span class="vi">{ICON[h["v"]]}{STAR if h["star"] else ""}</span></td>'
                 for h in hours], "v")
        row("Note", [f'<td class="{h["v"]} veto">{h["veto"]}</td>' if h["veto"]
                     else f'<td class="{h["v"]}">{h["score"]:.1f}</td>' for h in hours], "sc")
        R = [h["r"] for h in hours]
        row("Vagues (m)", [houle_cell(r.get('HTSGW', r.get('SWELL1'))) for r in R])
        row("Période (s)", [f"<td>{fmt(r.get('PERPW', r.get('SWPER1')))}</td>" for r in R])
        row("Dir. vagues", [f"<td>{arrow(r.get('DIRPW', r.get('SWDIR1')))} <small>{fmt(r.get('DIRPW', r.get('SWDIR1')))}°</small></td>" for r in R])
        row("Vent (nds)", [wind_cell(r.get("WINDSPD")) for r in R])
        row("Rafales (nds)", [wind_cell(r.get("GUST")) for r in R])
        cells = []
        for r in R:
            wd = r.get("WINDDIR")
            k = wind_kind(wd) if wd is not None else ""
            cls = {"offshore": "off", "onshore": "on"}.get(k, k)
            cells.append(f'<td class="{cls}">{arrow(wd)} <small class="{cls}">{k.replace("-shore", "")}</small></td>')
        row("Dir. vent", cells)
        row("Marée", [f"<td>{('↗ ' if h['tide']['rising'] else '↘ ') + h['tide']['label']}</td>" if h["tide"] else "<td>–</td>" for h in hours])
        viewer = ""
        if (day - now.date()).days < s["marc_days"]:
            vid = f"m{day:%m%d}"
            with_img = [h for h in hours if h["t"] in marc]
            cells = []
            for h in hours:
                link = e(marc_link(h["t"], s))
                if h["t"] in marc:
                    cells.append(f'<td><button type="button" class="marc mbtn" data-target="{vid}" '
                                 f'data-src="{marc_dir}/{marc[h["t"]]}" data-h="{h["t"]:%H}h" data-href="{link}" '
                                 f'aria-label="Voir la carte de {h["t"]:%H}h">{MAP_ICON}</button></td>')
                else:
                    cells.append(f'<td><a class="marc" href="{link}" target="_blank" rel="noopener" '
                                 f'aria-label="Carte MARC de {h["t"]:%H}h">{MAP_ICON}</a></td>')
            row("Carte MARC", cells)
            if with_img:
                best = [h for h in with_img if h["v"] == v] or with_img
                first = max(best, key=lambda h: h["score"])
                viewer = (f'<figure class="marcview" id="{vid}"><img src="{marc_dir}/{marc[first["t"]]}" '
                          f'alt="Carte MARC des vagues" loading="lazy" width="748" height="576">'
                          f'<figcaption>Hauteur et direction des vagues à <span class="mh">{first["t"]:%H}h</span>. '
                          f'Clique sur une icône de carte dans le tableau pour changer d’heure. '
                          f'<a href="{e(marc_link(first["t"], s))}" target="_blank" rel="noopener">Ouvrir sur MARC</a>'
                          f'</figcaption></figure>')
        row("Coef", [f"<td>{h['coef'] if h['coef'] else '–'}</td>" for h in hours])
        row("Air (°C)", [f"<td>{fmt(r.get('TMP'))}</td>" for r in R])
        row("Nuages (%)", [f"<td>{fmt(r.get('TCDC'))}</td>" for r in R])
        out.append("</tbody></table></div></div></section>")
        out.insert(grid_at, ('<div class="daygrid">' + viewer) if viewer else '<div class="dayonly">')

    out.append("<footer>Vent, houle et météo : Windguru. Marées, coefficients (calculés depuis Brest) "
               "et température de l’eau : Open-Meteo, modèles DWD, valeurs indicatives. "
               "Cartes de vagues : MARC, Ifremer.</footer>"
               "</main>" + VIEWER_JS + "</body></html>")
    return "\n".join(out), summary


def text_summary(summary, s=SPOT):
    emo = {"top": "💜 Top", "go": "🌊 GO", "ok": "🟢 Correct", "nul": "✖️ Nul"}
    lines = [f"Surf {s['name']}"]
    for day, (v, win, star) in summary.items():
        lines.append(f"{JOURS[day.weekday()][:3]} {day.day} : {emo[v]}" + (" ⭐" if star else "")
                     + (f" ({win})" if win else ""))
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


def inspect(forecasts, when, s=SPOT):
    """Affiche, pour une heure donnée, la valeur brute de chaque modèle Windguru."""
    tz = ZoneInfo(s["tz"])
    lt = datetime.strptime(when, "%Y-%m-%d %H").replace(tzinfo=tz)
    t = lt.astimezone(timezone.utc)
    merged = merge(forecasts)
    print(f"\n=== Inspection {lt:%a %d/%m %Hh} (heure de Paris) = {t:%H}h UTC ===")
    for m, fc in forecasts.items():
        name = MODEL_NAMES.get(m, "")
        kind = "vagues" if ("HTSGW" in fc or "SWELL1" in fc) else "météo"
        init = datetime.fromtimestamp(fc["initstamp"], tz=timezone.utc)
        ser = to_series(fc)
        r = ser.get(t)
        units = {k: v for k, v in fc.items() if "unit" in k.lower()}
        print(f"- modèle {m} {name} [{kind}], run du {init:%d/%m %Hh} UTC, {len(fc['hours'])} pas"
              + (f", unités {units}" if units else ""))
        if r is None:
            print("    pas de donnée à cette heure")
            continue
        i = fc["hours"].index(int(round((t - init).total_seconds() / 3600)))
        extra = {k: fc[k][i] for k in ("hr_d", "hr_h", "hr_weekday") if isinstance(fc.get(k), list)}
        vals = {k: r.get(k) for k in ("WINDSPD", "GUST", "WINDDIR", "TMP", "HTSGW", "PERPW", "SWELL1", "SWPER1")
                if k in r}
        print(f"    index {i}, heure selon Windguru {extra or '(non fournie)'}")
        print(f"    valeurs brutes {vals}")
    row = merged.get(t, {})
    print(f"→ Rapport : vent {row.get('WINDSPD')} / rafales {row.get('GUST')} pris du modèle "
          f"{row.get('_wind_model')}, houle du modèle {row.get('_wave_model')}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", type=int, default=SPOT["id"])
    ap.add_argument("--out", default="rapport.html")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--marc", action="store_true", help="photographier les cartes MARC (Ifremer)")
    ap.add_argument("--inspect", default=os.getenv("INSPECT", ""),
                    help='détail des modèles pour une heure, ex. "2026-10-31 10"')
    a = ap.parse_args()
    tz = ZoneInfo(SPOT["tz"])

    if a.demo:
        forecasts, tides = make_demo()
    else:
        try:
            forecasts = fetch_windguru(a.spot, a.debug or bool(a.inspect))
        except Exception as e:
            sys.exit(f"Impossible de joindre Windguru : {e}")
        if not forecasts:
            sys.exit("Aucune prévision Windguru reçue (relance avec --debug).")
        tides = get_tides(SPOT, tz, a.debug)

    if a.inspect:
        inspect(forecasts, a.inspect)
    days, tides_by_day = build_days(merge(forecasts, a.debug), tides)
    marc = {}
    if a.marc:
        today = datetime.now(tz).date()
        slots = [h["t"] for d, hs in days.items() if (d - today).days < SPOT["marc_days"] for h in hs]
        marc = capture_marc(slots, os.path.join(os.path.dirname(a.out) or ".", "marc"), debug=a.debug)
    page, summary = render_html(days, tides_by_day, demo=a.demo, marc=marc)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(page)
    txt = text_summary(summary)
    print(txt + f"\n\nRapport : {a.out}")
    if not a.demo:
        send_telegram(txt)


if __name__ == "__main__":
    main()
