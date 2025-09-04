# app.py — Hub polls upstream on its own; UI only reads cache (true hub & spoke)
# - Supports ALL races (P/S/G/H) and broad AP raceTypeId set (G,W,H,D,R,U,V,J,K,A,B,APP,SAP,N,NP,L,T,RET,...)
# - County-level for P/S/G via /v2/elections; District-level for H via /v2/districts
# - Background hub cycles (office, raceTypeId, state) and snapshots cache to ./temp/p_cache.json

import os, time, json, threading, itertools, queue
from collections import deque
from datetime import datetime
from flask import Flask, send_from_directory, jsonify, request
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__, static_folder='.', static_url_path='')

# ---------------- Tunables (env) ----------------
# Statewide offices (P/S/G) endpoint:
BASE_URL_E    = os.getenv("BASE_URL_E", os.getenv("BASE_URL", "https://api2-app2.onrender.com/v2/elections"))
# House districts endpoint:
BASE_URL_D    = os.getenv("BASE_URL_D", "https://api2-app2.onrender.com/v2/districts")

ELECTION_DATE = os.getenv("ELECTION_DATE", "2024-11-05")
HUB_MODE      = os.getenv("HUB_MODE", "1") in ("1","true","True","YES","yes")

# Restrict polling to only Presidential General
OFFICES    = ["P"]
RACE_TYPES = ["G"]


# Pace the hub to suit your infra:
MAX_CONCURRENCY        = int(os.getenv("MAX_CONCURRENCY", "10"))
STATES_PER_CYCLE       = int(os.getenv("STATES_PER_CYCLE", "50"))
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS",".1"))
DELAY_BETWEEN_CYCLES   = float(os.getenv("DELAY_BETWEEN_CYCLES",".1"))
TIMEOUT_SECONDS        = float(os.getenv("TIMEOUT_SECONDS","15.0"))

# Snapshot in ./temp (project root)
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(ROOT_DIR, "temp")
os.makedirs(TEMP_DIR, exist_ok=True)
CACHE_SNAPSHOT_PATH = os.getenv("CACHE_SNAPSHOT_PATH", os.path.join(TEMP_DIR, "p_cache.json"))

MIN_STATE_REFRESH_SEC  = float(os.getenv("MIN_STATE_REFRESH_SEC","15"))  # per-(combo,state) cooldown

ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY","DC","PR"
]

