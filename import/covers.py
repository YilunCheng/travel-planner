#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Give each trip a real DESTINATION PHOTO as its home-page cover (instead of the Apple
Pages itinerary-table screenshot).

  1) `claude -p` picks one iconic, photogenic landmark per trip (it knows "Iceland" ->
     Kirkjufell, "Peru" -> Machu Picchu, "SLC HI" -> Nā Pali Coast).
  2) Wikipedia/Wikimedia (free, no key) supplies that landmark's lead photo.
  3) sips resizes it to a cover and writes data/covers/<id>.jpg.

User-uploaded covers (coverSource=="upload") are never overwritten.

  python3 import/covers.py --all          # all trips lacking a destination photo
  python3 import/covers.py --all --force  # refetch every trip
  python3 import/covers.py --id <trip-id> [--landmark "Kirkjufell"]
"""

import os, sys, json, re, time, argparse, subprocess, tempfile
import urllib.request, urllib.parse, urllib.error
import common as C
import structure  # reuse ask_claude / parse_json

UA = "travel-planner-local/1.0 (personal itinerary tool)"

PROMPT = """For each trip, name ONE iconic, photogenic landmark or scene that best represents the destination AND is very likely to have a good lead photo on English Wikipedia. Prefer a specific named place (a mountain, waterfall, monument, old town, national park, beach, reef) over a bare country name. Output ONLY a JSON object mapping each id to a Wikipedia article title — nothing else.

Trips:
{trips}

