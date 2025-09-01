# app.py – with _parsed_cache optimization
from flask import Flask, request, jsonify, send_from_directory
import requests, xml.etree.ElementTree as ET, threading, time, re, sys, json, os
import unicodedata, concurrent.futures

app = Flask(__name__, static_folder=".", static_url_path="")

API_KEY       = os.environ.get("API_KEY", "4uwfiazjez9koo7aju9ig4zxhr")
BASE_URL      = "https://api2-app2.onrender.com/v2/elections"
ELECTION_DATE = "2024-11-05"
POLL_INTERVAL = 90  # seconds

# caches
_cache = {"states": {}, "districts": {}}
_parsed_cache = {}  # <-- new: (state, office) -> parsed JSON
_cache_lock = threading.Lock()

ALL_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","IA","ID","IL","IN","KS","KY",
    "LA","MA","MD","ME","MI","MN","MO","MS","MT","NC","ND","NE","NH","NJ","NM","NV","NY",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"
]

# ---------------- XML parsing ----------------
def _parse_election_xml(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"ok": False, "error": "parse-error"}
    out = {"ok": True, "counties": {}}
    for county in root.findall(".//county"):
        base = (county.attrib.get("name") or "").strip().lower()
        info = {"candidates": []}
        for c in county.findall("./candidate"):
            info["candidates"].append({
                "name": c.attrib.get("name"),
                "party": c.attrib.get("party"),
                "votes": c.attrib.get("votes")
            })
        out["counties"][base] = info
    return out

def _parse_districts_xml(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"ok": False, "error": "parse-error"}
    out = {"ok": True, "districts": {}}
    for d in root.findall(".//district"):
        key = (d.attrib.get("id") or "").strip()
        info = {"candidates": []}
        for c in d.findall("./candidate"):
            info["candidates"].append({
                "name": c.attrib.get("name"),
                "party": c.attrib.get("party"),
                "votes": c.attrib.get("votes")
            })
        out["districts"][key] = info
    return out

# ---------------- poller ----------------
def fetch_one_state(state, office):
    url = f"{BASE_URL}?apikey={API_KEY}&state={state}&office={office}&electionDate={ELECTION_DATE}"
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            text = r.text
            with _cache_lock:
                _cache["states"][(state, office)] = {"payload": text, "ts": time.time()}
                # pre-parse here
                parsed = _parse_election_xml(text)
                _parsed_cache[(state, office)] = parsed
            print("Fetched upstream:", (state, office), "->", len(text), "bytes")
    except Exception as e:
        print("Error fetching", state, office, e)

def fetch_one_district(state):
    url = f"{BASE_URL}?apikey={API_KEY}&state={state}&office=H&electionDate={ELECTION_DATE}"
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            text = r.text
            with _cache_lock:
                _cache["districts"][(state,)] = {"payload": text, "ts": time.time()}
                parsed = _parse_districts_xml(text)
                _parsed_cache[(state, "H")] = parsed
            print("Fetched upstream:", (state,), "->", len(text), "bytes")
    except Exception as e:
        print("Error fetching districts", state, e)

def poll_api():
    offices = ["P","S","G","H"]
    idx = 0
    while True:
        office = offices[idx % len(offices)]
        idx += 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:  # was 8
            futures = []
            for st in ALL_STATES:
                if office == "H":
                    futures.append(ex.submit(fetch_one_district, st))
                else:
                    futures.append(ex.submit(fetch_one_state, st, office))
            for f in futures: 
                try:
                    f.result()
                except Exception as e:
                    print("Poller task error:", e)
        time.sleep(POLL_INTERVAL)

threading.Thread(target=poll_api, daemon=True).start()

# ---------------- routes ----------------
@app.route("/")
def root():
    return send_from_directory(".", "index.html")

@app.route("/results_state")
def results_state():
    state = request.args.get("state")
    office = request.args.get("office")
    if not state or not office:
        return jsonify({"ok": False, "error": "missing-params"}), 400
    with _cache_lock:
        parsed = _parsed_cache.get((state, office))
    if not parsed:
        return jsonify({"ok": False, "error": "no-snapshot"}), 200
    return jsonify(parsed)

@app.route("/results_districts")
def results_districts():
    state = request.args.get("state")
    office = request.args.get("office")
    if not state or not office:
        return jsonify({"ok": False, "error": "missing-params"}), 400
    with _cache_lock:
        parsed = _parsed_cache.get((state, "H"))
    if not parsed:
        return jsonify({"ok": False, "error": "no-snapshot"}), 200
    return jsonify(parsed)

# ---------------- main ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
