from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import threading, time, requests
import logging, json, os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("control-panel-all")

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)  # allow browser fetches from any origin

HOST = os.getenv("TARGET_HOST", "127.0.0.1")  # set to remote host/ip if monitoring another machine
CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "1.0"))

# -----------------------------
# Hub side (existing)
# -----------------------------
APP_HUB_PORT = int(os.getenv("APP_HUB_PORT", "9052"))
HUB_REBOOTER1_PORT = int(os.getenv("HUB_REBOOTER1_PORT", "9050"))
HUB_REBOOTER2_PORT = int(os.getenv("HUB_REBOOTER2_PORT", "9049"))

# -----------------------------
# Manual Entry App (new)
# -----------------------------
APP_UI_MANUAL_PORT  = int(os.getenv("APP_UI_MANUAL_PORT",  "7052"))
APP_HUB_MANUAL_PORT = int(os.getenv("APP_HUB_MANUAL_PORT", "7051"))
UI_REBOOT_1_PORT    = int(os.getenv("UI_REBOOT_1_PORT",    "7046"))
UI_REBOOT_2_PORT    = int(os.getenv("UI_REBOOT_2_PORT",    "7047"))
HUB_REBOOT_1_PORT   = int(os.getenv("HUB_REBOOT_1_PORT",   "7049"))
HUB_REBOOT_2_PORT   = int(os.getenv("HUB_REBOOT_2_PORT",   "7050"))

def _urls(port):
    # Probe both common health endpoints
    return [f"http://{HOST}:{port}/health", f"http://{HOST}:{port}/api/health"]

# Targets to poll. Keys map 1:1 to what the HTML expects.
TARGETS = {
    # Hub group
    "app_hub":       {"urls": _urls(APP_HUB_PORT),       "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},
    "hub_rebooter1": {"urls": _urls(HUB_REBOOTER1_PORT), "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},
    "hub_rebooter2": {"urls": _urls(HUB_REBOOTER2_PORT), "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},

    # Manual Entry App group
    "app_ui_manual":  {"urls": _urls(APP_UI_MANUAL_PORT),  "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},
    "app_hub_manual": {"urls": _urls(APP_HUB_MANUAL_PORT), "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},

    # Manual rebooters (UI + Hub)
    "ui_reboot_1":   {"urls": _urls(UI_REBOOT_1_PORT),   "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},
    "ui_reboot_2":   {"urls": _urls(UI_REBOOT_2_PORT),   "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},
    "hub_reboot_1":  {"urls": _urls(HUB_REBOOT_1_PORT),  "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},
    "hub_reboot_2":  {"urls": _urls(HUB_REBOOT_2_PORT),  "alive": False, "healthy": False, "last_change": None, "url": None, "ms": None, "last_url": None},
}

_poller_started = False

def _best_effort_probe(url: str, timeout: float = 1.5):
    """Probe a single URL; return (alive_bool, healthy_bool_or_None, ms, status_code)."""
    t0 = time.time()
    r = requests.get(url, timeout=timeout)
    ms = (time.time() - t0) * 1000.0
    json_healthy = None
    try:
        body = r.json()
        json_healthy = body.get("healthy")
    except Exception:
        pass
    reachable = 200 <= r.status_code < 300
    # If JSON includes "healthy", treat that as the truthy indicator; else just use reachability.
    alive = bool(json_healthy) if (json_healthy is not None) else reachable
    return alive, json_healthy, ms, r.status_code

def poll_targets():
    log.info("poll_targets thread starting; targets=%s", list(TARGETS.keys()))
    while True:
        for name, info in TARGETS.items():
            alive = False
            healthy = None
            ms_val = None
            last_ok_url = None

            for url in info["urls"]:
                try:
                    a, h, ms, code = _best_effort_probe(url)
                    log.info("probe %-14s %s -> alive=%s healthy=%s http=%s in %.1fms",
                             name, url, a, h, code, ms)
                    if a or (200 <= code < 300):
                        alive = a
                        healthy = h if h is not None else (True if a else False)
                        ms_val = ms
                        last_ok_url = url
                        break
                except Exception as e:
                    log.info("probe %-14s %s -> ERROR: %s", name, url, e)
                    continue

            prev = info["alive"]
            info["alive"] = alive
            info["healthy"] = healthy if healthy is not None else None
            info["ms"] = ms_val
            info["last_url"] = last_ok_url
            if prev != alive:
                info["last_change"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                log.info("STATE CHANGE %-14s -> %s (url=%s, ms=%s)",
                         name, "UP" if alive else "DOWN",
                         last_ok_url, f"{ms_val:.1f}" if ms_val else "—")

        time.sleep(CHECK_INTERVAL)

def _start_poller_once():
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    log.info("Starting poller thread")
    threading.Thread(target=poll_targets, daemon=True).start()

# Start poller across Flask versions and also at import time (works under gunicorn)
try:
    app.before_serving(_start_poller_once)        # Flask >=3.0
except Exception:
    try:
        app.before_first_request(_start_poller_once)  # Flask <3.0
    except Exception:
        pass

if not getattr(app, "_poller_started_flag", False):
    app._poller_started_flag = True
    _start_poller_once()

@app.after_request
def _no_store(resp):
    # Helpful headers for fast, no-cache polling
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

@app.route("/api/status")
def api_status():
    snapshot = {}
    for k, v in TARGETS.items():
        d = dict(v)
        snapshot[k] = {
            "alive": d.get("alive"),
            "healthy": d.get("healthy"),
            "ms": d.get("ms"),
            "url": d["urls"][0] if d.get("urls") else None,
            "last_url": d.get("last_url"),
            "last_change": d.get("last_change"),
        }
    return jsonify(snapshot)

@app.route("/api/probe")
def api_probe():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "provide ?url="}), 400
    t0 = time.time()
    try:
        r = requests.get(url, timeout=2.0)
        ms = (time.time() - t0) * 1000.0
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]
        return jsonify({"status": r.status_code, "ms": ms, "body": body})
    except Exception as e:
        ms = (time.time() - t0) * 1000.0
        return jsonify({"error": str(e), "ms": ms}), 502

@app.route("/")
def serve_index():
    # Serve the accompanying HTML file
    return send_from_directory(".", "control-panel-all.html")

if __name__ == "__main__":
    # Run the panel (default 9048). Override via PANEL_PORT env var.
    app.run(host="0.0.0.0", port=int(os.getenv("PANEL_PORT", "9048")), debug=False)
