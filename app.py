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

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from email.utils import parsedate_to_datetime


app = Flask(__name__, static_folder='.', static_url_path='')

# Robust HTTP session with retries & backoff for transient errors
HTTP = requests.Session()
HTTP.trust_env = False
_retries = Retry(
    total=3,                 # overall tries
    connect=3, read=3,
    backoff_factor=0.5,      # 0.5, 1.0, 2.0s ...
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False
)
HTTP.mount("http://", HTTPAdapter(max_retries=_retries))
HTTP.mount("https://", HTTPAdapter(max_retries=_retries))


_hub_started = False
def _start_hub_once():
    # start the polling thread exactly once per process
    global _hub_started
    if _hub_started:
        return
    _hub_started = True
    threading.Thread(target=_hub_loop, daemon=True).start()

# NEW (works on Flask 3+, falls back on older Flask)
def _kick_hub_for_gunicorn():
    _start_hub_once()

# Try to start once when the worker begins serving (Flask 3+)
try:
    app.before_serving(_kick_hub_for_gunicorn)     # Flask 3.x
except Exception:
    # Fallbacks for older Flask
    try:
        app.before_request(_kick_hub_for_gunicorn) # runs on first request; guarded by _start_hub_once()
    except Exception:
        pass




def _load_overrides(force=False):
    """Hot-load overrides.json if it changed on disk, or on demand."""
    global _overrides_mtime, _overrides
    try:
        if not os.path.exists(OVERRIDES_PATH):
            with _overrides_lock:
                if force or not _overrides:
                    _overrides = {}
                    _overrides_mtime = 0.0
            return
        mt = os.path.getmtime(OVERRIDES_PATH)
        if force or mt != _overrides_mtime:
            with open(OVERRIDES_PATH, "r") as f:
                data = json.load(f) or {}
            with _overrides_lock:
                _overrides = data
                _overrides_mtime = mt
            log(f"Overrides loaded ({len(_overrides)} combos)")
    except Exception as e:
        log(f"Overrides load error: {e}", "WARN")


def _save_overrides():
    """Persist the overrides dict to disk (pretty for sanity)."""
    tmp = OVERRIDES_PATH + ".tmp"
    with _overrides_lock:
        data = json.dumps(_overrides, indent=2, sort_keys=True)
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, OVERRIDES_PATH)
    try:
        os.chmod(OVERRIDES_PATH, 0o640)
    except Exception:
        pass
    log(f"Overrides saved to {OVERRIDES_PATH}")


def _is_expired(payload):
    """Return True if payload has expires_at in the past."""
    exp = (payload or {}).get("expires_at")
    if not exp:
        # If no explicit expiry, synthesize one when default TTL > 0
        if OVERRIDE_DEFAULT_TTL_HOURS <= 0:
            return False
        try:
            created = (payload or {}).get("_created_at")
            if not created:
                return False
            t0 = datetime.fromisoformat(created.replace("Z",""))
            return (datetime.utcnow() - t0).total_seconds() > OVERRIDE_DEFAULT_TTL_HOURS*3600
        except Exception:
            return False
    try:
        return datetime.utcnow() > datetime.fromisoformat(exp.replace("Z",""))
    except Exception:
        return False


def _get_override(combo, unit):
    """Return an override payload for (combo, unit) if present and not expired."""
    _load_overrides()  # hot-reload if file changed
    with _overrides_lock:
        by_combo = (_overrides or {}).get(combo) or {}
        payload = by_combo.get(unit)
    if payload and _is_expired(payload):
        return None
    return payload


def _apply_psg_override(usps, parsed_state_blob, office, race_type):
    """P/S/G: overwrite state-level race_call if we have an override."""
    combo = _combo_key(office, race_type)
    ov = _get_override(combo, usps)
    if not ov:
        return

    # Current (API) view
    rc = parsed_state_blob.setdefault("race_call", {})
    cur_status = (rc.get("status") or "No Decision").strip()
    cur_party  = ((rc.get("winner") or {}).get("party") or "").strip().upper()
    sticky     = bool(ov.get("sticky"))

    # If API already says Called with a party, ignore non-sticky overrides
    if cur_status == "Called" and cur_party and not sticky:
        return

    # Apply override (normalize party when present)
    if ov.get("status"):
        rc["status"] = ov["status"]
    if ov.get("winner"):
        wname = ov["winner"].get("name")
        wpart = (ov["winner"].get("party") or "").strip().upper() or None
        rc["winner"] = {"name": wname, "party": wpart}
    rc["source"] = "override"


