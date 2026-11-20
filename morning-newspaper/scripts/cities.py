#!/usr/bin/env python3
"""Nearby places for the local section: which towns, villages and districts are close to the
reader's city, and how close. Built once by the scripts (never by the model reading data), stored
in config.json as "region", and used by collect.py to mark local candidates with
"near <place> <km>km", "other state?" or "where?" so the model never spends tokens working out
whether a town is a neighbour.

Sources, in order:
1. A country's own statistics office, when this file has a provider for it. Today: Brazil (IBGE:
   every municipality's boundary, from which the centre is computed, plus every district).
   Contributors can add a provider for their country (see PROVIDERS at the end).
2. GeoNames (geonames.org, CC-BY 4.0): the country's file of populated places, any size.
3. None of them answers: the model writes WORK/region.json from what it knows and this script
   checks it and stores it (`--from-model`), marked as source "model" so the scripts replace it
   with real data as soon as a source answers again.

No population cut: a village of 2,000 people next door matters more to the reader than a city
of 500,000 two hours away. Only the distance decides (radius_km, 60 by default), and places that
are not settlements (GeoNames: neighbourhoods, abandoned or historical places) are left out.

Usage (setup, or by hand):
  python3 cities.py --config WORK/config.json [--radius 60] [--write]
  python3 cities.py --config WORK/config.json --from-model WORK/region.json --write
"""

import argparse
import datetime as dt
import io
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request
import zipfile

UA = "morning-newspaper (+github.com/oantuneshenriqueo/morning-newspaper; nearby places for a personal newspaper)"
TIMEOUT = 30
DEFAULT_RADIUS = 60
MAX_PLACES = 250          # keeps config.json small even in a dense metropolitan area
REFRESH_MODEL_DAYS = 30   # a list written by the model is replaced by real data after this
GEONAMES_MAX_BYTES = 90_000_000  # the largest country files (US) are ~70 MB zipped
# GeoNames populated places that are not settlements of their own.
GEONAMES_SKIP = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH", "PPLCD"}

BR_STATES = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
    "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso",
    "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo",
    "SE": "Sergipe", "TO": "Tocantins"}


