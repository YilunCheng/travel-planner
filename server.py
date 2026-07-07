#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Travel Planner server.

Serves the single-file reader (index.html), the per-trip JSON, covers and raw text
from the app directory — like `python3 -m http.server` — and adds a small JSON API
to read/save trips, open supporting documents in their native macOS app, serve
document thumbnails, and re-run the import for one trip on demand.

Stdlib only, no installs. Modeled on daily-magazine/.magazine/server.py.
The original ~/Documents/Travel Plan folder is only ever READ (and opened via `open`);
all writes land under data/.

  python3 server.py        # -> http://localhost:8787
"""

import json, os, sys, re, time, subprocess, mimetypes, tempfile, ipaddress, shutil, signal, urllib.request, urllib.error
import datetime as _dt
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# iWork docs are common in Travel Plan; without these Safari gets octet-stream and won't QuickLook them
mimetypes.add_type("application/vnd.apple.pages", ".pages")
mimetypes.add_type("application/vnd.apple.numbers", ".numbers")
mimetypes.add_type("application/vnd.apple.keynote", ".key")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "import"))
import common as C   # noqa: E402  (module `common`, in the import/ dir on sys.path)
import scan          # noqa: E402  (reused for live document re-inventory on GET — import-safe, has a __main__ guard)

PORT = 8787   # 8765 is the magazine; 8770 is taken by macOS sharingd
PY = sys.executable
CONFIG_PATH = os.path.join(C.APP_DIR, "config.local.json")   # holds googleMapsApiKey / aeroDataBoxKey (key never sent to the browser)
FLIGHT_CACHE_PATH = os.path.join(C.CACHE_DIR, "flights.json")   # cache AeroDataBox lookups so we don't burn the quota
_flight_cache = None

# --- access control -------------------------------------------------------
# This server has NO authentication. When it is bound to anything other than
# loopback (e.g. HOST=0.0.0.0 so a phone can reach it over Tailscale), we STILL
# only answer clients on the loopback or the Tailscale tailnet — never the rest
# of whatever Wi-Fi/LAN the Mac happens to be on (airport, café…). Set ALLOW_LAN=1
# to also serve the local private network (the old "trusted home network" case).
_TAILSCALE_NETS = [ipaddress.ip_network("100.64.0.0/10"),        # Tailscale IPv4 (CGNAT range)
                   ipaddress.ip_network("fd7a:115c:a1e0::/48")]  # Tailscale IPv6 (ULA)
_LAN_NETS = [ipaddress.ip_network(n) for n in
             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")]
ALLOW_NETS = list(_TAILSCALE_NETS)
if os.environ.get("ALLOW_LAN", "").lower() in ("1", "true", "yes", "on"):
    ALLOW_NETS += _LAN_NETS


def flight_cache():
    global _flight_cache
    if _flight_cache is None:
        _flight_cache = C.read_json(FLIGHT_CACHE_PATH, {}) or {}
    return _flight_cache


def _flight_stale(ent, date_str):
    """Whether a cached flight entry should be re-fetched. A flight in the PAST is immutable (it's
    over) -> never stale, so history (and the frontend's "Arrived" normalization) stay put and cost
    no quota. An UPCOMING flight gets a TTL that tightens as it nears, since that's when delays / gate
    changes / cancellations actually move. Lazy: only a hover on a stale entry spends a lookup. Legacy
    entries (no `fetchedAt`) read as age=∞, so a future one refetches once and a past one stays."""
    try:
        days_out = (_dt.date.fromisoformat((date_str or "")[:10]) - _dt.date.today()).days
    except Exception:
        return False
    if days_out < 0:
        return False                       # immutable history — never refetch
    if days_out <= 1:
        ttl = 20 * 60                      # today/tomorrow: live operational window
    elif days_out <= 7:
        ttl = 6 * 3600                     # this week: schedule firming up
    else:
        ttl = 2 * 86400                    # further out: schedule stable, refresh occasionally
    return (time.time() - (ent.get("fetchedAt") or 0)) >= ttl


# ---- weather (Open-Meteo: keyless; needs only lat/lng, which trips already have from geocoding) ----
WEATHER_CACHE_PATH = os.path.join(C.CACHE_DIR, "weather.json")   # cache normals/forecasts off the free API
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"          # ~16 days ahead + up to 92 days of recent past
OM_ARCHIVE  = "https://archive-api.open-meteo.com/v1/archive"   # ERA5 reanalysis, 1940 → ~5 days ago
_weather_cache = None


def weather_cache():
    global _weather_cache
    if _weather_cache is None:
        _weather_cache = C.read_json(WEATHER_CACHE_PATH, {}) or {}
    return _weather_cache


def _hm(s):
    """'2026-06-28T03:34' -> '03:34' (or None)."""
    m = re.search(r"T(\d{2}:\d{2})", s or "")
    return m.group(1) if m else None


def _om_fetch(base, lat, lng, start, end, daily, hourly=None):
    """One Open-Meteo call -> a list of per-day dicts. RETRIES a few times on a transient failure, so a
    one-off network blip during a multi-place / pre-warm burst doesn't silently drop a whole leg's
    climate (the bug where a cluster vanished from the strip). When `hourly` is given (forecast path),
    the hourly precipitation_probability is folded into per-day `segPop` (night/morning/afternoon/
    evening max). Raises if all attempts fail (callers wrap in try/except so weather stays best-effort)."""
    params = {
        "latitude": round(lat, 4), "longitude": round(lng, 4),
        "daily": ",".join(daily), "timezone": "auto",
        "start_date": start, "end_date": end,
    }
    if hourly:
        params["hourly"] = ",".join(hourly)
    url = base + "?" + urllib.parse.urlencode(params)
    j, last = None, None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "travel-planner/1.0 (personal, localhost)"})
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            last = e; time.sleep(0.6 * (attempt + 1))
    if j is None:
        raise last or RuntimeError("open-meteo fetch failed")
    d = (j or {}).get("daily") or {}
    times = d.get("time") or []
    rows = []
    for i, t in enumerate(times):
        def g(k, _i=i):
            a = d.get(k) or []
            return a[_i] if _i < len(a) else None
        rows.append({"date": t, "code": g("weather_code"),
                     "tmax": g("temperature_2m_max"), "tmin": g("temperature_2m_min"),
                     "precip": g("precipitation_sum"),
                     "pop": g("precipitation_probability_max"),
                     "wind": g("wind_speed_10m_max"),
                     "sunrise": _hm(g("sunrise")), "sunset": _hm(g("sunset"))})
    # fold hourly rain-probability into per-day time-of-day segments
    hd = (j or {}).get("hourly") or {}
    htimes = hd.get("time") or []
    hprob = hd.get("precipitation_probability") or []
    if htimes and hprob:
        seg_by_date = {}
        for i, tm in enumerate(htimes):
            p = hprob[i] if i < len(hprob) else None
            if p is None:
                continue
            try:
                hr = int(tm[11:13])
            except Exception:
                continue
            seg = "night" if hr < 6 else "morning" if hr < 12 else "afternoon" if hr < 18 else "evening"
            sd = seg_by_date.setdefault(tm[:10], {})
            sd[seg] = max(sd.get(seg, 0), p)
        for row in rows:
            sp = seg_by_date.get(row["date"])
            if sp:
                row["segPop"] = sp
    return rows


def _climate_normals(lat, lng, start, end, dates=None):
    """Typical weather for the trip's calendar dates, averaged over the last ~10 years of ERA5
    (one archive call, then filter to those (month, day) pairs). `dates` (an explicit list of
    YYYY-MM-DD) gives an exact, possibly non-contiguous window — used for a single leg of a
    multi-place trip; otherwise the whole start..end span is used. None on any failure."""
    try:
        from collections import Counter
        if dates:                                       # exact per-leg window (the days you're actually there)
            wset = set()
            for s in dates:
                try:
                    dd = _dt.date.fromisoformat(s); wset.add((dd.month, dd.day))
                except Exception:
                    pass
        else:
            sd = _dt.date.fromisoformat(start)
            ed = _dt.date.fromisoformat(end)
            if ed < sd:
                ed = sd
            wset, d, n = set(), sd, 0
            while d <= ed and n < 92:                   # the (month, day) pairs the trip spans (cap 92d)
                wset.add((d.month, d.day)); d += _dt.timedelta(days=1); n += 1
        if not wset:
            return None
        today = _dt.date.today()
        a_start = _dt.date(today.year - 10, 1, 1).isoformat()
        a_end = (today - _dt.timedelta(days=5)).isoformat()   # archive lags ~5 days
        rows = _om_fetch(OM_ARCHIVE, lat, lng, a_start, a_end,
                         ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum"])
        sel = [r for r in rows if r.get("tmax") is not None
               and (int(r["date"][5:7]), int(r["date"][8:10])) in wset]
        if not sel:
            return None
        tmax = sum(r["tmax"] for r in sel) / len(sel)
        tmins = [r["tmin"] for r in sel if r.get("tmin") is not None]
        tmin = (sum(tmins) / len(tmins)) if tmins else tmax
        rainy = sum(1 for r in sel if (r.get("precip") or 0) >= 1.0)
        frac = rainy / len(sel)
        years = len({r["date"][:4] for r in sel})
        codes = [r["code"] for r in sel if r.get("code") is not None]
        code = Counter(codes).most_common(1)[0][0] if codes else None
        return {"tmax": round(tmax, 1), "tmin": round(tmin, 1),
                "precipDays": round(frac * len(wset), 1), "windowDays": len(wset),
                "rainFrac": round(frac, 2), "code": code, "nYears": years}
    except Exception:
        return None


def open_allowed(path):
    """True if `path` is safe to open/serve: a real file inside Travel Plan or our app.
    config.local.json (the API keys) is explicitly excluded — it must never leave the server."""
    try:
        rp = os.path.realpath(path)
    except Exception:
        return None
    if rp == os.path.realpath(CONFIG_PATH):
        return None
    roots = (os.path.realpath(C.TRAVEL_DIR), os.path.realpath(C.APP_DIR))
    if not any(rp == r or rp.startswith(r + os.sep) for r in roots):
        return None
    return rp


def trip_doc_folder(trip):
    """The trip's own folder under Travel Plan that documents live in (and can be added to): a real
    directory that is a DIRECT child of TRAVEL_DIR. None for single-file trips (whose sourceFolder is
    the whole library), trips with no folder, or a gone/unreadable folder (e.g. an unmounted drive)."""
    folder = trip.get("sourceFolder")
    if not folder:
        return None
    rp = open_allowed(folder)
    if not rp or not os.path.isdir(rp):
        return None
    if os.path.dirname(os.path.realpath(folder)) != os.path.realpath(C.TRAVEL_DIR):
        return None   # must be a direct child of Travel Plan (excludes single-file trips, whose folder == TRAVEL_DIR)
    return folder


def live_documents(trip):
    """Re-inventory the trip's folder straight from disk, so the Documents panel reflects manual
    add/rename/delete under ~/Documents/Travel Plan without a re-scan. None when there's no such
    folder (then the stored snapshot stands). Read-only; never persists."""
    folder = trip_doc_folder(trip)
    if not folder:
        return None
    try:
        return scan.inventory_documents(folder, trip.get("sourceMain"))
    except Exception:
        return None


def resolve_upload_dest(folder, name):
    """A safe, non-colliding destination path for an uploaded file inside `folder`. The name must be a
    plain filename — no path separators, no `..`, no leading dot — so a crafted name can neither escape
    the folder nor be silently renamed. Deduped as `name (1).ext` so it NEVER overwrites; realpath-confined
    to `folder`. Returns the dest path, or None if the name is unsafe."""
    safe = (name or "").strip()
    if (not safe or safe.startswith(".") or "/" in safe or "\\" in safe
            or os.sep in safe or ".." in safe or os.path.basename(safe) != safe):
        return None
    base, ext = os.path.splitext(safe)
    dest = os.path.join(folder, safe); k = 1
    while os.path.exists(dest):
        dest = os.path.join(folder, "%s (%d)%s" % (base, k, ext)); k += 1
    if os.path.dirname(os.path.realpath(dest)) != os.path.realpath(folder):
        return None
    return dest


def write_upload(folder, name, src, size):
    """Stream up to `size` bytes from file-like `src` into a deduped, gated dest inside `folder`, via a
    hidden temp + atomic rename. Returns the basename written, or None for an unsafe name. Append-only:
    never overwrites an existing file, never deletes anything."""
    dest = resolve_upload_dest(folder, name)
    if not dest:
        return None
    tmp = os.path.join(folder, "." + os.path.basename(dest) + ".part-upload")   # hidden temp (skipped by the inventory)
    try:
        remaining = size
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = src.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                f.write(chunk); remaining -= len(chunk)
        os.replace(tmp, dest)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise
    return os.path.basename(dest)


def make_thumb(path):
    """Generate (and cache) a thumbnail png for a file via qlmanage. Returns png path or None."""
    key = re.sub(r"[^0-9A-Za-z]", "_", path)[-120:]
    out = os.path.join(C.CACHE_DIR, "thumbs", key + ".png")
    if os.path.exists(out):
        return out
    src = os.path.join(C.CACHE_DIR, "thumbs", key + ".src")
    try:
        subprocess.run(["qlmanage", "-t", "-s", "320", "-o", os.path.dirname(src), path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        # qlmanage writes "<basename>.png" in the output dir
        cand = os.path.join(os.path.dirname(src), os.path.basename(path) + ".png")
        if os.path.exists(cand):
            os.replace(cand, out)
            return out
    except Exception:
        pass
    return None


CHROME_BINS = [  # headless HTML→PDF engine for GET /api/pdf (the phone's Export PDF) — first installed browser wins
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def chrome_bin():
    for p in CHROME_BINS:
        if os.path.exists(p):
            return p
    return None


def new_trip_skeleton(title, start):
    tid = C.compute_id(start[:4], start[5:7], title) if re.match(r"\d{4}-\d{2}", start or "") \
        else C.slug(title)
    return {
        "schemaVersion": C.SCHEMA_VERSION, "id": tid, "title": title or "New Trip",
        "startDate": start or "", "endDate": start or "", "status": "active",
        "timezone": None, "cover": None, "sourceFolder": None, "sourcePages": None,
        "structured": True, "rawTextRef": None,
        "days": [{"id": "d1", "date": start or "", "label": "Day 1", "stay": "", "items": []}],
        "documents": [],
    }


def trip_folder_name(trip):
    """Canonical `YYYY:MM Title [(cancelled)]` folder name for a trip — the literal-':' on-disk
    convention parse_trip_name expects — or None if the trip has no usable YYYY-MM start date."""
    m = re.match(r"^(\d{4})-(\d{2})", trip.get("startDate") or "")
    if not m:
        return None
    title = (trip.get("title") or "Trip").replace("/", "-").replace(os.sep, "-").strip().strip(".")
    name = "%s:%s %s" % (m.group(1), m.group(2), title or "Trip")
    if trip.get("status") == "cancelled":
        name += " (cancelled)"
    return name


def renamed_basename(cur_base, trip):
    """Given a trip's CURRENT on-disk folder basename (`YYYY:MM Title [(cancelled)]`), return the
    basename it should have after a title/status edit: the SAME `YYYY:MM` prefix with the title part
    refreshed from `trip['title']` (+ ` (cancelled)` per status). The month prefix is deliberately
    PRESERVED, never re-derived from startDate — imported folder months are the user's own labels and
    often legitimately differ from the itinerary's first day, so we must not auto-shift them. Returns
    None when the name lacks a `YYYY:MM` prefix (then it's left untouched)."""
    m = re.match(r"^(\d{4}:\d{2})\s+.+$", cur_base)
    if not m:
        return None
    title = (trip.get("title") or "Trip").replace("/", "-").replace(os.sep, "-").strip().strip(".") or "Trip"
    name = "%s %s" % (m.group(1), title)
    if trip.get("status") == "cancelled":
        name += " (cancelled)"
    return name


def _rebase_trip_paths(trip, old, new):
    """After a folder rename (or to heal a stale client-sent prefix), rewrite every stored absolute
    path that lived under folder `old` to sit under `new`. String-prefix based, matching exactly how
    these paths are stored (`sourceFolder` + os.sep + name); the literal ':' byte is fine."""
    old = old.rstrip("/")
    def reb(p):
        if isinstance(p, str) and p:
            if p == old:
                return new
            if p.startswith(old + os.sep):
                return new + p[len(old):]
        return p
    for k in ("sourceFolder", "sourcePages", "sourceMain", "sourceItinDoc"):
        if trip.get(k):
            trip[k] = reb(trip[k])
    for d in (trip.get("documents") or []):
        if isinstance(d, dict) and d.get("path"):
            d["path"] = reb(d["path"])


def sync_trip_folder(trip):
    """Keep a trip's folder under ~/Documents/Travel Plan named in step with its title — for BOTH
    app-created folders AND imported ones (a real directory that is a direct child of TRAVEL_DIR) —
    then rebase the trip's stored in-folder paths onto the live folder. Mutates the trip dict.

    Scope & safety:
      * Renames an existing folder only when the TITLE part of its name changed: it keeps the folder's
        existing `YYYY:MM` prefix (see renamed_basename) so an imported folder's intentional month is
        never auto-shifted by a drifting startDate. App-created trips with no folder yet are CREATED
        from the full `YYYY:MM Title`.
      * Only ever creates (makedirs) or renames (os.rename); it NEVER deletes, and never clobbers an
        existing folder of the target name (a name collision keeps the current folder as-is).
      * Every target is gated to a DIRECT child of TRAVEL_DIR, so a crafted title can't escape; single-
        file trips (sourceFolder == the whole library) have no folder to rename and are left alone.
      * The authoritative 'current' folder is the one stored on disk, not the client-sent value, so a
        stale client sourceFolder can't make us lose track of (and orphan) the real folder; after a
        rename the trip's sourcePages/sourceMain/documents[] are rebased so 'open original' keeps working.
      * Best-effort: any OSError is swallowed so a filesystem hiccup never blocks saving the trip."""
    sent_folder = trip.get("sourceFolder")           # the prefix the client's stored paths currently use
    stored = C.load_trip(trip.get("id") or "") or {}
    app_owned = bool(trip.get("ownsFolder") or stored.get("ownsFolder"))
    cur = stored.get("sourceFolder") or sent_folder  # authoritative current folder (what's on disk)
    TRAVEL = os.path.realpath(C.TRAVEL_DIR)

    def is_child_dir(p):
        return bool(p) and os.path.isdir(p) and os.path.dirname(os.path.realpath(p)) == TRAVEL

    # Decide the desired folder path (None ⇒ nothing to do).
    desired = None
    if is_child_dir(cur):
        nb = renamed_basename(os.path.basename(os.path.normpath(cur)), trip)
        if nb:
            desired = os.path.join(C.TRAVEL_DIR, nb)   # rename: keep prefix, track title
    elif app_owned:
        name = trip_folder_name(trip)
        if name:
            desired = os.path.join(C.TRAVEL_DIR, name)  # app-owned but folder missing -> create from full name

    authoritative = cur
    if desired and os.path.dirname(os.path.realpath(desired)) == TRAVEL:   # gate: direct child only
        try:
            if not is_child_dir(cur):
                if app_owned:
                    os.makedirs(desired, exist_ok=True)
                    authoritative = desired
            elif os.path.realpath(cur) != os.path.realpath(desired):
                if not os.path.exists(desired):        # rename to match — never clobber
                    os.rename(cur, desired)
                    authoritative = desired
                # else: name collision -> keep `cur` as-is
        except OSError:
            authoritative = cur

    # Heal the trip's in-folder paths: rebase from whatever prefix the client sent onto the live folder
    # (covers both a fresh rename and a stale client value re-sent on a later save).
    if authoritative and sent_folder and sent_folder != authoritative:
        _rebase_trip_paths(trip, sent_folder, authoritative)
    if authoritative:
        trip["sourceFolder"] = authoritative
    if app_owned:
        trip["ownsFolder"] = True


# ---- AI weather summary (shells the local `claude` CLI — same no-API-key path the import pipeline uses) ----
CLAUDE_BIN = C.CLAUDE_BIN   # $CLAUDE_BIN env → `claude` on PATH → ~/.local/bin/claude (resolved in common.py)
WX_SUMMARY_CACHE = os.path.join(C.CACHE_DIR, "wx_summary.json")


def _ask_claude_text(prompt, timeout=120):
    """One `claude -p` call → its text output. Reuses the user's login (no API key), like import/structure.py."""
    env = dict(os.environ)
    env["PATH"] = C.TOOL_PATH
    r = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--model", C.CLAUDE_MODEL, "--effort", "low",
         "--permission-mode", "bypassPermissions", "--output-format", "text"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError((r.stderr or "claude failed")[:200])
    return r.stdout.strip()


def _wx_summary_prompt(trip, cards=None):
    """Build the prompt from climate cards (the client's live cards if passed, else the trip's stored ones —
    avoids a race when a freshly-computed climate hasn't hit disk yet) + a condensed activity list. None if none."""
    cards = cards or (trip.get("climate") or {}).get("cards") or []
    if not cards:
        return None
    lines = []
    for c in cards:
        cl = c.get("climate") or {}
        ds = c.get("dates") or []
        rng = (ds[0] + "→" + ds[-1]) if len(ds) > 1 else (ds[0] if ds else "")
        tmax, tmin = cl.get("tmax"), cl.get("tmin")
        temp = ("%d-%d C" % (round(tmin), round(tmax))) if (tmax is not None and tmin is not None) else "?"
        pd, wd = cl.get("precipDays"), cl.get("windowDays")
        rain = ("%d of %d days with rain" % (pd, wd)) if (pd is not None and wd) else ""
        lines.append(("- %s (%s): %s, %s" % (c.get("label") or "?", rng, temp, rain)).rstrip(", "))
    acts = {}
    for d in trip.get("days") or []:
        for it in (d.get("items") or []):
            t = it.get("type")
            if t in ("ski", "hiking", "dive", "activity", "museum", "meal", "cruise"):
                acts[t] = acts.get(t, 0) + 1
    actline = ", ".join("%s x%d" % (k, v) for k, v in sorted(acts.items())) or "general sightseeing"
    return (
        "You are a concise travel-weather advisor. In 2-3 short sentences, summarize the weather a traveler "
        "should expect on \"%s\" and what it means for their plans — practical and specific (what to pack, "
        "what to watch for, best/worst conditions for their activities). Plain prose, no preamble, no markdown, "
        "no bullet points.\n\nTypical weather by place (10-year averages for the exact travel dates):\n%s\n\n"
        "Main activities: %s\n\nWeather summary:" % (trip.get("title") or "this trip", "\n".join(lines), actline)
    )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=C.APP_DIR, **k)

    # ---- helpers ----
    def _client_allowed(self):
        host = (self.client_address or ("",))[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None) is not None:   # ::ffff:100.x.x.x -> 100.x.x.x
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            return True
        return any(ip in net for net in ALLOW_NETS)

    def _guard(self):
        """Gate every request by client IP. No-op on the loopback default; the safety net when
        bound to 0.0.0.0 for phone access (answers only loopback + Tailscale — never the open LAN)."""
        if self._client_allowed():
            return True
        self.close_connection = True   # don't keep-alive a rejected (possibly body-bearing) request
        self.send_error(403)
        return False

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _send_file(self, path, ctype=None, name=None):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        if name:   # inline (not attachment): the phone opens docs IN the browser — Safari previews and titles/saves by this name
            self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(name))
        self.end_headers()
        self.wfile.write(data)

    # ---- GET ----
    def do_GET(self):
        if not self._guard():
            return
        p = urlparse(self.path)
        route = p.path
        if route == "/" :
            return self._send_file(os.path.join(C.APP_DIR, "index.html"), "text/html; charset=utf-8")
        if route == "/api/trips":
            return self._json(200, C.read_json(C.INDEX_PATH, []))
        if route == "/api/trip":
            return self.get_trip(parse_qs(p.query))
        if route == "/api/doc":
            return self.get_doc(parse_qs(p.query))
        if route == "/api/documents":
            return self.get_documents(parse_qs(p.query))
        if route == "/api/config":
            cfg = C.read_json(CONFIG_PATH, {}) or {}
            return self._json(200, {"googleMapsApiKey": cfg.get("googleMapsApiKey", ""),
                                    "hasFlightKey": bool((cfg.get("aeroDataBoxKey") or "").strip()),
                                    "pdfEngine": bool(chrome_bin())})   # server-side PDF available (touch clients use GET /api/pdf)
        if route == "/api/pdf":
            return self.get_pdf(parse_qs(p.query))
        if route == "/api/flight":
            return self.flight_info(parse_qs(p.query))
        if route == "/api/weather":
            return self.weather_info(parse_qs(p.query))
        # keep machinery / secrets private
        if route.startswith("/import") or route.startswith("/cache") or route == "/config.local.json":
            return self.send_error(404)
        return super().do_GET()

    def get_trip(self, qs):
        tid = (qs.get("id") or [""])[0]
        if not C.is_safe_trip_id(tid):   # block '../' ids from reading arbitrary *.json (e.g. config.local)
            return self._json(400, {"error": "bad id"})
        trip = C.load_trip(tid)
        if not trip:
            return self._json(404, {"error": "no such trip"})
        extra = {}
        # attach raw text if present (so un-structured trips are viewable)
        if trip.get("rawTextRef"):
            extra["rawText"] = _read_text(os.path.join(C.DATA_DIR, trip["rawTextRef"]))
        # live documents + the folder name (so the UI can offer drag-drop upload into it)
        folder = trip_doc_folder(trip)
        if folder:
            extra["docFolder"] = os.path.basename(folder)
        docs = live_documents(trip)
        if docs is not None:
            extra["documents"] = docs
        if extra:
            trip = dict(trip, **extra)
        return self._json(200, trip)

    def get_doc(self, qs):
        path = (qs.get("path") or [""])[0]
        rp = open_allowed(path)
        if not rp or not os.path.exists(rp):
            return self.send_error(404)
        if (qs.get("thumb") or [""])[0] in ("1", "true"):
            t = make_thumb(rp)          # qlmanage renders iWork bundle DIRECTORIES too
            if t:
                return self._send_file(t, "image/png")
            return self.send_error(404)
        if os.path.isdir(rp):
            # An iWork "file" that is really a bundle directory (classify by extension, not isdir — see
            # CLAUDE.md): stream its embedded preview so a phone can still view it. Preview.pdf = the whole
            # document (older format); the newer format only embeds first-page JPEGs.
            base = os.path.basename(rp)
            for rel, ctype, ext in (("QuickLook/Preview.pdf", "application/pdf", ".pdf"),
                                    ("preview.jpg", "image/jpeg", ".jpg"),
                                    ("QuickLook/Thumbnail.jpg", "image/jpeg", ".jpg"),
                                    ("preview-web.jpg", "image/jpeg", ".jpg")):
                cand = os.path.join(rp, rel)
                if os.path.isfile(cand):
                    return self._send_file(cand, ctype, name=base + ext)
            return self.send_error(404)
        return self._send_file(rp, name=os.path.basename(rp))

    def get_documents(self, qs):
        """Live documents for one trip — powers the UI's focus-refresh of the Documents panel.
        Re-reads the source folder; falls back to the stored list for single-file trips or an
        unreachable folder."""
        tid = (qs.get("id") or [""])[0]
        if not C.is_safe_trip_id(tid):
            return self._json(400, {"error": "bad id"})
        trip = C.load_trip(tid)
        if not trip:
            return self._json(404, {"error": "no such trip"})
        docs = live_documents(trip)
        if docs is None:
            docs = trip.get("documents") or []
        return self._json(200, {"ok": True, "documents": docs})

    def get_pdf(self, qs):
        """Render one trip to PDF with headless Chrome on the Mac (GET /api/pdf?id=). This is the
        PHONE's export path — window.print() is a no-op in an iOS standalone web app and mobile
        Safari ignores the custom pageless @page size, so the Mac prints FOR the phone. Chrome
        loads ?print=1#/trip/<id> (the frontend's read-only print boot: no live map build, no
        saves; it fills #pdfmap + injects the pageless @page) and honors the same @media print
        stylesheet the desktop Export uses, so both paths produce the same document."""
        tid = (qs.get("id") or [""])[0]
        if not C.is_safe_trip_id(tid):
            return self._json(400, {"error": "bad id"})
        trip = C.load_trip(tid)
        if not trip:
            return self._json(404, {"error": "no such trip"})
        chrome = chrome_bin()
        if not chrome:
            return self._json(501, {"error": "no headless browser on this Mac (install Google Chrome)"})
        td = tempfile.mkdtemp(prefix="tp-pdf-")
        out = os.path.join(td, "trip.pdf")
        # localhost (not 127.0.0.1): the Maps key is referrer-restricted to http://localhost:8787/*,
        # so the static map inside the render only loads under this origin.
        url = "http://localhost:%d/?print=1#/trip/%s" % (PORT, quote(tid, safe=""))
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", "--disable-sync", "--mute-audio", "--hide-scrollbars",
               "--window-size=1280,900",                          # desktop layout while measuring (isPhone()=false)
               "--user-data-dir=" + os.path.join(td, "profile"),  # throwaway profile: never joins/locks the user's running Chrome
               "--virtual-time-budget=20000",                     # fast-forward timers, wait out fetches (trip JSON, static map)
               "--no-pdf-header-footer",
               "--print-to-pdf=" + out, url]
        data = None
        # Chrome writes the PDF (atomically, at print completion) but then often LINGERS instead of
        # exiting — so don't wait on the process: poll for the file, then kill the whole process group.
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            deadline = time.time() + 90
            while time.time() < deadline:
                if os.path.isfile(out) and os.path.getsize(out) > 0:
                    time.sleep(0.6)   # settle: written in one shot at the end, this is belt-and-braces
                    with open(out, "rb") as f:
                        data = f.read()
                    break
                if proc.poll() is not None:                        # chrome exited by itself
                    if os.path.isfile(out) and os.path.getsize(out) > 0:
                        with open(out, "rb") as f:
                            data = f.read()
                    break
                time.sleep(0.3)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)            # whole tree (chrome spawns helpers)
                except Exception:
                    proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            shutil.rmtree(td, ignore_errors=True)
        if not data:
            return self._json(500, {"error": "PDF render failed or timed out"})
        name = (trip.get("title") or tid) + ".pdf"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(name))
        self.end_headers()
        self.wfile.write(data)

    def flight_info(self, qs):
        """Proxy a flight lookup to AeroDataBox (RapidAPI). Key stays server-side; results cached on disk."""
        no = re.sub(r"\s+", "", (qs.get("no") or [""])[0]).upper()
        date = (qs.get("date") or [""])[0]
        if not re.match(r"^[A-Z0-9]{2,8}$", no) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return self._json(400, {"ok": False, "reason": "bad params"})
        cache = flight_cache()
        ck = no + "|" + date
        cached = cache.get(ck)
        if cached is not None and not _flight_stale(cached, date):
            return self._json(200, cached)                    # fresh enough (a past flight never expires)
        cfg = C.read_json(CONFIG_PATH, {}) or {}
        key = (cfg.get("aeroDataBoxKey") or "").strip()
        if not key:                                           # can't refresh -> serve stale if we have it
            return self._json(200, cached if cached is not None else {"ok": False, "reason": "nokey"})
        url = "https://aerodatabox.p.rapidapi.com/flights/number/%s/%s" % (no, date)
        req = urllib.request.Request(url, headers={
            "X-RapidAPI-Key": key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) travel-planner"})  # default urllib UA is Cloudflare-blocked (1010)
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8") or "[]")
        except urllib.error.HTTPError as e:
            if e.code in (204, 400, 403, 404):   # no flight / date outside this plan's range -> treat as "no data"
                out = {"ok": True, "flights": [], "fetchedAt": time.time()}
                cache[ck] = out; C.write_json(FLIGHT_CACHE_PATH, cache)
                return self._json(200, out)
            if e.code == 429:                    # rate limited -> transient; serve stale rather than an error
                return self._json(200, cached if cached is not None else {"ok": False, "reason": "rate"})
            return self._json(200, cached if cached is not None else {"ok": False, "reason": "http %d" % e.code})
        except Exception as e:
            return self._json(200, cached if cached is not None else {"ok": False, "reason": str(e)[:120]})
        if isinstance(data, dict):
            data = data.get("flights") or data.get("data") or []
        flights = []
        for f in (data if isinstance(data, list) else []):
            dep = f.get("departure") or {}; arr = f.get("arrival") or {}
            dap = dep.get("airport") or {}; aap = arr.get("airport") or {}

            def _t(x):
                st = x.get("scheduledTime") or x.get("revisedTime") or {}
                m = re.search(r"\d{2}:\d{2}", st.get("local") or st.get("utc") or "")
                return m.group(0) if m else ""

            def _utc(x):   # actual UTC instant of dep/arr → lets duration account for the time-zone change
                st = x.get("scheduledTime") or x.get("revisedTime") or {}
                m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?\s*(Z|[+-]\d{2}:?\d{2})?",
                              st.get("utc") or st.get("local") or "")
                if not m:
                    return None
                base = _dt.datetime(*(int(m.group(i)) for i in range(1, 6)))
                z = m.group(6) or "Z"
                off = 0 if z == "Z" else (1 if z[0] == "+" else -1) * (int(z[1:3]) * 60 + int(z.replace(":", "")[3:5]))
                return base - _dt.timedelta(minutes=off)
            _du, _au = _utc(dep), _utc(arr)
            duration_min = int(round((_au - _du).total_seconds() / 60)) if (_du and _au and _au > _du) else None
            gc = f.get("greatCircleDistance") or {}
            flights.append({
                "airline": (f.get("airline") or {}).get("name"),
                "number": f.get("number"),
                "aircraft": (f.get("aircraft") or {}).get("model"),
                "status": f.get("status"),
                "durationMin": duration_min,
                "dep": {"iata": dap.get("iata"), "name": dap.get("name"), "city": dap.get("municipalityName"),
                        "time": _t(dep), "terminal": dep.get("terminal"), "gate": dep.get("gate")},
                "arr": {"iata": aap.get("iata"), "name": aap.get("name"), "city": aap.get("municipalityName"),
                        "time": _t(arr), "terminal": arr.get("terminal"), "belt": arr.get("baggageBelt")},
                "distanceKm": round(gc["km"]) if gc.get("km") else None,
            })
        out = {"ok": True, "flights": flights, "fetchedAt": time.time()}
        cache[ck] = out; C.write_json(FLIGHT_CACHE_PATH, cache)
        return self._json(200, out)

    def weather_info(self, qs):
        """Destination weather for a trip window. ALWAYS returns climate normals (≈10-yr averages for
        these calendar dates) as the planning summary; ADDS specific-day data when available — the
        Open-Meteo forecast for a near/recent trip (≤16 days out, or up to ~92 days past), or ERA5
        archive actuals for a trip well in the past. Keyless (Open-Meteo); results cached on disk.

        Climate is cached under a date-stable key; day-data under a per-today key so forecasts refresh
        daily. A failed fetch is NOT cached (so it retries), but a legitimately-empty far-future day-set
        is, so we don't re-hit on every view."""
        try:
            lat = float((qs.get("lat") or [""])[0]); lng = float((qs.get("lng") or [""])[0])
        except Exception:
            return self._json(400, {"error": "bad lat/lng"})
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return self._json(400, {"error": "bad lat/lng"})
        start = (qs.get("start") or [""])[0]; end = (qs.get("end") or [""])[0]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
            return self._json(400, {"error": "bad start"})
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
            end = start
        want = (qs.get("want") or ["both"])[0]
        if want not in ("both", "days", "climate"):
            want = "both"
        dates = [s for s in (qs.get("dates") or [""])[0].split(",") if re.match(r"^\d{4}-\d{2}-\d{2}$", s)]
        today = _dt.date.today()
        cache = weather_cache()
        rlat, rlng = round(lat, 2), round(lng, 2)
        out = {"mode": "climate", "climate": None, "days": None}

        # 1) climate normals — date-stable cache (keyed by the exact window when `dates` is given);
        #    don't cache a transient failure. (Skipped for want=days.)
        if want != "days":
            csig = ",".join(sorted(dates)) if dates else (start + ".." + end)
            ck_clim = "clim|%.2f,%.2f|%s" % (rlat, rlng, csig)
            if ck_clim in cache:
                out["climate"] = cache[ck_clim]
            else:
                out["climate"] = _climate_normals(lat, lng, start, end, dates or None)
                if out["climate"] is not None:
                    cache[ck_clim] = out["climate"]; C.write_json(WEATHER_CACHE_PATH, cache)

        # 2) specific-day data — per-today cache so forecasts refresh daily. (Skipped for want=climate.)
        if want != "climate":
            ck_days = "days2|%.2f,%.2f|%s|%s|%s" % (rlat, rlng, start, end, today.isoformat())
            if ck_days in cache:
                dd = cache[ck_days]; out["days"] = dd.get("days"); out["mode"] = dd.get("mode") or out["mode"]
            else:
                try:
                    sd = _dt.date.fromisoformat(start); ed = _dt.date.fromisoformat(end)
                except Exception:
                    sd = ed = None
                days, mode, status = None, "climate", "none"   # none = nothing to fetch · got · error
                if sd and ed:
                    horizon = today + _dt.timedelta(days=16)
                    recent = today - _dt.timedelta(days=92)
                    try:
                        if sd <= horizon and ed >= recent:             # near future / recent past -> live forecast
                            lo = max(sd, recent).isoformat(); hi = min(ed, horizon).isoformat()
                            days = _om_fetch(OM_FORECAST, lat, lng, lo, hi,
                                             ["weather_code", "temperature_2m_max", "temperature_2m_min",
                                              "precipitation_sum", "precipitation_probability_max", "wind_speed_10m_max",
                                              "sunrise", "sunset"],
                                             hourly=["precipitation_probability"])
                            mode, status = "forecast", ("got" if days else "none")
                        elif ed < recent:                              # well in the past -> archive actuals (no rain-probability)
                            days = _om_fetch(OM_ARCHIVE, lat, lng, start, end,
                                             ["weather_code", "temperature_2m_max", "temperature_2m_min",
                                              "precipitation_sum", "sunrise", "sunset"])
                            mode, status = "actual", ("got" if days else "none")
                    except Exception:
                        days, mode, status = None, "climate", "error"
                if status == "got":
                    out["days"] = days; out["mode"] = mode
                    cache[ck_days] = {"days": days, "mode": mode}; C.write_json(WEATHER_CACHE_PATH, cache)
                elif status == "none":
                    cache[ck_days] = {"days": None, "mode": out["mode"]}; C.write_json(WEATHER_CACHE_PATH, cache)
                # status == "error": leave uncached so it retries on the next view
        return self._json(200, out)

    # ---- POST ----
    def do_POST(self):
        if not self._guard():
            return
        route = urlparse(self.path).path
        try:
            if route == "/api/trip":
                return self.save_trip()
            if route == "/api/trip/new":
                return self.create_trip()
            if route == "/api/trip/delete":
                return self.delete_trip()
            if route == "/api/open":
                return self.open_doc()
            if route == "/api/doc/upload":
                return self.upload_doc()
            if route == "/api/geocode":
                return self.geocode()
            if route == "/api/geocode/reverse":
                return self.geocode_reverse()
            if route == "/api/geocode/search":
                return self.geocode_search()
            if route == "/api/geocode/city":
                return self.geocode_city()
            if route == "/api/weather/summary":
                return self.weather_summary()
            if route == "/api/cover":
                return self.upload_cover()
            if route == "/api/cover/auto":
                return self.auto_cover()
            if route == "/api/cover/nav":
                return self.cover_nav()
            if route == "/api/config":
                return self.save_config()
        except Exception as e:
            return self._json(500, {"error": str(e)[:500]})
        self.send_error(404)

    def save_config(self):
        b = self._body()
        cfg = C.read_json(CONFIG_PATH, {}) or {}
        if "googleMapsApiKey" in b:
            cfg["googleMapsApiKey"] = (b.get("googleMapsApiKey") or "").strip()
        if "aeroDataBoxKey" in b:
            cfg["aeroDataBoxKey"] = (b.get("aeroDataBoxKey") or "").strip()
        C.write_json(CONFIG_PATH, cfg)
        return self._json(200, {"ok": True})

    def save_trip(self):
        trip = self._body()
        if not trip.get("id"):
            return self._json(400, {"error": "missing id"})
        if not C.is_safe_trip_id(trip["id"]):   # block a crafted '../' id from writing outside data/trips
            return self._json(400, {"error": "bad id"})
        trip["schemaVersion"] = C.SCHEMA_VERSION
        for k in ("rawText", "docFolder"):       # GET-only runtime fields (get_trip attaches them) — never persist
            trip.pop(k, None)
        sync_trip_folder(trip)                   # rename the trip's Travel Plan folder to track its title (imported or app-owned)
        C.save_trip(trip)
        C.rebuild_index()
        return self._json(200, {"ok": True, "id": trip["id"], "sourceFolder": trip.get("sourceFolder")})

    def create_trip(self):
        b = self._body()
        trip = new_trip_skeleton(b.get("title", ""), b.get("startDate", ""))
        # avoid clobbering an existing id
        base, i = trip["id"], 2
        while os.path.exists(C.trip_path(trip["id"])):
            trip["id"] = "%s-%d" % (base, i); i += 1
        trip["ownsFolder"] = True            # the app manages this trip's Travel Plan folder (create + rename, never delete)
        sync_trip_folder(trip)               # create ~/Documents/Travel Plan/<YYYY:MM Title> and set sourceFolder
        C.save_trip(trip)
        C.rebuild_index()
        return self._json(200, trip)

    def delete_trip(self):
        """Remove a trip from the app's data store only. The original Travel Plan files
        are never touched (C.delete_trip is realpath-gated to DATA_DIR)."""
        tid = self._body().get("id", "")
        if not tid:
            return self._json(400, {"error": "missing id"})
        if not C.load_trip(tid):
            return self._json(404, {"error": "no such trip"})
        removed = C.delete_trip(tid)
        C.rebuild_index()
        return self._json(200, {"ok": True, "removed": removed})

    def open_doc(self):
        path = self._body().get("path", "")
        rp = open_allowed(path)
        if not rp or not os.path.exists(rp):
            return self._json(400, {"error": "path not allowed"})
        subprocess.run(["open", rp])
        return self._json(200, {"ok": True})

    def _run_import(self, script, tid, extra=None):
        env = dict(os.environ)
        env["PATH"] = C.TOOL_PATH
        cmd = [PY, os.path.join(C.IMPORT_DIR, script), "--id", tid] + (extra or [])
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=C.APP_DIR, timeout=900)

    def geocode(self):
        tid = self._body().get("id", "")
        if not tid:
            return self._json(400, {"error": "missing id"})
        r = self._run_import("geocode.py", tid, ["--force"])
        t = C.load_trip(tid)
        n = len((t or {}).get("places") or [])
        unresolved = []
        for line in (r.stdout or "").splitlines():     # geocode.py emits "__GEOCODE__{...}" with the queries Nominatim couldn't resolve
            if line.startswith("__GEOCODE__"):
                try:
                    unresolved = (json.loads(line[len("__GEOCODE__"):]) or {}).get("unresolved") or []
                except Exception:
                    unresolved = []
        return self._json(200, {"ok": r.returncode == 0 and n > 0, "placed": n, "unresolved": unresolved,
                                "error": ((r.stderr or "")[-300:] if r.returncode else ""),
                                "log": (r.stdout or "")[-1000:]})

    def geocode_reverse(self):
        # Reverse-geocode a clicked map point -> a suggested place label, for the in-UI "click the map to
        # add a place" action. Stateless: shares data/geocache.json (keyed "rev:lat,lng"
        # rounded) and the same Nominatim UA; never touches the trip (client merges + persists via /api/trip).
        b = self._body()
        try:
            lat = float(b.get("lat")); lng = float(b.get("lng"))
        except Exception:
            return self._json(400, {"error": "missing lat/lng"})
        key = "rev:%.5f,%.5f" % (lat, lng)
        cache_path = os.path.join(C.DATA_DIR, "geocache.json")
        cache = C.read_json(cache_path, {}) or {}
        label = None; kind = None
        cached = cache.get(key)
        if isinstance(cached, dict):
            label = cached.get("label"); kind = cached.get("kind")
        elif isinstance(cached, str):
            label = cached            # legacy cache entry (label-only) — no kind stored
        else:
            try:
                url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
                    {"lat": lat, "lon": lng, "format": "json", "zoom": 18, "addressdetails": 1, "accept-language": "en"})
                req = urllib.request.Request(url, headers={"User-Agent": "travel-planner/1.0 (personal, localhost)"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.load(resp)
                if isinstance(data, dict):
                    nm = (data.get("name") or "").strip()
                    if nm:
                        label = nm
                    else:
                        addr = data.get("address") or {}
                        for k in ("amenity", "building", "tourism", "leisure", "shop", "historic",
                                  "aeroway", "railway", "road", "pedestrian", "neighbourhood",
                                  "suburb", "hamlet", "village", "town", "city", "county", "state"):
                            v = (addr.get(k) or "").strip()
                            if v:
                                label = v; break
                        if not label:
                            dn = (data.get("display_name") or "").strip()
                            label = dn.split(",")[0].strip() if dn else None
                    kind = self._osm_kind(data.get("class") or data.get("category"), data.get("type"))
            except Exception:
                label = None; kind = None
            cache[key] = {"label": label, "kind": kind}
            C.write_json(cache_path, cache)
        if not label:
            return self._json(200, {"ok": False, "reason": "notfound"})
        return self._json(200, {"ok": True, "label": label, "kind": kind or ""})

    @staticmethod
    def _osm_kind(cls, typ):
        # Map a Nominatim class/type to the app's place kind (meal | stay | activity | other) so the
        # click-to-add form can default its type. Conservative: unknowns fall through to "other".
        cls = (cls or "").lower(); typ = (typ or "").lower()
        if cls == "amenity" and typ in {"restaurant", "cafe", "fast_food", "food_court", "bar",
                                        "pub", "biergarten", "ice_cream", "bbq"}: return "meal"
        if cls == "shop" and typ in {"bakery", "pastry", "confectionery", "deli", "coffee", "chocolate"}: return "meal"
        if cls == "tourism" and typ in {"hotel", "hostel", "guest_house", "motel", "apartment",
                                        "chalet", "alpine_hut", "camp_site", "caravan_site"}: return "stay"
        if cls == "building" and typ == "hotel": return "stay"
        if cls == "tourism" and typ in {"attraction", "museum", "gallery", "artwork", "viewpoint",
                                        "theme_park", "zoo", "aquarium", "picnic_site"}: return "activity"
        if cls in {"historic", "leisure", "natural"}: return "activity"
        if cls == "amenity" and typ in {"theatre", "cinema", "arts_centre", "place_of_worship",
                                        "marketplace", "fountain"}: return "activity"
        return "other"

    def geocode_search(self):
        # Forward-search for the in-map "search a place to add" box: return up to ~6 candidates (label +
        # coords + kind + a short context line) for the user to pick. Caches the result list under
        # "search:<q>" in data/geocache.json; same Nominatim UA; never touches the trip.
        b = self._body()
        query = (b.get("query") or "").strip()
        if not query:
            return self._json(400, {"error": "missing query"})
        cache_path = os.path.join(C.DATA_DIR, "geocache.json")
        cache = C.read_json(cache_path, {}) or {}
        ckey = "search:" + query.lower()
        cached = cache.get(ckey)
        if isinstance(cached, list):
            return self._json(200, {"ok": True, "results": cached})
        results = []
        try:
            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"q": query, "format": "json", "limit": 6, "addressdetails": 1, "accept-language": "en"})
            req = urllib.request.Request(url, headers={"User-Agent": "travel-planner/1.0 (personal, localhost)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            for d in (data or []):
                try:
                    lat = float(d["lat"]); lng = float(d["lon"])
                except Exception:
                    continue
                dn = (d.get("display_name") or "").strip()
                parts = [p.strip() for p in dn.split(",") if p.strip()]
                name = (d.get("name") or "").strip() or (parts[0] if parts else query)
                detail = ", ".join(parts[1:4])
                kind = self._osm_kind(d.get("class") or d.get("category"), d.get("type"))
                results.append({"label": name, "lat": lat, "lng": lng, "kind": kind, "detail": detail[:90]})
        except Exception:
            results = []
        cache[ckey] = results
        C.write_json(cache_path, cache)
        return self._json(200, {"ok": True, "results": results})

    def geocode_city(self):
        # Reverse-geocode a coordinate to its CITY name (zoom 10), for the weather climate-card labels —
        # a cluster's place is often a hotel ("At Six", "Amanjiwo"), so we resolve the city it sits in
        # ("Stockholm", "Magelang"). Cached as "city:lat,lng" in data/geocache.json; same Nominatim UA. A
        # rate-limit / error is NOT cached, so the client retries (and won't bake in a venue-name fallback).
        b = self._body()
        try:
            lat = float(b.get("lat")); lng = float(b.get("lng"))
        except Exception:
            return self._json(400, {"error": "bad lat/lng"})
        key = "city:%.3f,%.3f" % (lat, lng)
        cache_path = os.path.join(C.DATA_DIR, "geocache.json")
        cache = C.read_json(cache_path, {}) or {}
        hit = cache.get(key)
        if isinstance(hit, dict):
            return self._json(200, {"ok": True, "city": hit.get("city", ""), "country": hit.get("country", "")})
        try:
            url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
                {"lat": lat, "lon": lng, "format": "json", "zoom": 10, "addressdetails": 1, "accept-language": "en"})
            req = urllib.request.Request(url, headers={"User-Agent": "travel-planner/1.0 (personal, localhost)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return self._json(200, {"ok": False, "reason": "rate"})   # transient: don't cache, let the client retry
            data = None
        except Exception:
            data = None
        if not isinstance(data, dict):
            return self._json(200, {"ok": False, "reason": "error"})
        addr = data.get("address") or {}
        city = (addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
                or addr.get("county") or addr.get("state") or addr.get("region") or "")
        country = addr.get("country") or ""
        cache[key] = {"city": city, "country": country}
        C.write_json(cache_path, cache)
        return self._json(200, {"ok": True, "city": city, "country": country})

    def weather_summary(self):
        # AI weather summary tailored to the itinerary: prompt built from the trip's STORED climate cards +
        # activity mix, run through the local `claude` CLI (no API key). Cached by prompt-hash in
        # cache/wx_summary.json so a hover is instant and a re-gen only happens when the weather/plan changes.
        # Best-effort: any failure returns ok:False and the UI falls back to the plain card.
        b = self._body()
        tid = (b.get("id") or "").strip()
        if not C.is_safe_trip_id(tid):
            return self._json(400, {"error": "bad id"})
        trip = C.load_trip(tid)
        if not trip:
            return self._json(404, {"error": "no such trip"})
        cards = b.get("cards")
        prompt = _wx_summary_prompt(trip, cards if isinstance(cards, list) else None)
        if not prompt:
            return self._json(200, {"ok": False, "reason": "noclimate"})   # weather not computed yet
        import hashlib
        sig = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16]
        cache = C.read_json(WX_SUMMARY_CACHE, {}) or {}
        hit = cache.get(tid)
        if isinstance(hit, dict) and hit.get("sig") == sig and hit.get("text"):
            return self._json(200, {"ok": True, "text": hit["text"], "cached": True})
        try:
            text = _ask_claude_text(prompt)
        except Exception as e:
            return self._json(200, {"ok": False, "reason": str(e)[:160]})   # transient — don't cache, let it retry
        text = (text or "").strip()
        if not text:
            return self._json(200, {"ok": False, "reason": "empty"})
        cache[tid] = {"sig": sig, "text": text}
        C.write_json(WX_SUMMARY_CACHE, cache)
        return self._json(200, {"ok": True, "text": text})

    def upload_cover(self):
        """Body = raw image bytes; ?id=<trip>. Convert+resize to data/covers/<id>.jpg."""
        qs = parse_qs(urlparse(self.path).query)
        tid = (qs.get("id") or [""])[0]
        if not C.is_safe_trip_id(tid):   # tid flows into covers/<tid>.jpg — gate it like save_trip
            return self._json(400, {"error": "bad id"})
        trip = C.load_trip(tid)
        if not trip:
            return self._json(404, {"error": "no such trip"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n or n > 30 * 1024 * 1024:
            return self._json(400, {"error": "bad image size"})
        raw = self.rfile.read(n)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".img", dir=C.CACHE_DIR)
        tmp.write(raw); tmp.close()
        out = os.path.join(C.COVERS_DIR, tid + ".jpg")
        C.cover_history_seed(trip)         # keep the cover currently on screen as a go-back target
        try:
            r = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1400", tmp.name, "--out", out],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        finally:
            try: os.unlink(tmp.name)
            except OSError: pass
        if r.returncode != 0 or not os.path.exists(out):
            return self._json(400, {"error": "not a valid image"})
        trip["cover"] = "covers/%s.jpg" % tid
        trip["coverSource"] = "upload"
        trip["coverImageUrl"] = None       # an upload has no source URL; re-rolls start fresh from here
        trip["coverVer"] = C.bump_cover_ver(trip.get("coverVer"))
        C.cover_history_add(trip, out)     # record the uploaded cover as the newest history entry
        C.save_trip(trip); C.rebuild_index()
        cb, cf = C.cover_history_flags(trip)
        return self._json(200, {"ok": True, "cover": trip["cover"], "coverVer": trip["coverVer"],
                                "canBack": cb, "canFwd": cf})

    def upload_doc(self):
        """Body = raw file bytes; ?id=<trip>&name=<filename>. Copy a drag-dropped file INTO the trip's
        Travel Plan folder. Append-only: written inside `trip_doc_folder` (gated to a direct child of
        TRAVEL_DIR), name deduped so it NEVER overwrites, and nothing is ever deleted."""
        qs = parse_qs(urlparse(self.path).query)
        tid = (qs.get("id") or [""])[0]
        if not C.is_safe_trip_id(tid):
            return self._json(400, {"error": "bad id"})
        trip = C.load_trip(tid)
        if not trip:
            return self._json(404, {"error": "no such trip"})
        folder = trip_doc_folder(trip)
        if not folder:
            return self._json(400, {"error": "this trip has no Travel Plan folder"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > 500 * 1024 * 1024:
            return self._json(400, {"error": "bad size"})
        try:
            final = write_upload(folder, (qs.get("name") or [""])[0], self.rfile, n)
        except Exception:
            return self._json(500, {"error": "write failed"})
        if not final:
            return self._json(400, {"error": "bad filename"})
        return self._json(200, {"ok": True, "name": final})

    def auto_cover(self):
        """Re-roll a DIFFERENT destination photo for one trip (home 'Change photo' button). covers.py
        --rotate appends the new cover to the trip's history (and exits nonzero if it can't find one)."""
        tid = self._body().get("id", "")
        if not tid:
            return self._json(400, {"error": "missing id"})
        r = self._run_import("covers.py", tid, ["--rotate"])
        t = C.load_trip(tid) or {}
        cb, cf = C.cover_history_flags(t)
        return self._json(200, {"ok": r.returncode == 0,
                                "cover": t.get("cover"), "coverVer": t.get("coverVer"),
                                "landmark": t.get("coverLandmark"),
                                "canBack": cb, "canFwd": cf,
                                "log": (r.stdout or "")[-600:]})

    def cover_nav(self):
        """Step the cover back (dir<0) or forward (dir>0) through the trip's full cover history,
        copying that stored cover over the live one. Lossless: nothing is discarded, so the user can
        walk through ALL past covers and forward again. Touches only data/covers."""
        b = self._body()
        tid = b.get("id", "")
        if not C.is_safe_trip_id(tid):   # tid flows into covers/<tid>.*.jpg — gate like upload_cover
            return self._json(400, {"error": "bad id"})
        trip = C.load_trip(tid)
        if not trip:
            return self._json(404, {"error": "no such trip"})
        try:
            delta = int(b.get("dir", -1)) or -1
        except (TypeError, ValueError):
            delta = -1
        e = C.cover_history_go(trip, -1 if delta < 0 else 1)
        if not e:
            return self._json(400, {"error": "nothing more that way"})
        C.save_trip(trip); C.rebuild_index()
        cb, cf = C.cover_history_flags(trip)
        return self._json(200, {"ok": True, "cover": trip["cover"], "coverVer": trip["coverVer"],
                                "landmark": trip.get("coverLandmark"),
                                "canBack": cb, "canFwd": cf})

    def log_message(self, *a):
        pass


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _tailscale_url():
    """Best-effort http URL to open on another device (your phone) over Tailscale, or None."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        for line in (out.stdout or "").splitlines():
            ip = line.strip()
            if ip:
                return "http://%s:%d" % (ip, PORT)
    except Exception:
        pass
    return None


if __name__ == "__main__":
    C.ensure_dirs()
    # This server has NO auth; HOST controls the bind address:
    #   (unset)      -> 127.0.0.1, loopback only. Safest; only this Mac.                    [default]
    #   HOST=0.0.0.0 -> listen on every interface so your phone can reach it over Tailscale,
    #                   BUT _client_allowed() still answers only loopback + the Tailscale
    #                   tailnet, so the café/airport Wi-Fi you're on cannot. `localhost` still works.
    #   ALLOW_LAN=1  -> additionally serve the local private network (the old LAN behaviour).
    # (You can still pin HOST=<your Tailscale IP> to physically bind that one interface.)
    HOST = os.environ.get("HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    shown = "localhost" if HOST in ("127.0.0.1", "") else HOST
    print("Travel Planner on http://%s:%d  dir=%s" % (shown, PORT, C.APP_DIR), flush=True)
    if HOST not in ("127.0.0.1", "::1", "localhost"):
        gate = "loopback + Tailscale" + (" + LAN" if len(ALLOW_NETS) > len(_TAILSCALE_NETS) else "")
        print("  serving: %s only (no auth)" % gate, flush=True)
        ts = _tailscale_url()
        if ts:
            print("  on your phone (Tailscale): %s" % ts, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