def _apply_house_overrides(usps, parsed_districts, office, race_type):
    """
    Apply House overrides for a given state.
    Accepts unit keys as either 'CA-12' (USPS + 2-digit district) OR raw DistrictId.
    Normalizes inputs so admin keys like 'DE-00' and 'MN-08' match parsed districts.
    """
    combo = _combo_key(office, race_type)
    _load_overrides()  # ensure freshest view

    # Build an index from various key forms -> canonical DistrictId present in parsed_districts
    idx = {}
    for did, row in (parsed_districts or {}).items():
        dnum_raw = (row.get("district_num") or "").strip()
        idx[did] = did  # allow direct DistrictId lookups
        if dnum_raw:
            d2 = dnum_raw.zfill(2)           # pad "0"→"00", "8"→"08"
            idx[f"{usps}-{d2}"] = did
            if d2 == "00":                   # common at-large aliases
                idx[f"{usps}-0"]  = did
                idx[f"{usps}-AL"] = did

    with _overrides_lock:
        by_combo = (_overrides or {}).get(combo) or {}
        for unit_key, payload in by_combo.items():
            if _is_expired(payload):
                continue

            # Normalize incoming key
            uk = (unit_key or "").strip().upper()

            # Only act if it's for this state (USPS-XX) or a direct DistrictId we have
            if not (uk.startswith((usps or "").upper() + "-") or uk in parsed_districts or uk in idx):
                continue

            # usps-AL → usps-00
            if uk.endswith("-AL"):
                uk = uk[:-3] + "-00"

            # If pattern USPS-# or USPS-##, pad to two digits
            parts = uk.split("-")
            if len(parts) == 2 and parts[1].isdigit():
                uk = parts[0] + "-" + parts[1].zfill(2)

            # Resolve to canonical DistrictId
            did = idx.get(uk, uk)
            if did not in parsed_districts:
                continue

            # Apply override
            rc = parsed_districts[did].setdefault("race_call", {})
            if payload.get("status"):
                rc["status"] = payload["status"]
            if payload.get("winner"):
                rc["winner"] = {
                    "name":  payload["winner"].get("name"),
                    "party": (payload["winner"].get("party") or "").strip().upper() or None
                }
            rc["source"] = "override"


def interpret_race_type(race_type: str) -> dict:
    """
    Minimal classifier just to decide which date to use.
    Treat D/R as primaries; everything else as general.
    """
    raw = (race_type or "G").upper()
    mode = "primary" if raw in ("D", "R") else "general"
    return {"raw": raw, "mode": mode}


# ---------------- Tunables (env) ----------------
# Statewide offices (P/S/G) endpoint:
BASE_URL_E    = os.getenv("BASE_URL_E", os.getenv("BASE_URL", "http://localhost:5037/v2/elections"))
# BASE_URL_E    = os.getenv("BASE_URL_E", os.getenv("BASE_URL", "http://127.0.0.1:15037/v2/elections"))
# House districts endpoint:
BASE_URL_D    = os.getenv("BASE_URL_D", "http://localhost:5037/v2/districts")
# BASE_URL_D    = os.getenv("BASE_URL_D", "http://127.0.0.1:15037/v2/districts")

# ---------------- Tunables (env) ----------------
# Statewide offices (P/S/G) endpoint:
# BASE_URL_E = os.getenv("BASE_URL_E", "http://127.0.0.1:5037/v2/elections")
# House districts endpoint:
# BASE_URL_D = os.getenv("BASE_URL_D", "http://127.0.0.1:5037/v2/districts")

LEVEL_PARAM = os.getenv("LEVEL_PARAM", "ru")


