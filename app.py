# app.py — "slow & gentle" edition with clear throttling levers
# Compatible with your existing index.html and endpoints.
#
# TUNABLE LEVERS (safe to change):
#   POLL_INTERVAL_SEC       — how often the background poll wakes up
#   STATES_PER_TICK         — how many states to poll per wake
#   MAX_WORKERS             — parallel upstream requests (keep tiny)
#   PER_REQUEST_DELAY_MS    — pause between each upstream request
#   REQUEST_TIMEOUT_SEC     — upstream request timeout
#   CACHE_TTL_SEC           — how long parsed snapshots are considered fresh
#   SLOW_ENDPOINT_MS        — artificial delay added to *every* API response
#   ENABLE_POLLING          — flip to False to disable background poller

from flask import Flask, send_from_directory, request, jsonify
import requests, xml.etree.ElementTree as ET
import time, re, threading, sys, json, random
import unicodedata, os
import concurrent.futures

app = Flask(__name__, static_folder='.', static_url_path='')

# ----------------------------- EASY THROTTLE LEVERS -----------------------------
API_KEY               = os.getenv("ELECTIONS_API_KEY", "4uwfiazjez9koo7aju9ig4zxhr")
BASE_URL              = "https://api2-app2.onrender.com/v2/elections"
DISTRICTS_BASE        = "https://api2-app2.onrender.com/v2/districts"
ELECTION_DATE         = "2024-11-05"

ENABLE_POLLING        = True          # set False to fully stop background poller
POLL_INTERVAL_SEC     = 30            # wakeup cadence (e.g., every 30s)
STATES_PER_TICK       = 2             # how many states to poll each wake
MAX_WORKERS           = 1             # parallel upstream calls (keep small on free tier)
PER_REQUEST_DELAY_MS  = 350           # pause between *each* upstream hit
REQUEST_TIMEOUT_SEC   = 6             # upstream timeout
CACHE_TTL_SEC         = 30            # how long parsed snapshots are "fresh"
SLOW_ENDPOINT_MS      = 150           # add artificial delay to *all* responses (0 to disable)

# Optional jitter so traffic isn't bursty (set to 0 to disable)
JITTER_MS_LO, JITTER_MS_HI = 25, 125

# --------------------------------------------------------------------------------

# Round-robin list of states (includes DC)
ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY","DC"
]

# Raw XML snapshots (from background poller)
_raw_cache = {
    "states":    {},  # key: (state, office) -> {payload:str, ts:float}
    "districts": {}   # key: (state,)       -> {payload:str, ts:float}
}
_raw_lock = threading.Lock()

# Parsed per-state cache that endpoints serve
_parsed_cache = {}  # key: (state, office) -> (ts, dict)
_inflight_evt  = {} # key: (state, office) -> threading.Event
_cache_lock    = threading.Lock()

# ----------------------------- Helper utilities -----------------------------
def _normalize(txt: str) -> str:
    txt = (txt or "").strip().lower()
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^\w\s]", "", txt)

_SUFFIX_RE = re.compile(
    r"\s+(city and borough|census area|borough|municipality|parish|county)$",
    re.IGNORECASE
)

def _base_name(n: str) -> str:
    return _SUFFIX_RE.sub("", _normalize(n))

def _safe_int(raw_vote: str) -> int:
    try:
        return int(re.sub(r"[^\d]", "", raw_vote or "0"))
    except ValueError:
        return 0

def _sleep_ms(ms: int):
    # adds tiny jitter so bursts don't align perfectly
    if ms <= 0:
        return
    j = random.randint(JITTER_MS_LO, JITTER_MS_HI) if (JITTER_MS_HI > 0) else 0
    time.sleep((ms + j) / 1000.0)

# ----------------------------- Poller (very slow) -----------------------------
def _fetch_upstream(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"x-api-key": API_KEY}, timeout=REQUEST_TIMEOUT_SEC)
        if r.ok:
            return r.text
        else:
            sys.stdout.write(f"[POLL] non-200 {r.status_code} for {url}\n")
            sys.stdout.flush()
            return None
    except Exception as e:
        sys.stdout.write(f"[POLL] error {e} for {url}\n")
        sys.stdout.flush()
        return None

def _poll_one_state(state: str):
    # Hit P, S, G one by one (with delays), then districts
    offices = ["P","S","G"]
    for office in offices:
        url = f"{BASE_URL}/{ELECTION_DATE}?statepostal={state}&officeId={office}&level=ru"
        xml = _fetch_upstream(url)
        if xml:
            with _raw_lock:
                _raw_cache["states"][(state, office)] = {"payload": xml, "ts": time.time()}
        _sleep_ms(PER_REQUEST_DELAY_MS)

    # districts
    url = f"{DISTRICTS_BASE}/{ELECTION_DATE}?statepostal={state}&officeId=H&level=ru"
    xml = _fetch_upstream(url)
    if xml:
        with _raw_lock:
            _raw_cache["districts"][(state,)] = {"payload": xml, "ts": time.time()}
    _sleep_ms(PER_REQUEST_DELAY_MS)