Examples: "Iceland"->"Kirkjufell"; "Peru"->"Machu Picchu"; "Stockholm"->"Gamla stan"; "Botswana"->"Okavango Delta"; "Galapagos"->"Pinnacle Rock"; "Tanzania"->"Serengeti National Park"; "SLC HI"->"Nā Pali Coast"; "Courchevel"->"Courchevel"; "Maldives SriLanka"->"Maldives"."""

# Hand-curated, verified landmark per trip — the authoritative source (claude's mapping is
# only a fallback for ids not listed here). A bare country/visa name geocodes to a flag or a
# map, which we reject; a specific iconic place reliably has a good Wikipedia lead photo.
# Admin-only trips with no real destination (visa/admin folders) are intentionally
# omitted — they keep the letter placeholder until the user uploads a cover.
# Personal data (real trip ids → landmarks), so it lives in data/cover_landmarks.json,
# not in code (missing file ⇒ empty dict, claude/title fallbacks take over).
FALLBACK_LANDMARKS = C.read_json(os.path.join(C.DATA_DIR, "cover_landmarks.json"), {}) or {}


def wiki_image(title, tries=5):
    """Lead image URL for a Wikipedia article title (follows redirects/normalization).
    Exponential backoff — survives sustained 429 rate-limiting, not just a transient blip."""
    if not title:
        return None
    t = urllib.parse.quote(title.strip().replace(" ", "_"), safe="")
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + t
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
            return (d.get("originalimage") or d.get("thumbnail") or {}).get("source")
        except urllib.error.HTTPError as e:
            if e.code == 404:           # genuine miss — no point retrying
                return None
            time.sleep(1.5 * (2 ** k))  # 1.5, 3, 6, 12, 24s — back off hard on 429/5xx
        except Exception:
            time.sleep(1.5 * (2 ** k))
    return None


def commons_images(query, limit=20):
    """Up to `limit` decent Commons photo URLs for a free-text query, in search-rank order —
    the candidate pool the home 'Change photo' re-roll cycles through for variety."""
    out = []
    try:
        url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "generator": "search", "gsrsearch": query,
            "gsrnamespace": "6", "gsrlimit": str(limit), "prop": "imageinfo",
            "iiprop": "url", "iiurlwidth": "1400", "format": "json"})
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        pages = (d.get("query") or {}).get("pages") or {}
        for p in sorted(pages.values(), key=lambda x: x.get("index", 1e9)):   # keep search ranking
            ii = (p.get("imageinfo") or [{}])[0]
            u = ii.get("thumburl") or ii.get("url")
            if u and re.search(r"\.(jpe?g|png)$", u, re.I):
                out.append(u)
    except Exception:
        pass
    return out


def commons_image(query):
    """Fallback: first decent Commons photo matching a free-text query."""
    xs = commons_images(query, 1)
    return xs[0] if xs else None


def bad_image(url):
    """Reject flags / coats of arms / maps / SVGs — never good trip covers."""
    return not url or bool(re.search(r"Flag_of|Coat_of_arms|Map_of|Location_|Orthographic|\.svg", url, re.I))


def img_key(url):
    """Identity of the underlying photo, independent of thumbnail size / query string — so the same
    file as a 1400px thumb and as the article original count as one 'seen' image during re-rolls."""
    base = (url or "").split("?")[0].rsplit("/", 1)[-1]
    return re.sub(r"^\d+px-", "", base).lower()


def save_cover(trip_id, img_url):
    if not img_url:
        return False
    data = None
    for k in range(5):                 # retry download (image CDN rate-limits bursts)
        try:
            req = urllib.request.Request(img_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            if data and len(data) >= 4000:
                break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
        except Exception:
            pass
        time.sleep(1.5 * (2 ** k)); data = None   # 1.5,3,6,12,24s — back off hard on 429
    if not data or len(data) < 4000:
        return False
    suffix = ".png" if img_url.lower().split("?")[0].endswith(".png") else ".jpg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=C.CACHE_DIR)
    tmp.write(data); tmp.close()
    out = os.path.join(C.COVERS_DIR, trip_id + ".jpg")
    try:
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1400", tmp.name, "--out", out],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        ok = r.returncode == 0 and os.path.exists(out)
    finally:
        try: os.unlink(tmp.name)
        except OSError: pass
    return ok


def cover_trip(trip, landmark=None):
    title = trip.get("title", "")
    if trip.get("coverSource") == "upload":   # a user upload must survive EVERY path, incl. --id (batch already skips)
        return False, "user-uploaded cover — left untouched"
    landmark = landmark or FALLBACK_LANDMARKS.get(trip["id"])   # curated pick wins (also drives the --id button)
    if landmark:                      # persist the pick now, so re-runs can reuse it
        trip["coverLandmark"] = landmark
        C.save_trip(trip)
    # Try, in order: claude landmark -> title -> Commons scenic search. A flag / map /
    # coat-of-arms / SVG is never an acceptable cover, so skip it and keep looking.
    img = used = None
    for cand in [landmark or trip.get("coverLandmark"), title]:
        if not cand:
            continue
        u = wiki_image(cand)
        if u and not bad_image(u):
            img, used = u, cand; break
    if not img:
        for q in (title + " landscape", title + " landmark", title):
            u = commons_image(q)
            if u and not bad_image(u):
                img, used = u, title; break
    if not save_cover(trip["id"], img):
        return False, "no usable image for '%s'" % title
    trip["cover"] = "covers/%s.jpg" % trip["id"]
    trip["coverSource"] = "wikipedia"
    trip["coverLandmark"] = used
    trip["coverImageUrl"] = img            # remember the source photo so a later re-roll can avoid it
    trip["coverSeen"] = [img_key(img)]
    trip["coverSeenLandmarks"] = [used]
    trip["coverVer"] = C.bump_cover_ver(trip.get("coverVer"))
    C.cover_history_reset(trip, C.cover_path(trip["id"]))   # batch/first cover starts a fresh history
    C.save_trip(trip)
    return True, "← %s" % used


LIST_PROMPT = """Name {n} DISTINCT iconic, photogenic landmarks or scenes for this trip's destination — each a specific named place (a mountain, waterfall, glacier, monument, old town, national park, beach, reef, lake) that is very likely to have a good lead photo on English Wikipedia. Order them most-iconic first. Avoid the bare country/city name. Output ONLY a JSON array of Wikipedia article titles, nothing else.

