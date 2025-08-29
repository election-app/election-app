# app.py – production version with bulletproof log collector (soft-error responses)
from flask import Flask, send_from_directory, request, jsonify
import requests, xml.etree.ElementTree as ET, time, re, threading, sys, json

app = Flask(__name__, static_folder='.', static_url_path='')

API_KEY       = "4uwfiazjez9koo7aju9ig4zxhr"
ELECTION_DATE = "2024-11-05"
import os
BASE_URL = "https://api2-app2.onrender.com/v2/elections"



@app.after_request
def add_no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
import unicodedata

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
# Cache
# --------------------------------------------------------------------------- #
_state_cache = {}
_CACHE_TTL   = 8
_cache_lock  = threading.Lock()

def _fetch_state_xml(state_code: str, office: str = "P") -> str:
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
            # surface a soft error to caller
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
        # keep this a real 400 so you can see bad client logging payloads if needed
        return jsonify({"ok": False, "error": "bad-json"}), 400
    sys.stdout.write("[CLIENTLOG] " + json.dumps(payload) + "\n")
    sys.stdout.flush()
    return jsonify({"ok": True}), 200

@app.route("/results")
def per_county():
    state  = request.args.get("state", "").upper()
    county = request.args.get("county", "")
    office = (request.args.get("office", "P") or "P").upper()

    # SOFT-ERROR: always HTTP 200 with ok:false
    if not (state and county):
        return jsonify({"ok": False, "error": "missing-params"}), 200

    try:
        xml_blob = _fetch_state_xml(state, office)
    except Exception as e:
        return jsonify({"ok": False, "error": "api-unavailable", "detail": str(e)}), 200

    try:
        root = ET.fromstring(xml_blob)
    except ET.ParseError:
        return jsonify({"ok": False, "error": "bad-xml"}), 200

    for ru in root.iter("ReportingUnit"):
        ru_name = ru.attrib.get("Name")
        if _cmp_key(ru_name) == _cmp_key(county):
            candidates = []
            for c in ru.findall("Candidate"):
                votes = _safe_int(c.attrib.get("VoteCount"))
                candidates.append({
                    "name": f"{c.attrib.get('First','').strip()} {c.attrib.get('Last','').strip()}".strip(),
                    "party": c.attrib.get("Party"),
                    "votes": votes,
                })
            return jsonify({
                "ok": True,
                "county": county,
                "state": state,
                "office": office,
                "results": candidates
            }), 200

    return jsonify({"ok": False, "error": "county-not-found"}), 200

@app.route("/results_cd")
def per_district():
    state  = request.args.get("state", "").upper()
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
                votes = int(re.sub(r"[^\d]", "", c.attrib.get("VoteCount") or "0") or 0)
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


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "9032")), debug=False)


