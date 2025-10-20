# app.py — AP Elections API simulator (randomizes every 30s) + MANUAL OVERRIDES + PRIMARIES
#
# New (this version):
#   - Added district overrides for House (general + primary scaling).
#   - Added /registry/districts for UI dropdown.
#   - Kept county overrides EXACTLY as-is for P/G/S; no schema changes.
#   - Office dropdown support is handled in the UI; preview hits the right endpoint.
#
# --------------------------------------------------------------------

import os, time, re, hashlib, json
from typing import Dict, List, Tuple, Optional, Any
import httpx

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="AP Elections API Simulator (30s random epochs + overrides + primaries)")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8001",
        "http://localhost:8008",
        "http://localhost:8009",
        "http://localhost:8002",
        "http://localhost:9053",
        "http://127.0.0.1:8001",
        "http://127.0.0.1:9053",
        "http://127.0.0.1:9051",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------- Metrics ---------------------------- #


# ---- SIMPLE, MANUAL STATE-CALL OVERRIDES FOR S 2026 (GENERAL) ----
# You can provide either:
#   - "winner": "Full Name"   (auto-splits), OR
#   - "winner_first": "...", "winner_last": "..."
SIMPLE_OVERRIDES_2026_S = {

}

# -------------------------------------------------------------------



from collections import deque
TOTAL_CALLS = 0
REQUEST_TIMES = deque(maxlen=10000)

@app.middleware("http")
async def _count_calls(request: Request, call_next):
    is_metrics = request.url.path == "/metrics"
    resp = await call_next(request)
    if not is_metrics:
        global TOTAL_CALLS
        TOTAL_CALLS += 1
        REQUEST_TIMES.append(time.time())
    return resp

@app.get("/metrics")
def metrics():
    now = time.time()
    last_min = [t for t in REQUEST_TIMES if now - t <= 60]
    return {"total_calls": TOTAL_CALLS, "calls_per_minute": len(last_min)}

@app.get("/api/ping", response_class=PlainTextResponse)
def ping():
    return "pong"

# ---------------------------- Tunables ---------------------------- #

UPDATE_BUCKETS  = int(os.getenv("UPDATE_BUCKETS", "36"))  # compat
BASELINE_DRIFT  = int(os.getenv("BASELINE_DRIFT", "0"))   # compat
TICK_SECONDS    = int(os.getenv("TICK_SECONDS", "10"))    # compat
EPOCH_SECONDS   = int(os.getenv("EPOCH_SECONDS", "50"))   # epoch cadence
SAVE_OVERRIDES  = os.getenv("SAVE_OVERRIDES", "0") in ("1","true","True","YES","yes")
OVERRIDE_PATH   = os.getenv("OVERRIDE_PATH", "overrides.json")

US_ATLAS_COUNTIES_URL = "https://cdn.jsdelivr.net/npm/us-atlas@3/counties-10m.json"

STATE_FIPS_TO_USPS = {
    "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE","11":"DC",
    "12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA","20":"KS","21":"KY",
    "22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT",
    "31":"NE","32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH",
    "40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD","47":"TN","48":"TX","49":"UT",
    "50":"VT","51":"VA","53":"WA","54":"WV","55":"WI","56":"WY","72":"PR"
}
USPS_TO_STATE_FIPS = {v:k for k,v in STATE_FIPS_TO_USPS.items()}
PARISH_STATES = {"LA"}
INDEPENDENT_CITY_STATES = {"VA"}

# --- NYC Mayor (borough-only) support ---------------------------------
NYC_BOROUGH_FIPS = ["36005","36047","36061","36081","36085"]  # Bronx, Kings, New York, Queens, Richmond

NYC_BOROUGH_NAME = {
    "36005": "Bronx",
    "36047": "Brooklyn",       # Kings County
    "36061": "Manhattan",      # New York County
    "36081": "Queens",
    "36085": "Staten Island",  # Richmond County
}


# ----------------------- Helpers / RNG / Names -------------------- #

def _nyc_borough_vote_vector(fips: str, office_key: str = "M") -> List[int]:
    """
    Deterministic borough-level votes for REP, DEM, IND, OTH, WRI.
    Derived from the existing simulated county model, then split IND.
    """
    rep, dem, ind = simulated_votes_30s(fips, office_key=office_key)
    seed = seeded_rng_u32(f"{fips}-{office_key}-NYC")
    oth_share = 0.10 + ((seed & 0xFF) / 255.0) * 0.07   # ~10–17% of IND
    wri_share = 0.03 + (((seed >> 8) & 0xFF) / 255.0) * 0.04  # ~3–7% of IND
    oth = int(ind * oth_share)
    wri = int(ind * wri_share)
    ind = max(0, ind - oth - wri)
    return [rep, dem, ind, oth, wri]  # order must match the slate below


def _raw_and_numeric(x):
    """Return (raw_string, numeric_value_or_0)."""
    raw = "" if x is None else str(x)
    try:
        num = int(raw.strip())
    except Exception:
        num = 0
    return raw, num

def simulated_percent_in(key: str) -> float:
    epoch = _epoch_key()
    base  = seeded_rng_u32(f"%IN-{key}-{epoch}")
    return round((base % 1001) / 10.0, 1)  # 0.0, 0.1, ..., 100.0

