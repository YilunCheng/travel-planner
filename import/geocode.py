#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geocode a trip's places so the per-trip map can plot them.

Two stages:
  1) `claude -p` turns the messy day items into clean geocodable queries — it knows
     "Central" is a restaurant in Lima, "Goðafoss" a waterfall in Iceland, "Mombo" a
     camp in the Okavango — and returns {places:[{day,label,type,query}]}, dropping
     non-geographic lines (free day, rebook, packing).
  2) Nominatim (OpenStreetMap, free, no key) resolves each unique query -> lat/lng,
     cached in data/geocache.json (1 req/sec per their usage policy).

Result is written to trip["places"] = [{day,label,type,lat,lng}], consumed by the
per-trip map (Google or Leaflet) in index.html.

  python3 import/geocode.py --id <trip-id>
  python3 import/geocode.py --all          # all structured trips lacking places
  python3 import/geocode.py --all --force  # re-geocode everything
"""

import os, sys, json, re, time, argparse, urllib.request, urllib.parse
import common as C
import structure  # reuse ask_claude / parse_json

GEOCACHE = os.path.join(C.DATA_DIR, "geocache.json")
UA = "travel-planner-local/1.0 (personal itinerary tool)"
MAX_PLACES = 40

EXTRACT_PROMPT = """From this trip itinerary, extract the physical places to plot on a map. Output ONLY JSON, no prose.

Trip: {title}
Days (numbered):
{days}

Output: {{"places":[{{"day":<day number above>,"label":"short readable name","type":"stay|activity|meal|other","query":"Specific Place, City, Country"}}]}}

Rules:
- One entry per distinct real-world place (hotel/lodge/ship port, sight/museum/park, restaurant, airport if notable). Use your own knowledge to add the correct city + country and fix names so OpenStreetMap can find it (e.g. "Central" -> "Central Restaurante, Lima, Peru"; "Goðafoss" -> "Goðafoss, Iceland"; "Mombo" -> "Mombo Camp, Okavango Delta, Botswana").
- "day" = the day number it belongs to. "label" = concise (no times/codes).
- SKIP non-places: flights between airports written as codes, "free day", "rebook", packing, generic "day tour" with no named place.
- At most {maxp} places, prioritizing stays and named sights. Keep chronological order."""


def load_cache():
    return C.read_json(GEOCACHE, {}) or {}


def save_cache(cache):
    C.write_json(GEOCACHE, cache)


def nominatim(query, cache):
    if query in cache:
        return cache[query]
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        time.sleep(1.1)  # be polite — Nominatim allows ~1 req/sec
        result = [float(data[0]["lat"]), float(data[0]["lon"])] if data else None
    except Exception:
        return None       # transient network failure — do NOT cache, so the query retries next run
    cache[query] = result  # cache hits AND genuine misses (delete an entry in geocache.json to retry)
    return result


def days_text(trip):
    lines = []
    for i, d in enumerate(trip.get("days") or [], 1):
        bits = []
        st = C.stay_text(d.get("stay"))               # day.stay may be an item object now — read its text
        if st: bits.append("stay=" + st)
        for it in d.get("items") or []:
            bits.append(it.get("text", ""))
        lines.append("%d. %s" % (i, " | ".join(b for b in bits if b)))
    return "\n".join(lines)[:9000]


def geocode_trip(trip, cache, force=False):
    if not trip.get("days"):
        return False, "no days", {}
    if trip.get("places") and not force:
        return False, "already has places (use --force)", {}
    prompt = EXTRACT_PROMPT.format(title=trip.get("title", ""), days=days_text(trip), maxp=MAX_PLACES)
    out = structure.ask_claude(prompt)
    data = structure.parse_json(out)
    raw = (data.get("places") or [])[:MAX_PLACES]
    places, unresolved = [], []
    for p in raw:
        q = (p.get("query") or "").strip()
        if not q:
            continue
        try:
            di = int(p.get("day") or 0)
        except Exception:
            di = 0
        coord = nominatim(q, cache)
        if not coord:
            # keep WHICH queries Nominatim couldn't resolve so the UI can retry them (Google Places) instead of silently dropping
            unresolved.append({"day": di, "label": p.get("label") or q,
                               "query": q, "type": p.get("type") or "other"})
            continue
        places.append({"day": di, "label": p.get("label") or q,
                       "type": p.get("type") or "other",
                       "lat": coord[0], "lng": coord[1]})
    if not places and trip.get("places"):
        # a run that resolved NOTHING (Nominatim down / rate-limited) must not wipe an existing map
        return False, "0 resolved — kept the existing %d places" % len(trip["places"]), \
            {"placed": len(trip["places"]), "unresolved": unresolved}
    trip["places"] = places
    C.save_trip(trip)
    info = {"placed": len(places), "unresolved": unresolved}
    return True, "%d placed, %d unresolved" % (len(places), len(unresolved)), info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    C.ensure_dirs()
    if args.id:
        trips = [C.load_trip(args.id)]
    elif args.all:
        trips = [t for t in C.all_trips()
                 if t and t.get("structured") and t.get("days")
                 and (args.force or not t.get("places")) and not t.get("archive")]
    else:
        print("specify --id or --all"); return
    cache = load_cache()
    try:
        for t in sorted([x for x in trips if x], key=lambda x: x["id"]):
            info = {}
            try:
                ok, msg, info = geocode_trip(t, cache, force=args.force)
            except Exception as e:
                ok, msg = False, "ERROR " + str(e)[:160]
            print("  %-30s %s %s" % (t["id"], "✓" if ok else "·", msg), flush=True)
            if args.id and info:   # single-trip run (the server path): emit machine-readable unresolved list for the UI
                print("__GEOCODE__" + json.dumps(info, ensure_ascii=False), flush=True)
            save_cache(cache)   # persist after each trip so a crash keeps progress
    finally:
        save_cache(cache)


if __name__ == "__main__":
    main()
