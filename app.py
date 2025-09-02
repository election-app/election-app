# app.py — Hub polls upstream on its own; UI only reads cache (true hub & spoke)
# - Background "hub" thread (HUB_MODE=1) cycles states and caches results.
# - Browser never calls upstream; it only hits /cache/* and /health.
# - Opening more tabs never increases upstream calls.

import os, time, json, threading, itertools, queue
from collections import deque
from datetime import datetime
from flask import Flask, send_from_directory, jsonify, request
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__, static_folder='.', static_url_path='')

# ---------------- Tunables (env) ----------------
BASE_URL      = os.getenv("BASE_URL", "https://api2-app2.onrender.com/v2/elections")
ELECTION_DATE = os.getenv("ELECTION_DATE", "2024-11-05")
HUB_MODE      = os.getenv("HUB_MODE", "1") in ("1","true","True","YES","yes")

# Pace the hub to suit your infra:
MAX_CONCURRENCY        = int(os.getenv("MAX_CONCURRENCY", "10"))
STATES_PER_CYCLE       = int(os.getenv("STATES_PER_CYCLE", "50"))
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS",".1"))
DELAY_BETWEEN_CYCLES   = float(os.getenv("DELAY_BETWEEN_CYCLES",".1"))
TIMEOUT_SECONDS        = float(os.getenv("TIMEOUT_SECONDS","15.0"))
CACHE_SNAPSHOT_PATH    = os.getenv("CACHE_SNAPSHOT_PATH","/tmp/p_cache.json")
MIN_STATE_REFRESH_SEC  = float(os.getenv("MIN_STATE_REFRESH_SEC","15"))  # per-state cooldown

ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY","DC","PR"
]

# ---------------- Cache & Stats -----------------
_cache_lock = threading.Lock()
_log_seq = 0
_cache = {
    "p_by_state": {},      # "CA": {"updated": ts, "counties": {...}}
    "last_cycle_end": 0.0,
    "log": deque(maxlen=4000),
}
_stats = {
    "upstream_calls": 0,
    "upstream_bytes": 0,
    "errors": 0,
    "per_state": {},       # "CA": {"last_fetch": ts, "ok": n, "err": n}
}
_inflight = set()

