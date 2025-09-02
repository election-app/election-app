# app.py — Hub & Spokes cache (true hub-only polling; tabs never hit upstream)
# - One background "hub" thread (HUB_MODE=1) polls upstream slowly and writes to an in-memory cache.
# - All HTTP requests from browsers are "spokes" that read ONLY from the cache; they never call upstream.
# - Multiple tabs/browsers will NOT increase upstream calls.
#
# Tunables (environment variables):
#   HUB_MODE="1"                  # "1" to enable hub poller, "0" to serve-only
#   MAX_CONCURRENCY=1             # number of parallel upstream fetch workers (careful on free tiers)
#   STATES_PER_CYCLE=4            # states per batch
#   DELAY_BETWEEN_REQUESTS=6.0    # seconds between worker fetches
#   DELAY_BETWEEN_CYCLES=20.0     # seconds between batches
#   TIMEOUT_SECONDS=15.0          # upstream HTTP timeout
#   CACHE_SNAPSHOT_PATH="/tmp/p_cache.json"
#   MIN_STATE_REFRESH_SEC=900     # do not re-fetch same state more often than this (guard vs. thrash)
#   FORCE_CYCLE_COOLDOWN_SEC=20   # server-wide cooldown for /force_cycle
#
# Upstream contract (AP-style XML mocked by your middle layer):
#   GET {BASE_URL}/{ELECTION_DATE}?statepostal=XX&raceTypeId=G&raceId=0&level=ru&officeId=P
#
# Notes:
# - /force_cycle is throttled on the server; it schedules server-side fetches but never exposes direct upstream.
# - /metrics and /log help you prove that opening more tabs does not raise upstream call count.

import os, time, json, threading, itertools, queue
from collections import deque
from datetime import datetime
from flask import Flask, send_from_directory, jsonify, request
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__, static_folder='.', static_url_path='')

# --------------------------- Tunables --------------------------- #
BASE_URL      = os.getenv("BASE_URL", "https://api2-app2.onrender.com/v2/elections")
ELECTION_DATE = os.getenv("ELECTION_DATE", "2024-11-05")
HUB_MODE      = os.getenv("HUB_MODE", "1") in ("1","true","True","YES","yes")

MAX_CONCURRENCY        = int(os.getenv("MAX_CONCURRENCY", "10"))
STATES_PER_CYCLE       = int(os.getenv("STATES_PER_CYCLE", "10"))
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS",".1"))
DELAY_BETWEEN_CYCLES   = float(os.getenv("DELAY_BETWEEN_CYCLES",".1"))
TIMEOUT_SECONDS        = float(os.getenv("TIMEOUT_SECONDS","15.0"))
CACHE_SNAPSHOT_PATH    = os.getenv("CACHE_SNAPSHOT_PATH","/tmp/p_cache.json")
MIN_STATE_REFRESH_SEC  = float(os.getenv("MIN_STATE_REFRESH_SEC","1"))
FORCE_CYCLE_COOLDOWN_SEC = float(os.getenv("FORCE_CYCLE_COOLDOWN_SEC","2"))

# USPS states + DC + PR
ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY","DC","PR"
]

# --------------------------- Cache & Stats ---------------------- #
_cache_lock = threading.Lock()
_log_seq = 0
_cache = {
    "p_by_state": {},         # "CA": {"updated": ts, "counties": {...}}
    "states_seen": set(),
    "last_cycle_end": 0.0,
    "log": deque(maxlen=4000),
}
_stats = {
    "upstream_calls": 0,
    "upstream_bytes": 0,
    "errors": 0,
    "per_state": {},          # "CA": {"last_fetch": ts, "ok": n, "err": n}
}
_inflight = set()             # states currently being fetched (guard for concurrency)
_last_force_cycle = 0.0

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
                "_stats": _stats,
            }
        with open(CACHE_SNAPSHOT_PATH, "w") as f:
            json.dump(data, f)
        log(f"Saved snapshot to {CACHE_SNAPSHOT_PATH}")
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
                _s = data.get("_stats", {})
                _stats.update(_s)
            log(f"Loaded snapshot from {CACHE_SNAPSHOT_PATH}")
    except Exception as e:
        log(f"Snapshot load error: {e}", "WARN")

# ---------------------- XML -> JSON parsing --------------------- #
def parse_president_counties(xml_text, usps):
    out = {"office": "P", "state": usps, "counties": {}}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"XML parse error for {usps}: {e}")

    for ru in root.findall(".//ReportingUnit"):
        fips = (ru.attrib.get("FIPS") or "").zfill(5)
        name = ru.attrib.get("Name") or "Unknown"
        cands, total = [], 0
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
            "state": usps, "fips": fips, "name": name,
            "candidates": cands, "total": total
        }
    return out

# ---------------------- Upstream fetcher ------------------------ #
def build_url(usps: str) -> str:
    return f"{BASE_URL}/{ELECTION_DATE}?statepostal={usps}&raceTypeId=G&raceId=0&level=ru&officeId=P"

def should_refetch(usps: str) -> bool:
    with _cache_lock:
        last = _stats["per_state"].get(usps, {}).get("last_fetch", 0.0)
    return (time.time() - last) >= MIN_STATE_REFRESH_SEC