PRIMARY_DATE  = os.getenv("PRIMARY_DATE", "2026-02-15")
GENERAL_DATE  = os.getenv("GENERAL_DATE", "2025-11-04")  # ← change to 2025-11-04

HUB_MODE      = os.getenv("HUB_MODE", "1") in ("1","true","True","YES","yes")


# Which combos to poll (comma-separated). Add others like "Lib,Grn,NP,APP,RET" if desired.
OFFICES       = [x.strip().upper() for x in os.getenv("OFFICES", "G").split(",") if x.strip()]
RACE_TYPES    = [x.strip() for x in os.getenv("RACE_TYPES", "G").split(",") if x.strip()]

# Pace the hub to suit your infra:
MAX_CONCURRENCY        = int(os.getenv("MAX_CONCURRENCY", "10"))
STATES_PER_CYCLE       = int(os.getenv("STATES_PER_CYCLE", "50"))
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS",".1"))
DELAY_BETWEEN_CYCLES   = float(os.getenv("DELAY_BETWEEN_CYCLES",".1"))
TIMEOUT_SECONDS        = float(os.getenv("TIMEOUT_SECONDS","15.0"))

# Snapshot in ./temp (project root)
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = ROOT_DIR  # go one folder up
TEMP_DIR      = os.path.join(ROOT_DIR, "temp")                 # ../temp
os.makedirs(TEMP_DIR, exist_ok=True)

# === Overrides config ===
OVERRIDES_PATH = os.getenv("OVERRIDES_PATH", os.path.join(TEMP_DIR, "overrides.json"))
OVERRIDE_DEFAULT_TTL_HOURS = float(os.getenv("OVERRIDE_DEFAULT_TTL_HOURS", "0"))  # 0 = no TTL by default

# Basic auth for admin endpoints
OVERRIDE_USER = os.getenv("OVERRIDE_USER", "")
OVERRIDE_PASS = os.getenv("OVERRIDE_PASS", "")

_overrides_lock = threading.Lock()
_overrides = {}         # in-memory dict { "P:G": { "CA": {...} }, "H:G": { "CA-12": {...} } }
_overrides_mtime = 0.0  # to hot-reload when the file changes

FAVICONS_DIR  = os.path.join(ROOT_DIR, "favicons")

# --- Hardcoded states to skip entirely (never poll) ---
SKIP_STATES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA',
    'HI','IA','ID','IL','IN','KS','KY','LA','MA','MD',
    'ME','MI','MN','MO','MS','MT','NC','ND','NE','NH',
    'NM','NV','NY','OH','OK','OR','PA','RI','SC',
    'SD','TN','TX','UT','VT','WA','WI','WV','WY','DC','PR'
}


@app.route("/favicons/<path:filename>")
def serve_favicons(filename):
    return send_from_directory(FAVICONS_DIR, filename)
    
CACHE_SNAPSHOT_PATH = os.getenv(
    "CACHE_SNAPSHOT_PATH",
    os.path.join(TEMP_DIR, "p_cache.json")
)

# Serve ../HistoricData as /HistoricData
HISTORIC_DIR  = os.path.join(ROOT_DIR, "HistoricData")

@app.route("/HistoricData/<path:filename>")
def serve_historic(filename):
    return send_from_directory(HISTORIC_DIR, filename)


TOPOJSON_DIR  = os.path.join(ROOT_DIR, "topojson")

@app.route("/topojson/<path:filename>")
def serve_topojson(filename):
    return send_from_directory(TOPOJSON_DIR, filename)
    
# Serve ../fonts as /fonts
FONTS_DIR     = os.path.join(ROOT_DIR, "fonts")

@app.route("/fonts/<path:filename>")
def serve_fonts(filename):
    return send_from_directory(FONTS_DIR, filename)


IMAGES_DIR    = os.path.join(ROOT_DIR, "images")

@app.route("/images/<path:filename>")
def serve_images(filename):
    return send_from_directory(IMAGES_DIR, filename)


MIN_STATE_REFRESH_SEC  = float(os.getenv("MIN_STATE_REFRESH_SEC","15"))  # per-(combo,state) cooldown

ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VA","VT","WA","WI","WV","WY","DC","PR"
]

