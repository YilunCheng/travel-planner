# -*- coding: utf-8 -*-
"""
Shared helpers for the Travel Planner import pipeline AND the server.

Both the import/*.py scripts (run by path) and the top-level server.py use this.
The server adds this dir to sys.path and does `import common` (the dir being named
`import` is irrelevant — we import the *module* `common`, never the keyword).

Stdlib only. The original Travel Plan folder is treated as strictly read-only;
everything this writes lands under DATA_DIR.
"""

import json, os, re, time, datetime, tempfile, shutil, math

# ---- paths -----------------------------------------------------------------
# APP_DIR = this checkout (derived from this file's location, works from any clone);
# TRAVEL_DIR = the original documents folder (READ-ONLY source) — `TRAVEL_DIR` env overrides.
TRAVEL_DIR = os.environ.get("TRAVEL_DIR") or os.path.expanduser("~/Documents/Travel Plan")
APP_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPORT_DIR = os.path.join(APP_DIR, "import")
DATA_DIR   = os.path.join(APP_DIR, "data")
TRIPS_DIR  = os.path.join(DATA_DIR, "trips")
RAW_DIR    = os.path.join(DATA_DIR, "raw")
COVERS_DIR = os.path.join(DATA_DIR, "covers")
CACHE_DIR  = os.path.join(APP_DIR, "cache")
DOCX_DIR   = os.path.join(CACHE_DIR, "docx")
INDEX_PATH = os.path.join(DATA_DIR, "trips_index.json")

SCHEMA_VERSION = 1

# Entries in TRAVEL_DIR that are not trips (misfiled archives etc.) — hidden by default.
# Personal data (real folder names), so it lives in data/archive_names.json, not in code
# (missing file ⇒ none).
try:
    with open(os.path.join(DATA_DIR, "archive_names.json"), encoding="utf-8") as _f:
        ARCHIVE_NAMES = set(json.load(_f) or [])
except Exception:
    ARCHIVE_NAMES = set()

ITIN_EXTS = ("pages", "pdf", "xlsx", "xls", "docx", "doc")

