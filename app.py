# app.py — Hub & Spokes cache for Presidential counties (slow-by-default)
# - One background "hub" thread (opt-in via HUB_MODE=1) polls upstream very slowly
# - All HTTP "spokes" serve cached JSON; multiple tabs won't multiply upstream calls
#
# Speed levers (safe defaults for Render free tier):
#   HUB_MODE                = os.environ.get("HUB_MODE","1")   # "1" = poller on, "0" = serve-only
#   MAX_CONCURRENCY         = int(os.getenv("MAX_CONCURRENCY","1"))
#   STATES_PER_CYCLE        = int(os.getenv("STATES_PER_CYCLE","4"))
#   DELAY_BETWEEN_REQUESTS  = float(os.getenv("DELAY_BETWEEN_REQUESTS","6.0"))   # seconds
#   DELAY_BETWEEN_CYCLES    = float(os.getenv("DELAY_BETWEEN_CYCLES","20.0"))    # seconds
#   TIMEOUT_SECONDS         = float(os.getenv("TIMEOUT_SECONDS","15.0"))
#   CACHE_SNAPSHOT_PATH     = os.getenv("CACHE_SNAPSHOT_PATH","/tmp/p_cache.json")
#
# Scale up later by raising STATES_PER_CYCLE, lowering the delays, and (carefully) bumping MAX_CONCURRENCY.



import os, time, json, threading, itertools, queue
from collections import deque
from datetime import datetime
from flask import Flask, send_from_directory, jsonify, Response, request
import requests
import xml.etree.ElementTree as ET

# add this near other globals / tunables
_hub_q = queue.Queue()

app = Flask(__name__, static_folder='.', static_url_path='')

# --------------------------- Tunables --------------------------- #
BASE_URL      = "https://api2-app2.onrender.com/v2/elections"  # upstream base
ELECTION_DATE = os.getenv("ELECTION_DATE", "2024-11-05")
HUB_MODE      = os.getenv("HUB_MODE", "1") in ("1","true","True","YES","yes")

MAX_CONCURRENCY        = int(os.getenv("MAX_CONCURRENCY", "1"))     # keep 1 on free tier
STATES_PER_CYCLE       = int(os.getenv("STATES_PER_CYCLE", "4"))     # how many states per mini-cycle
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS","6.0"))
DELAY_BETWEEN_CYCLES   = float(os.getenv("DELAY_BETWEEN_CYCLES","20.0"))
TIMEOUT_SECONDS        = float(os.getenv("TIMEOUT_SECONDS","15.0"))

CACHE_SNAPSHOT_PATH    = os.getenv("CACHE_SNAPSHOT_PATH","/tmp/p_cache.json")

# USPS states + DC + PR
ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY","DC","PR"
]

# --------------------------- Cache ------------------------------ #
_cache_lock = threading.Lock()
_log_seq = 0
_cache = {
    "p_by_state": {},       # { "CA": { "updated": ts, "counties": { "06037": {...}, ... }}, ... }
    "states_seen": set(),   # track coverage
    "last_cycle_end": 0.0,
    "log": deque(maxlen=2000)  # [{seq, ts, lvl, msg}]
}

def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(msg, lvl="INFO"):
    global _log_seq
    with _cache_lock:
        _log_seq += 1
        _cache["log"].append({"seq": _log_seq, "ts": _now_iso(), "lvl": lvl, "msg": str(msg)})

def snapshot_save():
    try:
        with _cache_lock:
            data = {
                "p_by_state": _cache["p_by_state"],
                "states_seen": list(_cache["states_seen"]),
                "last_cycle_end": _cache["last_cycle_end"],
            }
        with open(CACHE_SNAPSHOT_PATH, "w") as f:
            json.dump(data, f)
        log(f"Saved cache snapshot to {CACHE_SNAPSHOT_PATH}")
    except Exception as e:
        log(f"Snapshot save error: {e}", "WARN")

def snapshot_load():
    try:
        if os.path.exists(CACHE_SNAPSHOT_PATH):
            with open(CACHE_SNAPSHOT_PATH, "r") as f:
                data = json.load(f)
            with _cache_lock:
                _cache["p_by_state"]  = data.get("p_by_state", {})
                _cache["states_seen"] = set(data.get("states_seen", []))
                _cache["last_cycle_end"] = data.get("last_cycle_end", 0.0)
            log(f"Loaded cache snapshot from {CACHE_SNAPSHOT_PATH}")
    except Exception as e:
        log(f"Snapshot load error: {e}", "WARN")

# ---------------------- XML -> JSON parsing --------------------- #
def parse_president_counties(xml_text, usps):
    """
    Convert <ElectionResults><ReportingUnit ... FIPS=""><Candidate .../></ReportingUnit>* into
    { "counties": { fips: { "state": usps, "fips": fips, "name": Name, "candidates":[...], "total": int }}, "office":"P" }
    """
    out = {"office": "P", "state": usps, "counties": {}}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"XML parse error for {usps}: {e}")

    for ru in root.findall(".//ReportingUnit"):
        fips = (ru.attrib.get("FIPS") or "").zfill(5)
        name = ru.attrib.get("Name") or "Unknown"
        cands = []
        total = 0
        for c in ru.findall("./Candidate"):
            first = c.attrib.get("First","").strip()
            last  = c.attrib.get("Last","").strip()
            party = c.attrib.get("Party","").strip()
            try:
                votes = int(c.attrib.get("VoteCount","0") or "0")
            except ValueError:
                votes = 0
            total += votes
            full = (first + " " + last).strip()
            cands.append({"name": full, "party": party, "votes": votes})
        out["counties"][fips] = {
            "state": usps,
            "fips": fips,
            "name": name,
            "candidates": cands,
            "total": total
        }
    return out

