#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Structure messy extracted itinerary text into days[] JSON via the local `claude`
CLI — the same already-authenticated headless tool the daily-magazine uses (no API
key, no separate billing). Used for trips whose itinerary is NOT an Apple Pages
table (older PDF/DOCX/XLSX trips) after docparse.py has produced raw text.

  python3 import/structure.py --id <trip-id>
  python3 import/structure.py --all-raw          # every unstructured trip that has raw text
"""

import os, sys, json, re, subprocess, argparse
import common as C

CLAUDE = C.CLAUDE_BIN    # $CLAUDE_BIN env → `claude` on PATH → ~/.local/bin/claude (see common.py)
MODEL = C.CLAUDE_MODEL   # $CLAUDE_MODEL env, default claude-opus-4-8
EFFORT = "low"          # mechanical extraction — low effort is plenty and fast
TIMEOUT = 240

PROMPT = """You convert a traveler's raw itinerary text into strict JSON. Output ONLY a JSON object, no markdown fences, no commentary.

Trip: {title}
The trip takes place starting around {ym} (year-month from the trip's folder). The raw text was extracted from a personal document (PDF/Word/Excel/notes export) and may be messy: tables flattened into lines, columns merged, OCR-ish artifacts, things out of order.

People record trips in very different ways — read ANY style and reconstruct the day-by-day plan:
- day-by-day tables or lists (dated, numbered "Day 1/2/3", weekday-labeled, or with no dates at all)
- free-form prose / diary-style paragraphs describing the plan
- a stack of booking confirmations (flights, hotels, trains, rental cars, tours) in any order
- spreadsheet exports, bullet notes, copy-pasted emails, mixed languages, mixed 12h/24h time formats
- itinerary mixed with non-itinerary material (packing lists, budgets, visa notes, contacts)

Output schema:
{{"days":[{{"date":"YYYY-MM-DD","label":"M/D","stay":"hotel/lodge/ship you sleep at that night, or empty","items":[{{"type":"flight|train|transfer|cruise|hotel|activity|meal|note","text":"...","time":"HH:MM 24h or null"}}]}}]}}

Rules:
- One object per calendar day, in chronological order. Anchor dates by precedence: explicit dates in the text win; else follow day numbers / weekday sequences; else assume the trip starts in {ym} and advance one day at a time (roll into the next month/year as days advance). If the source is unordered (e.g. a pile of bookings), sort everything onto the right day.
- "stay" = where they sleep that night (hotel/lodge/resort/cruise ship/camp/friend's place). Repeat it for each night of a multi-night stay. Do not also duplicate it as an item.
- Classify each item with the closest type: flight (airline codes/flight numbers/airport pairs like JFK-CDG), train (rail journeys), transfer (drive/taxi/bus/shuttle/ferry between places), cruise (boat legs), hotel (check-in/out notes), meal (breakfast/lunch/dinner/bar/restaurant reservations), activity (tours, sights, hikes, shows, tickets), note (anything else worth keeping: confirmation numbers, reminders, freeform remarks). When unsure, prefer note over guessing.
- Keep flight codes, times and "+1" next-day markers verbatim inside text. For flights set time to null (the text holds dep/arr). For other items set time to the start time in 24h when present, else null.
- Preserve the traveler's original spelling and language — never translate or "correct" place names; keep any Chinese text as-is.
- Drop pure noise (page headers, repeated table headers, page numbers), but keep genuinely useful loose info as note items on the day it belongs to. If the text has no day structure at all (only bookings or a flat list), still produce the best day-by-day plan you can infer.

RAW TEXT:
---
{raw}
---
JSON:"""


def ask_claude(prompt):
    env = dict(os.environ)
    env["PATH"] = C.TOOL_PATH
    r = subprocess.run(
        [CLAUDE, "-p", prompt, "--model", MODEL, "--effort", EFFORT,
         "--permission-mode", "bypassPermissions", "--output-format", "text"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError((r.stderr or "claude failed")[:300])
    return r.stdout.strip()


def parse_json(s):
    s = s.strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    return json.loads(s)


def structure_trip(trip, force=False):
    if trip.get("structured") and not force:   # don't silently replace edited days (mirror extract_pages)
        return False, "already structured (use --force)"
    ref = trip.get("rawTextRef")
    if not ref:
        return False, "no raw text"
    raw = open(os.path.join(C.DATA_DIR, ref), encoding="utf-8").read()
    if len(raw) > 14000:
        raw = raw[:14000]
    # month comes from the FOLDER (the id is YYYY-MM-slug), never from startDate —
    # startDate may already hold a previous (wrong) structuring result.
    ym = trip["id"][:7] if re.match(r"\d{4}-\d{2}", trip["id"]) else trip["startDate"][:7]
    prompt = PROMPT.format(title=trip.get("title", ""), ym=ym, raw=raw)
    out = ask_claude(prompt)
    data = parse_json(out)
    days = data.get("days") or []
    if not days:
        return False, "claude returned no days"
    # assign stable ids + normalize
    for di, d in enumerate(days, 1):
        d["id"] = "d%d" % di
        d.setdefault("label", "")
        d.setdefault("stay", "")
        items = d.get("items") or []
        for ii, it in enumerate(items):
            it["id"] = "i%d-%d" % (di, ii)
            it.setdefault("type", "note")
            if it.get("time") in ("", "null", None):
                it["time"] = None
        d["items"] = items
    trip["days"] = days
    trip["structured"] = True
    isos = [d["date"] for d in days if re.match(r"\d{4}-\d{2}-\d{2}", d.get("date") or "")]
    if isos:
        trip["startDate"], trip["endDate"] = min(isos), max(isos)
    C.save_trip(trip)
    return True, "%d days, %d items" % (len(days), sum(len(d.get("items") or []) for d in days))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--all-raw", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-structure even if the trip already has days")
    args = ap.parse_args()
    C.ensure_dirs()
    if args.id:
        trips = [C.load_trip(args.id)]
    elif args.all_raw:
        trips = [t for t in C.all_trips()
                 if t and not t.get("structured") and t.get("rawTextRef") and not t.get("archive")]
    else:
        print("specify --id or --all-raw"); return
    for t in sorted([x for x in trips if x], key=lambda x: x["id"]):
        try:
            ok, msg = structure_trip(t, force=args.force)
        except Exception as e:
            ok, msg = False, "ERROR " + str(e)[:200]
        print("  %-30s %s %s" % (t["id"], "✓" if ok else "·", msg))
    C.rebuild_index()


if __name__ == "__main__":
    main()
