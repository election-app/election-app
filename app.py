# app.py
from flask import Flask, send_file, jsonify, Response
import os, io, json
from pathlib import Path

# ---- plotting deps (server-side) ----
import math
import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt

app = Flask(__name__, static_folder='.', static_url_path='')

# ======== Config ========
# Allow-list of JSONs we’ll serve via /data?state=XX (lowercase keys)
ALLOWED_JSON = {
    "az": "statewide_plot_data_az.json",
    "ga": "statewide_plot_data_ga.json",
    "ia": "statewide_plot_data_ia.json",
    "mi": "statewide_plot_data_mi.json",
    "pa": "statewide_plot_data_pa.json",
    "nv": "statewide_plot_data_nv.json",
    "sc": "statewide_plot_data_sc.json",
    "wi": "statewide_plot_data_wi.json",
}
def _json_for_request(state_or_file: str | None) -> Path:
    """
    Resolve a JSON path from either a ?state=xx or ?file=filename.json query.
    Falls back to env JSON_FILE (DEFAULT_JSON) if nothing matches.
    """
    if state_or_file:
        s = str(state_or_file).strip()
        # prefer ?state=xx (two-letter); else if it endswith .json treat as filename
        key = s.lower()
        if len(key) == 2 and key in ALLOWED_JSON:
            return _resolve(ALLOWED_JSON[key])
        if s.lower().endswith(".json"):
            # Only allow files in our allow-list values to prevent traversal
            if s in ALLOWED_JSON.values():
                return _resolve(s)
    # fallback to env/default
    return _resolve(JSON_FILE)


MI_CSV   = os.environ.get('MI_INPUT_CSV', 'mi_data_output.csv')
PLOT_PNG = os.environ.get('MI_PLOT_PNG', 'statewide_margin_pct_vs_percent_in_mi.png')

# ======== Basic file helpers ========
def _resolve(path_like: str) -> Path:
    p = Path(path_like)
    return p if p.is_absolute() else Path.cwd() / p

# ======== Left pane (existing) ========
@app.route("/")
def root():
    return send_file("index.html")

from flask import request  # <-- add at top if not already imported

@app.route("/data")
def data():
    # Accept either /data?state=mi  or  /data?file=statewide_plot_data_mi.json
    state = request.args.get("state")
    file_ = request.args.get("file")
    jp = _json_for_request(state or file_)
    if not jp.exists():
        return jsonify({"error": f"JSON not found at {str(jp)}"}), 404
    try:
        with jp.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to read JSON: {e}"}), 500
    return jsonify(payload)


@app.route("/health")
def health():
    return jsonify({"ok": True})

# ======== Right pane plot (MI) ========
# --- helpers adapted from your model-test-nv-9 style ---
def _to_num(s, default=np.nan):
    s = pd.to_numeric(s, errors="coerce")
    if not (isinstance(default, float) and np.isnan(default)):
        s = s.fillna(default)
    return s

def find_statewide_percent_in_column(df: pd.DataFrame):
    candidates = [
        "state_eevp","statewide_eevp","eevp_statewide",
        "percent_in_statewide","state_percent_in","statewide_percent_in",
        "statewide_percent","statewide%in","statewide_pct_in"
    ]
    for c in df.columns:
        if c.lower() in candidates:
            s = _to_num(df[c])
            m = s.max() if s.notna().any() else None
            if m is not None and m <= 1.5:
                s = s * 100.0
            return (s.clip(lower=0, upper=100), c)
    loose = [c for c in df.columns if c.lower() in ("percent_in","eevp")]
    for c in loose:
        s = _to_num(df[c])
        if s.notna().any():
            m = s.max()
            if m is not None and m <= 1.5:
                s = s * 100.0
            return (s.clip(lower=0, upper=100), c)
    return (None, None)

def derive_statewide_votes(df: pd.DataFrame):
    hv_col = next((c for c in df.columns if c.lower() in ("harris_votes_statewide","statewide_harris_votes")), None)
    tv_col = next((c for c in df.columns if c.lower() in ("trump_votes_statewide","statewide_trump_votes")), None)
    ov_col = next((c for c in df.columns if c.lower() in ("other_votes_statewide","statewide_other_votes")), None)
    if hv_col and tv_col and ov_col:
        h = _to_num(df[hv_col], 0.0)
        t = _to_num(df[tv_col], 0.0)
        o = _to_num(df[ov_col], 0.0)
        return h, t, (h + t + o)

    h_snap = next((c for c in df.columns if c.lower() in ("harris_votes","harris_snapshot_votes","h_votes")), None)
    t_snap = next((c for c in df.columns if c.lower() in ("trump_votes","trump_snapshot_votes","t_votes")), None)
    o_snap = next((c for c in df.columns if c.lower() in ("other_votes","other_snapshot_votes","o_votes")), None)
    if h_snap and t_snap and o_snap:
        h = _to_num(df[h_snap], 0.0)
        t = _to_num(df[t_snap], 0.0)
        o = _to_num(df[o_snap], 0.0)
        return h, t, (h + t + o)

    idx = df.index
    z = pd.Series(np.nan, index=idx)
    return z, z, z

