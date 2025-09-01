# app.py – production version with bulletproof log collector (soft-error responses)
from flask import Flask, send_from_directory, request, jsonify
import requests, xml.etree.ElementTree as ET, time, re, threading, sys, json
import unicodedata, os

app = Flask(__name__, static_folder='.', static_url_path='')

API_KEY       = "4uwfiazjez9koo7aju9ig4zxhr"
BASE_URL      = "https://api2-app2.onrender.com/v2/elections"
ELECTION_DATE = "2024-11-05"
POLL_INTERVAL = 15  # seconds

# Track outgoing upstream API calls
_outgoing_count = 0
_outgoing_lock = threading.Lock()


_cache = {"states": {}, "districts": {}}
_cache_lock = threading.Lock()

import concurrent.futures

ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY"
]

def fetch_one(url, cache_key, cache_bucket):
    try:
        r = requests.get(url, headers={"x-api-key": API_KEY}, timeout=10)
        if r.ok:
            with _cache_lock:
                _cache[cache_bucket][cache_key] = {"payload": r.text, "ts": time.time()}
    except Exception as e:
        print("Poll error:", e)

def poll_api():
    while True:
        start = time.time()
        tasks = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            for state in ALL_STATES:
                # P, S, G
                for office in ["P","S","G"]:
                    url = f"{BASE_URL}/{ELECTION_DATE}?statepostal={state}&officeId={office}&level=ru"
                    tasks.append(pool.submit(fetch_one, url, (state, office), "states"))
                # House
                url = f"{BASE_URL}/{ELECTION_DATE}".replace("/v2/elections", "/v2/districts")
                url = f"{url}?statepostal={state}&officeId=H&level=ru"
                tasks.append(pool.submit(fetch_one, url, (state,), "districts"))
            concurrent.futures.wait(tasks)
        elapsed = time.time() - start
        time.sleep(max(0, POLL_INTERVAL - elapsed))

threading.Thread(target=poll_api, daemon=True).start()



@app.after_request
def add_no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
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

def _cmp_key(n: str) -> str:
    return re.sub(r"\s+", "", _base_name(n))

def _safe_int(raw_vote: str) -> int:
    try:
        return int(re.sub(r"[^\d]", "", raw_vote or "0"))
    except ValueError:
        return 0

# --------------------------------------------------------------------------- #
# Caches + single-flight dedupe
# --------------------------------------------------------------------------- #
# Raw XML cache (as before) – caps upstream calls to at most once per (state,office,TTL)
_state_cache: dict[tuple[str,str], tuple[float,str]] = {}
# Parsed JSON snapshot cache – what the browsers will read
_parsed_cache: dict[tuple[str,str], tuple[float,dict]] = {}
# In-flight guards so concurrent requests don’t double-hit upstream for same key
_inflight: dict[tuple[str,str], threading.Event] = {}

_CACHE_TTL = 15  # seconds; tune as you like
_cache_lock = threading.Lock()

def _fetch_state_xml(state_code: str, office: str = "P") -> str:
    """Fetch raw XML once per TTL; retries/backoff included."""
    office = (office or "P").upper()
    now = time.time()
    key = (state_code, office)

    with _cache_lock:
        ts_xml = _state_cache.get(key)
        if ts_xml and now - ts_xml[0] < _CACHE_TTL:
            return ts_xml[1]

    url     = f"{BASE_URL}/{ELECTION_DATE}?statepostal={state_code}&raceTypeId=G&raceId=0&level=ru&officeId={office}"
    headers = {"x-api-key": API_KEY}

    back_off = 0.5
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=6)
        except Exception as err:
            if attempt < 2:
                time.sleep(back_off * (2 ** attempt))
                continue
            raise RuntimeError("network-error") from err

        if resp.status_code == 200:
            xml_blob = resp.text
            with _cache_lock:
                _state_cache[key] = (now, xml_blob)
            return xml_blob

        if resp.status_code in (403, 404, 429, 500, 502, 503) and attempt < 2:
            time.sleep(back_off * (2 ** attempt))
            continue

        raise RuntimeError(f"upstream-{resp.status_code}")

    raise RuntimeError("unreachable")