def poller_loop():
    idx = 0
    n = len(ALL_STATES)
    while True:
        tick_start = time.time()

        if not ENABLE_POLLING:
            _sleep_ms(int(POLL_INTERVAL_SEC * 1000))
            continue

        # round-robin chunk [idx : idx+STATES_PER_TICK)
        chunk = []
        for _ in range(STATES_PER_TICK):
            chunk.append(ALL_STATES[idx % n])
            idx += 1

        sys.stdout.write(f"[POLL] tick states: {', '.join(chunk)}\n")
        sys.stdout.flush()

        # VERY low concurrency
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_poll_one_state, st) for st in chunk]
            for fut in concurrent.futures.as_completed(futures):
                # ensure any exception logs (but we don't raise)
                try:
                    fut.result()
                except Exception as e:
                    sys.stdout.write(f"[POLL] worker error: {e}\n")
                    sys.stdout.flush()

        tick_elapsed = time.time() - tick_start
        # sleep the difference (never negative)
        to_sleep = max(0.0, POLL_INTERVAL_SEC - tick_elapsed)
        time.sleep(to_sleep)

# Start the background poller (daemon)
if ENABLE_POLLING:
    threading.Thread(target=poller_loop, daemon=True).start()

# ----------------------------- Parsing utilities -----------------------------
def _parse_election_xml(xml_blob: str) -> dict:
    """
    Parse once into a per-county structure:

    { "<base_name>": {
        "candidates": [ { "name": "First Last", "party": "REP|DEM|IND", "votes": 123 }, ... ],
        "total": 12345
      }, ...
    }
    """
    try:
        root = ET.fromstring(xml_blob)
    except ET.ParseError:
        raise RuntimeError("bad-xml")

    out: dict[str, dict] = {}
    for ru in root.iter("ReportingUnit"):
        ru_name = ru.attrib.get("Name", "")
        key = _base_name(ru_name)
        cands = []
        total = 0
        for c in ru.findall("Candidate"):
            votes = _safe_int(c.attrib.get("VoteCount"))
            first = (c.attrib.get("First","") or "").strip()
            last  = (c.attrib.get("Last","") or "").strip()
            full  = f"{first} {last}".strip()
            party = (c.attrib.get("Party") or "").upper()
            cands.append({"name": full, "party": party, "votes": votes})
            total += votes
        out[key] = {"candidates": cands, "total": total}
    return out

def _get_parsed_state(state_code: str, office: str = "P") -> dict | None:
    """
    Single-flight dedupe: return cached parsed snapshot if fresh; otherwise
    parse a fresh one from the raw cache. Never bursts the upstream.
    """
    office = (office or "P").upper()
    now    = time.time()
    key    = (state_code, office)

    with _cache_lock:
        snap = _parsed_cache.get(key)
        if snap and now - snap[0] < CACHE_TTL_SEC:
            return snap[1]
        evt = _inflight_evt.get(key)
        if not evt:
            evt = threading.Event()
            _inflight_evt[key] = evt
            owner = True
        else:
            owner = False

    if not owner:
        # wait for owner to finish
        evt.wait(timeout=max(2 * CACHE_TTL_SEC, 10))
        with _cache_lock:
            snap = _parsed_cache.get(key)
            return snap[1] if snap else None

    try:
        with _raw_lock:
            raw = _raw_cache["states"].get((state_code, office))
        if not raw:
            return None
        parsed = _parse_election_xml(raw["payload"])
        with _cache_lock:
            _parsed_cache[key] = (now, parsed)
        return parsed
    finally:
        with _cache_lock:
            ev = _inflight_evt.get(key)
            if ev is not None:
                ev.set()
                _inflight_evt.pop(key, None)

# ----------------------------- Response slow-down -----------------------------
@app.after_request
def add_no_store(resp):
    # cache busting for browsers
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

def _slow_endpoint():
    if SLOW_ENDPOINT_MS > 0:
        _sleep_ms(SLOW_ENDPOINT_MS)

# ----------------------------- Routes -----------------------------
@app.route("/")
def root():
    return send_from_directory(".", "index.html")

@app.route("/clientlog", methods=["POST"])
def clientlog():
    try:
        payload = request.get_json(force=True)
    except Exception:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "bad-json"}), 200
    sys.stdout.write("[CLIENTLOG] " + json.dumps(payload) + "\n")
    sys.stdout.flush()
    _slow_endpoint()
    return jsonify({"ok": True}), 200