def fetch_state(usps: str):
    # Guard: do not duplicate work and avoid too-frequent refresh of same state
    with _cache_lock:
        if usps in _inflight:
            return False
        _inflight.add(usps)
    try:
        if not should_refetch(usps):
            log(f"Skip {usps}: within refresh window ({int(MIN_STATE_REFRESH_SEC)}s)", "INFO")
            return True

        url = build_url(usps)
        t0 = time.time()
        try:
            r = requests.get(url, timeout=TIMEOUT_SECONDS)
            sz = len(r.content or b"")
            with _cache_lock:
                _stats["upstream_calls"] += 1
                _stats["upstream_bytes"] += sz
        except Exception as e:
            with _cache_lock:
                _stats["errors"] += 1
                _stats["per_state"].setdefault(usps, {}).setdefault("err", 0)
                _stats["per_state"][usps]["err"] += 1
            log(f"Fetch transport error {usps}: {e}", "WARN")
            return False

        if r.status_code != 200:
            with _cache_lock:
                _stats["errors"] += 1
                _stats["per_state"].setdefault(usps, {}).setdefault("err", 0)
                _stats["per_state"][usps]["err"] += 1
            log(f"HTTP {r.status_code} for {usps}", "WARN")
            return False

        try:
            parsed = parse_president_counties(r.text, usps)
        except Exception as e:
            with _cache_lock:
                _stats["errors"] += 1
                _stats["per_state"].setdefault(usps, {}).setdefault("err", 0)
                _stats["per_state"][usps]["err"] += 1
            log(f"Parse error {usps}: {e}", "WARN")
            return False

        with _cache_lock:
            _cache["p_by_state"][usps] = {
                "updated": time.time(),
                "office": "P",
                "counties": parsed["counties"]
            }
            _cache["states_seen"].add(usps)
            ps = _stats["per_state"].setdefault(usps, {})
            ps["last_fetch"] = time.time()
            ps["ok"] = ps.get("ok", 0) + 1

        dt = time.time() - t0
        log(f"Fetched {usps}: {len(parsed['counties'])} counties in {dt:.1f}s")
        return True
    finally:
        with _cache_lock:
            _inflight.discard(usps)

def hub_loop():
    log("Hub poller starting..." if HUB_MODE else "Hub disabled (serve-only).")
    if not HUB_MODE:
        return
    snapshot_load()
    rr = itertools.cycle(ALL_STATES)
    q = queue.Queue()

    def worker():
        while True:
            usps = q.get()
            if usps is None:
                q.task_done()
                return
            try:
                fetch_state(usps)
            finally:
                q.task_done()
                time.sleep(DELAY_BETWEEN_REQUESTS)

    workers = []
    for _ in range(max(1, MAX_CONCURRENCY)):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        workers.append(t)

    while True:
        batch = [next(rr) for _ in range(STATES_PER_CYCLE)]
        log(f"Cycle start: {batch}")
        for s in batch:
            q.put(s)
        q.join()
        with _cache_lock:
            _cache["last_cycle_end"] = time.time()
        snapshot_save()
        log(f"Cycle end. States cached: {len(_cache['p_by_state'])}/{len(ALL_STATES)}")
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
    return send_from_directory(app.static_folder, "index.html")

@app.route("/health")
def health():
    with _cache_lock:
        states_cached = len(_cache["p_by_state"])
        counties_total = sum(len(v["counties"]) for v in _cache["p_by_state"].values())
        last_cycle_end = _cache["last_cycle_end"]
        stats_copy = json.loads(json.dumps(_stats))
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
            "TIMEOUT_SECONDS": TIMEOUT_SECONDS,
            "MIN_STATE_REFRESH_SEC": MIN_STATE_REFRESH_SEC
        },
        "stats": stats_copy
    })

@app.route("/metrics")
def metrics():
    with _cache_lock:
        per_state = _stats["per_state"].copy()
        upstream_calls = _stats["upstream_calls"]
        upstream_bytes = _stats["upstream_bytes"]
        errors = _stats["errors"]
    return jsonify({
        "upstream_calls": upstream_calls,
        "upstream_megabytes": round(upstream_bytes/1024/1024,3),
        "errors": errors,
        "per_state": per_state
    })

@app.route("/log")
def get_log():
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
    rows.sort(key=lambda r: (r["state"], int(r["fips"])))
    return jsonify({"office": "P", "rows": rows})

@app.route("/cache/state")
def cache_state():
    usps = (request.args.get("state") or "").upper().strip()
    if not usps:
        return jsonify({"ok": False, "msg": "missing state"}), 400
    with _cache_lock:
        blob = _cache["p_by_state"].get(usps)
    if not blob:
        return jsonify({"ok": True, "state": usps, "counties": {}, "updated": None})
    return jsonify({"ok": True, "state": usps, "counties": blob["counties"], "updated": blob["updated"]})

@app.route("/force_cycle")
def force_cycle():
    if not HUB_MODE:
        return jsonify({"ok": False, "msg": "Hub disabled"}), 400
    global _last_force_cycle
    now = time.time()
    if now - _last_force_cycle < FORCE_CYCLE_COOLDOWN_SEC:
        return jsonify({"ok": False, "msg": "cooldown"}), 429
    _last_force_cycle = now

    want = request.args.get("n") or 2
    try:
        want = int(want)
        if want < 1:
            want = 1
    except:
        want = 2
    picks = ALL_STATES[:want]
    log(f"Force-cycle requested for {picks}")
    def _force():
        for s in picks:
            fetch_state(s)
            time.sleep(DELAY_BETWEEN_REQUESTS)
    threading.Thread(target=_force, daemon=True).start()
    return jsonify({"ok": True, "scheduled": picks})

if __name__ == "__main__":
    threading.Thread(target=hub_loop, daemon=True).start()
    port = int(os.getenv("PORT","5022"))
    app.run(host="0.0.0.0", port=port)