# ---- local `claude` CLI (the only LLM; OPTIONAL — every caller degrades without it) ----
# Resolution: $CLAUDE_BIN env → `claude` on PATH → the default install location.
CLAUDE_BIN   = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL") or "claude-opus-4-8"
# Deterministic PATH for every shelled tool (claude, pandoc, pdftotext, headless chrome…),
# independent of how the server/importer was launched.
TOOL_PATH = os.pathsep.join(dict.fromkeys(
    [os.path.dirname(CLAUDE_BIN) or "/usr/bin", os.path.expanduser("~/.local/bin"),
     "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))


def ensure_dirs():
    for d in (TRIPS_DIR, RAW_DIR, COVERS_DIR, DOCX_DIR, os.path.join(CACHE_DIR, "thumbs")):
        os.makedirs(d, exist_ok=True)


# ---- naming ----------------------------------------------------------------
def slug(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "trip"


def parse_trip_name(name):
    """`YYYY:MM Title [(cancelled)][.ext]` -> dict, or None if it doesn't match.
    NOTE: the ':' in the name is a literal byte on disk — never convert it."""
    m = re.match(r"^(\d{4}):(\d{2})\s+(.+)$", name)
    if not m:
        return None
    year, month, rest = m.group(1), m.group(2), m.group(3)
    ext = None
    em = re.search(r"\.(" + "|".join(ITIN_EXTS) + r")$", rest, re.I)
    if em:
        ext = em.group(1).lower()
        rest = rest[: em.start()]
    status = "active"
    cm = re.search(r"\s*\((cancelled)\)\s*$", rest, re.I)
    if cm:
        status = "cancelled"
        rest = rest[: cm.start()]
    title = rest.strip()
    return {"year": year, "month": month, "title": title, "status": status, "ext": ext}


def compute_id(year, month, title):
    return "{}-{}-{}".format(year, month, slug(title))


# ---- document categorization ----------------------------------------------
# (category, [keywords]) — first match wins; keywords matched case-insensitively
# against the filename. CJK terms included for older Chinese-named trips.
_DOC_RULES = [
    ("Media",      [".mp4", ".mov", ".m4v", "slideshow", "voyage slideshow"]),
    ("E-tickets",  ["eticket", "e-ticket", "e ticket", "boarding", "electronicticket",
                    "travel document", "登机", "机票"]),
    ("Hotels",     ["hotel", "confirmation", "reservation", "lodge", "guest", "booking",
                    "resort", "camp", "check-in", "checkin", "酒店", "住宿", "机酒"]),
    ("Visa",       ["visa", "invitation", "appointment", "passport", "ds-160", "ds160",
                    "embassy", "consulate", "签证", "邀请", "护照", "在职", "证明", "银行流水"]),
    ("Maps",       ["map", "piste", "pistenplan", "skimap", "地图", "路线"]),
    ("Invoices",   ["invoice", "receipt", "deposit", "statement", "quotation", "quote",
                    "payment", "balance", "发票", "收据", "报价"]),
    ("Insurance",  ["insurance", "policy", "保险"]),
    ("Itinerary",  ["itinerary", "schedule", "行程", "日程"]),
    ("Packing",    ["what to pack", "packing", "pack list", "打包"]),
    ("Info",       ["faq", "frequently asked", "guide", "information", "info sheet",
                    "experience", "reading list", "wildlife", "须知"]),
]


def categorize_doc(name):
    low = name.lower()
    for cat, kws in _DOC_RULES:
        for kw in kws:
            if kw in low:
                return cat
    return "Other"


# ---- json io (atomic) ------------------------------------------------------
def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Unique temp name (not a fixed ".tmp-<basename>.part") so concurrent writers under the
    # threading server never share one temp file and corrupt the target. Temp lives in CACHE_DIR
    # (same volume as every target, and 404'd by the server) so os.replace stays atomic.
    fd, tmp = tempfile.mkstemp(prefix=".tmp-" + os.path.basename(path) + "-", suffix=".part", dir=CACHE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)   # mkstemp creates 0600; keep the prior 0644 for data/index files
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def trip_path(trip_id):
    return os.path.join(TRIPS_DIR, trip_id + ".json")


def is_safe_trip_id(trip_id):
    """True if trip_id maps to a real path DIRECTLY inside TRIPS_DIR — no '..', no '/', no
    absolute path. Mirrors the realpath gate delete_trip uses, but for writes, so an untrusted
    id can't make save_trip / upload_cover write outside data/. Only blocks traversal — it allows
    any filename characters (incl. CJK), so it never rejects a legitimately-slugged id."""
    if not trip_id or not isinstance(trip_id, str):
        return False
    rp = os.path.realpath(trip_path(trip_id))
    return os.path.dirname(rp) == os.path.realpath(TRIPS_DIR)


def load_trip(trip_id):
    return read_json(trip_path(trip_id))


def _stay_norm(s):
    return re.sub(r"[^a-z0-9一-鿿]+", " ", (s or "").lower()).strip()


def stay_text(stay):
    """A day's stay is an item OBJECT {id,type:'hotel',text,geo}; older data (and mid-import) may still be a
    bare string. Return the hotel text from either shape ('' for none). Use this in every reader of day.stay."""
    if not stay:
        return ""
    if isinstance(stay, dict):
        return (stay.get("text") or "").strip()
    return str(stay).strip()


def normalize_stays(trip):
    """Wrap each day's string stay into the item-object shape the client uses ({id,type:'hotel',text,geo}),
    sharing ONE id across nights with the SAME hotel (so the client's hydrateTrip re-shares them → edit-once).
    Idempotent: existing objects keep their id; empty/None stays are left alone. Mirrors index.html hydrateTrip."""
    by_text = {}
    for d in (trip.get("days") or []):
        s = d.get("stay")
        if s is None or s == "":
            continue
        if isinstance(s, dict):                                   # already an object (client edit / prior normalize)
            s.setdefault("type", "hotel")
            if not isinstance(s.get("geo"), dict):
                s["geo"] = {"q": "", "lat": None, "lng": None}
            if not s.get("id"):
                s["id"] = "stay" + os.urandom(3).hex()
            by_text[_stay_norm(s.get("text", ""))] = s["id"]
            continue
        text = str(s).strip()
        if not text:
            continue
        key = _stay_norm(text)
        sid = by_text.get(key)
        if not sid:
            sid = "stay" + os.urandom(3).hex()
            by_text[key] = sid
        d["stay"] = {"id": sid, "type": "hotel", "text": text, "geo": {"q": "", "lat": None, "lng": None}}
    return trip


def save_trip(trip):
    normalize_stays(trip)                       # Phase 4: emit the stay-as-item object shape so imports land already-converted
    write_json(trip_path(trip["id"]), trip)


def bump_cover_ver(old=0):
    """A strictly-increasing cover cache-buster for the <img>?v= URL. Plain time()-seconds COLLIDES
    when two cover ops land in the same second (a fast re-roll, or a back/forward step), leaving ?v=
    unchanged so the browser keeps showing the stale image. Use ms resolution and force it past the
    previous value, so every cover change is guaranteed to bust the cache."""
    return max(int(time.time() * 1000), int(old or 0) + 1)


# ---- cover history (multi-level back/forward) ------------------------------
# Every distinct cover a trip has shown is kept as its own JPEG (covers/<id>.h<n>.jpg) plus a
# metadata snapshot in trip["coverHistory"]; trip["coverPos"] is the index of the one currently
# live at covers/<id>.jpg. Back/forward just move coverPos and copy that file over the live one, so
# nothing is ever lost — the user can step through ALL past covers and forward again.
COVER_HISTORY_MAX = 40           # safety cap: keep at most this many most-recent covers per trip

def cover_path(trip_id):
    return os.path.join(COVERS_DIR, trip_id + ".jpg")

def _cover_snapshot(trip, src_file):
    """Copy the live cover at src_file into a uniquely-named per-trip history JPEG; return its
    snapshot (the file's rel path + the trip's CURRENT cover metadata). Sequence-numbered so files
    never collide or alias — safe to delete one without affecting another."""
    seq = int(trip.get("coverHistSeq") or 0) + 1
    trip["coverHistSeq"] = seq
    rel = "covers/%s.h%d.jpg" % (trip["id"], seq)
    try:
        shutil.copy2(src_file, os.path.join(DATA_DIR, rel))
    except OSError:
        return None
    return {"f": rel, "source": trip.get("coverSource"), "landmark": trip.get("coverLandmark"),
            "imageUrl": trip.get("coverImageUrl"), "ver": trip.get("coverVer")}

def _cover_unlink(rel):
    """Delete a history JPEG by its rel path, realpath-gated to data/covers (never escapes)."""
    if not rel:
        return
    rp = os.path.realpath(os.path.join(DATA_DIR, rel))
    if rp.startswith(os.path.realpath(COVERS_DIR) + os.sep) and os.path.isfile(rp):
        try: os.remove(rp)
        except OSError: pass

def cover_history_seed(trip):
    """Capture the trip's CURRENT live cover as history[0] when there's no history yet — so the
    original / pre-existing cover stays a go-back target once re-rolls begin. Also clears the legacy
    single-level backup this replaced."""
    if trip.get("coverHistory"):
        return
    trip.pop("coverPrev", None)
    _cover_unlink("covers/%s.prev.jpg" % trip["id"])
    live = cover_path(trip["id"])
    if not os.path.exists(live):
        trip["coverHistory"] = []; trip["coverPos"] = -1; return
    snap = _cover_snapshot(trip, live)
    trip["coverHistory"] = [snap] if snap else []
    trip["coverPos"] = 0 if snap else -1

def cover_history_add(trip, src_file):
    """Append the CURRENT cover (src_file + the trip's current meta) as the newest entry and point
    coverPos at it. Drops any redo branch (entries after coverPos) and caps total length."""
    hist = list(trip.get("coverHistory") or [])
    pos = trip.get("coverPos", len(hist) - 1)
    if 0 <= pos < len(hist) - 1:                      # new cover made from a backed-up position
        for e in hist[pos + 1:]:                      # the redo branch is now unreachable
            _cover_unlink(e.get("f"))
        hist = hist[:pos + 1]
    snap = _cover_snapshot(trip, src_file)
    if snap:
        hist.append(snap)
    if len(hist) > COVER_HISTORY_MAX:                 # keep only the most-recent COVER_HISTORY_MAX
        for e in hist[:len(hist) - COVER_HISTORY_MAX]:
            _cover_unlink(e.get("f"))
        hist = hist[-COVER_HISTORY_MAX:]
    trip["coverHistory"] = hist
    trip["coverPos"] = len(hist) - 1

def cover_history_go(trip, delta):
    """Step coverPos by delta (back<0 / forward>0): copy that cover's stored image over the live
    cover, restore its metadata, bump the version. Returns the target snapshot, or None if the move
    isn't possible (no history, or already at the end)."""
    hist = trip.get("coverHistory") or []
    if len(hist) < 2:
        return None
    pos = trip.get("coverPos", len(hist) - 1)
    npos = max(0, min(len(hist) - 1, pos + int(delta)))
    if npos == pos:
        return None
    e = hist[npos]
    src = os.path.join(DATA_DIR, e.get("f", ""))
    if not os.path.isfile(src):
        return None
    try:
        shutil.copy2(src, cover_path(trip["id"]))
    except OSError:
        return None
    trip["coverSource"] = e.get("source") or "wikipedia"
    trip["coverLandmark"] = e.get("landmark")
    trip["coverImageUrl"] = e.get("imageUrl")
    trip["cover"] = "covers/%s.jpg" % trip["id"]
    trip["coverPos"] = npos
    trip["coverVer"] = bump_cover_ver(trip.get("coverVer"))
    return e

def cover_history_reset(trip, src_file):
    """Forget history and start over with a single entry (src_file = the new live cover). Used by the
    batch / first-time cover so a forced refetch doesn't accumulate stale entries."""
    for e in (trip.get("coverHistory") or []):
        _cover_unlink(e.get("f"))
    trip.pop("coverPrev", None)
    _cover_unlink("covers/%s.prev.jpg" % trip["id"])
    snap = _cover_snapshot(trip, src_file)            # coverHistSeq keeps climbing → unique filename
    trip["coverHistory"] = [snap] if snap else []
    trip["coverPos"] = 0 if snap else -1

def cover_history_flags(trip):
    """(can_go_back, can_go_forward) for the trip's current history position."""
    hist = trip.get("coverHistory") or []
    pos = trip.get("coverPos", len(hist) - 1)
    return (pos > 0, pos < len(hist) - 1)


def delete_trip(trip_id):
    """Remove a trip's files (json + cover + raw text) — ONLY ever under DATA_DIR.
    The read-only `~/Documents/Travel Plan` source is never touched. Each candidate path
    is realpath-checked to be inside DATA_DIR before unlinking, so a crafted id with `../`
    can't escape. Returns the list of removed paths (relative to DATA_DIR)."""
    t = read_json(trip_path(trip_id)) or {}
    candidates = [
        trip_path(trip_id),
        os.path.join(COVERS_DIR, trip_id + ".jpg"),
        os.path.join(COVERS_DIR, trip_id + ".prev.jpg"),   # legacy single-level revert backup
        os.path.join(RAW_DIR, trip_id + ".txt"),
    ]
    if os.path.isdir(COVERS_DIR):                           # every cover-history file: covers/<id>.h<n>.jpg
        candidates += [os.path.join(COVERS_DIR, fn) for fn in os.listdir(COVERS_DIR)
                       if fn.startswith(trip_id + ".h") and fn.endswith(".jpg")]
    # Field-derived paths (cover / rawTextRef) are followed ONLY when the file belongs to THIS trip
    # (basename starts with "<trip_id>."): a trip whose fields point at another trip's files — e.g. a
    # copied JSON that kept the original's rawTextRef — must not delete that other trip's data.
    for field in ("cover", "rawTextRef"):
        v = t.get(field)
        if v and os.path.basename(v).startswith(trip_id + "."):
            candidates.append(os.path.join(DATA_DIR, v))
    data_root = os.path.realpath(DATA_DIR)
    removed, seen = [], set()
    for p in candidates:
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        if rp == data_root or not rp.startswith(data_root + os.sep):
            continue   # safety net: refuse to delete anything outside data/
        if os.path.isfile(rp):
            try:
                os.remove(rp)
                removed.append(os.path.relpath(rp, DATA_DIR))
            except OSError:
                pass
    return removed


def all_trips():
    if not os.path.isdir(TRIPS_DIR):
        return []
    out = []
    for fn in os.listdir(TRIPS_DIR):
        if fn.endswith(".json"):
            t = read_json(os.path.join(TRIPS_DIR, fn))
            if t:
                out.append(t)
    return out


# ---- index -----------------------------------------------------------------
MAP_CLUSTER_KM = 400   # stays farther apart than this become SEPARATE global-map dots
                       # (mirrors the weather CLIMATE_THR: a ring-road trip stays one dot,
                       # genuinely different regions split — see clusterLocs in index.html)


def _haversine_km(a, b):
    R = 6371.0
    r = math.pi / 180.0
    dlat = (b[0] - a[0]) * r
    dlng = (b[1] - a[1]) * r
    x = math.sin(dlat / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(x)))


def _map_points(trip):
    """Representative coordinates for the *global* map (`#/map`), auto-derived so the
    home map is fully data-driven — there is no hand-maintained coords file.

    Basis = the trip's STAY places (where nights were actually spent) — this excludes
    departure/arrival airports and sightseeing stops, which would otherwise scatter
    spurious dots (a Peru trip must not plant a marker on JFK). The stays are grouped by
    single-linkage clustering at MAP_CLUSTER_KM, so one dot lands per region genuinely
    stayed in: a one-base or ring-road trip -> 1 dot, a multi-country trip -> several.
    Each dot is the cluster's centroid, rounded. Returns [] when a trip has no geocoded
    stays — such a trip simply isn't plotted (geocode it, or give it a stay place, to
    put it on the map)."""
    places = trip.get("places") or []
    stays = []
    for p in places:
        if p.get("type") != "stay":
            continue
        la, ln = p.get("lat"), p.get("lng")
        if isinstance(la, (int, float)) and isinstance(ln, (int, float)):
            stays.append((float(la), float(ln)))
    if not stays:
        return []
    n = len(stays)
    par = list(range(n))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _haversine_km(stays[i], stays[j]) < MAP_CLUSTER_KM:
                par[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    pts = []
    for idxs in groups.values():
        lat = sum(stays[i][0] for i in idxs) / len(idxs)
        lng = sum(stays[i][1] for i in idxs) / len(idxs)
        pts.append([round(lat, 5), round(lng, 5)])
    return pts


def _index_entry(t):
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "startDate": t.get("startDate"),
        "endDate": t.get("endDate"),
        "status": t.get("status", "active"),
        "cover": t.get("cover"),
        "coverVer": t.get("coverVer", 0),
        "canCoverBack": cover_history_flags(t)[0],   # ⇒ home card shows the ↩ "previous photo" button
        "canCoverFwd": cover_history_flags(t)[1],     # ⇒ home card shows the ↪ "next photo" button
        "structured": bool(t.get("structured")),
        "archive": bool(t.get("archive")),
        "dayCount": len(t.get("days") or []),
        "docCount": len(t.get("documents") or []),
        "climateV": (t.get("climate") or {}).get("v"),   # stored-climate schema version ⇒ home pre-warm re-warms trips that are missing/older than the current WX_V (without loading them)
        "mapPoints": _map_points(t),   # auto-derived global-map dots (clustered STAY places); [] ⇒ trip isn't plotted on the home map
    }


def rebuild_index():
    trips = all_trips()
    trips.sort(key=lambda t: (t.get("startDate") or "", t.get("title") or ""))
    idx = [_index_entry(t) for t in trips]
    write_json(INDEX_PATH, idx)
    return idx