def _parse_election_xml(xml_blob: str) -> dict:
    """
    Parse once into a per-county structure usable by /results and /results_state:

    {
      "<base_name>": {
        "candidates": [ { "name": "First Last", "party": "REP|DEM|IND", "votes": 123 }, ... ],
        "total": 12345
      },
      ...
    }
    """
    try:
        root = ET.fromstring(xml_blob)
    except ET.ParseError:
        raise RuntimeError("bad-xml")

    out: dict[str, dict] = {}
    for ru in root.iter("ReportingUnit"):
        ru_name = ru.attrib.get("Name", "")
        key = _base_name(ru_name)  # stable county key inside a state
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
    Single-flight wrapper: if a parse for (state, office) is in progress, wait for it;
    otherwise do the fetch+parse and publish to the cache exactly once.
    """
    office = (office or "P").upper()
    now    = time.time()
    key    = (state_code, office)

    # Fast path: fresh parsed snapshot already cached
    with _cache_lock:
        snap = _parsed_cache.get(key)
        if snap and now - snap[0] < _CACHE_TTL:
            return snap[1]
        evt = _inflight.get(key)
        if not evt:
            evt = threading.Event()
            _inflight[key] = evt
            owner = True
        else:
            owner = False

    if not owner:
        # Another thread is fetching/parsing; wait for it to publish.
        evt.wait(timeout=max(2 * _CACHE_TTL, 10))
        with _cache_lock:
            snap = _parsed_cache.get(key)
            return snap[1] if snap else None

    # We are the owner: fetch + parse, then publish and release waiters.
    try:
        xml_blob = _fetch_state_xml(state_code, office)
        parsed   = _parse_election_xml(xml_blob)
        with _cache_lock:
            _parsed_cache[key] = (now, parsed)
        return parsed
    finally:
        with _cache_lock:
            ev = _inflight.get(key)
            if ev is not None:
                ev.set()
                _inflight.pop(key, None)

# --------------------------------------------------------------------------- #
# Flask routes
# --------------------------------------------------------------------------- #
@app.route("/")
def root():
    return send_from_directory(".", "index.html")

# Client-side logging endpoint (unchanged)
@app.route("/clientlog", methods=["POST"])
def clientlog():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "bad-json"}), 400
    sys.stdout.write("[CLIENTLOG] " + json.dumps(payload) + "\n")
    sys.stdout.flush()
    return jsonify({"ok": True}), 200

@app.route("/results")
def per_county():
    state  = (request.args.get("state") or "").upper()
    county = request.args.get("county", "")
    office = (request.args.get("office", "P") or "P").upper()

    # SOFT-ERROR: always HTTP 200 with ok:false
    if not (state and county):
        return jsonify({"ok": False, "error": "missing-params"}), 200

    snap = None
    try:
        snap = _get_parsed_state(state, office)
    except Exception as e:
        return jsonify({"ok": False, "error": "api-unavailable", "detail": str(e)}), 200

    if not snap:
        return jsonify({"ok": False, "error": "no-snapshot"}), 200

    want_key = _base_name(county)
    info = snap.get(want_key)
    if not info:
        return jsonify({"ok": False, "error": "county-not-found"}), 200

    # Return the same shape you already consume on the front-end
    return jsonify({
        "ok": True,
        "state": state,
        "county": county,
        "office": office,
        "results": info["candidates"]
    }), 200

# --- add this bulk endpoint next to /results_state ---
@app.route("/results_districts")
def per_districts_bulk():
    state = (request.args.get("state") or "").upper()
    with _cache_lock:
        snap = _cache["districts"].get((state,))
    if not snap:
        return jsonify({"ok": False, "error": "no-snapshot"}), 200

    try:
        root = ET.fromstring(snap["payload"])
    except ET.ParseError:
        return jsonify({"ok": False, "error": "bad-xml"}), 200

    districts = {}
    for ru in root.iter("ReportingUnit"):
        geoid = (ru.attrib.get("DistrictId") or "").strip()
        if not geoid: continue
        cands, total = [], 0
        for c in ru.findall("Candidate"):
            votes = _safe_int(c.attrib.get("VoteCount"))
            full  = f"{(c.attrib.get('First','') or '').strip()} {(c.attrib.get('Last','') or '').strip()}".strip()
            party = (c.attrib.get("Party") or "").upper()
            cands.append({"name": full, "party": party, "votes": votes})
            total += votes
        districts[geoid] = {"candidates": cands, "total": total}

    return jsonify({
        "ok": True,
        "state": state,
        "office": "H",
        "districts": districts
    }), 200



@app.route("/results_state")
def per_state_bulk():
    state  = (request.args.get("state") or "").upper()
    office = (request.args.get("office", "P") or "P").upper()
    with _cache_lock:
        snap = _cache["states"].get((state, office))
    if not snap:
        return jsonify({"ok": False, "error": "no-snapshot"}), 200

    try:
        parsed = _parse_election_xml(snap["payload"])
    except Exception as e:
        return jsonify({"ok": False, "error": "parse-failed", "detail": str(e)}), 200

    return jsonify({
        "ok": True,
        "state": state,
        "office": office,
        "counties": parsed
    }), 200


# (District endpoint left as-is; can be upgraded similarly later.)
@app.route("/results_cd")
def per_district():
    state  = (request.args.get("state") or "").upper()
    district_id = request.args.get("district", "")
    if not (state and district_id):
        return jsonify({"ok": False, "error": "missing-params"}), 200

    url     = f"{BASE_URL}/{ELECTION_DATE}".replace("/v2/elections/", "/v2/districts/")
    headers = {"x-api-key": API_KEY}

    try:
        resp = requests.get(f"{url}?statepostal={state}&level=ru", headers=headers, timeout=6)
        if resp.status_code != 200:
            return jsonify({"ok": False, "error": f"upstream-{resp.status_code}"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "api-unavailable", "detail": str(e)}), 200

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
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
            return jsonify({
                "ok": True,
                "district": district_id,
                "state": state,
                "results": out
            }), 200

    return jsonify({"ok": False, "error": "district-not-found"}), 200
    
@app.route("/outgoing_count")
def outgoing_count():
    with _outgoing_lock:
        count = _outgoing_count
    return jsonify({"ok": True, "outgoing_api_calls": count}), 200



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "9032")), debug=False)