Trip: {title}"""


def landmark_list(trip, n=8):
    """Ask claude ONCE for several photogenic landmarks for this destination (cached on the trip),
    so re-rolls can move between DIFFERENT places, not just photos of one."""
    try:
        res = structure.parse_json(structure.ask_claude(LIST_PROMPT.format(n=n, title=trip.get("title", ""))))
    except Exception:
        return []
    if isinstance(res, dict):                       # tolerate {"landmarks":[...]} or an id-keyed object
        res = next((v for v in res.values() if isinstance(v, list)), [])
    return [str(x).strip() for x in res if str(x).strip()][:n] if isinstance(res, list) else []


def landmark_options(trip):
    """Ordered, deduped landmark candidates for a destination: the curated pick first, then a cached
    (or freshly-asked) claude list for variety, then the current landmark and the title as backstops."""
    opts = []
    def add(x):
        x = (x or "").strip()
        if x and x not in opts:
            opts.append(x)
    add(FALLBACK_LANDMARKS.get(trip["id"]))
    for x in (trip.get("coverLandmarks") or []):
        add(x)
    if len(opts) < 4:                               # not enough variety yet → ask claude once, then cache it
        got = landmark_list(trip, 8)
        if got:
            trip["coverLandmarks"] = got
            C.save_trip(trip)
            for x in got:
                add(x)
    add(trip.get("coverLandmark"))
    add(trip.get("title"))
    return opts


def rotate_cover(trip):
    """Re-roll the cover to a DIFFERENT landmark of the destination (then, once landmarks are
    exhausted, a different photo of one). Cycles landmarks via `coverSeenLandmarks` and photos via
    `coverSeen`, so repeat presses keep surfacing something new instead of bouncing between two.
    Seeds the current cover into the multi-level history (cover_history_seed/add) so the home
    '↩'/'↪' can walk back and forward. Drives the home 'Change photo' button via `covers.py --id --rotate`."""
    title = trip.get("title", "")
    lms = landmark_options(trip)
    if not lms:
        return False, "no landmark for '%s'" % (title or trip["id"])
    cur_lm = trip.get("coverLandmark")
    current = trip.get("coverImageUrl")
    ph_list = list(trip.get("coverSeen") or [])            # ordered photo history (img_key), recent last
    cur_key = img_key(current) if current else None
    if not cur_key and trip.get("cover") and cur_lm:      # 1st re-roll on a pre-existing cover: assume it's
        lead = wiki_image(cur_lm)                          # the current landmark's lead → never re-pick it
        if lead:
            cur_key = img_key(lead)
    if cur_key and cur_key not in ph_list:                # record what's on screen so it's not re-picked soon
        ph_list.append(cur_key)
    seen_ph = set(ph_list)
    lm_list = list(trip.get("coverSeenLandmarks") or [])
    if cur_lm and cur_lm not in lm_list:
        lm_list.append(cur_lm)                             # don't re-show the landmark already on screen
    seen_lm = set(lm_list)
    order = [l for l in lms if l not in seen_lm] or [l for l in lms if l != cur_lm] or lms

    pick = pick_lm = None
    reset = False
    for lm in order:                                        # first landmark that still has an unseen photo
        pool, keys = [], set()
        for u in [wiki_image(lm)] + commons_images(lm, 20):
            if u and not bad_image(u):
                k = img_key(u)
                if k not in keys:
                    keys.add(k); pool.append(u)
        if not pool:
            continue
        fresh = [u for u in pool if img_key(u) not in seen_ph]
        if fresh:
            pick, pick_lm, reset = fresh[0], lm, False     # a genuinely-new photo wins over any reset fallback
            break
        if pick is None:                                   # fallback: every photo seen — reuse this landmark's
            cand = [u for u in pool if not cur_key or img_key(u) != cur_key] or pool
            pick, pick_lm, reset = cand[0], lm, True       # best (minus the exact current) and start a new cycle
    if pick is None:
        return False, "no photos found for '%s'" % (cur_lm or title or trip["id"])
    # Capture the cover currently on screen as a go-back target, THEN replace it. save_cover only
    # overwrites covers/<id>.jpg on a successful download, so a failure never destroys the old cover.
    C.cover_history_seed(trip)
    if not save_cover(trip["id"], pick):
        return False, "couldn't download a new photo"
    pk = img_key(pick)
    if reset:                              # showed every photo of every landmark → restart the cycle
        ph_list, lm_list = [], []
    ph_list = [x for x in ph_list if x != pk] + [pk]            # move-to-end keeps recent history last
    lm_list = [x for x in lm_list if x != pick_lm] + [pick_lm]
    trip["coverSeen"] = ph_list[-24:]      # bounded so we cycle rather than grow forever
    trip["coverSeenLandmarks"] = lm_list[-12:]
    trip["coverImageUrl"] = pick
    trip["cover"] = "covers/%s.jpg" % trip["id"]
    trip["coverSource"] = "wikipedia"
    trip["coverLandmark"] = pick_lm
    trip["coverVer"] = C.bump_cover_ver(trip.get("coverVer"))
    C.cover_history_add(trip, C.cover_path(trip["id"]))         # record the new cover as the newest entry
    C.save_trip(trip)
    return True, "↻ %s" % pick_lm


def pick_landmarks(trips):
    """Map id -> Wikipedia landmark title, in chunks so no single response truncates."""
    out = {}
    for i in range(0, len(trips), 22):
        chunk = trips[i:i + 22]
        listing = "\n".join("%s | %s" % (t["id"], t.get("title", "")) for t in chunk)
        try:
            res = structure.parse_json(structure.ask_claude(PROMPT.format(trips=listing)))
            if isinstance(res, dict):
                out.update(res)
        except Exception as e:
            print("  (landmark chunk failed: %s)" % str(e)[:100])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--landmark", help="explicit Wikipedia landmark for --id")
    ap.add_argument("--rotate", action="store_true", help="re-roll a DIFFERENT photo (home 'Change photo' button)")
    args = ap.parse_args()
    C.ensure_dirs()

    if args.id:
        t = C.load_trip(args.id)
        if not t: print("no such trip"); return
        ok, msg = rotate_cover(t) if args.rotate else cover_trip(t, landmark=args.landmark)
        print("  %-30s %s %s" % (t["id"], "✓" if ok else "·", msg))
        C.rebuild_index()
        sys.exit(0 if ok else 1)   # nonzero ⇒ the home button reports "no new photo"

    trips = [t for t in C.all_trips() if t and not t.get("archive")]
    if args.force:                       # refetch, but never clobber user uploads in batch mode
        trips = [t for t in trips if t.get("coverSource") != "upload"]
    else:
        trips = [t for t in trips if t.get("coverSource") not in ("wikipedia", "upload")]
    if not trips:
        print("nothing to do (use --force to refetch)"); return
    # Curated dict is authoritative; only ask claude for trips it doesn't cover.
    gaps = [t for t in trips if t["id"] not in FALLBACK_LANDMARKS]
    landmarks = pick_landmarks(gaps) if gaps else {}
    for t in sorted(trips, key=lambda x: x["id"]):
        try:
            ok, msg = cover_trip(t, landmark=FALLBACK_LANDMARKS.get(t["id"]) or landmarks.get(t["id"]))
        except Exception as e:
            ok, msg = False, "ERROR " + str(e)[:160]
        print("  %-30s %s %s" % (t["id"], "✓" if ok else "·", msg), flush=True)
        time.sleep(1.2)               # space out wiki/commons requests to avoid burst rate-limiting
    C.rebuild_index()


if __name__ == "__main__":
    main()