# Add this small USPS → statefp map near ALL_STATES (once)
USPS_TO_STATEFP = {
    "AL":"01","AK":"02","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10","FL":"12","GA":"13",
    "HI":"15","ID":"16","IL":"17","IN":"18","IA":"19","KS":"20","KY":"21","LA":"22","ME":"23","MD":"24",
    "MA":"25","MI":"26","MN":"27","MS":"28","MO":"29","MT":"30","NE":"31","NV":"32","NH":"33","NJ":"34",
    "NM":"35","NY":"36","NC":"37","ND":"38","OH":"39","OK":"40","OR":"41","PA":"42","RI":"44","SC":"45",
    "SD":"46","TN":"47","TX":"48","UT":"49","VT":"50","VA":"51","WA":"53","WV":"54","WI":"55","WY":"56",
    "DC":"11","PR":"72"
}


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
def _build_url(state: str, office: str, race_type: str) -> str:
    office_u = (office or "").upper()
    base = BASE_URL_E if office_u in ("P", "S", "G") else BASE_URL_D
    rt = interpret_race_type(race_type)
    date = GENERAL_DATE if rt["mode"] == "general" else PRIMARY_DATE
    # now includes &level=ru (or whatever LEVEL_PARAM is)
    return f"{base}/{date}?statepostal={state}&officeId={office_u}&raceTypeId={rt['raw']}&level={LEVEL_PARAM}"

def _looks_like_xml(s: str) -> bool:
    if not isinstance(s, str):
        return False
    head = s.lstrip()[:200].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return False
    return head.startswith("<")



# -------------- Parsers ----------------
def _parse_state_ru(xml_text: str, usps: str, office: str, race_type: str) -> dict:
    out = {"counties": {}, "percent_in": None}
    if not _looks_like_xml(xml_text):
        return out
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log(f"XML parse error (statewide) {usps} {office}:{race_type}: {e}", "WARN")
        return None  # signal parse failure to caller
    
    
    
    state_status = (root.attrib.get("RaceCallStatus") or "No Decision").strip()
    wf = (root.attrib.get("WinnerFirst") or "").strip()
    wl = (root.attrib.get("WinnerLast") or "").strip()
    wp = (root.attrib.get("WinnerParty") or "").strip().upper()
    winner_full = (wf + " " + wl).strip() if (wf or wl) else None

    # If not "Called", do not carry a winner from the API
    winner_payload = None
    if state_status == "Called" and (winner_full or wp):
        winner_payload = {"name": winner_full or None, "party": wp or None}

    out["race_call"] = {
        "status": state_status,
        "winner": winner_payload,
        "source": "api"  # so we know where this came from
    }


    # Grab state-level PercentIn from <ElectionResults>
    out["percent_in"] = root.attrib.get("PercentIn")

    for ru in root.findall(".//ReportingUnit"):
        fips = (ru.attrib.get("FIPS") or "").zfill(5)
        name = ru.attrib.get("Name") or "Unknown"
        ru_percent = ru.attrib.get("PercentIn")
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
            cands.append({"name": (first + " " + last).strip(),
                          "party": party, "votes": votes})
        out["counties"][fips] = {
            "state": usps, "fips": fips, "name": name,
            "candidates": cands, "total": total,
            "percent_in": ru_percent
        }
    return out

