#!/usr/bin/env python3
"""Generate data/airport_tz.json — a COMPLETE IATA → IANA-timezone map.

The UI computes a flight's TRUE cross-timezone duration from the two airports'
time zones (CDG 20:30 → JFK 22:40 is 8h10m, not 2h10m). That needs an
IATA→timezone lookup. Rather than hand-maintain one airport at a time (the old
inline AIRPORT_TZ table, which silently omitted a duration whenever a new code
appeared), this fetches the public OpenFlights airport database once and writes
the full table to data/airport_tz.json. index.html loads it at startup and
merges it UNDER the small curated AIRPORT_TZ overrides (curated entries win), so
any real airport you ever type just works.

Also writes data/airport_geo.json (IATA -> [lat, lng]) from the same dataset —
exact airport-code coordinates for the map's flight endpoints (loadAirportGeo).

Re-runnable; safe to re-run anytime to refresh.

    python3 import/airport_tz.py            # fetch + write data/airport_tz.json
    python3 import/airport_tz.py --check    # report counts only, don't write
"""
import csv, io, json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT  = os.path.join(ROOT, "data", "airport_tz.json")
OUT_GEO = os.path.join(ROOT, "data", "airport_geo.json")   # IATA -> [lat, lng], for EXACT airport-code geocoding (no fuzzy text search → no "BBK"→Bangkok)

# OpenFlights airports.dat — the canonical open dataset (one row per airport, no header).
# Columns: 0 id, 1 name, 2 city, 3 country, 4 IATA, 5 ICAO, 6 lat, 7 lng, 8 alt,
#          9 utc-offset, 10 dst, 11 Olson tz, 12 type, 13 source
URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
IATA_COL, TZ_COL, LAT_COL, LNG_COL = 4, 11, 6, 7

try:
    from zoneinfo import ZoneInfo, available_timezones
    _ZONES = available_timezones()
except Exception:                      # zoneinfo/tzdata unavailable → accept any plausible "Area/City"
    _ZONES = None


def valid_tz(tz):
    if _ZONES is not None:
        return tz in _ZONES
    return "/" in tz


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "travel-planner/1.0 (airport-tz)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main():
    check = "--check" in sys.argv
    raw = fetch(URL)
    out, geo, skipped_tz, dupes = {}, {}, 0, 0
    for row in csv.reader(io.StringIO(raw)):
        if len(row) <= TZ_COL:
            continue
        iata = (row[IATA_COL] or "").strip().strip('"').upper()
        tz   = (row[TZ_COL]   or "").strip().strip('"')
        if not (len(iata) == 3 and iata.isalpha()):
            continue
        if iata not in geo:            # exact coords for this IATA (first occurrence wins) — independent of tz
            try:
                geo[iata] = [round(float(row[LAT_COL]), 4), round(float(row[LNG_COL]), 4)]
            except Exception:
                pass
        if not tz or tz == r"\N":
            continue
        if not valid_tz(tz):
            skipped_tz += 1
            continue
        if iata in out:
            dupes += 1
            continue                   # first occurrence wins (codes are globally unique in practice)
        out[iata] = tz
    out = dict(sorted(out.items()))
    geo = dict(sorted(geo.items()))
    print(f"airports with IATA+tz: {len(out)}  | with coords: {len(geo)}  (skipped bad/unknown tz: {skipped_tz}, dup IATA: {dupes})")
    if check:
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    for path, obj in ((OUT, out), (OUT_GEO, geo)):
        tmp = path + ".tmp"                      # atomic: a mid-write kill must not truncate the live table
        with open(tmp, "w") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
        print(f"wrote {path} ({os.path.getsize(path):,} bytes)")


if __name__ == "__main__":
    main()
