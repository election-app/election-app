# api1.py — AP Elections API simulator with officeId (P/S/G/H) support
#
# Endpoints:
#   - /api/ping
#   - /v2/elections/{date}?statepostal=XX&raceTypeId=G&raceId=0&level=ru&officeId=P|S|G
#       -> county-level <ReportingUnit ...><Candidate .../></ReportingUnit>
#   - /v2/districts/{date}?statepostal=XX&level=ru&officeId=H
#       -> congressional-district <ReportingUnit ...><Candidate .../></ReportingUnit>
#
# Notes:
# - Vote counts are deterministic per FIPS/district but “tick” upwards in staggered buckets.
# - Statewide candidates for S/G are deterministic by state+office; House candidates by district.
# - President uses fixed names (Trump/Harris/Kennedy) to mirror typical visuals.

import os, time, re, hashlib, httpx
from typing import Dict, List, Tuple
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, Response
import json


app = FastAPI(title="AP Elections API Simulator")

# ---------------------------- Tunables ---------------------------- #
UPDATE_BUCKETS  = int(os.getenv("UPDATE_BUCKETS", "36"))  # ∝ how “live” the ticking feels
BASELINE_DRIFT  = int(os.getenv("BASELINE_DRIFT", "0"))   # tiny per-tick drift when not selected
TICK_SECONDS    = int(os.getenv("TICK_SECONDS", "10"))    # 10s by default

US_ATLAS_COUNTIES_URL = "https://cdn.jsdelivr.net/npm/us-atlas@3/counties-10m.json"
US_CONGRESS_TOPO_URL = "cb_2024_us_cd119_500k.json"

STATE_FIPS_TO_USPS = {
    "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE","11":"DC",
    "12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA","20":"KS","21":"KY",
    "22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT",
    "31":"NE","32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH",
    "40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD","47":"TN","48":"TX","49":"UT",
    "50":"VT","51":"VA","53":"WA","54":"WV","55":"WI","56":"WY",
    "72":"PR"
}
USPS_TO_STATE_FIPS = {v:k for k,v in STATE_FIPS_TO_USPS.items()}

PARISH_STATES = {"LA"}
INDEPENDENT_CITY_STATES = {"VA"}  # city-county equivalents