def _parse_house_ru(xml_text: str, usps: str, office: str, race_type: str) -> dict:
    out = {"districts": {}, "percent_in": None}
    if not _looks_like_xml(xml_text):
        return out
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log(f"XML parse error (house) {usps} {office}:{race_type}: {e}", "WARN")
        return None


    out["percent_in"] = root.attrib.get("PercentIn")

    for ru in root.findall(".//ReportingUnit"):
        did   = (ru.attrib.get("DistrictId") or "").strip()
        dnum  = (ru.attrib.get("District") or "").strip()
        name  = ru.attrib.get("Name") or f"District {dnum or did}"
        ru_percent = ru.attrib.get("PercentIn")
        ru_status  = (ru.attrib.get("RaceCallStatus") or "No Decision").strip()  # NEW

        cands, total = [], 0
        winner_name, winner_party = None, None  # NEW

        for c in ru.findall("./Candidate"):
            first = c.attrib.get("First","").strip()
            last  = c.attrib.get("Last","").strip()
            party = c.attrib.get("Party","").strip()
            raw_v = (c.attrib.get("VoteCount", "0") or "0")
            try:
                votes = int(raw_v)
            except (ValueError, TypeError):
                prev_lookup_id = did or (USPS_TO_STATEFP.get(usps, "") + (dnum or "").zfill(2)) or dnum
                prev = _get_prev_vote_district(usps, prev_lookup_id, party, office, race_type)
                votes = prev if prev is not None else 0
                log(f"Non-numeric VoteCount='{raw_v}' {usps} {did or dnum} party={party} -> using {votes}", "WARN")

            total += votes

            # Detect district winner via Winner="X"
            is_winner = (c.attrib.get("Winner","").upper() == "X")
            if is_winner:
                nm = (first + " " + last).strip()
                winner_name, winner_party = (nm or None), (party or None)

            cands.append({
                "name": (first + " " + last).strip(),
                "party": party,
                "votes": votes,
                "winner": bool(is_winner)
            })

        #
        statefp = USPS_TO_STATEFP.get(usps, "")
        d2 = (dnum or "").zfill(2)
        canonical_did = (did or (statefp + d2) or dnum)

        out["districts"][canonical_did] = {
            "state": usps,
            "district_id": canonical_did,   # <- always populated
            "district_num": dnum,
            "name": name,
            "candidates": cands,
            "total": total,
            "percent_in": ru_percent,
            "race_call": {
                "status": ru_status,
                "winner": None if not winner_name and not winner_party else {
                    "name":  winner_name,
                    "party": winner_party
                }
            }
        }
    return out

# -------------- Hub helpers ----------------

def _record_error(office, race_type, usps, status_code=None, retry_after=None, why=None):
    pk = _combo_state_key(office, race_type, usps)
    now = time.time()
    with _cache_lock:
        m = _stats["per_combo_state"].setdefault(pk, {})
        m["err"] = m.get("err", 0) + 1
        m["consecutive_err"] = m.get("consecutive_err", 0) + 1
        m["last_error"] = {"ts": now, "status": status_code, "why": why}

        # exponential backoff with caps; honor Retry-After when present
        base = 2 ** min(6, m["consecutive_err"] - 1)  # 1,2,4,8,16,32,64
        delay = base
        if retry_after:
            try:
                delay = max(delay, int(retry_after))
            except Exception:
                try:
                    ra_dt = parsedate_to_datetime(str(retry_after))
                    if ra_dt:
                        delay = max(delay, (ra_dt.timestamp() - now))
                except Exception:
                    pass

        # specialize by status
        if status_code in (401, 403):        # auth/forbidden → longer pause
            delay = max(delay, 15 * 60)
        elif status_code == 429:              # rate limited
            delay = max(delay, 60)
        elif status_code in (500, 502, 503, 504):
            delay = max(delay, 10)

        m["next_ok_at"] = now + min(delay, 30 * 60)  # cap at 30m


def _record_success(office, race_type, usps):
    pk = _combo_state_key(office, race_type, usps)
    now = time.time()
    with _cache_lock:
        m = _stats["per_combo_state"].setdefault(pk, {})
        m["ok"] = m.get("ok", 0) + 1
        m["last_fetch"] = now
        m["consecutive_err"] = 0
        m["next_ok_at"] = now



def _combo_key(office: str, race_type: str) -> str:
    return f"{office.upper()}:{race_type}"

def _combo_state_key(office: str, race_type: str, usps: str) -> str:
    return f"{office.upper()}:{race_type}|{usps}"

def _ensure_combo_bucket(office: str, race_type: str):
    key = _combo_key(office, race_type)
    with _cache_lock:
        _cache["cache_by_combo"].setdefault(key, {"states": {}, "updated": 0.0})