def seeded_rng_u32(seed: str) -> int:
    import hashlib as _h
    h = _h.blake2b(seed.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big")

def _epoch_key() -> int:
    return int(time.time() // max(1, EPOCH_SECONDS))

def apize_name(usps: str, canonical: str) -> str:
    n = canonical
    if n.startswith("Saint "): n = "St. " + n[6:]
    n = n.replace("Doña", "Dona")
    n = re.sub(r"\bLa\s+Salle\b", "LaSalle", n)
    n = n.replace("DeKalb", "De Kalb")
    return n

def county_suffix(usps: str, name: str) -> str:
    if name.lower().endswith("city"):
        return name
    if usps in PARISH_STATES:
        return name if name.lower().endswith("parish") else f"{name} Parish"
    return name if name.lower().endswith("county") else f"{name} County"

def ordinal(n: int) -> str:
    return "%d%s" % (n, "th" if 11<=n%100<=13 else {1:"st",2:"nd",3:"rd"}.get(n%10, "th"))

def district_label(n: int) -> str:
    return f"{ordinal(n)} Congressional District"

FIRSTS = ["Alex","Taylor","Jordan","Casey","Riley","Avery","Morgan","Quinn","Hayden","Rowan",
          "Elliot","Jesse","Drew","Parker","Reese"]
LASTS  = ["Smith","Johnson","Brown","Jones","Garcia","Miller","Davis","Martinez","Clark","Lewis",
          "Walker","Young","Allen","King","Wright"]
PARTY_POOL = ["REP","DEM","IND"]

# Safe int for override fields that may arrive as strings/None
def _to_int(x: object, default: int = 0) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return default

def gen_cd_candidates(did: str, n: int = 3) -> List[Tuple[str,str]]:
    """
    General-election district slate:
      - Exactly one REP, one DEM, one IND.
      - Primaries use separate generators; this only affects general flow.
    """
    base = seeded_rng_u32(did)
    out, used = [], set()
    for i in range(3):
        f = FIRSTS[(base + i*13) % len(FIRSTS)]
        l = LASTS[(base // 7 + i*17) % len(LASTS)]
        nm = f"{f} {l}"
        if nm in used: nm = f"{nm} Jr."
        used.add(nm)
        out.append((nm, "IND"))  # temp; will be reassigned below

    # Force party uniqueness in a deterministic order: [REP, DEM, IND]
    names = [x for x in out]
    names[0] = (names[0][0], "REP")
    names[1] = (names[1][0], "DEM")
    names[2] = (names[2][0], "IND")
    return names


def gen_statewide_candidates(usps: str, office: str, n: int = 3) -> List[Tuple[str,str]]:
    """
    General-election statewide slate:
      - Always exactly one REP, one DEM, one IND.
      - Primaries use separate generators; this only affects general flow.
    """
    office = (office or "P").upper()
    if office == "P":
        # Keep your deterministic P slates as-is; they already satisfy uniqueness.
        return [("Donald Trump","REP"), ("Kamala Harris","DEM"), ("Robert Kennedy","IND")]

    base = seeded_rng_u32(f"{usps}-{office}")
    out, used = [], set()
    for i in range(3):
        f = FIRSTS[(base + i*11) % len(FIRSTS)]
        l = LASTS[(base // 5 + i*7) % len(LASTS)]
        nm = f"{f} {l}"
        if nm in used: nm = f"{nm} II"
        used.add(nm)
        out.append((nm, "IND"))  # temp; will be reassigned below

    # Force unique parties in a stable way: [REP, DEM, IND]
    names = [x for x in out]
    names[0] = (names[0][0], "REP")
    names[1] = (names[1][0], "DEM")
    names[2] = (names[2][0], "IND")
    return names

def _get_state_call_override(usps: str, office: str, year: str, race_code: str):
    """
    Returns the override dict if it matches (office/year/race), else None.
    Expected shape:
      {"office":"S","year":2026,"race":"G","status":"Called","winner_party":"DEM","winner_name":"Taylor Lewis"}
    """
    ovr = OVERRIDES.get("state_calls", {}).get(usps)
    if not ovr:
        return None
    if str(ovr.get("office", "")).upper() != str(office).upper():
        return None
    if str(ovr.get("race", "")).upper()  != str(race_code).upper():
        return None
    if str(ovr.get("year", ""))          != str(year):
        return None
    return ovr


def gen_primary_candidates(usps: str, office: str, party_code: str, n: int = 3) -> List[Tuple[str,str]]:
    base = seeded_rng_u32(f"{usps}-{office}-PRIMARY-{party_code}")
    out, used = [], set()
    for i in range(n):
        f = FIRSTS[(base + i*9) % len(FIRSTS)]
        l = LASTS[(base // 3 + i*5) % len(LASTS)]
        nm = f"{f} {l}"
        if nm in used: nm = f"{nm} III"
        used.add(nm)
        out.append((nm, party_code))
    return out

def gen_cd_primary_candidates(did: str, party_code: str, n: int = 3) -> List[Tuple[str,str]]:
    base = seeded_rng_u32(f"{did}-PRIMARY-{party_code}")
    out, used = [], set()
    for i in range(n):
        f = FIRSTS[(base + i*9) % len(FIRSTS)]
        l = LASTS[(base // 3 + i*5) % len(LASTS)]
        nm = f"{f} {l}"
        if nm in used: nm = f"{nm} III"
        used.add(nm)
        out.append((nm, party_code))
    return out

# --------------------- AP-like decision helpers ------------------- #

def _race_call_status(percent_in: float, margin_pct: float) -> str:
    """
    Minimal AP-like decision status for the *race*:
      - 'Called' once %in is high and margin is comfortable
      - 'Too Early to Call' when very low %in
      - else 'No Decision'
    """
    if percent_in >= 40.0 and margin_pct >= 1:
        return "Called"
    if percent_in < 40.0:
        return "Too Early to Call"
    return "No Decision"

def _ru_called(percent_in: float, votes: List[int]) -> Tuple[bool, Optional[int], float]:
    """
    Decide if a Reporting Unit is 'called' and who the winning index is,
    using % in and margin between top two. Returns (called, winner_idx, margin_pct).
    """
    total = max(1, sum(votes))
    if total <= 0 or not votes:
        return (False, None, 0.0)
    order = sorted(range(len(votes)), key=lambda i: votes[i], reverse=True)
    top, second = votes[order[0]], votes[order[1]] if len(order) > 1 else 0
    margin_pct = 100.0 * (top - second) / total
    called = percent_in >= 40.0 and margin_pct >= 1
    return (called, order[0], margin_pct)

# --------------------- Randomized vote models (30s) -------------------- #

def simulated_votes_30s(fips: str, office_key: str = "GEN") -> Tuple[int,int,int]:
    epoch = _epoch_key()
    base_seed = seeded_rng_u32(f"{fips}-{office_key}-{epoch}")
    r1 = (base_seed & 0xFFFF)
    r2 = ((base_seed >> 16) & 0xFFFF)

    base_total = 2000 + (base_seed % 250000)
    rep = int(base_total * (0.23 + (r1 % 50) / 100.0))
    dem = int(base_total * (0.27 + (r2 % 50) / 100.0))
    ind = max(0, base_total - rep - dem)

    if (base_seed % 3) == 0 and ind > 0:
        shift = min(ind // 10, 500)
        rep += shift; ind -= shift
    elif (base_seed % 3) == 1 and ind > 0:
        shift = min(ind // 10, 500)
        dem += shift; ind -= shift
    return max(rep,0), max(dem,0), max(ind,0)

def simulated_cd_votes_30s(did: str, k: int = 3) -> List[int]:
    epoch = _epoch_key()
    rnd   = seeded_rng_u32(f"{did}-{epoch}")

    base_total = 120_000 + (rnd % 480_000)

    weights = []
    for i in range(k):
        r = ((rnd >> (i * 7)) & 0xFFFF) / 65535.0
        w = (0.3 + 1.7 * r) ** 3
        weights.append(w)

    fav_idx = rnd % k
    fav_bump = 2.2 + (((rnd >> 13) & 7) / 4.0)
    weights[fav_idx] *= fav_bump

    if (rnd % 3) == 0 and k >= 2:
        order = sorted(range(k), key=lambda i: weights[i])
        second = order[-2]
        weights[second] *= 1.25

    total_w = sum(weights) if sum(weights) > 0 else k
    shares = [w / total_w for w in weights]

    votes = [int(round(s * base_total)) for s in shares]
    drift = base_total - sum(votes)
    if drift != 0:
        votes[0] += drift
        
    if len(votes) >= 3:
        votes[2] = int(votes[2] * 0.00)  # cut IND down to 25% of what RNG gave
    
    return [max(0, v) for v in votes[:k]]

def simulated_primary_votes_30s(fips: str, party_tag: str, k: int = 3, office_key: str = "GEN") -> List[int]:
    epoch = _epoch_key()
    base_seed = seeded_rng_u32(f"{fips}-{office_key}-{party_tag}-{epoch}")
    base_total = 30_000 + (base_seed % 300_000)
    parts, rem = [], base_total
    for i in range(k - 1):
        slice_i = int((0.20 + ((base_seed >> (i*4)) % 45)/100.0) * (rem / (k - i)))
        parts.append(max(1, slice_i))
        rem -= slice_i
    parts.append(max(1, rem))
    if k >= 3 and (base_seed % 3) == 0:
        parts[0], parts[1] = parts[1], parts[0]
    return [max(1, x) for x in parts[:k]]

def simulated_cd_primary_votes_30s(did: str, party_tag: str, k: int = 3) -> List[int]:
    epoch = _epoch_key()
    base_seed = seeded_rng_u32(f"{did}-{party_tag}-{epoch}")
    base_total = 40_000 + (base_seed % 400_000)
    parts, rem = [], base_total
    for i in range(k - 1):
        slice_i = int((0.20 + ((base_seed >> (i*4)) % 45)/100.0) * (rem / (k - i)))
        parts.append(max(1, slice_i))
        rem -= slice_i
    parts.append(max(1, rem))
    if k >= 3 and (base_seed % 3) == 1:
        parts[0], parts[1] = parts[1], parts[0]
    return [max(1, x) for x in parts[:k]]

def _scale_votes_to_total(votes: List[int], total: int) -> List[int]:
    if total <= 0 or not votes:
        return [0]*len(votes)
    s = sum(votes)
    if s <= 0:
        equal = total // len(votes)
        out = [equal]*len(votes)
        out[0] += total - sum(out)
        return out
    out = [int(round(v * (total / s))) for v in votes]
    drift = total - sum(out)
    if drift != 0:
        out[0] += drift
    return [max(0, x) for x in out]

# --------------------- Registries built at startup ---------------- #

STATE_REGISTRY: Dict[str, List[Tuple[str,str,str]]] = {}
STATE_CD_REGISTRY: Dict[str, List[Tuple[str,int,str]]] = {}

@app.on_event("startup")
async def bootstrap():
    # Counties
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(US_ATLAS_COUNTIES_URL); r.raise_for_status()
        topo = r.json()
    geoms = topo.get("objects", {}).get("counties", {}).get("geometries", [])
    for g in geoms:
        fips = str(g.get("id", "")).zfill(5)
        props = g.get("properties", {}) or {}
        name = props.get("name") or props.get("NAMELSAD") or fips
        state_fips = fips[:2]
        usps = STATE_FIPS_TO_USPS.get(state_fips)
        if not usps:
            continue
        canonical = re.sub(r"\s+(County|Parish|city)$", "", name)
        apname = county_suffix(usps, canonical)
        apname = apize_name(usps, apname)
        STATE_REGISTRY.setdefault(usps, []).append((fips, canonical, apname))
    for usps in STATE_REGISTRY:
        STATE_REGISTRY[usps].sort(key=lambda t: t[0])

    # Congressional districts (local TopoJSON file)
    with open(os.path.join(TOPOJSON_DIR, "cb_2024_us_cd119_500k.json"), "r", encoding="utf-8") as f:
        cd_topo = json.load(f)

    cd_obj = (cd_topo.get("objects", {}).get("districts")
        or cd_topo.get("objects", {}).get("congressional-districts")
        or cd_topo.get("objects", {}).get(next(iter(cd_topo.get("objects", {})), ""), {}))
    geoms_cd = cd_obj.get("geometries", []) if cd_obj else []

    for g in geoms_cd:
        gid = str(g.get("id", "")).strip()
        props = g.get("properties", {}) or {}

        m = re.match(r"^\s*(\d{2})", gid) or re.match(r"^\s*(\d{2})", str(props.get("STATEFP") or ""))
        if not m:
            continue
        state_fips = m.group(1)
        usps = STATE_FIPS_TO_USPS.get(state_fips)
        if not usps:
            continue

        if usps in {"DC", "PR"}:
            continue
            
        dnum = None
        for key in ("CD119FP","district","DISTRICT","cd","CD","number","NUM"):
            if key in props and str(props[key]).strip().isdigit():
                dnum = int(str(props[key]).strip())
                break
        if dnum is None:
            import re as _re
            tail = _re.findall(r"(\d{1,2})$", gid.replace("-", ""))
            dnum = int(tail[0]) if tail else 1

        label = district_label(dnum)
        STATE_CD_REGISTRY.setdefault(usps, []).append((gid, dnum, label))

    for usps in STATE_CD_REGISTRY:
        STATE_CD_REGISTRY[usps].sort(key=lambda t: (t[1], t[0]))

    _load_overrides()

# ---------------------------- Overrides --------------------------- #
# OVERRIDES = {
#   "counties":  { "06037": {"REP":..., "DEM":..., "IND":...}, ... },
#   "districts": { "06-01": {"REP":..., "DEM":..., "IND":...}, ... }
# }
OVERRIDES: Dict[str, Dict] = {"counties": {}, "districts": {}, "state_calls": {}}

def _save_overrides():
    if not SAVE_OVERRIDES:
        return
    try:
        with open(OVERRIDE_PATH, "w", encoding="utf-8") as f:
            json.dump(OVERRIDES, f, indent=2, sort_keys=True)
    except Exception as e:
        print("[WARN] Failed to save overrides:", e)

def _load_overrides():
    global OVERRIDES
    if not SAVE_OVERRIDES:
        return
    try:
        if os.path.exists(OVERRIDE_PATH):
            with open(OVERRIDE_PATH, "r", encoding="utf-8") as f:
                OVERRIDES = json.load(f)
            # ensure keys exist
            OVERRIDES.setdefault("counties", {})
            OVERRIDES.setdefault("districts", {})
            OVERRIDES.setdefault("state_calls", {})
        else:
            OVERRIDES = {"counties": {}, "districts": {}, "state_calls": {}}
    except Exception as e:
        print("[WARN] Failed to load overrides:", e)
        OVERRIDES = {"counties": {}, "districts": {}, "state_calls": {}}


@app.get("/overrides")
def get_overrides():
    return OVERRIDES

@app.post("/override/county")
async def set_county_override(payload: dict):
    fips = str(payload.get("fips") or "").zfill(5)
    rep = payload.get("rep")
    dem = payload.get("dem")
    ind = payload.get("ind")

    if not fips.isdigit() or len(fips) != 5:
        raise HTTPException(status_code=400, detail="invalid fips")

    OVERRIDES.setdefault("counties", {})[fips] = {"REP": rep, "DEM": dem, "IND": ind}
    _save_overrides()
    return {"ok": True, "fips": fips, "override": OVERRIDES["counties"][fips]}

@app.delete("/override/county")
def delete_county_override(fips: str):
    fips = str(fips).zfill(5)
    if fips in OVERRIDES.get("counties", {}):
        OVERRIDES["counties"].pop(fips, None)
        _save_overrides()
        return {"ok": True, "removed": fips}
    return {"ok": True, "removed": None}

# --- NEW: District override endpoints ---

@app.post("/override/district")
async def set_district_override(payload: dict):
    did = str(payload.get("district_id") or "").strip()
    rep = payload.get("rep")
    dem = payload.get("dem")
    ind = payload.get("ind")
    if not did:
        raise HTTPException(status_code=400, detail="invalid district_id")
    OVERRIDES.setdefault("districts", {})[did] = {"REP": rep, "DEM": dem, "IND": ind}
    _save_overrides()
    return {"ok": True, "district_id": did, "override": OVERRIDES["districts"][did]}

@app.delete("/override/district")
def delete_district_override(district_id: str):
    did = str(district_id).strip()
    if did in OVERRIDES.get("districts", {}):
        OVERRIDES["districts"].pop(did, None)
        _save_overrides()
        return {"ok": True, "removed": did}
    return {"ok": True, "removed": None}

@app.delete("/overrides")
def clear_overrides():
    OVERRIDES["counties"].clear()
    OVERRIDES["districts"].clear()
    OVERRIDES["state_calls"].clear()
    _save_overrides()
    return {"ok": True}

# --- NEW: State-call override endpoints (for statewide races like S 2026 G) ---

def _normalize_state(usps: str) -> str:
    s = str(usps or "").upper().strip()
    if s not in USPS_TO_STATE_FIPS:  # known USPS keys
        raise HTTPException(status_code=400, detail="invalid state USPS")
    return s

@app.post("/override/statecall")
async def set_state_call_override(payload: dict):
    """
    Body:
      {
        "state": "AZ",
        "office": "S",
        "year": 2026,
        "race": "G",
        "status": "Called" | "No Decision" | "Recount" | "Pending" | ...,
        "winner_party": "DEM" | "REP" | "IND" | ""  (optional),
        "winner_name": "Taylor Lewis"               (optional)
      }
    """
    try:
        state = _normalize_state(payload.get("state"))
    except HTTPException as e:
        raise e

    office = str(payload.get("office") or "S").upper()
    year   = int(str(payload.get("year") or "2026"))
    race   = str(payload.get("race") or "G").upper()
    status = str(payload.get("status") or "No Decision")
    winner_party = (payload.get("winner_party") or "").upper().strip()
    winner_name  = str(payload.get("winner_name") or "").strip()

    # Store exactly as provided; UI filters to S/2026/G but API can be generic
    OVERRIDES.setdefault("state_calls", {})[state] = {
        "office": office,
        "year": year,
        "race": race,
        "status": status,
        "winner_party": winner_party,
        "winner_name": winner_name,
    }
    _save_overrides()
    return {"ok": True, "state": state, "override": OVERRIDES["state_calls"][state]}

@app.delete("/override/statecall")
def delete_state_call_override(state: str, office: str = "S", year: int = 2026, race: str = "G"):
    st = _normalize_state(state)
    cur = OVERRIDES.setdefault("state_calls", {})
    if st in cur:
        # Optionally only remove if it matches the filter (office/year/race):
        v = cur.get(st, {})
        if (str(v.get("office","")).upper() == str(office).upper()
            and int(v.get("year", 0)) == int(year)
            and str(v.get("race","")).upper() == str(race).upper()):
            cur.pop(st, None)
        else:
            # If it doesn't match, still remove for simplicity:
            cur.pop(st, None)
        _save_overrides()
        return {"ok": True, "removed": st}
    return {"ok": True, "removed": None}

@app.delete("/override/statecall/all")
def delete_all_state_call_overrides(office: str = "S", year: int = 2026, race: str = "G"):
    cur = OVERRIDES.setdefault("state_calls", {})
    # Purge only those matching the filter to be safe
    to_delete = [k for k,v in cur.items()
                 if str(v.get("office","")).upper() == str(office).upper()
                 and int(v.get("year",0)) == int(year)
                 and str(v.get("race","")).upper() == str(race).upper()]
    for k in to_delete:
        cur.pop(k, None)
    _save_overrides()
    return {"ok": True, "removed_count": len(to_delete)}


# --------------------------- Registries for UI -------------------- #

@app.get("/registry/states")
def list_states():
    out = sorted(STATE_REGISTRY.keys())
    return out

@app.get("/registry/counties")
def list_counties(state: str = Query(..., min_length=2, max_length=2)):
    usps = state.upper()
    rows = STATE_REGISTRY.get(usps, [])
    return [{"fips": f, "name": ap} for (f, _canonical, ap) in rows]

# NEW: districts registry for a given state (UI dropdown for H)
@app.get("/registry/districts")
def list_districts(state: str = Query(..., min_length=2, max_length=2)):
    usps = state.upper()
    rows = STATE_CD_REGISTRY.get(usps, [])
    return [{"id": did, "district": dnum, "label": label} for (did, dnum, label) in rows]

# --------------------- RaceType handling (AP-like) ---------------- #

GENERAL_LIKE = {"G","W","H","L","T","S","N","NP","APP","SAP","RET"}
DEM_PRIMARY_IDS = {"D","U","J","A","E"}
GOP_PRIMARY_IDS = {"R","V","K","B"}

def interpret_race_type(raceTypeId: Optional[str]) -> Dict[str, Any]:
    rt = (raceTypeId or "G").strip()
    rt_up = rt.upper()
    if rt_up in GENERAL_LIKE:
        return {"mode": "general", "party_label": None, "override_bucket": None, "raw_code": rt}
    if rt_up in DEM_PRIMARY_IDS:
        return {"mode": "primary", "party_label": "DEM", "override_bucket": "DEM", "raw_code": rt}
    if rt_up in GOP_PRIMARY_IDS:
        return {"mode": "primary", "party_label": "REP", "override_bucket": "REP", "raw_code": rt}
    if rt_up not in GENERAL_LIKE:
        return {"mode": "primary", "party_label": rt, "override_bucket": None, "raw_code": rt}
    return {"mode": "general", "party_label": None, "override_bucket": None, "raw_code": rt}

# ---------------------------- Counties API ------------------------ #

@app.get("/v2/elections/{date}")
def elections_state_ru(
    request: Request,
    date: str,
    statepostal: str = Query(..., min_length=2, max_length=2),
    raceTypeId: str = Query("G"),
    raceId: str = Query("0"),
    level: str = Query("ru"),
    officeId: str = Query("P", regex="^[A-Z]{1,3}$"),
):
    usps = statepostal.upper()
    officeId = (officeId or "P").upper()
# --- NYC MAYOR (borough-only) AP-style feed ----------------------
    if usps == "NY" and officeId == "M" and level.lower() == "ru":
        rt = interpret_race_type(raceTypeId)
        epoch = _epoch_key()

        # Fixed general-election slate (REP/DEM/IND + OTH, WRI)
        slate = [
            ("Curtis Sliwa",  "REP"),
            ("Zohran Mamdani","DEM"),
            ("Andrew Cuomo",  "IND"),
            ("Other",         "OTH"),
            ("Write-ins",     "WRI"),
        ]
        k = len(slate)

        county_parts = []
        statewide_totals = [0]*k

        for fips in NYC_BOROUGH_FIPS:
            ru_percent_in = simulated_percent_in(f"CTY-{fips}-M-{rt['raw_code']}")
            votes_vec = _nyc_borough_vote_vector(fips, office_key="M")  # [REP, DEM, IND, OTH, WRI]
            statewide_totals = [a+b for a,b in zip(statewide_totals, votes_vec)]

            borough_name = NYC_BOROUGH_NAME.get(fips, fips)
            county_parts.append(f'  <ReportingUnit Name="{borough_name}" FIPS="{fips}" PercentIn="{ru_percent_in}">')
            for (full, party), vv in zip(slate, votes_vec):
                first = full.split(" ", 1)[0]
                last  = full.split(" ", 1)[-1] if " " in full else ""
                county_parts.append(f'    <Candidate First="{first}" Last="{last}" Party="{party}" VoteCount="{vv}"/>')
            county_parts.append("  </ReportingUnit>")

        # Race-level call (AP-like)
        total_votes = max(1, sum(statewide_totals))
        order = sorted(range(k), key=lambda i: statewide_totals[i], reverse=True)
        top = statewide_totals[order[0]]
        second = statewide_totals[order[1]] if k > 1 else 0
        margin_pct = 100.0 * (top - second) / total_votes
        state_percent_in = simulated_percent_in(f"STATE-NY-M-{rt['raw_code']}")
        race_status = _race_call_status(state_percent_in, margin_pct)

        if race_status == "Called":
            w_name, w_party = slate[order[0]]
            w_first = w_name.split(" ", 1)[0]
            w_last  = w_name.split(" ", 1)[-1] if " " in w_name else ""
            root = (
                f'<ElectionResults Date="{date}" StatePostal="NY" Office="M" '
                f'Epoch="{epoch}" RaceTypeID="{rt["raw_code"]}" PercentIn="{state_percent_in}" '
                f'RaceCallStatus="{race_status}" WinnerFirst="{w_first}" WinnerLast="{w_last}" WinnerParty="{w_party}">'
            )
        else:
            root = (
                f'<ElectionResults Date="{date}" StatePostal="NY" Office="M" '
                f'Epoch="{epoch}" RaceTypeID="{rt["raw_code"]}" PercentIn="{state_percent_in}" '
                f'RaceCallStatus="{race_status}">'
            )

        parts = [root] + county_parts + ["</ElectionResults>"]
        return Response(content="\n".join(parts), media_type="application/xml")

    counties = STATE_REGISTRY.get(usps, [])
    if not counties:
        xml = f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}"></ElectionResults>'
        return Response(content=xml, media_type="application/xml")

    rt = interpret_race_type(raceTypeId)
    epoch = _epoch_key()

    # We'll compute statewide totals, decide the race-level winner, and *then* render the root tag.
    county_parts: List[str] = []

    if rt["mode"] == "general":
    # build statewide candidate slate
        if officeId == "G":
            # Governor (General) — lock slates for NJ & VA
            # IMPORTANT: Order = [REP, DEM, IND] to align with display_vec [rep, dem, ind]
            if usps == "NJ":
                slate = [
                    ("Jack Ciattarelli", "REP"),
                    ("Mikie Sherrill",   "DEM"),
                    ("Other",            "IND"),
                ]
            elif usps == "VA":
                slate = [
                    ("Winsome Earle-Sears", "REP"),
                    ("Abigail Spanberger",  "DEM"),
                    ("Other",               "IND"),
                ]
            else:
                slate = gen_statewide_candidates(usps, officeId, n=3)
        elif officeId == "A":
            # Attorney General (General) — lock VA slate to Miyares (REP), Jones (DEM), Other (IND)
            if usps == "VA":
                slate = [
                    ("Jason Miyares", "REP"),
                    ("Jay Jones",     "DEM"),
                    ("Other",         "IND"),
                ]
            else:
                slate = gen_statewide_candidates(usps, officeId, n=3)


        elif officeId == "P":
            y = str(date)[:4]
            if y == "2026":
                slate = [("JD Vance", "REP"), ("JB Pritzker", "DEM")]
            elif y == "2020":
                slate = [("Donald Trump", "REP"), ("Joe Biden", "DEM")]
            elif y == "2016":
                slate = [("Donald Trump", "REP"), ("Hillary Clinton", "DEM")]
            else:
                slate = gen_statewide_candidates(usps, officeId, n=3)
            if len(slate) == 2:
                slate = slate + [("Robert Kennedy", "IND")]
        else:
            slate = gen_statewide_candidates(usps, officeId, n=3)


        k = len(slate)
        statewide_totals = [0]*k

        for fips, canonical, apname in counties:
            ru_percent_in = simulated_percent_in(f"CTY-{fips}-{officeId}-{rt['raw_code']}")
            ovr = OVERRIDES.get("counties", {}).get(fips)
            if ovr:
                rep_raw, rep_num = _raw_and_numeric(ovr.get("REP"))
                dem_raw, dem_num = _raw_and_numeric(ovr.get("DEM"))
                ind_raw, ind_num = _raw_and_numeric(ovr.get("IND"))
                display_vec = [rep_raw, dem_raw, ind_raw][:k]
                numeric_vec = [rep_num, dem_num, ind_num][:k]
            else:
                rep, dem, ind = simulated_votes_30s(fips, office_key=officeId)
                display_vec = [str(rep), str(dem), str(ind)][:k]
                numeric_vec = [rep, dem, ind][:k]
            statewide_totals = [a+b for a,b in zip(statewide_totals, numeric_vec)]

            # County RUs: **NO Winner flags** for statewide races
            county_parts.append(f'  <ReportingUnit Name="{apname}" FIPS="{fips}" PercentIn="{ru_percent_in}">')
            for (full, party), vv in zip(slate, display_vec):
                first = full.split(" ", 1)[0]
                last  = full.split(" ", 1)[-1] if " " in full else ""
                county_parts.append(f'    <Candidate First="{first}" Last="{last}" Party="{party}" VoteCount="{vv}"/>')
            county_parts.append(  "  </ReportingUnit>")

        # Decide race-level call
        total_votes = max(1, sum(statewide_totals))
        order = sorted(range(k), key=lambda i: statewide_totals[i], reverse=True)
        top = statewide_totals[order[0]]
        second = statewide_totals[order[1]] if k > 1 else 0
        margin_pct = 100.0 * (top - second) / total_votes
        state_percent_in = simulated_percent_in(f"STATE-{usps}-{officeId}-{rt['raw_code']}")
        race_status = _race_call_status(state_percent_in, margin_pct)

        # --- SIMPLE MANUAL OVERRIDE (S 2026 G) ---
        # Only affects Senate 2026 General statewide topline.
        forced_w_first = forced_w_last = forced_w_party = None
        if officeId == "S" and str(date).startswith("2026") and rt.get("raw_code") in ("G", "GEN"):
            ovr = (SIMPLE_OVERRIDES_2026_S or {}).get(usps)
            if ovr:
                # status override
                s = (ovr.get("status") or "").strip()
                if s:
                    race_status = s

                # winner/name/party override (only meaningful if status is "Called")
                if race_status == "Called":
                    fn = (ovr.get("winner_first") or "").strip()
                    ln = (ovr.get("winner_last")  or "").strip()
                    full = (ovr.get("winner") or "").strip()
                    if full and not (fn or ln):
                        parts = full.split(" ", 1)
                        fn = parts[0]
                        ln = parts[1] if len(parts) > 1 else ""
                    forced_w_first = fn or None
                    forced_w_last  = ln or None
                    forced_w_party = ((ovr.get("party") or "").strip().upper() or None)


        # root with race-level winner attributes (when called)
                # root with race-level winner attributes (when called)
        if race_status == "Called":
            if forced_w_first is not None or forced_w_last is not None or forced_w_party is not None:
                w_first = forced_w_first or ""
                w_last  = forced_w_last or ""
                w_party = forced_w_party or ""
            else:
                w_name, w_party = slate[order[0]]
                w_first = w_name.split(" ", 1)[0]
                w_last  = w_name.split(" ", 1)[-1] if " " in w_name else ""

            root = (
                f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}" '
                f'Epoch="{epoch}" RaceTypeID="{rt["raw_code"]}" PercentIn="{state_percent_in}" '
                f'RaceCallStatus="{race_status}" WinnerFirst="{w_first}" WinnerLast="{w_last}" WinnerParty="{w_party}">'
            )
        else:
            root = (
                f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}" '
                f'Epoch="{epoch}" RaceTypeID="{rt["raw_code"]}" PercentIn="{state_percent_in}" '
                f'RaceCallStatus="{race_status}">'
            )


        parts = [root] + county_parts + ["</ElectionResults>"]
        return Response(content="\n".join(parts), media_type="application/xml")

    # PRIMARY MODE (single-party statewide) — no county winners; race-level only
    party_label = rt["party_label"] or "IND"
    statewide_totals: List[int] = []
    slate_primary: List[Tuple[str,str]] = []
    first_county = True

    for fips, canonical, apname in counties:
        ru_percent_in = simulated_percent_in(f"CTY-{fips}-{officeId}-{rt['raw_code']}")
        primary_slate = gen_primary_candidates(usps, officeId, party_label, n=3)
        k = len(primary_slate)
        if first_county:
            slate_primary = primary_slate
            statewide_totals = [0]*k
            first_county = False

        intra_votes = simulated_primary_votes_30s(fips, party_label, k=k, office_key=officeId)

        ovr = OVERRIDES.get("counties", {}).get(fips)
        if ovr and rt["override_bucket"] in ("DEM","REP"):
            try:
                raw = ovr.get(rt["override_bucket"])
                total = int(str(raw)) if str(raw).isdigit() else None
            except Exception:
                total = None
            if total is not None:
                intra_votes = _scale_votes_to_total(intra_votes, total)

        statewide_totals = [a+b for a,b in zip(statewide_totals, intra_votes)]

        # County RUs: **NO Winner flags** for statewide primaries either
        county_parts.append(f'  <ReportingUnit Name="{apname}" FIPS="{fips}" PercentIn="{ru_percent_in}">')
        for (full, party_code), v in zip(primary_slate, intra_votes):
            first = full.split(" ", 1)[0]
            last  = full.split(" ", 1)[-1] if " " in full else ""
            county_parts.append(f'    <Candidate First="{first}" Last="{last}" Party="{party_code}" VoteCount="{v}"/>')
        county_parts.append(  "  </ReportingUnit>")

    # Decide race-level call for primary
    total_votes = max(1, sum(statewide_totals)) if statewide_totals else 1
    if statewide_totals:
        order = sorted(range(len(statewide_totals)), key=lambda i: statewide_totals[i], reverse=True)
        top = statewide_totals[order[0]]
        second = statewide_totals[order[1]] if len(statewide_totals) > 1 else 0
        margin_pct = 100.0 * (top - second) / total_votes
    else:
        order = [0]
        margin_pct = 0.0

    state_percent_in = simulated_percent_in(f"STATE-{usps}-{officeId}-{rt['raw_code']}")
    race_status = _race_call_status(state_percent_in, margin_pct)

    if race_status == "Called" and slate_primary:
        w_name, w_party = slate_primary[order[0]]
        w_first = w_name.split(" ", 1)[0]
        w_last  = w_name.split(" ", 1)[-1] if " " in w_name else ""
        root = (
            f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}" '
            f'Epoch="{epoch}" RaceTypeID="{rt["raw_code"]}" PercentIn="{state_percent_in}" '
            f'RaceCallStatus="{race_status}" WinnerFirst="{w_first}" WinnerLast="{w_last}" WinnerParty="{w_party}">'
        )
    else:
        root = (
            f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}" '
            f'Epoch="{epoch}" RaceTypeID="{rt["raw_code"]}" PercentIn="{state_percent_in}" '
            f'RaceCallStatus="{race_status}">'
        )

    parts = [root] + county_parts + ["</ElectionResults>"]
    return Response(content="\n".join(parts), media_type="application/xml")

# ----------------------- Congressional Districts API -------------- #
# Now supports general and single-party primaries (House), with overrides.

@app.get("/v2/districts/{date}")
def districts_state_ru(
    request: Request,
    date: str,
    statepostal: str = Query(..., min_length=2, max_length=2),
    level: str = Query("ru"),
    officeId: str = Query("H", regex="^[H]$"),  # House only
    raceTypeId: str = Query("G"),
):
    usps = statepostal.upper()
    districts = STATE_CD_REGISTRY.get(usps, [])

    if not districts:
        xml = f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}"></ElectionResults>'
        return Response(content=xml, media_type="application/xml")

    rt = interpret_race_type(raceTypeId)
    epoch = _epoch_key()
    # Keep root status neutral; each district is its own race and will have its own status.
    state_percent_in = simulated_percent_in(f"STATE-{usps}-{officeId}-{rt['raw_code']}")
    root_status = "No Decision"

    parts = [(
        f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}" '
        f'Epoch="{epoch}" RaceTypeID="{rt["raw_code"]}" PercentIn="{state_percent_in}" '
        f'RaceCallStatus="{root_status}">'
    )]

    if rt["mode"] == "general":
        for did, dnum, label in districts:
            ru_percent_in = simulated_percent_in(f"DIST-{did}-{rt['raw_code']}")
            # Hardcode slate for TX-18 in House Special (any S* code)
            rt_raw = rt["raw_code"].upper()
            if usps == "TX" and rt_raw.startswith("S") and int(dnum) == 18:
                cand = [
                    ("Feldon Bonner II",   "DEM"),
                    ("Amanda Edwards",     "DEM"),
                    ("Stephen Huey",       "DEM"),
                    ("Jolanda Jones",      "DEM"),
                    ("Isaiah Martin",      "DEM"),
                    ("Christian Menefee",  "DEM"),
                    ("Valencia Williams",  "DEM"),
                    ("Theodis Daniel",     "REP"),
                    ("Ollie Knox",         "REP"),
                    ("Carmen Montiel",     "REP"),
                    ("Carter Page",        "REP"),
                    ("Ronald Whitfield",   "REP"),
                    ("Tammie Rochester",   "GRN"),
                    ("Reyna Anderson",     "IND"),
                    ("Vince Duncan",       "IND"),
                    ("George Foreman IV",  "IND"),
                    ("Dave Frost",         "ABC"),
                    ("Maria Resse",        "IND"),
                ]
            else:
                cand = gen_cd_candidates(did, n=3)

            sim_votes = simulated_cd_votes_30s(did, k=len(cand))

            IND_PENALTY = float(os.getenv("IND_PENALTY", "0.03"))  # 20% of baseline; tune as you like
            for i, (_full, party) in enumerate(cand):
                if party == "IND":
                    sim_votes[i] = int(sim_votes[i] * IND_PENALTY)
        
            # Apply district override if available (map by party: REP/DEM/IND)
            ovr = OVERRIDES.get("districts", {}).get(did)
            if ovr:
                party_to_val = {"REP": ovr.get("REP"), "DEM": ovr.get("DEM"), "IND": ovr.get("IND")}
                display_votes, numeric_votes = [], []
                for i, (_full, party) in enumerate(cand):
                    raw, num = _raw_and_numeric(party_to_val.get(party))
                    # If no override provided, fall back to simulated
                    if party_to_val.get(party) is None:
                        raw = str(sim_votes[i])
                        num = sim_votes[i]
                    display_votes.append(raw)
                    numeric_votes.append(num)
            else:
                display_votes = [str(v) for v in sim_votes]
                numeric_votes = sim_votes

            called, win_idx, margin_pct = _ru_called(ru_percent_in, numeric_votes)
            ru_status = _race_call_status(ru_percent_in, margin_pct)

            parts.append(
                f'  <ReportingUnit Name="{label}" DistrictId="{did}" District="{dnum}" '
                f'PercentIn="{ru_percent_in}" RaceCallStatus="{ru_status}">'
            )
            for i, ((full, party), v) in enumerate(zip(cand, display_votes)):
                first = full.split(" ", 1)[0]
                last  = full.split(" ", 1)[-1] if " " in full else ""
                winner_attr = ' Winner="X"' if (called and win_idx == i) else ""
                parts.append(f'    <Candidate First="{first}" Last="{last}" Party="{party}" VoteCount="{v}"{winner_attr}/>')
            parts.append(  "  </ReportingUnit>")
        parts.append("</ElectionResults>")
        return Response(content="\n".join(parts), media_type="application/xml")

    # PRIMARY MODE (single-party per district) — scale to override bucket if present
    party_label = rt["party_label"] or "IND"
    for did, dnum, label in districts:
        ru_percent_in = simulated_percent_in(f"DIST-{did}-{rt['raw_code']}")
        primary_slate = gen_cd_primary_candidates(did, party_label, n=3)
        k = len(primary_slate)
        intra_votes = simulated_cd_primary_votes_30s(did, party_label, k=k)

        ovr = OVERRIDES.get("districts", {}).get(did)
        if ovr and rt["override_bucket"] in ("DEM","REP"):
            try:
                raw = ovr.get(rt["override_bucket"])
                total = int(str(raw)) if str(raw).isdigit() else None
            except Exception:
                total = None
            if total is not None:
                intra_votes = _scale_votes_to_total(intra_votes, total)

        called, win_idx, margin_pct = _ru_called(ru_percent_in, intra_votes)
        ru_status = _race_call_status(ru_percent_in, margin_pct)

        parts.append(
            f'  <ReportingUnit Name="{label}" DistrictId="{did}" District="{dnum}" '
            f'PercentIn="{ru_percent_in}" RaceCallStatus="{ru_status}">'
        )
        for i, ((full, party_code), v) in enumerate(zip(primary_slate, intra_votes)):
            first = full.split(" ", 1)[0]
            last  = full.split(" ", 1)[-1] if " " in full else ""
            winner_attr = ' Winner="X"' if (called and win_idx == i) else ""
            parts.append(f'    <Candidate First="{first}" Last="{last}" Party="{party_code}" VoteCount="{v}"{winner_attr}/>')
        parts.append(  "  </ReportingUnit>")

    parts.append("</ElectionResults>")
    return Response(content="\n".join(parts), media_type="application/xml")

# ---------------------------- Static & Index ---------------------- #

app.mount("/static", StaticFiles(directory="static"), name="static")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

TOPOJSON_DIR = os.path.abspath(os.path.join(ROOT_DIR, "..", "topojson"))

app.mount("/topojson", StaticFiles(directory=TOPOJSON_DIR), name="topojson")

@app.get("/", response_class=Response)
def read_index():
    try:
        with open("indexapi.html", "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/html")
    except FileNotFoundError:
        return Response(content="<h1>API is running</h1>", media_type="text/html")

# ----------------------------- Local dev -------------------------- #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5022")))