@app.route("/results_state")
def per_state_bulk():
    state  = (request.args.get("state") or "").upper()
    office = (request.args.get("office", "P") or "P").upper()

    with _raw_lock:
        raw = _raw_cache["states"].get((state, office))
    if not raw:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "no-snapshot"}), 200

    # parse from cached raw (and memoize parsed)
    try:
        parsed = _get_parsed_state(state, office)
        if parsed is None:
            # fallback: parse directly now to be helpful
            parsed = _parse_election_xml(raw["payload"])
    except Exception as e:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "parse-failed", "detail": str(e)}), 200

    _slow_endpoint()
    return jsonify({
        "ok": True,
        "state": state,
        "office": office,
        "counties": parsed
    }), 200

@app.route("/results")
def per_county():
    state  = (request.args.get("state") or "").upper()
    county = request.args.get("county", "")
    office = (request.args.get("office", "P") or "P").upper()

    # soft-error contract (always 200)
    if not (state and county):
        _slow_endpoint()
        return jsonify({"ok": False, "error": "missing-params"}), 200

    try:
        snap = _get_parsed_state(state, office)
    except Exception as e:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "api-unavailable", "detail": str(e)}), 200

    if not snap:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "no-snapshot"}), 200

    want_key = _base_name(county)
    info = snap.get(want_key)
    if not info:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "county-not-found"}), 200

    _slow_endpoint()
    return jsonify({
        "ok": True,
        "state": state,
        "county": county,
        "office": office,
        "results": info["candidates"]
    }), 200

@app.route("/results_districts")
def per_districts_bulk():
    state = (request.args.get("state") or "").upper()
    with _raw_lock:
        snap = _raw_cache["districts"].get((state,))
    if not snap:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "no-snapshot"}), 200

    try:
        root = ET.fromstring(snap["payload"])
    except ET.ParseError:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "bad-xml"}), 200

    districts = {}
    for ru in root.iter("ReportingUnit"):
        geoid = (ru.attrib.get("DistrictId") or "").strip()
        if not geoid:
            continue
        cands, total = [], 0
        for c in ru.findall("Candidate"):
            votes = _safe_int(c.attrib.get("VoteCount"))
            full  = f"{(c.attrib.get('First','') or '').strip()} {(c.attrib.get('Last','') or '').strip()}".strip()
            party = (c.attrib.get("Party") or "").upper()
            cands.append({"name": full, "party": party, "votes": votes})
            total += votes
        districts[geoid] = {"candidates": cands, "total": total}

    _slow_endpoint()
    return jsonify({
        "ok": True,
        "state": state,
        "office": "H",
        "districts": districts
    }), 200

@app.route("/results_cd")
def per_district():
    state  = (request.args.get("state") or "").upper()
    district_id = request.args.get("district", "")
    if not (state and district_id):
        _slow_endpoint()
        return jsonify({"ok": False, "error": "missing-params"}), 200

    # we fetch only the state districts snapshot and search inside it (keeps upstream slow)
    url = f"{DISTRICTS_BASE}/{ELECTION_DATE}?statepostal={state}&level=ru"
    try:
        resp = requests.get(url, headers={"x-api-key": API_KEY}, timeout=REQUEST_TIMEOUT_SEC)
        if resp.status_code != 200:
            _slow_endpoint()
            return jsonify({"ok": False, "error": f"upstream-{resp.status_code}"}), 200
    except Exception as e:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "api-unavailable", "detail": str(e)}), 200

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        _slow_endpoint()
        return jsonify({"ok": False, "error": "bad-xml"}), 200

    want = str(district_id).strip().lstrip("0")
    for ru in root.iter("ReportingUnit"):
        did   = (ru.attrib.get("DistrictId") or "").strip()
        dnum  = (ru.attrib.get("District") or "").strip()
        label = (ru.attrib.get("Name") or "").strip()

        match = (
            (did and want == str(did).strip().lstrip("0")) or
            (dnum and want == str(dnum).strip().lstrip("0")) or
            (label and want in label)
        )
        if match:
            out = []
            for c in ru.findall("Candidate"):
                votes = _safe_int(c.attrib.get("VoteCount"))
                first = (c.attrib.get('First','').strip())
                last  = (c.attrib.get('Last','').strip())
                out.append({
                    "name": f"{first} {last}".strip(),
                    "party": c.attrib.get("Party"),
                    "votes": votes
                })
            _slow_endpoint()
            return jsonify({
                "ok": True,
                "district": district_id,
                "state": state,
                "results": out
            }), 200

    _slow_endpoint()
    return jsonify({"ok": False, "error": "district-not-found"}), 200

if __name__ == "__main__":
    # Render/Heroku-style: PORT is injected; default for local dev is 9032
    port = int(os.getenv("PORT", "9032"))
    # debug=False keeps polling as-is; you can set to True locally if desired
    app.run(host="0.0.0.0", port=port, debug=False)