def _should_refetch(office: str, race_type: str, usps: str) -> bool:
    pk = _combo_state_key(office, race_type, usps)
    now = time.time()
    with _cache_lock:
        st = _stats["per_combo_state"].get(pk, {})
        last = st.get("last_fetch", 0.0)
        next_ok_at = st.get("next_ok_at", 0.0)
    if now < next_ok_at:
        return False
    return (now - last) >= MIN_STATE_REFRESH_SEC

    
    
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
            r = HTTP.get(url, timeout=TIMEOUT_SECONDS)
            with _cache_lock:
                _stats["upstream_calls"] += 1
                _stats["upstream_bytes"] += len(r.content or b"")
        except requests.exceptions.RequestException as e:
            _record_error(office, race_type, usps, why=type(e).__name__)
            log(f"Transport error {combo} {usps}: {e}", "WARN")
            return False

        if r.status_code != 200:
            ra = r.headers.get("Retry-After")
            _record_error(office, race_type, usps, status_code=r.status_code, retry_after=ra)
            log(f"HTTP {r.status_code} for {combo} {usps}", "WARN")
            return False

        # ---- Parse (guard against XML failures) ----
        if office.upper() == "H":
            parsed = _parse_house_ru(r.text, usps, office, race_type)
            if parsed is None:
                _record_error(office, race_type, usps, why="parse")
                return False
            _apply_house_overrides(usps, parsed.get("districts"), office, race_type)
        else:
            parsed = _parse_state_ru(r.text, usps, office, race_type)
            if parsed is None:
                _record_error(office, race_type, usps, why="parse")
                return False
            _apply_psg_override(usps, parsed, office, race_type)


        # Store
        _ensure_combo_bucket(office, race_type)
        with _cache_lock:
            bucket = _cache["cache_by_combo"][combo]
            if office.upper() == "H":
                bucket["states"][usps] = {
                    "updated": time.time(),
                    "office": "H",
                    "percent_in": parsed.get("percent_in"),
                    "districts": parsed["districts"]          # carries race_call after overrides
                }
            else:
                bucket["states"][usps] = {
                    "updated": time.time(),
                    "office": office.upper(),
                    "percent_in": parsed.get("percent_in"),
                    "counties": parsed["counties"],
                    "race_call": parsed.get("race_call")       # may be overridden
                }

            bucket["updated"] = time.time()
            pk = _combo_state_key(office, race_type, usps)
            ps = _stats["per_combo_state"].setdefault(pk,{})
            ps["last_fetch"] = time.time()
            ps["ok"] = ps.get("ok",0) + 1

        _record_success(office, race_type, usps)
        dt = time.time()-t0
        nodes = len(parsed.get("districts") or parsed.get("counties") or {})
        log(f"Fetched {combo} {usps}: {nodes} units in {dt:.1f}s")
        return True
    finally:
        with _cache_lock:
            _inflight.discard(inflight_key)

def _hub_loop():
    log("Hub poller starting..." if HUB_MODE else "Hub disabled (serve-only).")
    if not HUB_MODE:
        return

    _snapshot_load()

    # --- NEW: build the active state list once (skip hardcoded states) ---
    active_states = [s for s in ALL_STATES if s.upper() not in SKIP_STATES]
    if not active_states:
        log("All states are in SKIP_STATES; hub will idle.", "WARN")

    rr_states = itertools.cycle(active_states or ALL_STATES)

    combos = [(o, rt) for o in OFFICES for rt in RACE_TYPES]
    if not combos:
        combos = [("P", "G")]

    q = queue.Queue()

    def worker():
        while True:
            item = q.get()
            if item is None:
                q.task_done()
                return
            try:
                usps, office, race_type = item
                try:
                    # (Optional belt-and-suspenders; safe even if already filtered)
                    if usps.upper() in SKIP_STATES:
                        log(f"Skip {office}:{race_type} {usps} (hardcoded SKIP_STATES)")
                    else:
                        _fetch_state(usps, office, race_type)
                except Exception as e:
                    log(f"Unhandled exception in _fetch_state for {office}:{race_type} {usps}: {e}", "WARN")
            finally:
                q.task_done()
                time.sleep(DELAY_BETWEEN_REQUESTS)

    for _ in range(max(1, MAX_CONCURRENCY)):
        threading.Thread(target=worker, daemon=True).start()

    while True:
        if not active_states:
            # Nothing to do—idle politely so we don't spin hot
            time.sleep(DELAY_BETWEEN_CYCLES)
            continue

        batch_states = [next(rr_states) for _ in range(STATES_PER_CYCLE)]
        log(f"Cycle start: states={batch_states} combos={combos}")
        for office, race_type in combos:
            for s in batch_states:
                # Redundant check is harmless; keeps queue clean even if active_states changes
                if s.upper() not in SKIP_STATES:
                    q.put((s, office, race_type))
        q.join()
        with _cache_lock:
            _cache["last_cycle_end"] = time.time()
        _snapshot_save()
        log(f"Cycle end. Cached combos: {len(_cache['cache_by_combo'])}")
        time.sleep(DELAY_BETWEEN_CYCLES)