def fold(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", txt).split())


def km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def get(url, limit=40_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


def country_of(cfg):
    city = cfg.get("city") or {}
    cc = str(city.get("country") or (cfg.get("news_locale") or {}).get("gl") or "").strip().upper()
    return cc if re.fullmatch(r"[A-Z]{2}", cc) else ""


# ---------------------------------------------------------------- Brazil: IBGE

def _ring_centroid(ring):
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        f = x1 * y2 - x2 * y1
        a += f
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    if abs(a) < 1e-12:
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        return sum(ys) / len(ys), sum(xs) / len(xs), 0.0
    return cy / (3 * a), cx / (3 * a), abs(a) / 2


def centroid(geometry):
    """(lat, lon) of a GeoJSON Polygon or MultiPolygon: the centre of its largest part."""
    polys = geometry["coordinates"] if geometry.get("type") == "MultiPolygon" else [geometry["coordinates"]]
    best = None
    for poly in polys:
        ring = [tuple(p[:2]) for p in poly[0]]
        lat, lon, area = _ring_centroid(ring)
        if best is None or area > best[2]:
            best = (lat, lon, area)
    return best[0], best[1]


def ibge(cfg, radius):
    """Brazil: municipality centres from IBGE's boundaries, names and districts from IBGE's places API."""
    city = cfg["city"]
    lat0, lon0 = float(city["latitude"]), float(city["longitude"])
    malha = json.loads(get("https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR?" + urllib.parse.urlencode(
        {"intrarregiao": "municipio", "formato": "application/vnd.geo+json", "qualidade": "minima"})))
    centres = {}
    for f in malha.get("features") or []:
        code = str((f.get("properties") or {}).get("codarea") or "")
        if code and f.get("geometry"):
            centres[code] = centroid(f["geometry"])
    if len(centres) < 5000:
        raise ValueError(f"IBGE boundaries incomplete ({len(centres)} municipalities)")
    near = {c: km(lat0, lon0, la, lo) for c, (la, lo) in centres.items()}
    in_reach = {c: d for c, d in near.items() if d <= radius + 25}
    uf_ids = sorted({c[:2] for c in in_reach})
    munis, dists = {}, []
    for uf in uf_ids:
        for m in json.loads(get(f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios?view=nivelado")):
            munis[str(m["municipio-id"])] = (m["municipio-nome"], m["UF-sigla"])
        dists += json.loads(get(f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/distritos?view=nivelado"))
    # The reader's municipality: the one with the city's name closest to its coordinates (a name
    # shared by towns in other states never wins over the one at the reader's coordinates).
    want = fold(city.get("name"))
    named = [c for c in in_reach if c in munis and fold(munis[c][0]) == want]
    home = min(named or [c for c in in_reach if c in munis], key=lambda c: near[c])
    home_name, home_uf = munis[home]
    places = []
    for code, d in in_reach.items():
        if code == home or code not in munis or d > radius:
            continue
        name, uf = munis[code]
        places.append({"name": name, "km": round(d), "uf": uf})
    seats = {str(m): munis[str(m)] for m in munis}
    for x in dists:
        mid = str(x.get("municipio-id"))
        if mid not in seats or mid not in near or near[mid] > radius:
            continue
        name = x.get("distrito-nome") or ""
        seat, uf = seats[mid]
        if fold(name) == fold(seat):
            continue  # the district that is the town itself
        places.append({"name": name, "km": 0 if mid == home else round(near[mid]), "seat": seat, "uf": uf})
    return {"source": "ibge", "state": home_uf, "places": places, "home": home_name}


# ---------------------------------------------------------------- GeoNames (any country)

def geonames(cfg, radius, cc):
    city = cfg["city"]
    lat0, lon0 = float(city["latitude"]), float(city["longitude"])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{cc}.zip")
        req = urllib.request.Request(f"https://download.geonames.org/export/dump/{cc}.zip", headers={"User-Agent": UA})
        size = 0
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(path, "wb") as out:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > GEONAMES_MAX_BYTES:
                    raise ValueError("country file too large")
                out.write(chunk)
        rows = []
        with zipfile.ZipFile(path) as z, z.open(f"{cc}.txt") as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                f = line.rstrip("\n").split("\t")
                if len(f) < 11 or f[6] != "P" or f[7] in GEONAMES_SKIP:
                    continue
                try:
                    la, lo = float(f[4]), float(f[5])
                except ValueError:
                    continue
                d = km(lat0, lon0, la, lo)
                if d <= radius + 5:
                    rows.append((d, f[1], f[10]))
    if not rows:
        raise ValueError("no places near the city")
    rows.sort()
    want = fold(city.get("name"))
    home = next((r for r in rows if fold(r[1]) == want and r[0] <= 15), rows[0])
    places = [{"name": n, "km": round(d), "uf": a1} for d, n, a1 in rows
              if d <= radius and fold(n) != fold(home[1])]
    return {"source": "geonames", "state": home[2], "places": places, "home": home[1]}


# ---------------------------------------------------------------- the stored list

PROVIDERS = {"BR": [ibge]}  # country code -> official sources tried before GeoNames


def compact(found, radius, city_name):
    """The form stored in config.json: [name, km] or [name, km, seat] for a district, plus the
    state code when it is not the reader's. Nearest first, one entry per name."""
    home_state = found.get("state") or ""
    seen, rows = set(), []
    for p in sorted(found["places"], key=lambda p: (p["km"], p["name"])):
        key = fold(p["name"])
        if not key or key in seen or key == fold(city_name):
            continue
        seen.add(key)
        row = [p["name"], int(p["km"])]
        seat = p.get("seat") or ""
        uf = p.get("uf") or ""
        if seat or (uf and uf != home_state):
            row.append(seat)
        if uf and uf != home_state:
            row.append(uf)
        rows.append(row)
    return {"source": found["source"], "city": city_name, "radius_km": radius, "state": home_state,
            "built": dt.date.today().isoformat(), "places": rows[:MAX_PLACES],
            **({"cut": len(rows) - MAX_PLACES} if len(rows) > MAX_PLACES else {})}


def build(cfg, radius=None, log=None):
    """(region, notes). region is None when no source answered; notes say why, one per source."""
    city = cfg.get("city") or {}
    if not (city.get("name") and "latitude" in city and "longitude" in city):
        return None, ["no city with coordinates in config.json"]
    radius = int(radius or (cfg.get("region") or {}).get("radius_km") or DEFAULT_RADIUS)
    cc = country_of(cfg)
    notes = []
    for provider in PROVIDERS.get(cc, []) + ["geonames"]:
        name = provider if isinstance(provider, str) else provider.__name__
        try:
            if name == "geonames" and not cc:
                raise ValueError("unknown country (set city.country, e.g. \"BR\")")
            found = geonames(cfg, radius, cc) if provider == "geonames" else provider(cfg, radius)
            return compact(found, radius, city["name"]), notes
        except Exception as e:
            notes.append(f"{name}: {type(e).__name__} {str(e)[:80]}".strip())
    return None, notes


def needs_build(cfg, today=None):
    """True when the stored list is missing, belongs to another city or radius, or was written by
    the model long enough ago that real data should replace it."""
    region = cfg.get("region")
    city = (cfg.get("city") or {}).get("name")
    if not city or not isinstance(region, dict) or not region.get("places"):
        return bool(city)
    if fold(region.get("city")) != fold(city):
        return True
    if region.get("source") == "model":
        try:
            built = dt.date.fromisoformat(region.get("built") or "")
        except ValueError:
            return True
        return ((today or dt.date.today()) - built).days >= REFRESH_MODEL_DAYS
    return False


def from_model(path, cfg, radius=None):
    """Checks a list the model wrote (WORK/region.json): {"places": [["Town", 25], ["Village", 12, "Seat town"]]}.
    Returns (region, problems)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("places") if isinstance(data, dict) else data
    problems, places = [], []
    radius = int(radius or (data.get("radius_km") if isinstance(data, dict) else 0) or DEFAULT_RADIUS)
    for i, r in enumerate(rows or []):
        if not (isinstance(r, list) and len(r) >= 2 and isinstance(r[0], str) and isinstance(r[1], (int, float))):
            problems.append(f"places[{i}]: must be [\"Name\", km] or [\"Name\", km, \"seat town\"]")
            continue
        if r[1] < 0 or r[1] > radius:
            problems.append(f"places[{i}]: {r[0]} is {r[1]} km away, outside the {radius} km radius")
            continue
        places.append({"name": r[0].strip(), "km": int(round(r[1])), "seat": (r[2] if len(r) > 2 else "") or ""})
    if not places:
        problems.append("no valid place")
        return None, problems
    city = cfg["city"]["name"]
    return compact({"source": "model", "state": "", "places": places}, radius, city), problems


# ---------------------------------------------------------------- matching titles (used by collect.py)

def matcher(region, city_name=""):
    """Prepared list for places_in(): longest names first, so "Porto Feliz" wins over "Feliz"."""
    rows = []
    for r in (region or {}).get("places") or []:
        if not (isinstance(r, list) and len(r) >= 2):
            continue
        f = fold(r[0])
        if len(f) < 3:
            continue
        rows.append((f, r[0], r[1], r[2] if len(r) > 2 else "", r[3] if len(r) > 3 else ""))
    rows.sort(key=lambda x: -len(x[0]))
    return {"rows": rows, "city": fold(city_name), "state": (region or {}).get("state") or ""}


def _spans(title):
    """Folded words of the title with whether each started with a capital letter in the original."""
    out = []
    for m in re.finditer(r"[^\W_]+", title or ""):
        w = fold(m.group(0))
        if w:
            out.append((w, m.group(0)[:1].isupper()))
    return out


def places_in(title, prepared):
    """Places of the list named in the title: [(name, km, seat, state)], nearest first. A place
    counts only when written as a name (capitalized), so "salto" in "sapato de salto alto"
    never counts as the town of Salto."""
    toks = _spans(title)
    words = [w for w, _ in toks]
    taken = [False] * len(words)
    found = []
    for f, name, dist, seat, uf in prepared["rows"]:
        parts = f.split()
        n = len(parts)
        for i in range(len(words) - n + 1):
            if words[i:i + n] == parts and not any(taken[i:i + n]) and toks[i][1]:
                for j in range(i, i + n):
                    taken[j] = True
                found.append((name, dist, seat, uf))
                break
    return sorted(found, key=lambda x: x[1])


def other_state(title, prepared):
    """A Brazilian state other than the reader's named in the title ("… em Salto (RN)",
    "Extremoz, no Rio Grande do Norte"): then a place of the list is probably its namesake there."""
    home = prepared.get("state") or ""
    if home not in BR_STATES or all_caps(title):
        return ""
    for uf, name in BR_STATES.items():
        if uf == home:
            continue
        if re.search(rf"(?:\(|\s[-–—/,]\s?|/){uf}\b|\b{uf}\)|\s{uf}$", title or ""):
            return uf
        # The state's name as written (capital and accent): "Pará" is the state, "para" is a
        # preposition, and "Pará de Minas" is a town.
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)(?! de Minas)", title or ""):
            return uf
    return ""


def all_caps(txt):
    letters = [c for c in txt or "" if c.isalpha()]
    return len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.8


# ---------------------------------------------------------------- command line

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--radius", type=int)
    ap.add_argument("--from-model", metavar="FILE", help="a list the model wrote, when no source answered")
    ap.add_argument("--write", action="store_true", help="save the list into the config file")
    a = ap.parse_args()
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    if a.from_model:
        region, notes = from_model(a.from_model, cfg, a.radius)
        for n in notes:
            print("SKIPPED:", n)
    else:
        region, notes = build(cfg, a.radius)
    if not region:
        print("REGION: no source answered (" + "; ".join(notes) + "). Write WORK/region.json with the towns, "
              "villages and districts within the radius, as {\"radius_km\": 60, \"places\": [[\"Town\", 25], "
              "[\"District\", 8, \"its town\"]]}, then run this script again with --from-model WORK/region.json --write.")
        return 1
    cut = f" (the {region['cut']} farthest left out)" if region.get("cut") else ""
    print(f"REGION: {len(region['places'])} places within {region['radius_km']} km of {region['city']} "
          f"from {region['source']}{cut}; nearest: " + ", ".join(f"{p[0]} {p[1]} km" for p in region["places"][:6]))
    if a.write:
        cfg["region"] = region
        with open(a.config, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        # assemble.py sees this and publishes config.json with today's files.
        with open(os.path.join(os.path.dirname(os.path.abspath(a.config)), "config.changed"), "w") as f:
            f.write(region["built"])
        print("Saved in", a.config, "(assemble.py publishes it with today's edition)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