# ----------------------- Helpers / RNG / Names -------------------- #
def seeded_rng_u32(seed: str) -> int:
    h = hashlib.blake2b(seed.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big")

def _tick_bucket_key() -> int:
    # map time to 10s (or TICK_SECONDS) “step”
    return int(time.time() // max(1, TICK_SECONDS))

def county_bucket(fips: str) -> int:
    return seeded_rng_u32(fips) % max(1, UPDATE_BUCKETS)

def district_bucket(did: str) -> int:
    return seeded_rng_u32(did) % max(1, UPDATE_BUCKETS)

def apize_name(usps: str, canonical: str) -> str:
    n = canonical
    if n.startswith("Saint "):
        n = "St. " + n[6:]
    n = n.replace("Doña", "Dona")
    n = re.sub(r"\bLa\s+Salle\b", "LaSalle", n)
    n = n.replace("DeKalb", "De Kalb")
    return n

def county_suffix(usps: str, name: str) -> str:
    # Keep "City" county-equivalents as-is (Carson City NV, all VA independent cities, DC style names, etc.)
    if name.lower().endswith("city"):
        return name
    if usps in PARISH_STATES:
        return name if name.lower().endswith("parish") else f"{name} Parish"
    return name if name.lower().endswith("county") else f"{name} County"


# District naming like “1st Congressional District”
def ordinal(n: int) -> str:
    return "%d%s" % (n, "th" if 11<=n%100<=13 else {1:"st",2:"nd",3:"rd"}.get(n%10, "th"))

def district_label(n: int) -> str:
    return f"{ordinal(n)} Congressional District"

# Random-ish (deterministic) candidate name bank
FIRSTS = ["Alex","Taylor","Jordan","Casey","Riley","Avery","Morgan","Quinn","Hayden","Rowan","Elliot","Jesse","Drew","Parker","Reese"]
LASTS  = ["Smith","Johnson","Brown","Jones","Garcia","Miller","Davis","Martinez","Clark","Lewis","Walker","Young","Allen","King","Wright"]
PARTY_POOL = ["REP","DEM","IND"]

def gen_cd_candidates(did: str, n: int = 3) -> List[Tuple[str,str]]:
    """Return list of (FullName, Party) deterministic by district id."""
    base = seeded_rng_u32(did)
    out = []
    used = set()
    for i in range(n):
        f = FIRSTS[(base + i*13) % len(FIRSTS)]
        l = LASTS[(base // 7 + i*17) % len(LASTS)]
        nm = f"{f} {l}"
        if nm in used:
            nm = f"{nm} Jr."
        used.add(nm)
        party = PARTY_POOL[(base // (i+3)) % len(PARTY_POOL)] if i < 3 else "IND"
        out.append((nm, party))
    # Ensure top two are REP and DEM for color balance
    names = [x for x in out]
    have = {p for _,p in names[:2]}
    if "REP" not in have:
        names[0] = (names[0][0], "REP")
    if "DEM" not in have:
        names[1] = (names[1][0], "DEM")
    return names

def gen_statewide_candidates(usps: str, office: str, n: int = 3) -> List[Tuple[str,str]]:
    """
    Deterministic statewide slate per state+office.
    For President we return fixed names to match common on-air visuals.
    """
    office = (office or "P").upper()
    if office == "P":
        return [("Donald Trump","REP"), ("Kamala Harris","DEM"), ("Robert Kennedy","IND")]
    base = seeded_rng_u32(f"{usps}-{office}")
    out = []
    used = set()
    for i in range(n):
        f = FIRSTS[(base + i*11) % len(FIRSTS)]
        l = LASTS[(base // 5 + i*7) % len(LASTS)]
        nm = f"{f} {l}"
        if nm in used:
            nm = f"{nm} II"
        used.add(nm)
        party = PARTY_POOL[(base // (i+2)) % len(PARTY_POOL)] if i < 3 else "IND"
        out.append((nm, party))
    # Ensure REP/DEM among first two
    names = [x for x in out]
    have = {p for _,p in names[:2]}
    if "REP" not in have:
        names[0] = (names[0][0], "REP")
    if "DEM" not in have:
        names[1] = (names[1][0], "DEM")
    return names

def simulated_votes(fips: str) -> Tuple[int,int,int]:
    base_seed = seeded_rng_u32(fips)
    r1 = (base_seed & 0xFFFF)
    r2 = ((base_seed >> 16) & 0xFFFF)
    tick = _tick_bucket_key()

    base_total = 2000 + (base_seed % 250000)
    rep = int(base_total * (0.35 + (r1 % 30) / 100.0))
    dem = base_total - rep
    ind = int(base_total * (0.01 + (r2 % 3) / 100.0))

    current_bucket = tick % max(1, UPDATE_BUCKETS)
    my_bucket      = county_bucket(fips)
    growth = 20 + (base_seed % 20)

    if my_bucket == current_bucket:
        rep += growth // 2
        dem += growth // 2
        ind += max(1, growth // 10)
    elif BASELINE_DRIFT:
        rep += BASELINE_DRIFT
        dem += BASELINE_DRIFT // 2
        ind += max(0, BASELINE_DRIFT // 5)

    return max(rep, 0), max(dem, 0), max(ind, 0)

def simulated_cd_votes(did: str, k: int = 3) -> List[int]:
    """
    Return k vote totals for district 'did' (for k candidates),
    deterministic + bucketed live ticks.
    """
    base_seed = seeded_rng_u32(did)
    tick      = _tick_bucket_key()
    current_bucket = tick % max(1, UPDATE_BUCKETS)
    my_bucket      = district_bucket(did)

    # Base turnout per district ~ 150k–900k
    base_total = 150_000 + (base_seed % 750_000)
    # Split base_total among k with mild bias
    shares = []
    rem = base_total
    for i in range(k - 1):
        slice_i = int((0.25 + ((base_seed >> (i*3)) % 40)/100.0) * (rem / (k - i)))
        shares.append(max(1, slice_i))
        rem -= slice_i
    shares.append(max(1, rem))

    # Growth per tick when selected
    growth = 2000 + (base_seed % 4000)  # bigger jumps than counties
    if my_bucket == current_bucket:
        bump = growth
    else:
        bump = BASELINE_DRIFT

    # Distribute bump with small bias to first two
    bumps = [int(bump*0.45), int(bump*0.45)] + [max(0, bump - int(bump*0.9))]
    bumps = (bumps + [0]*k)[:k]

    return [max(0, s + b) for s,b in zip(shares, bumps)]

# --------------------- Registries built at startup ---------------- #
# counties: USPS → List[(FIPS, canonical_name, ap_name)]
STATE_REGISTRY: Dict[str, List[Tuple[str,str,str]]] = {}
# districts: USPS → List[(district_id, district_num, ap_label)]
STATE_CD_REGISTRY: Dict[str, List[Tuple[str,int,str]]] = {}

@app.on_event("startup")
async def bootstrap():
    async with httpx.AsyncClient(timeout=30) as client:
        # Counties
        r = await client.get(US_ATLAS_COUNTIES_URL); r.raise_for_status()
        topo = r.json()
        geoms = topo.get("objects", {}).get("counties", {}).get("geometries", [])
        for g in geoms:
            fips = str(g.get("id", "")).zfill(5)
            props = g.get("properties", {}) or {}
            name = props.get("name") or props.get("NAMELSAD") or fips
            state_fips = fips[:2]
            usps = STATE_FIPS_TO_USPS.get(state_fips)
            if not usps: continue
            canonical = re.sub(r"\s+(County|Parish|city)$", "", name)
            apname = county_suffix(usps, canonical)
            apname = apize_name(usps, apname)
            STATE_REGISTRY.setdefault(usps, []).append((fips, canonical, apname))
        for usps in STATE_REGISTRY:
            STATE_REGISTRY[usps].sort(key=lambda t: t[0])

        # Congressional districts
        # Congressional districts (use local 119th TopoJSON)
        with open("cb_2024_us_cd119_500k.json", "r") as f:
            cd_topo = json.load(f)

        cd_obj = (cd_topo.get("objects", {}).get("districts")
            or cd_topo.get("objects", {}).get("congressional-districts")
            or cd_topo.get("objects", {}).get(next(iter(cd_topo.get("objects", {})), ""), {}))
        geoms_cd = cd_obj.get("geometries", []) if cd_obj else []

        for g in geoms_cd:
            gid = str(g.get("id", "")).strip()
            props = g.get("properties", {}) or {}

        # Use STATEFP from Census file, not "state"
            m = re.match(r"^\s*(\d{2})", gid) or re.match(r"^\s*(\d{2})", str(props.get("STATEFP") or ""))
            if not m:
                continue

            state_fips = m.group(1)
            usps = STATE_FIPS_TO_USPS.get(state_fips)
            if not usps:
                continue

            dnum = None
            # Prefer Census field CD119FP for district number
            for key in ("CD119FP","district","DISTRICT","cd","CD","number","NUM"):
                if key in props and str(props[key]).strip().isdigit():
                    dnum = int(str(props[key]).strip())
                    break

            if dnum is None:
                tail = re.findall(r"(\d{1,2})$", gid.replace("-", ""))
                dnum = int(tail[0]) if tail else 1

            label = district_label(dnum)
            STATE_CD_REGISTRY.setdefault(usps, []).append((gid, dnum, label))

        for usps in STATE_CD_REGISTRY:
            STATE_CD_REGISTRY[usps].sort(key=lambda t: (t[1], t[0]))


@app.get("/api/ping", response_class=PlainTextResponse)
def ping():
    return "pong"

# ---------------------------- Counties API ------------------------ #
@app.get("/v2/elections/{date}")
def elections_state_ru(
    request: Request,
    date: str,
    statepostal: str = Query(..., min_length=2, max_length=2),
    raceTypeId: str = Query("G"),
    raceId: str = Query("0"),
    level: str = Query("ru"),
    officeId: str = Query("P", regex="^[PSG]$"),  # President/Senate/Governor
):
    """
    Simulated statewide (by-county) endpoint.
    officeId:
      P = President (fixed Trump/Harris/Kennedy)
      S = U.S. Senate (generated names)
      G = Governor (generated names)
    """
    usps = statepostal.upper()
    officeId = (officeId or "P").upper()
    counties = STATE_REGISTRY.get(usps, [])

    if not counties:
        xml = f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}"></ElectionResults>'
        return Response(content=xml, media_type="application/xml")

    # Prepare the candidate slate (consistent across counties for the race)
    slate = gen_statewide_candidates(usps, officeId, n=3)

    parts = [f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}">']
    for fips, canonical, apname in counties:
        v = simulated_votes(fips)
        parts.append(f'  <ReportingUnit Name="{apname}" FIPS="{fips}">')
        for (full, party), vv in zip(slate, v + (0,)):  # map first 3 vote buckets if present
            first = full.split(" ", 1)[0]
            last  = full.split(" ", 1)[-1] if " " in full else ""
            parts.append(f'    <Candidate First="{first}" Last="{last}" Party="{party}" VoteCount="{vv}"/>')
        parts.append(  "  </ReportingUnit>")
    parts.append("</ElectionResults>")
    xml = "\n".join(parts)
    return Response(content=xml, media_type="application/xml")

# ----------------------- Congressional Districts API -------------- #
@app.get("/v2/districts/{date}")
def districts_state_ru(
    request: Request,
    date: str,
    statepostal: str = Query(..., min_length=2, max_length=2),
    level: str = Query("ru"),
    officeId: str = Query("H", regex="^[H]$"),  # House only here
):
    """
    Simulated congressional districts endpoint (House).
    - Emits one <ReportingUnit> per district in the requested state.
    - Each district has 3 generated candidates with parties (REP/DEM/IND).
    """
    usps = statepostal.upper()
    districts = STATE_CD_REGISTRY.get(usps, [])

    if not districts:
        xml = f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}"></ElectionResults>'
        return Response(content=xml, media_type="application/xml")

    parts = [f'<ElectionResults Date="{date}" StatePostal="{usps}" Office="{officeId}">']
    for did, dnum, label in districts:
        cand = gen_cd_candidates(did, n=3)
        votes = simulated_cd_votes(did, k=len(cand))
        parts.append(f'  <ReportingUnit Name="{label}" DistrictId="{did}" District="{dnum}">')
        for (full, party), v in zip(cand, votes):
            first = full.split(" ", 1)[0]
            last  = full.split(" ", 1)[-1] if " " in full else ""
            parts.append(f'    <Candidate First="{first}" Last="{last}" Party="{party}" VoteCount="{v}"/>')
        parts.append(  "  </ReportingUnit>")
    parts.append("</ElectionResults>")
    xml = "\n".join(parts)
    return Response(content=xml, media_type="application/xml")

# ----------------------------- Local dev -------------------------- #
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5022")))
