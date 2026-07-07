#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scan ~/Documents/Travel Plan and (re)build the per-trip JSON files + index.

For each entry it parses the `YYYY:MM Title` name, classifies single-doc vs folder
trip BY THE NAME'S EXTENSION (Stockholm/Cancun .pages are directories — never use
isdir for this), locates the main itinerary, inventories supporting documents, and
copies the .pages preview.jpg as a cover.

Merge-safe: if a trip JSON already exists it PRESERVES days / structured / edits and
only refreshes scan-derived fields (documents, cover, source paths). So re-running is
safe and never clobbers your edits. Use --force to also reset metadata to the folder.

Usage:
  python3 import/scan.py            # scan all, merge-safe
  python3 import/scan.py --id <trip-id>   # one trip
  python3 import/scan.py --force    # also refresh title/dates/status from folder name
"""

import os, sys, shutil, subprocess, argparse
import common as C


def find_main_itinerary(folder, foldername):
    """Return absolute path to the trip's main itinerary doc inside a folder trip."""
    try:
        entries = os.listdir(folder)
    except OSError:
        return None
    pages = [e for e in entries if e.lower().endswith(".pages")]
    # 1) <foldername>.pages (case-insensitive)
    want = foldername.lower() + ".pages"
    for e in pages:
        if e.lower() == want:
            return os.path.join(folder, e)
    # 2) largest .pages at top level
    if pages:
        pages.sort(key=lambda e: _size(os.path.join(folder, e)), reverse=True)
        return os.path.join(folder, pages[0])
    # 3) a doc named like the folder (e.g. "2022:09 France Sicily.pdf")
    for ext in (".pdf", ".docx", ".doc", ".xlsx"):
        cand = os.path.join(folder, foldername + ext)
        if os.path.exists(cand):
            return cand
    # 4) an itinerary-named doc (incl. Chinese 行程)
    for e in entries:
        low = e.lower()
        if ("itinerary" in low or "行程" in e) and low.endswith((".pdf", ".docx", ".doc", ".xlsx")):
            return os.path.join(folder, e)
    return None


def _size(path):
    try:
        if os.path.isdir(path):  # .pages bundle — sum its files
            total = 0
            for r, _, fs in os.walk(path):
                for f in fs:
                    try: total += os.path.getsize(os.path.join(r, f))
                    except OSError: pass
            return total
        return os.path.getsize(path)
    except OSError:
        return 0


def inventory_documents(folder, main_path):
    """Recursively list supporting files in a folder trip (skip bundle internals,
    hidden/.DS_Store, and the main itinerary itself)."""
    docs = []
    main_abs = os.path.abspath(main_path) if main_path else None
    n = 0
    for root, dirs, files in os.walk(folder):
        # don't descend into bundles (.pages/.numbers/.key) or hidden dirs
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and not d.lower().endswith((".pages", ".numbers", ".key", ".app"))]
        for fn in files:
            if fn.startswith(".") or fn == "Icon\r":
                continue
            path = os.path.join(root, fn)
            if main_abs and os.path.abspath(path) == main_abs:
                continue
            ext = os.path.splitext(fn)[1].lstrip(".").lower()
            n += 1
            docs.append({
                "id": "doc%d" % n,
                "name": fn,
                "path": path,                       # absolute, literal ':' preserved
                "rel": os.path.relpath(path, folder),
                "category": C.categorize_doc(fn),
                "ext": ext,
                "size": _size(path),
            })
    # group order: itinerary/tickets/hotels first, media/other last
    order = {c: i for i, c in enumerate(
        ["Itinerary", "E-tickets", "Hotels", "Visa", "Maps", "Invoices",
         "Insurance", "Packing", "Info", "Other", "Media"])}
    docs.sort(key=lambda d: (order.get(d["category"], 99), d["name"].lower()))
    return docs


