#!/usr/bin/env python3
"""
app-rebooter1.py — PRIMARY watchdog for app_hub + peer babysitter (rebooter2), with **no logs**.
- Exposes its own /health on R1_HEALTH_PORT (default :9050)
- Checks peer /health (default http://127.0.0.1:9049/health) and restarts rebooter2 if it's down
- Watches app_hub on HUB_PORT (default :9051) and restarts it (clearing port first) if unhealthy

Env overrides (optional):
  HUB_PORT=9051
  CHECK_EVERY=3.0
  REQ_TIMEOUT=2.0
  BACKOFF_MIN=1.0 BACKOFF_MAX=20.0
  R1_HEALTH_PORT=9050
  R2_HEALTH_URL=http://127.0.0.1:9049/health

Hard no-logs policy:
- This script silences its own stdout/stderr.
- Internal log() is a no-op.
- Gunicorn is launched with access/error logs to stdout ("-"), critical level, captured,
  and our Popen discards it to DEVNULL. No log folders/files are ever created.
"""

import os, sys, time, json, socket, subprocess, signal, shutil, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# --------- Hard silence any output from this script ----------
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

def log(*args, **kwargs):
    # No-op logger (absolutely no console/file output)
    return

# ---------------- Tunables ----------------
PRIMARY        = True
PORT           = int(os.getenv("HUB_PORT", "7052"))

# Ensure Gunicorn won't inherit any pre-set file-logging args
os.environ.pop("GUNICORN_CMD_ARGS", None)

# Force a gunicorn command that never writes files and is very quiet.
HUB_CMD = (
    f"gunicorn -w 1 -b 0.0.0.0:{PORT} "
    "--access-logfile - --error-logfile - --log-level critical "
    "--capture-output app_hub:app"
)

CHECK_EVERY    = float(os.getenv("CHECK_EVERY", "3.0"))
REQ_TIMEOUT    = float(os.getenv("REQ_TIMEOUT", "2.0"))
BACKOFF_MIN    = float(os.getenv("BACKOFF_MIN", "1.0"))
BACKOFF_MAX    = float(os.getenv("BACKOFF_MAX", "20.0"))

# Rebooter health ports/URLs
R1_HEALTH_PORT = int(os.getenv("R1_HEALTH_PORT", "7050"))
R2_HEALTH_URL  = os.getenv("R2_HEALTH_URL", "http://127.0.0.1:7049/health")

# Peer (SECONDARY) launch details
PEER_FILE    = "app-reboot2.py"
PEER_PIDFILE = "reboot2.pid"

# ---------------- Globals ----------------
HERE = os.path.dirname(os.path.abspath(__file__))
last_restart_ts = 0.0
backoff = BACKOFF_MIN
started_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def shell(cmd: str):
    # Useful for port clears; output captured internally (we don't print)
    return subprocess.run(cmd, shell=True, cwd=HERE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except Exception:
        return False

def http_get(url: str, timeout: float):
    # requests if available; else curl; else socket check only
    try:
        import requests  # type: ignore
        try:
            r = requests.get(url, timeout=timeout)
            return r.status_code, r.text
        except Exception:
            return 0, ""
    except Exception:
        if shutil.which("curl"):
            if shutil.which("timeout"):
                r = shell(f'timeout {int(timeout)} curl -fsS --max-time {int(timeout)} "{url}"')
            else:
                r = shell(f'curl -fsS --max-time {int(timeout)} "{url}"')
            if r.returncode == 0:
                return 200, r.stdout
            return 0, ""
        # last resort: if URL is this host, just check port
        try:
            host, port = "127.0.0.1", int(url.split(":")[2].split("/")[0])
            return (200 if port_listening(port) else 0), ""
        except Exception:
            return 0, ""

# ------------ app_hub health/restart ------------
def hub_health_ok() -> bool:
    # fast check: port
    if not port_listening(PORT):
        return False
    code, _ = http_get(f"http://127.0.0.1:{PORT}/health", REQ_TIMEOUT)
    return code in (200, 204)  # treat 200/204 as ok

def clear_port(port: int):
    # fully silent port clear
    if shutil.which("lsof"):
        shell(f'kill -15 $(lsof -t -i :{port}) 2>/dev/null')
    if shutil.which("fuser"):
        shell(f'fuser -k {port}/tcp 2>/dev/null')
    time.sleep(0.3)

def start_hub():
    global last_restart_ts, backoff
    P = subprocess.Popen(
        HUB_CMD,
        shell=True,
        cwd=HERE,
        stdout=subprocess.DEVNULL,      # discard all output
        stderr=subprocess.STDOUT,       # merge stderr into stdout (also discarded)
        start_new_session=True
    )
    last_restart_ts = time.time()
    backoff = BACKOFF_MIN

def restart_hub_with_clear():
    global backoff
    clear_port(PORT)
    start_hub()
    time.sleep(backoff)
    backoff = min(BACKOFF_MAX, max(BACKOFF_MIN, backoff * 2.0))

# ------------ peer (rebooter2) watchdog ------------
def peer_health_ok() -> bool:
    code, _ = http_get(R2_HEALTH_URL, REQ_TIMEOUT)
    return code == 200

def start_peer_inline():
    """
    Start app-rebooter2.py silently; write only reboot2.pid. **No log dirs/files.**
    """
    pid_path = os.path.join(HERE, PEER_PIDFILE)
    try:
        P = subprocess.Popen(
            ["python3", PEER_FILE],
            cwd=HERE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with open(pid_path, "w") as pf:
            pf.write(str(P.pid))
    except Exception:
        # fully silent by design
        pass

# ------------ lightweight /health server ------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            body = {
                "role": "primary",
                "pid": os.getpid(),
                "started_utc": started_at,
                "hub_port": PORT,
                "healthy": True
            }
            buf = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):  # silence BaseHTTPRequestHandler logging
        return

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def start_health_server():
    srv = _ThreadingHTTPServer(("0.0.0.0", R1_HEALTH_PORT), _HealthHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

# ------------ main loop ------------
def handle_signals():
    def _sig(signum, _):
        sys.exit(0)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

def main():
    handle_signals()
    start_health_server()
    while True:
        # keep peer alive via /health
        if not peer_health_ok():
            start_peer_inline()

        # keep hub alive
        if not hub_health_ok():
            restart_hub_with_clear()

        time.sleep(CHECK_EVERY)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # stay silent even on fatal errors
        raise