# ---------------- HTTP (spokes read-only) ----------------
@app.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    # add POST for admin endpoints
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp





# (optional) handle preflight quickly
@app.route("/<path:_any>", methods=["OPTIONS"])
def _any_options(_any):
    return ("", 204)



@app.route("/")
def root(): return send_from_directory(app.static_folder,"index.html")

@app.route("/health")
def health():
    with _cache_lock:
        combos = list(_cache["cache_by_combo"].keys())
        states_cached = {k: len(v.get("states",{})) for k,v in _cache["cache_by_combo"].items()}
        last_cycle_end = _cache["last_cycle_end"]
    return jsonify({
        "hub_mode": HUB_MODE,
        "combos": combos,
        "states_cached_by_combo": states_cached,
        "last_cycle_end_utc": datetime.utcfromtimestamp(last_cycle_end).strftime("%Y-%m-%d %H:%M:%S") if last_cycle_end else None,
    })

@app.route("/cache/p")  # Back-compat: presidential general counties
def cache_p():
    # Exactly what your UI expects today (P general)
    return cache_ru()  # will default to office=P, raceTypeId=G below

@app.route("/cache/ru")
def cache_ru():
    office = (request.args.get("office") or "P").upper()
    race_type = request.args.get("raceTypeId") or "G"
    combo = _combo_key(office, race_type)

    with _cache_lock:
        bucket = _cache["cache_by_combo"].get(combo, {})
        if not bucket:
        # try normalized "raw" (e.g. GEN → G)
            raw = interpret_race_type(race_type)["raw"]
            combo_raw = _combo_key(office, raw)
            bucket = _cache["cache_by_combo"].get(combo_raw, {})
        states = bucket.get("states", {})

    rows = []
    if office == "H":
        for usps, blob in states.items():
            for did, d in (blob.get("districts") or {}).items():
                rc  = (d.get("race_call") or {})                  # NEW
                win = (rc.get("winner") or {})
                rows.append({
                    "state": usps,
                    "district_id": did,
                    "district_num": d.get("district_num"),
                    "name": d["name"],
                    "candidates": d["candidates"],
                    "total": d["total"],
                    "updated": blob["updated"],
                    "percent_in": d.get("percent_in"),
                    "state_percent_in": blob.get("percent_in"),
                    # NEW:
                    "race_call_status": rc.get("status") or "No Decision",
                    "race_called_winner_name": win.get("name"),
                    "race_called_winner_party": win.get("party"),
                    "raceTypeId": race_type,
                    "office": office,
                })
    else:
        for usps, blob in states.items():
            rc  = (blob.get("race_call") or {})                   # NEW
            win = (rc.get("winner") or {})
            for fips, c in (blob.get("counties") or {}).items():
                rows.append({
                    "state": usps,
                    "fips": fips,
                    "name": c["name"],
                    "candidates": c["candidates"],
                    "total": c["total"],
                    "updated": blob["updated"],
                    "percent_in": c.get("percent_in"),
                    "state_percent_in": blob.get("percent_in"),
                    # NEW (state-level applies to all counties in state):
                    "race_call_status": rc.get("status") or "No Decision",
                    "race_called_winner_name": win.get("name"),
                    "race_called_winner_party": win.get("party")
                })
        # keep your sort, but guard int() safely
        rows.sort(key=lambda r: (r["state"], int(r.get("fips","0")) if str(r.get("fips","0")).isdigit() else 0))

    return jsonify({"office": office, "raceTypeId": race_type, "rows": rows})


