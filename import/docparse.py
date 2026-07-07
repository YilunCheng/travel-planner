#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract plain text from non-.pages itinerary docs (pdf / docx / doc / xlsx / xls)
so older trips are at least viewable and searchable. Used for trips whose main
itinerary isn't an Apple Pages table.

  pdf      -> pdftotext -layout   (poppler, installed)
  docx/doc -> textutil            (built-in; preserves CJK), pandoc fallback
  xlsx     -> stdlib zipfile + XML (no openpyxl needed)

  python3 import/docparse.py            # raw-text every unstructured non-archive trip
  python3 import/docparse.py --id <trip-id>
"""

import os, sys, re, subprocess, zipfile, argparse
import xml.etree.ElementTree as ET
import common as C


def pdf_to_text(path):
    try:
        r = subprocess.run(["pdftotext", "-layout", path, "-"],
                           capture_output=True, text=True, timeout=120)
        return r.stdout or ""
    except Exception:
        return ""


def docx_to_text(path):
    for cmd in (["textutil", "-convert", "txt", "-stdout", path],
                ["pandoc", "-f", "docx", "-t", "plain", path]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except Exception:
            pass
    return ""


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
def xlsx_to_text(path):
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return ""
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    out = []
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n))
    for sh in sheets:
        root = ET.fromstring(z.read(sh))
        for row in root.iter(_NS + "row"):
            cells = []
            for c in row.iter(_NS + "c"):
                v = c.find(_NS + "v")
                if v is None or v.text is None:
                    continue
                if c.get("t") == "s":
                    try: cells.append(shared[int(v.text)])
                    except Exception: cells.append(v.text)
                else:
                    cells.append(v.text)
            if cells:
                out.append("  |  ".join(cells))
    return "\n".join(out)


def extract_text(path):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext == "pdf":  return pdf_to_text(path)
    if ext in ("docx", "doc"): return docx_to_text(path)
    if ext in ("xlsx", "xls"): return xlsx_to_text(path)
    return ""


def find_itinerary_doc(trip):
    """Best itinerary source for a trip with no .pages: sourceMain, else an
    itinerary-categorized / itinerary-named document, else None (e.g. visa folders)."""
    main = trip.get("sourceMain")
    if main and os.path.splitext(main)[1].lstrip(".").lower() in ("pdf", "docx", "doc", "xlsx", "xls"):
        return main
    docs = trip.get("documents") or []
    for d in docs:
        if d.get("category") == "Itinerary":
            return d["path"]
    for d in docs:
        if ("itinerary" in d["name"].lower() or "行程" in d["name"]) and d.get("ext") in ("pdf", "docx", "doc", "xlsx", "xls"):
            return d["path"]
    return None


def rawtext_trip(trip):
    src = find_itinerary_doc(trip)
    if not src:
        return False, "no itinerary doc (documents-only trip)"
    text = extract_text(src)
    if not text.strip():
        return False, "extraction empty for " + os.path.basename(src)
    raw_path = os.path.join(C.RAW_DIR, trip["id"] + ".txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(text)
    trip["rawTextRef"] = "raw/" + trip["id"] + ".txt"
    trip["sourceItinDoc"] = src
    C.save_trip(trip)
    return True, "raw text from " + os.path.basename(src) + " (%d chars)" % len(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    args = ap.parse_args()
    C.ensure_dirs()
    trips = [C.load_trip(args.id)] if args.id else C.all_trips()
    for t in sorted([x for x in trips if x], key=lambda x: x["id"]):
        if t.get("structured") or t.get("archive"):
            continue
        try:
            ok, msg = rawtext_trip(t)
        except Exception as e:
            ok, msg = False, "ERROR " + str(e)[:160]
        print("  %-30s %s %s" % (t["id"], "✓" if ok else "·", msg))
    C.rebuild_index()


if __name__ == "__main__":
    main()