def make_cover(trip_id, pages_path):
    """Copy the .pages preview.jpg to data/covers/<id>.jpg, downscaled. Returns
    the cover rel path or None."""
    if not pages_path or not pages_path.lower().endswith(".pages"):
        return None
    # .pages may be a flat zip OR an expanded bundle dir; preview.jpg lives at top
    src = None
    if os.path.isdir(pages_path):
        for cand in ("preview.jpg", "preview-web.jpg", "QuickLook/Thumbnail.jpg"):
            p = os.path.join(pages_path, cand)
            if os.path.exists(p):
                src = p; break
    else:
        # flat .pages is a zip — extract preview.jpg
        import zipfile
        try:
            with zipfile.ZipFile(pages_path) as z:
                for cand in ("preview.jpg", "preview-web.jpg", "QuickLook/Thumbnail.jpg"):
                    if cand in z.namelist():
                        dst = os.path.join(C.COVERS_DIR, trip_id + ".jpg")
                        with z.open(cand) as zf, open(dst, "wb") as out:
                            shutil.copyfileobj(zf, out)
                        _resize(dst)
                        return "covers/%s.jpg" % trip_id
        except Exception:
            return None
        return None
    if not src:
        return None
    dst = os.path.join(C.COVERS_DIR, trip_id + ".jpg")
    try:
        shutil.copyfile(src, dst)
        _resize(dst)
        return "covers/%s.jpg" % trip_id
    except Exception:
        return None


def _resize(path):
    try:
        subprocess.run(["sips", "-Z", "1000", path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass


def scan_entry(name, force=False):
    meta = C.parse_trip_name(name)
    if not meta:
        return None
    tid = C.compute_id(meta["year"], meta["month"], meta["title"])
    full = os.path.join(C.TRAVEL_DIR, name)
    is_single = meta["ext"] is not None     # classify by NAME extension, not isdir

    if is_single:
        source_folder = C.TRAVEL_DIR
        main_path = full
        documents = []
    else:
        source_folder = full
        main_path = find_main_itinerary(full, name)
        documents = inventory_documents(full, main_path)

    source_pages = main_path if (main_path and main_path.lower().endswith(".pages")) else None

    existing = C.load_trip(tid) or {}
    # Cover: seed from the .pages preview ONLY when the trip has no usable cover yet — a rescan must
    # never clobber a destination photo / user upload (make_cover overwrites covers/<id>.jpg in place).
    cover = None
    if source_pages and not (existing.get("cover") and os.path.exists(C.cover_path(tid))):
        cover = make_cover(tid, source_pages)

    placeholder_start = "%s-%s-01" % (meta["year"], meta["month"])

    trip = dict(existing)
    trip["schemaVersion"] = C.SCHEMA_VERSION
    trip["id"] = tid
    # metadata: set if missing, or overwrite when --force
    if force or "title" not in trip:    trip["title"] = meta["title"]
    if force or "startDate" not in trip: trip["startDate"] = placeholder_start
    if force or "endDate" not in trip:   trip["endDate"] = existing.get("endDate", placeholder_start)
    if force or "status" not in trip:        # status is UI-editable (cancel/uncancel) — preserve it on a
        trip["status"] = meta["status"]      # merge-safe rescan; only --force re-derives it from the folder name
    trip.setdefault("timezone", None)
    # scan-derived fields: always refresh
    trip["sourceFolder"] = source_folder
    trip["sourcePages"] = source_pages
    trip["sourceMain"] = main_path
    trip["mainExt"] = (os.path.splitext(main_path)[1].lstrip(".").lower() if main_path else meta["ext"])
    trip["documents"] = documents if documents else existing.get("documents", [])
    if cover:
        trip["cover"] = cover
    trip.setdefault("cover", cover)
    trip["archive"] = name in C.ARCHIVE_NAMES
    # preserve structure
    trip.setdefault("structured", False)
    trip.setdefault("days", [])
    trip.setdefault("rawTextRef", None)

    C.save_trip(trip)
    return trip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="only this trip id")
    ap.add_argument("--force", action="store_true", help="refresh metadata from folder name too")
    args = ap.parse_args()

    C.ensure_dirs()
    names = sorted(os.listdir(C.TRAVEL_DIR))
    done = 0
    for name in names:
        if name == ".DS_Store" or name.startswith("."):
            continue
        meta = C.parse_trip_name(name)
        if not meta:
            print("  skip (no date prefix):", name)
            continue
        tid = C.compute_id(meta["year"], meta["month"], meta["title"])
        if args.id and tid != args.id:
            continue
        t = scan_entry(name, force=args.force)
        if t:
            done += 1
            flag = " [archive]" if t.get("archive") else ""
            print("  %-26s %2d docs  %s%s" % (tid, len(t.get("documents") or []),
                                              "single" if meta["ext"] else "folder", flag))
    idx = C.rebuild_index()
    print("\nScanned %d trips -> %s (%d in index)" % (done, C.INDEX_PATH, len(idx)))


if __name__ == "__main__":
    main()