# ---------------------- Upstream fetcher ------------------------ #
def build_url(usps: str) -> str:
    # /v2/elections/{date}?statepostal=XX&raceTypeId=G&raceId=0&level=ru&officeId=P
    return f"{BASE_URL}/{ELECTION_DATE}?statepostal={usps}&raceTypeId=G&raceId=0&level=ru&officeId=P"

def fetch_state(usps: str):
    url = build_url(usps)
    t0 = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT_SECONDS)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        parsed = parse_president_counties(r.text, usps)
        with _cache_lock:
            _cache["p_by_state"][usps] = {
                "updated": time.time(),
                "office": "P",
                "counties": parsed["counties"]
            }
            _cache["states_seen"].add(usps)
        dt = time.time() - t0
        log(f"Fetched {usps}: {len(parsed['counties'])} counties in {dt:.1f}s")
        return True
    except Exception as e:
        log(f"Fetch error {usps}: {e}", "WARN")
        return False

def hub_loop():
    log("Hub poller starting..." if HUB_MODE else "Hub disabled (serve-only).")
    if not HUB_MODE:
        return
    snapshot_load()

    # Round-robin through states slowly
    rr = itertools.cycle(ALL_STATES)

    # Start a fixed worker pool that drains the single global queue
    def worker():
        while True:
            usps = _hub_q.get()
            try:
                if usps is None:
                    # reserved for future clean shutdowns
                    return
                fetch_state(usps)
                time.sleep(DELAY_BETWEEN_REQUESTS)
            finally:
                _hub_q.task_done()

    for _ in range(max(1, MAX_CONCURRENCY)):
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    # Enqueue batches into the shared queue; don't spawn per-cycle queues/threads
    while True:
        batch = [next(rr) for _ in range(STATES_PER_CYCLE)]
        log(f"Cycle enqueue: {batch}")
        for s in batch:
            _hub_q.put(s)

        with _cache_lock:
            _cache["last_cycle_end"] = time.time()
        snapshot_save()

        # Pace the cycle; workers will keep draining the queue at MAX_CONCURRENCY
        time.sleep(DELAY_BETWEEN_CYCLES)

# ------------------------ HTTP routes --------------------------- #
@app.after_request
def no_store(resp):
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/")
def root():
    # serve index.html from the same directory
    return send_from_directory(app.static_folder, "index.html")

@app.route("/health")
def health():
    with _cache_lock:
        states_cached = len(_cache["p_by_state"])
        counties_total = sum(len(v["counties"]) for v in _cache["p_by_state"].values())
        last_cycle_end = _cache["last_cycle_end"]
    return jsonify({
        "hub_mode": HUB_MODE,
        "states_cached": states_cached,
        "counties_total": counties_total,
        "last_cycle_end_utc": datetime.utcfromtimestamp(last_cycle_end).strftime("%Y-%m-%d %H:%M:%S") if last_cycle_end else None,
        "levers": {
            "MAX_CONCURRENCY": MAX_CONCURRENCY,
            "STATES_PER_CYCLE": STATES_PER_CYCLE,
            "DELAY_BETWEEN_REQUESTS": DELAY_BETWEEN_REQUESTS,
            "DELAY_BETWEEN_CYCLES": DELAY_BETWEEN_CYCLES,
            "TIMEOUT_SECONDS": TIMEOUT_SECONDS
        }
    })

@app.route("/log")
def get_log():
    """Return logs optionally after a given seq (for append-only UI)."""
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    with _cache_lock:
        items = [x for x in list(_cache["log"]) if x["seq"] > since]
        max_seq = _log_seq
    return jsonify({"max_seq": max_seq, "items": items})

@app.route("/cache/p")
def cache_p():
    """Flattened presidential county list across all cached states (sorted)."""
    with _cache_lock:
        rows = []
        for usps, blob in _cache["p_by_state"].items():
            for fips, c in blob["counties"].items():
                rows.append({
                    "state": usps,
                    "fips": fips,
                    "name": c["name"],
                    "candidates": c["candidates"],
                    "total": c["total"],
                    "updated": blob["updated"]
                })
    # sort by state then numeric FIPS
    rows.sort(key=lambda r: (r["state"], int(r["fips"])))
    return jsonify({"office": "P", "rows": rows})

@app.route("/force_cycle")
def force_cycle():
    """Manual nudge: enqueue states into the hub queue (respects MAX_CONCURRENCY)."""
    if not HUB_MODE:
        return jsonify({"ok": False, "msg": "Hub disabled"}), 400

    want = request.args.get("n") or 2
    try:
        want = int(want)
        if want < 1:
            want = 1
    except:
        want = 2

    # Keep it simple: take the first N states; workers dedupe naturally by overwriting cache
    picks = ALL_STATES[:want]
    for s in picks:
        _hub_q.put(s)

    log(f"Force-cycle enqueued {len(picks)} states: {picks}")
    return jsonify({"ok": True, "enqueued": picks})


if __name__ == "__main__":
    # start hub thread
    threading.Thread(target=hub_loop, daemon=True).start()
    port = int(os.getenv("PORT","5022"))
    app.run(host="0.0.0.0", port=port)