# ---------------- Cache & Stats -----------------
_cache_lock = threading.Lock()
_log_seq = 0
_cache = {
    # cache_by_combo: { "P:G": { "states": { "CA": {...}}, "updated": ts }, ... }
    "cache_by_combo": {},
    "last_cycle_end": 0.0,
    "log": deque(maxlen=4000),
}
_stats = {
    "upstream_calls": 0,
    "upstream_bytes": 0,
    "errors": 0,
    # per_combo_state: { "P:G|CA": {"last_fetch": ts, "ok": n, "err": n}, ... }
    "per_combo_state": {},
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
                "cache_by_combo": _cache["cache_by_combo"],
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
                _cache["cache_by_combo"] = data.get("cache_by_combo",{})
                _cache["last_cycle_end"] = data.get("last_cycle_end",0.0)
                # merge stats shallowly
                for k,v in data.get("_stats",{}).items():
                    _stats[k] = v
            log(f"Loaded snapshot from {CACHE_SNAPSHOT_PATH}")
    except Exception as e:
        log(f"Snapshot load error: {e}","WARN")

# -------------- AP-style URL builder ----------------
def _build_url(usps: str, office: str, race_type: str) -> str:
    office = office.upper()
    if office == "H":
        return (f"{BASE_URL_D}/{ELECTION_DATE}"
                f"?statepostal={usps}&raceTypeId={race_type}&raceId=0&level=ru&officeId=H")
    else:
        return (f"{BASE_URL_E}/{ELECTION_DATE}"
                f"?statepostal={usps}&raceTypeId={race_type}&raceId=0&level=ru&officeId={office}")

# -------------- Parsers ----------------
def _parse_state_ru(xml_text: str, usps: str, office: str, race_type: str) -> dict:
    """
    Generic county-level parser for P/S/G responses from /v2/elections.
    Returns {"counties": { fips: {state,fips,name,candidates,total} } }
    """
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
            raw_v = (c.attrib.get("VoteCount", "0") or "0")
            try:
                votes = int(raw_v)
            except (ValueError, TypeError):
                prev = _get_prev_vote_county(usps, fips, party, office, race_type)
                if prev is not None:
                    votes = prev
                    log(f"Non-numeric VoteCount='{raw_v}' {usps} {fips} party={party} -> using last cached {prev}", "WARN")
                else:
                    votes = 0
                    log(f"Non-numeric VoteCount='{raw_v}' {usps} {fips} party={party} -> using 0 (no prior)", "WARN")
            # 👇 notice these are OUTSIDE the try/except
            total += votes
            cands.append({"name": (first + " " + last).strip(), "party": party, "votes": votes})
        out["counties"][fips] = {"state": usps, "fips": fips, "name": name,
                                 "candidates": cands, "total": total}
    return out

def _parse_house_ru(xml_text: str, usps: str, office: str, race_type: str) -> dict:
    """
    District-level parser for H responses from /v2/districts.
    Returns {"districts": { did: {state,did,district,name,candidates,total} } }
    """
    out = {"districts": {}}
    root = ET.fromstring(xml_text)
    for ru in root.findall(".//ReportingUnit"):
        did   = (ru.attrib.get("DistrictId") or "").strip()
        dnum  = (ru.attrib.get("District") or "").strip()
        name  = ru.attrib.get("Name") or f"District {dnum or did}"
        cands, total = [], 0
        for c in ru.findall("./Candidate"):
            first = c.attrib.get("First","").strip()
            last  = c.attrib.get("Last","").strip()
            party = c.attrib.get("Party","").strip()
            raw_v = (c.attrib.get("VoteCount", "0") or "0")
            try:
                votes = int(raw_v)
            except (ValueError, TypeError):
                prev = _get_prev_vote_district(usps, did or dnum, party, office, race_type)
                if prev is not None:
                    votes = prev
                    log(f"Non-numeric VoteCount='{raw_v}' {usps} {did} party={party} -> using last cached {prev}", "WARN")
                else:
                    votes = 0
                    log(f"Non-numeric VoteCount='{raw_v}' {usps} {did} party={party} -> using 0 (no prior)", "WARN")
            # 👇 outside try/except
            total += votes
            cands.append({"name": (first + " " + last).strip(), "party": party, "votes": votes})
        out["districts"][did or dnum] = {
            "state": usps, "district_id": did, "district_num": dnum, "name": name,
            "candidates": cands, "total": total
        }
    return out

# -------------- Hub helpers ----------------
def _combo_key(office: str, race_type: str) -> str:
    return f"{office.upper()}:{race_type}"

def _combo_state_key(office: str, race_type: str, usps: str) -> str:
    return f"{office.upper()}:{race_type}|{usps}"

def _ensure_combo_bucket(office: str, race_type: str):
    key = _combo_key(office, race_type)
    with _cache_lock:
        _cache["cache_by_combo"].setdefault(key, {"states": {}, "updated": 0.0})

def _should_refetch(office: str, race_type: str, usps: str) -> bool:
    with _cache_lock:
        last = _stats["per_combo_state"].get(_combo_state_key(office, race_type, usps),{}).get("last_fetch",0.0)
    return (time.time() - last) >= MIN_STATE_REFRESH_SEC
    
    
def _get_prev_vote_county(usps: str, fips: str, party: str, office: str, race_type: str):
    combo = _combo_key(office, race_type)
    with _cache_lock:
        bucket = _cache["cache_by_combo"].get(combo, {})
        state_blob = (bucket.get("states") or {}).get(usps, {})
        counties = state_blob.get("counties") or {}
        c = counties.get(fips)
        if not c: return None
        for cand in c.get("candidates", []):
            if cand.get("party") == party:
                try:
                    return int(cand.get("votes") or 0)
                except Exception:
                    return None
    return None

def _get_prev_vote_district(usps: str, did: str, party: str, office: str, race_type: str):
    combo = _combo_key(office, race_type)
    with _cache_lock:
        bucket = _cache["cache_by_combo"].get(combo, {})
        state_blob = (bucket.get("states") or {}).get(usps, {})
        districts = state_blob.get("districts") or {}
        d = districts.get(did)
        if not d: return None
        for cand in d.get("candidates", []):
            if cand.get("party") == party:
                try:
                    return int(cand.get("votes") or 0)
                except Exception:
                    return None
    return None


def _fetch_state(usps: str, office: str, race_type: str):
    combo = _combo_key(office, race_type)
    inflight_key = f"{combo}|{usps}"
    with _cache_lock:
        if inflight_key in _inflight: return False
        _inflight.add(inflight_key)
    try:
        if not _should_refetch(office, race_type, usps):
            log(f"Skip {combo} {usps}: within refresh window ({int(MIN_STATE_REFRESH_SEC)}s)")
            return True

        url = _build_url(usps, office, race_type)
        t0 = time.time()
        try:
            r = requests.get(url, timeout=TIMEOUT_SECONDS)
            with _cache_lock:
                _stats["upstream_calls"] += 1
                _stats["upstream_bytes"] += len(r.content or b"")
        except Exception as e:
            with _cache_lock:
                _stats["errors"] += 1
                pk = _combo_state_key(office, race_type, usps)
                _stats["per_combo_state"].setdefault(pk,{}).setdefault("err",0)
                _stats["per_combo_state"][pk]["err"] += 1
            log(f"Transport error {combo} {usps}: {e}","WARN")
            return False

        if r.status_code != 200:
            with _cache_lock:
                _stats["errors"] += 1
                pk = _combo_state_key(office, race_type, usps)
                _stats["per_combo_state"].setdefault(pk,{}).setdefault("err",0)
                _stats["per_combo_state"][pk]["err"] += 1
            log(f"HTTP {r.status_code} for {combo} {usps}","WARN")
            return False

        # Parse
        if office.upper() == "H":
            parsed = _parse_house_ru(r.text, usps, office, race_type)
        else:
            parsed = _parse_state_ru(r.text, usps, office, race_type)

        # Store
        _ensure_combo_bucket(office, race_type)
        with _cache_lock:
            bucket = _cache["cache_by_combo"][combo]
            if office.upper() == "H":
                bucket["states"][usps] = {
                    "updated": time.time(),
                    "office": "H",
                    "districts": parsed["districts"]
                }
            else:
                bucket["states"][usps] = {
                    "updated": time.time(),
                    "office": office.upper(),
                    "counties": parsed["counties"]
                }
            bucket["updated"] = time.time()
            pk = _combo_state_key(office, race_type, usps)
            ps = _stats["per_combo_state"].setdefault(pk,{})
            ps["last_fetch"] = time.time()
            ps["ok"] = ps.get("ok",0) + 1

        dt = time.time()-t0
        nodes = len(parsed.get("districts") or parsed.get("counties") or {})
        log(f"Fetched {combo} {usps}: {nodes} units in {dt:.1f}s")
        return True
    finally:
        with _cache_lock:
            _inflight.discard(inflight_key)

def _hub_loop():
    log("Hub poller starting..." if HUB_MODE else "Hub disabled (serve-only).")
    if not HUB_MODE: return
    _snapshot_load()
    rr_states = itertools.cycle(ALL_STATES)

    combos = [(o, rt) for o in OFFICES for rt in RACE_TYPES]
    if not combos:
        combos = [("P","G")]

    q = queue.Queue()

    def worker():
        while True:
            item = q.get()
            if item is None: q.task_done(); return
            try:
                usps, office, race_type = item
                _fetch_state(usps, office, race_type)
            finally:
                q.task_done()
                time.sleep(DELAY_BETWEEN_REQUESTS)

    for _ in range(max(1, MAX_CONCURRENCY)):
        threading.Thread(target=worker, daemon=True).start()

    while True:
        batch_states = [next(rr_states) for _ in range(STATES_PER_CYCLE)]
        log(f"Cycle start: states={batch_states} combos={combos}")
        for office, race_type in combos:
            for s in batch_states:
                q.put((s, office, race_type))
        q.join()
        with _cache_lock: _cache["last_cycle_end"] = time.time()
        _snapshot_save()
        log(f"Cycle end. Cached combos: {len(_cache['cache_by_combo'])}")
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
        combos = list(_cache["cache_by_combo"].keys())
        states_cached = {k: len(v.get("states",{})) for k,v in _cache["cache_by_combo"].items()}
        last_cycle_end = _cache["last_cycle_end"]
        stats_copy = json.loads(json.dumps(_stats))
    return jsonify({
        "hub_mode": HUB_MODE,
        "combos": combos,
        "states_cached_by_combo": states_cached,
        "last_cycle_end_utc": datetime.utcfromtimestamp(last_cycle_end).strftime("%Y-%m-%d %H:%M:%S") if last_cycle_end else None,
        "stats": stats_copy
    })

@app.route("/cache/p")  # Back-compat: presidential general counties
def cache_p():
    # Exactly what your UI expects today (P general)
    return cache_ru()  # will default to office=P, raceTypeId=G below

@app.route("/cache/ru")
def cache_ru():
    """
    Flattened rows for the UI.
    Query:
      - office: P|S|G|H  (default P)
      - raceTypeId: G|W|H|D|R|U|V|J|K|A|B|APP|SAP|N|NP|L|T|RET|... (default G)
    """
    office = (request.args.get("office") or "P").upper()
    race_type = request.args.get("raceTypeId") or "G"
    combo = _combo_key(office, race_type)

    with _cache_lock:
        bucket = _cache["cache_by_combo"].get(combo, {})
        states = bucket.get("states", {})

    rows = []
    if office == "H":
        # Flatten districts
        for usps, blob in states.items():
            for did, d in (blob.get("districts") or {}).items():
                rows.append({
                    "state": usps,
                    "district_id": did,
                    "district_num": d.get("district_num"),
                    "name": d["name"],
                    "candidates": d["candidates"],
                    "total": d["total"],
                    "updated": blob["updated"]
                })
        rows.sort(key=lambda r:(r["state"], str(r.get("district_id") or ""), str(r.get("district_num") or "")))
    else:
        # Flatten counties
        for usps, blob in states.items():
            for fips, c in (blob.get("counties") or {}).items():
                rows.append({
                    "state": usps,
                    "fips": fips,
                    "name": c["name"],
                    "candidates": c["candidates"],
                    "total": c["total"],
                    "updated": blob["updated"]
                })
        rows.sort(key=lambda r:(r["state"], int(r["fips"])) if r.get("fips","00000").isdigit() else (r["state"], 0))

    return jsonify({"office": office, "raceTypeId": race_type, "rows": rows})

@app.route("/log")
def get_log():
    try: since = int(request.args.get("since","0"))
    except ValueError: since = 0
    with _cache_lock:
        items = [x for x in list(_cache["log"]) if x["seq"] > since]
        max_seq = _log_seq
    return jsonify({"max_seq":max_seq,"items":items})

if __name__ == "__main__":
    threading.Thread(target=_hub_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","9052")))