def statewide_margin_pct_by_percent_in(df: pd.DataFrame):
    sw, _ = find_statewide_percent_in_column(df)
    if sw is None:
        return np.array([]), np.array([])
    df = df.copy()
    df["__x__"] = _to_num(sw).round(3)
    df = df.loc[df["__x__"] > 0].copy()
    if df.empty: return np.array([]), np.array([])

    hv, tv, tot = derive_statewide_votes(df)
    if hv.isna().all() or tv.isna().all(): return np.array([]), np.array([])

    vt = pd.DataFrame({"x": df["__x__"], "hv": hv, "tv": tv}).dropna()
    if vt.empty: return np.array([]), np.array([])

    has_statewide_cols = any(c.lower() == "harris_votes_statewide" for c in df.columns) \
                         or any(c.lower() == "statewide_harris_votes" for c in df.columns)
    if has_statewide_cols:
        vt = vt.groupby("x", as_index=False)[["hv","tv"]].mean().sort_values("x")
    else:
        vt = vt.groupby("x", as_index=False)[["hv","tv"]].sum().sort_values("x")

    with np.errstate(divide="ignore", invalid="ignore"):
        total = (vt["hv"] + vt["tv"]).to_numpy(dtype=float)
        h_pct = np.where(total > 0, (vt["hv"].to_numpy(dtype=float) / total) * 100.0, np.nan)
        t_pct = np.where(total > 0, (vt["tv"].to_numpy(dtype=float) / total) * 100.0, np.nan)
        margin_pct = h_pct - t_pct  # Harris% − Trump%
    return vt["x"].to_numpy(dtype=float), margin_pct

def build_valid_mask(df: pd.DataFrame):
    sw, _ = find_statewide_percent_in_column(df)
    if sw is None:
        return pd.Series(False, index=df.index)
    return _to_num(sw, 0.0) > 0

def generate_mi_plot(csv_path: Path, out_png: Path) -> bool:
    if not csv_path.exists(): return False
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return False

    mask = build_valid_mask(df)
    df = df.loc[mask].copy()
    if df.empty: return False

    # X and statewide margin pct series (right axis)
    x_v, margin_pct = statewide_margin_pct_by_percent_in(df)
    if len(x_v) == 0: return False

    # Optional: If you have leader/trailer series, you can layer them in too.
    # For now, mimic your uploaded MI plot — line for margin and the scatter overlays:
    # We’ll try to scatter leader/trailer if the columns exist; otherwise just show the margin line.

    fig, ax1 = plt.subplots(figsize=(12, 6))

    # Try to plot “leader” and “trailer” confidences if present (columns like you use elsewhere)
    def maybe_scatter(prefix, color, marker, alpha):
        ycols = [c for c in df.columns if c.lower().startswith(prefix)]
        xsw, _ = find_statewide_percent_in_column(df)
        if len(ycols) == 0 or xsw is None: return
        x = _to_num(xsw).round(3).to_numpy()
        y = _to_num(df[ycols[0]]).to_numpy()
        good = np.isfinite(x) & np.isfinite(y) & (x > 0)
        if good.any():
            ax1.scatter(x[good], y[good], s=28, c=color, marker=marker, alpha=alpha, edgecolor="none")

    maybe_scatter("leader_win_conf", "tab:gray", "o", 0.9)    # if you have such a column
    maybe_scatter("trailer_win_conf", "tab:gray", "s", 0.6)   # if you have such a column
    maybe_scatter("trump_prob", "red", "o", 0.9)              # candidate color proxies if present
    maybe_scatter("harris_prob", "blue", "o", 0.9)

    ax1.axhline(50, linestyle="--", linewidth=1, color="#3b82f6")
    ax1.set_xlim(0, 100)
    ax1.set_ylim(0, 100)
    ax1.set_xlabel("Statewide % in")
    ax1.set_ylabel("Win confidence (p)")

    ax2 = ax1.twinx()
    ax2.plot(x_v, margin_pct, color="#0b6a8a", linewidth=2)
    ax2.set_ylabel("Statewide margin (pp) = Harris% − Trump%")
    ax2.axhline(0, linestyle="--", linewidth=1, color="#64748b")

    ax1.grid(True, alpha=0.25)
    ax1.set_title("MI 2024 Presidential Race")

    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    return True

@app.route("/plot.png")
def serve_plot():
    # Generate (or overwrite) and return the PNG
    ok = generate_mi_plot(_resolve(MI_CSV), _resolve(PLOT_PNG))
    if not ok:
        # Return a small placeholder PNG with a message
        fig, ax = plt.subplots(figsize=(6, 2))
        ax.axis('off')
        ax.text(0.02, 0.5, f"Could not build plot.\nLooking for: {MI_CSV}", va='center', ha='left')
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return Response(buf.read(), mimetype="image/png")

    return send_file(str(_resolve(PLOT_PNG)), mimetype="image/png", max_age=0)

@app.route("/replot", methods=["POST", "GET"])
def replot():
    # Force a rebuild and return a JSON ok for the client to refresh <img> src
    ok = generate_mi_plot(_resolve(MI_CSV), _resolve(PLOT_PNG))
    return jsonify({"ok": bool(ok)})

if __name__ == "__main__":
    # Run: python app.py
    # Env overrides:
    #   JSON_FILE=/path/to/statewide_plot_data_mi.json
    #   MI_INPUT_CSV=/path/to/mi_data_output.csv
    #   MI_PLOT_PNG=/path/to/output.png
    app.run(host="127.0.0.1", port=5000, debug=True)
