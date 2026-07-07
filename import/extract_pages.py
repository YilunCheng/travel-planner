#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract a trip's day-by-day itinerary from its Apple Pages table into structured
days[] on the trip JSON.

Pipeline (all verified working on this machine):
  .pages --(osascript / Pages)--> .docx --(pandoc -t html)--> <table>
  -> header-driven column mapping -> one day per <tr> -> classify/split cell items
  -> ISO dates (M/D + folder year, with Dec->Jan rollover) -> trip.days[]

Deterministic, no LLM, no network — runs free over all .pages trips. For trips whose
main doc is NOT .pages (xlsx/pdf), use docparse.py + structure.py instead. Falls back
to iwa_reader.py if the Pages export fails.

  python3 import/extract_pages.py                 # all .pages trips
  python3 import/extract_pages.py --id <trip-id>
  python3 import/extract_pages.py --force         # re-extract even if structured
"""

import os, sys, re, subprocess, argparse
from html.parser import HTMLParser
import common as C


# ---------- Pages -> docx -> html ----------
def pages_to_docx(pages_path, out_docx):
    script = (
        'tell application "Pages"\n'
        '  set d to open POSIX file "%s"\n'
        '  export d to POSIX file "%s" as Microsoft Word\n'
        '  close d saving no\n'
        'end tell\n'
    ) % (pages_path.replace('"', '\\"'), out_docx.replace('"', '\\"'))
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(out_docx):
        raise RuntimeError("osascript export failed: " + (r.stderr or "")[:300])
    return out_docx


def docx_to_html(docx):
    r = subprocess.run(["pandoc", "-f", "docx", "-t", "html", docx],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("pandoc failed: " + (r.stderr or "")[:300])
    return r.stdout


# ---------- HTML table parser (cells keep newlines for <br>/<p>) ----------
class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self._t, self._row, self._cell = [], None, None, None
        self._depth_cell = 0
    def handle_starttag(self, tag, attrs):
        if tag == "table": self._t = []
        elif tag == "tr" and self._t is not None: self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []; self._depth_cell = 1
        elif tag in ("br",) and self._cell is not None: self._cell.append("\n")
        elif tag == "p" and self._cell is not None and self._cell: self._cell.append("\n")
    def handle_endtag(self, tag):
        if tag == "table" and self._t is not None:
            self.tables.append(self._t); self._t = None
        elif tag == "tr" and self._row is not None:
            self._t.append(self._row); self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(self._cell)).strip()
            self._row.append(text); self._cell = None
    def handle_data(self, data):
        if self._cell is not None: self._cell.append(data)


def best_table(html):
    """Pick the table that most looks like a day-by-day itinerary."""
    p = TableParser(); p.feed(html)
    best, score = None, -1
    for t in p.tables:
        if not t: continue
        date_rows = sum(1 for r in t if r and re.match(r"\s*\d{1,2}\s*/\s*\d{1,2}", r[0]))
        s = date_rows * 10 + len(t)
        if s > score: best, score = t, s
    return best


# ---------- column mapping ----------
HEADER_HINTS = {
    "date": ["date", "day", "日期"],
    "stay": ["stay", "hotel", "where", "lodging", "accommodation", "住", "酒店"],
    "plan": ["plan", "itinerary", "activities", "activity", "schedule", "行程", "活动"],
    "flight": ["flight", "transfer", "transport", "航班", "交通"],
}
def map_columns(header):
    cols = {}
    for i, cell in enumerate(header):
        low = (cell or "").strip().lower()
        for key, hints in HEADER_HINTS.items():
            if key not in cols and any(h in low for h in hints):
                cols[key] = i
    return cols


# ---------- item classification ----------
FLIGHT_RE = re.compile(r"\b[A-Z]{3}-[A-Z]{3}\b|\b[A-Z]{2}\d{2,4}\b")
MEAL_RE = re.compile(r"^\s*(lunch|dinner|breakfast|brunch|bar|drinks?|snack|reservation)\b", re.I)
TRANSFER_RE = re.compile(r"^\s*(train|bus|ferry|drive|transfer|fly|boat|car)\b|train:|drive to|train/bus", re.I)
TIME_12 = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.I)


def norm_time(s):
    m = TIME_12.search(s or "")
    if not m: return None
    h = int(m.group(1)); mn = int(m.group(2) or 0); ap = m.group(3).lower()
    if ap == "p" and h != 12: h += 12
    if ap == "a" and h == 12: h = 0
    if h > 23 or mn > 59: return None
    return "%02d:%02d" % (h, mn)


def classify(text):
    t = text.strip()
    if MEAL_RE.search(t): return "meal"
    if FLIGHT_RE.search(t): return "flight"
    if TRANSFER_RE.search(t): return "transfer"
    return "activity"


def split_items(cell):
    out = []
    for line in (cell or "").split("\n"):
        line = line.strip()
        if not line: continue
        typ = classify(line)
        # flights carry their own dep-arr times in the text; a single time badge
        # would ambiguously show one of them, so leave it blank for flights.
        tm = None if typ == "flight" else norm_time(line)
        out.append({"id": C.slug(line)[:6] + "-" + str(len(out)),
                    "type": typ, "text": line, "time": tm})
    return out


# ---------- dates ----------
def build_dates(rows, date_col, start_year):
    """Return list of (iso_or_'', leftover_text) aligned to rows, rolling the year
    when month decreases (Dec -> Jan)."""
    out = []
    year = int(start_year); prev_m = None
    for r in rows:
        cell = r[date_col] if date_col < len(r) else ""
        m = re.match(r"\s*(\d{1,2})\s*/\s*(\d{1,2})", cell or "")
        iso, leftover = "", (cell or "").strip()
        if m:
            mo, da = int(m.group(1)), int(m.group(2))
            if prev_m is not None and mo < prev_m - 1:   # wrapped to next year
                year += 1
            prev_m = mo
            try:
                iso = "%04d-%02d-%02d" % (year, mo, da)
            except Exception:
                iso = ""
            leftover = (cell[m.end():]).strip("  \t")
            # drop a leading weekday token
            leftover = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b[\s,]*", "", leftover, flags=re.I)
        out.append((iso, leftover))
    return out


# ---------- main extraction ----------
def extract_trip(trip, force=False):
    pages = trip.get("sourcePages")
    if not pages or not pages.lower().endswith(".pages"):
        return False, "not a .pages trip"
    if trip.get("structured") and not force:
        return False, "already structured (use --force)"

    docx = os.path.join(C.DOCX_DIR, trip["id"] + ".docx")
    try:
        if os.path.exists(docx): os.remove(docx)
        pages_to_docx(pages, docx)
        html = docx_to_html(docx)
    except Exception as e:
        return _fallback_iwa(trip, str(e))

    table = best_table(html)
    if not table or len(table) < 2:
        return _fallback_iwa(trip, "no itinerary table found")

    # header detection
    cols = map_columns(table[0])
    if "date" in cols:
        body = table[1:]
    else:
        cols = {"date": 0, "plan": 1, "stay": 2}   # observed default layout
        body = table  # no header row

    date_col = cols.get("date", 0)
    stay_col = cols.get("stay")
    plan_cols = [c for k, c in cols.items() if k in ("plan", "flight")]
    if not plan_cols:
        plan_cols = [i for i in range(len(table[0])) if i not in (date_col, stay_col)]

    # year seed comes from the FOLDER-derived id (YYYY-MM-slug), like structure.py — never from
    # startDate, which this function itself overwrites (and which may hold a prior bad value)
    yr = trip["id"][:4] if re.match(r"\d{4}-\d{2}", trip.get("id") or "") else (trip.get("startDate") or "")[:4]
    dates = build_dates(body, date_col, yr)
    days, raw_lines = [], ["Date | Stay | Plan"]
    for idx, row in enumerate(body):
        iso, leftover = dates[idx]
        stay = (row[stay_col].strip() if (stay_col is not None and stay_col < len(row)) else "")
        items = []
        if leftover:   # extra text left in the date cell (e.g. "Day 2 Embarkation")
            items += split_items(leftover)
        for c in plan_cols:
            if c < len(row) and row[c].strip():
                items += split_items(row[c])
        # skip fully-empty rows
        if not iso and not stay and not items:
            continue
        label = ""
        mld = re.match(r"\s*(\d{1,2})\s*/\s*(\d{1,2})", row[date_col] if date_col < len(row) else "")
        if mld: label = "%d/%d" % (int(mld.group(1)), int(mld.group(2)))
        days.append({"id": "d%d" % (len(days) + 1), "date": iso, "label": label,
                     "stay": stay, "items": items})
        raw_lines.append("%s | %s | %s" % (label or iso, stay, " / ".join(i["text"] for i in items)))

    if not days:
        return _fallback_iwa(trip, "table had no day rows")

    # trailing prose after the table -> trip notes
    notes = []
    after = html.split("</table>", 1)
    if len(after) > 1:
        for m in re.finditer(r"<p>(.*?)</p>", after[1], re.S):
            txt = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if txt: notes.append(txt)

    # write raw text (viewable fallback / search source)
    raw_path = os.path.join(C.RAW_DIR, trip["id"] + ".txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write("\n".join(raw_lines))
        if notes: f.write("\n\nNotes:\n- " + "\n- ".join(notes))

    # refine trip dates from parsed days
    isos = [d["date"] for d in days if d["date"]]
    trip["days"] = days
    trip["notes"] = notes
    trip["structured"] = True
    trip["rawTextRef"] = "raw/" + trip["id"] + ".txt"
    if isos:
        trip["startDate"] = min(isos); trip["endDate"] = max(isos)
    C.save_trip(trip)
    return True, "%d days, %d items" % (len(days), sum(len(d["items"]) for d in days))


def _fallback_iwa(trip, why):
    """Last resort: recover flat cell text via the stdlib IWA reader -> raw text only."""
    try:
        import iwa_reader
        text = iwa_reader.extract_text(trip["sourcePages"])
    except Exception as e:
        return False, "%s; iwa fallback failed: %s" % (why, e)
    if not text.strip():
        return False, why + "; iwa produced no text"
    raw_path = os.path.join(C.RAW_DIR, trip["id"] + ".txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(text)
    trip["rawTextRef"] = "raw/" + trip["id"] + ".txt"
    C.save_trip(trip)
    return False, why + " -> saved raw text via IWA fallback"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    C.ensure_dirs()
    trips = [C.load_trip(args.id)] if args.id else C.all_trips()
    trips = [t for t in trips if t]
    ok = 0
    for t in sorted(trips, key=lambda x: x["id"]):
        if not (t.get("sourcePages") or "").lower().endswith(".pages"):
            continue
        try:
            good, msg = extract_trip(t, force=args.force)
        except Exception as e:
            good, msg = False, "ERROR: " + str(e)[:200]
        ok += 1 if good else 0
        print("  %-28s %s %s" % (t["id"], "✓" if good else "·", msg))
    C.rebuild_index()
    print("\nExtracted %d trips." % ok)


if __name__ == "__main__":
    main()