@app.route("/log")
def get_log():
    try: since = int(request.args.get("since","0"))
    except ValueError: since = 0
    with _cache_lock:
        items = [x for x in list(_cache["log"]) if x["seq"] > since]
        max_seq = _log_seq
    return jsonify({"max_seq":max_seq,"items":items})
    
    
# === Basic Auth decorator for override admin ===
from functools import wraps
import base64

def _check_auth(auth_header):
    if not OVERRIDE_USER or not OVERRIDE_PASS:
        # No auth configured → deny all mutating calls; allow GET read-only
        return False
    try:
        scheme, b64 = (auth_header or "").split(" ", 1)
        if scheme.lower() != "basic":
            return False
        userpass = base64.b64decode(b64).decode("utf-8", "ignore")
        u, p = userpass.split(":", 1)
        return (u == OVERRIDE_USER and p == OVERRIDE_PASS)
    except Exception:
        return False

def require_override_auth(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if request.method == "GET":  # allow read-only even when creds unset
            return fn(*args, **kwargs)
        if _check_auth(request.headers.get("Authorization")):
            return fn(*args, **kwargs)
        return jsonify({"error":"unauthorized"}), 401
    return inner


@app.route("/overrides", methods=["GET"])
def overrides_get():
    _load_overrides()
    with _overrides_lock:
        # also return a small derived view of 'active' (not expired) items
        active = {}
        for combo, m in (_overrides or {}).items():
            for unit, payload in (m or {}).items():
                if not _is_expired(payload):
                    active.setdefault(combo, {})[unit] = payload
    return jsonify({"path": OVERRIDES_PATH, "overrides": active})


@app.route("/overrides/upsert", methods=["POST"])
def overrides_upsert():
    """
    Body:
      {
        "combo": "P:G" | "S:G" | "G:G" | "H:G" (etc),
        "unit":  "CA" (for P/S/G) or "CA-12" / "0601" (for H),
        "payload": {
            "status": "Called",
            "winner": {"name":"Jane Doe","party":"DEM"},
            "note": "Desk call 21:13 ET",
            "sticky": true,
            "expires_at": "2026-11-06T09:00:00Z"   # optional
        }
      }
    """
    try:
        j = request.get_json(force=True) or {}
        combo = str(j.get("combo","")).strip().upper()
        unit  = str(j.get("unit","")).strip()
        payload = j.get("payload") or {}
        if not combo or not unit:
            return jsonify({"error":"combo and unit are required"}), 400

        # stamp creation/update time (helps TTL if used)
        payload = dict(payload)
        payload["_created_at"] = datetime.utcnow().isoformat() + "Z"

        _load_overrides()
        with _overrides_lock:
            _overrides.setdefault(combo, {})[unit] = payload
        _save_overrides()
        log(f"Override upsert {combo}/{unit}: {payload}")
        return jsonify({"ok": True})
    except Exception as e:
        log(f"/overrides/upsert error: {e}", "WARN")
        return jsonify({"error": str(e)}), 500


@app.route("/overrides/delete", methods=["POST"])
def overrides_delete():
    try:
        j = request.get_json(force=True) or {}
        combo = str(j.get("combo","")).strip().upper()
        unit  = str(j.get("unit","")).strip()
        if not combo or not unit:
            return jsonify({"error":"combo and unit are required"}), 400

        _load_overrides()
        removed = False
        with _overrides_lock:
            byc = _overrides.get(combo)
            if byc and unit in byc:
                byc.pop(unit, None)
                if not byc:
                    _overrides.pop(combo, None)
                removed = True
        if removed:
            _save_overrides()
            log(f"Override deleted {combo}/{unit}")
        return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        log(f"/overrides/delete error: {e}", "WARN")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    _start_hub_once()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","9051")))