def _now_iso(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(msg, lvl="INFO"):
    global _log_seq
    with _cache_lock:
        _log_seq += 1
        _cache["log"].append({"seq": _log_seq, "ts": _now_iso(), "lvl": lvl, "msg": str(msg)})

# -------------- Snapshot (optional) -------------
def _snapshot_save():
    try:
        with _cache_lock:
            data = {
                "p_by_state": _cache["p_by_state"],
                "last_cycle_end": _cache["last_cycle_end"],
                "_stats": _stats,
            }
        with open(CACHE_SNAPSHOT_PATH,"w") as f: json.dump(data,f)
        log(f"Saved snapshot to {CACHE_SNAPSHOT_PATH}")
    except Exception as e:
        log(f"Snapshot save error: {e}","WARN")

def _snapshot_load():
    try:
        if os.path.exists(CACHE_SNAPSHOT_PATH):
            with open(CACHE_SNAPSHOT_PATH,"r") as f: data=json.load(f)
            with _cache_lock:
                _cache["p_by_state"] = data.get("p_by_state",{})
                _cache["last_cycle_end"] = data.get("last_cycle_end",0.0)
                _stats.update(data.get("_stats",{}))
            log(f"Loaded snapshot from {CACHE_SNAPSHOT_PATH}")
    except Exception as e:
        log(f"Snapshot load error: {e}","WARN")

# -------------- Upstream parsing ----------------
def _build_url(usps):  # AP-style mock
    return f"{BASE_URL}/{ELECTION_DATE}?statepostal={usps}&raceTypeId=G&raceId=0&level=ru&officeId=P"

def _parse_president_counties(xml_text, usps):
    out = {"counties": {}}
    root = ET.fromstring(xml_text)
    for ru in root.findall(".//ReportingUnit"):
        fips = (ru.attrib.get("FIPS") or "").zfill(5)
        name = ru.attrib.get("Name") or "Unknown"
        cands, total = [], 0
        for c in ru.findall("./Candidate"):
            first = c.attrib.get("First","").strip()
            last  = c.attrib.get("Last","").strip()
            party = c.attrib.get("Party","").strip()
            try: votes = int(c.attrib.get("VoteCount","0") or "0")
            except ValueError: votes = 0
            total += votes
            cands.append({"name": (first+" "+last).strip(), "party": party, "votes": votes})
        out["counties"][fips] = {"state": usps, "fips": fips, "name": name,
                                 "candidates": cands, "total": total}
    return out

def _should_refetch(usps):
    with _cache_lock:
        last = _stats["per_state"].get(usps,{}).get("last_fetch",0.0)
    return (time.time() - last) >= MIN_STATE_REFRESH_SEC

def _fetch_state(usps):
    with _cache_lock:
        if usps in _inflight: return False
        _inflight.add(usps)
    try:
        if not _should_refetch(usps):
            log(f"Skip {usps}: within refresh window ({int(MIN_STATE_REFRESH_SEC)}s)")
            return True

        url = _build_url(usps)
        t0 = time.time()
        try:
            r = requests.get(url, timeout=TIMEOUT_SECONDS)
            with _cache_lock:
                _stats["upstream_calls"] += 1
                _stats["upstream_bytes"] += len(r.content or b"")
        except Exception as e:
            with _cache_lock:
                _stats["errors"] += 1
                _stats["per_state"].setdefault(usps,{}).setdefault("err",0)
                _stats["per_state"][usps]["err"] += 1
            log(f"Transport error {usps}: {e}","WARN")
            return False

        if r.status_code != 200:
            with _cache_lock:
                _stats["errors"] += 1
                _stats["per_state"].setdefault(usps,{}).setdefault("err",0)
                _stats["per_state"][usps]["err"] += 1
            log(f"HTTP {r.status_code} for {usps}","WARN")
            return False

        parsed = _parse_president_counties(r.text, usps)

        with _cache_lock:
            _cache["p_by_state"][usps] = {
                "updated": time.time(),
                "office": "P",
                "counties": parsed["counties"]
            }
            ps = _stats["per_state"].setdefault(usps,{})
            ps["last_fetch"] = time.time()
            ps["ok"] = ps.get("ok",0) + 1

        log(f"Fetched {usps}: {len(parsed['counties'])} counties in {time.time()-t0:.1f}s")
        return True
    finally:
        with _cache_lock:
            _inflight.discard(usps)

def _hub_loop():
    log("Hub poller starting..." if HUB_MODE else "Hub disabled (serve-only).")
    if not HUB_MODE: return
    _snapshot_load()
    rr = itertools.cycle(ALL_STATES)
    q = queue.Queue()

    def worker():
        while True:
            usps = q.get()
            if usps is None: q.task_done(); return
            try: _fetch_state(usps)
            finally:
                q.task_done()
                time.sleep(DELAY_BETWEEN_REQUESTS)

    for _ in range(max(1, MAX_CONCURRENCY)):
        threading.Thread(target=worker, daemon=True).start()

    while True:
        batch = [next(rr) for _ in range(STATES_PER_CYCLE)]
        log(f"Cycle start: {batch}")
        for s in batch: q.put(s)
        q.join()
        with _cache_lock: _cache["last_cycle_end"] = time.time()
        _snapshot_save()
        log(f"Cycle end. States cached: {len(_cache['p_by_state'])}/{len(ALL_STATES)}")
        time.sleep(DELAY_BETWEEN_CYCLES)

# ---------------- HTTP (spokes read-only) ----------------
@app.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/")
def root(): return send_from_directory(app.static_folder,"index.html")

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
        "stats": stats_copy
    })

@app.route("/cache/p")  # flattened county rows for the UI
def cache_p():
    with _cache_lock:
        rows=[]
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
    rows.sort(key=lambda r:(r["state"], int(r["fips"])))
    return jsonify({"office":"P","rows":rows})

@app.route("/log")
def get_log():
    try: since = int(request.args.get("since","0"))
    except ValueError: since = 0
    with _cache_lock:
        items = [x for x in list(_cache["log"]) if x["seq"] > since]
        max_seq = _log_seq
    return jsonify({"max_seq":max_seq,"items":items})


# after defining app = Flask(...)
# and after defining _hub_loop

def _start_hub_once():
    if HUB_MODE:
        t = threading.Thread(target=_hub_loop, daemon=True)
        t.start()
        log("Hub thread launched at import time.")

# kick off the hub as soon as the module is imported (gunicorn workers too)
_start_hub_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","9050")))
