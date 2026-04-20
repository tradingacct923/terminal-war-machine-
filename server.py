"""""
Altaris Dev - Flask Web Server
"""
import sys, os, json, secrets, hashlib, time, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import logging
from logging.handlers import RotatingFileHandler
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FMT = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
_root_logger = logging.getLogger()
if not any(isinstance(h, RotatingFileHandler) for h in _root_logger.handlers):
    _fh = RotatingFileHandler(
        os.path.join(_LOG_DIR, "server.log"),
        maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    _fh.setFormatter(_LOG_FMT)
    _fh.setLevel(logging.INFO)
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setFormatter(_LOG_FMT)
    _ch.setLevel(logging.INFO)
    _root_logger.addHandler(_fh)
    _root_logger.addHandler(_ch)
    _root_logger.setLevel(logging.INFO)

from flask import Flask, jsonify, request, send_from_directory, Response, make_response
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from data_provider import fetch_all
from config import TICKER as _DEFAULT_TICKER

# ── Auth config (set in .env) ────────────────────────────────────────────────
_DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "Kaali4426")
_DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "war_machine")
_TOKEN_TTL          = 24 * 60 * 60   # 24 hours
_TOKEN_SECRET       = os.getenv("TOKEN_SECRET", "wm-greeksite-secret-key-2024")

import hmac as _hmac
import threading as _threading

def _valid_token(tok: str) -> bool:
    """Verify an HMAC-signed stateless token (works across serverless invocations)."""
    if not tok:
        return False
    try:
        parts = tok.split(".")
        if len(parts) != 3:
            return False
        ts_hex, user_hex, sig = parts
        issued = int(ts_hex, 16)
        if time.time() - issued > _TOKEN_TTL:
            return False
        # Verify signature
        expected = _hmac.new(
            _TOKEN_SECRET.encode(), f"{ts_hex}.{user_hex}".encode(), hashlib.sha256
        ).hexdigest()[:32]
        return _hmac.compare_digest(sig, expected)
    except Exception:
        return False

def _issue_token() -> str:
    """Create an HMAC-signed stateless token: timestamp.username.signature"""
    ts_hex = format(int(time.time()), 'x')
    user_hex = _DASHBOARD_USERNAME.encode().hex()
    payload = f"{ts_hex}.{user_hex}"
    sig = _hmac.new(
        _TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{payload}.{sig}"

def _revoke_token(tok: str):
    pass  # Stateless tokens can't be revoked; they expire naturally

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_DEFAULT_CONFIG = {
    "ticker": _DEFAULT_TICKER,
    "options_api_key": "",
    "options_api_provider": "simulated",
    "alpha_vantage_key": "",
    # data
    "strike_range": 30,
    "max_expirations": 3,
    "refresh_interval": 30,
    "risk_free_rate": 0.045,
    "dividend_yield": 0.005,
    # visual
    "heatmap_pos_color": "#2ecc8a",
    "heatmap_neg_color": "#e8435a",
    "heatmap_neutral_color": "#2a2f45",
    # layout
    "panels_visible": {
        "gex": True, "dex": True, "vex": True, "tex": True,
        "vannex": True, "cex": True, "oi": True, "max_pain": True,
    },
}

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                c = json.load(f)
            # Merge with defaults so new keys always exist
            merged = {**_DEFAULT_CONFIG, **c}
            return merged
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)

def save_config(data: dict):
    merged = {**load_config(), **data}
    with open(CONFIG_PATH, "w") as f:
        json.dump(merged, f, indent=2)
    return merged

def get_ticker():
    return load_config().get("ticker", _DEFAULT_TICKER).upper()

# ── Thundering-herd guard for fetch_all ──────────────────────────────────────
# When the cache is cold and N threads hit /api/data simultaneously, only one
# actually calls fetch_all(); the rest wait on the Event and reuse the result.
_fetch_locks: dict = {}          # ticker → threading.Event
_fetch_results: dict = {}        # ticker → (data, timestamp)
_fetch_meta_lock = _threading.Lock()
_FETCH_TTL = 28                  # seconds — slightly under the 30s refresh interval

def _cached_fetch_all(ticker: str):
    """Thread-safe, single-flight wrapper around fetch_all."""
    now = time.time()
    # Fast path: valid cached result
    with _fetch_meta_lock:
        cached = _fetch_results.get(ticker)
        if cached and now - cached[1] < _FETCH_TTL:
            return cached[0]
        # Slow path: are we already fetching?
        evt = _fetch_locks.get(ticker)
        if evt is None:
            # We are the leader — create the Event and start fetching
            evt = _threading.Event()
            _fetch_locks[ticker] = evt
            leader = True
        else:
            leader = False

    if leader:
        try:
            data = fetch_all(ticker)
            with _fetch_meta_lock:
                _fetch_results[ticker] = (data, time.time())
                del _fetch_locks[ticker]
            evt.set()           # wake all waiters
            return data
        except Exception:
            with _fetch_meta_lock:
                _fetch_locks.pop(ticker, None)
            evt.set()
            raise
    else:
        evt.wait(timeout=30)   # wait for leader, max 30s
        with _fetch_meta_lock:
            cached = _fetch_results.get(ticker)
        return cached[0] if cached else fetch_all(ticker)

app = Flask(__name__, static_folder="web", static_url_path="")
CORS(app)  # Open CORS — user will configure domain later
# gevent async mode: native WebSocket support, ~5ms latency, cooperative multitasking.
# Replaces threading mode (37ms GIL latency + broken WS handshake under Werkzeug 3.x).
# eventlet breaks SSL on Python 3.9 (RecursionError in ssl.py) — gevent is safe.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent",
                    ping_timeout=30, ping_interval=10)

# Build version = git short hash (falls back to file mtime)
def _build_ver():
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        try:
            return str(int(os.path.getmtime(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "app.js")
            )))
        except Exception:
            return "unknown"

_BUILD_VER = _build_ver()


# ── Auth middleware ────────────────────────────────────────────────────────────
_PUBLIC_PATHS = {"/login", "/api/login", "/login.css", "/style.css", "/app.js",
                 "/login.html", "/index.html"}
_PUBLIC_PREFIXES = ("/web/",)
_PUBLIC_EXTENSIONS = ('.css', '.js', '.html', '.ico', '.png', '.jpg', '.svg',
                      '.woff', '.woff2', '.ttf', '.map')

@app.before_request
def require_auth():
    path = request.path

    # Always allow static assets (CSS, JS, images, fonts)
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return None
    if any(path.endswith(ext) for ext in _PUBLIC_EXTENSIONS):
        return None

    # Block server source files
    blocked_exts  = ('.py', '.pyc', '.env', '.git', '.bat', '.sh')
    blocked_paths = ('/.git', '/.env', '/config.json', '/config.py',
                     '/data_provider.py', '/server.py', '/macro_provider.py',
                     '/Procfile', '/requirements.txt', '/__pycache__')
    # TEMP: diagnostic flow endpoints — bypass auth for proof collection
    if path.startswith('/api/_debug/'):
        return None
    if any(path.endswith(e) for e in blocked_exts) or \
       any(path.startswith(b) for b in blocked_paths):
        return jsonify({"error": "Forbidden"}), 403

    # Check token — cookie (browser nav) OR header (API calls)
    tok = (request.cookies.get("wm_auth")
           or request.headers.get("X-Auth-Token")
           or request.args.get("token", ""))
    if not _valid_token(tok):
        if path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        resp = Response(status=302, headers={"Location": "/login"})
        resp.delete_cookie("wm_auth")
        return resp
    return None

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response

# ── Login page ────────────────────────────────────────────────────────────────
@app.route("/login")
def login_page():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "login.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("email", "") or data.get("username", "")).strip().lower()
    password = (data.get("password", "") or "")

    ok_user = username == _DASHBOARD_USERNAME.strip().lower()
    ok_pass = secrets.compare_digest(password, _DASHBOARD_PASSWORD)

    if ok_user and ok_pass:
        tok = _issue_token()
        resp = jsonify({"token": tok, "expires_in": _TOKEN_TTL})
        resp.set_cookie("wm_auth", tok, max_age=_TOKEN_TTL, httponly=True, samesite="Lax")
        return resp
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    tok = request.cookies.get("wm_auth") or request.headers.get("X-Auth-Token", "")
    _revoke_token(tok)
    resp = jsonify({"ok": True})
    resp.delete_cookie("wm_auth")
    return resp

# ── Dashboard ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("BUILD_VERSION", _BUILD_VER)
    resp = Response(content, mimetype="text/html")
    # Force no caching — ensures Render/CDN always serves fresh HTML
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ── Debug: prove what HTML is being served ────────────────────────────────────
@app.route("/api/debug/html")
def debug_html():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    has_sidebar = "sidebar" in content
    has_terminal = "terminal" in content
    line_count = content.count("\n") + 1
    return jsonify({
        "file": html_path,
        "line_count": line_count,
        "has_sidebar": has_sidebar,
        "has_terminal": has_terminal,
        "first_500_chars": content[:500],
        "build_ver": _BUILD_VER,
    })

# â”€â”€ Settings API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = load_config()
    def mask(k):
        return ("*" * (len(k) - 4) + k[-4:]) if len(k) > 4 else ("*" * len(k))
    pv = cfg.get("panels_visible", _DEFAULT_CONFIG["panels_visible"])
    return jsonify({
        # API keys (masked)
        "ticker":               cfg["ticker"],
        "options_api_provider": cfg["options_api_provider"],
        "options_api_key":      mask(cfg["options_api_key"]) if cfg["options_api_key"] else "",
        "alpha_vantage_key":    mask(cfg["alpha_vantage_key"]) if cfg["alpha_vantage_key"] else "",
        "options_api_key_set":  bool(cfg["options_api_key"]),
        "alpha_vantage_key_set":bool(cfg["alpha_vantage_key"]),
        # data
        "strike_range":      cfg.get("strike_range", 30),
        "max_expirations":   cfg.get("max_expirations", 3),
        "refresh_interval":  cfg.get("refresh_interval", 30),
        "risk_free_rate":    cfg.get("risk_free_rate", 0.045),
        "dividend_yield":    cfg.get("dividend_yield", 0.005),
        # visual
        "heatmap_pos_color":     cfg.get("heatmap_pos_color", "#2ecc8a"),
        "heatmap_neg_color":     cfg.get("heatmap_neg_color", "#e8435a"),
        "heatmap_neutral_color": cfg.get("heatmap_neutral_color", "#2a2f45"),
        # layout
        "panels_visible": {
            "gex":      pv.get("gex", True),
            "dex":      pv.get("dex", True),
            "vex":      pv.get("vex", True),
            "tex":      pv.get("tex", True),
            "vannex":   pv.get("vannex", True),
            "cex":      pv.get("cex", True),
            "oi":       pv.get("oi", True),
            "max_pain": pv.get("max_pain", True),
        },
    })

@app.route("/api/test-connection", methods=["GET", "POST"])
def api_test_connection():
    import urllib.request as _ur, json as _json
    kind = request.args.get("type", "alpha_vantage")
    cfg  = load_config()
    # Allow passing the key directly in the POST body so the user can
    # test before saving (key field may not be in config.json yet)
    body_key = ""
    if request.method == "POST" or request.content_length:
        try:
            body_key = (request.get_json(force=True, silent=True) or {}).get("key", "")
        except Exception:
            pass
    try:
        if kind == "alpha_vantage":
            key = body_key or cfg.get("alpha_vantage_key", "")
            if not key:
                return jsonify({"ok": False, "message": "No Alpha Vantage key configured"})
            url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=IBM&apikey={key}"
            with _ur.urlopen(_ur.Request(url), timeout=8) as r:
                data = _json.loads(r.read())
            if "Global Quote" in data and data["Global Quote"]:
                return jsonify({"ok": True,  "message": "✔ Alpha Vantage key is valid — connection successful"})
            elif "Note" in data:
                return jsonify({"ok": False, "message": "⚠ Rate limit hit — key is valid but API call limit reached"})
            elif "Error Message" in data:
                return jsonify({"ok": False, "message": "✖ Invalid API key"})
            else:
                return jsonify({"ok": False, "message": f"Unexpected response: {str(data)[:120]}"})

        elif kind == "options":
            provider = cfg.get("options_api_provider", "simulated")
            # Fall back to env var just like _get_token() does
            key = body_key or cfg.get("options_api_key", "") or os.getenv("TRADIER_TOKEN", "")

            if provider == "simulated":
                return jsonify({"ok": True, "message": "✔ Simulated mode — no connection needed"})

            elif provider == "yfinance":
                import yfinance as yf
                t     = yf.Ticker(cfg.get("ticker", "QQQ"))
                price = getattr(t.fast_info, "last_price", None)
                if price:
                    return jsonify({"ok": True,  "message": f"✔ yFinance connected — {cfg.get('ticker','QQQ')} @ ${price:.2f}"})
                else:
                    return jsonify({"ok": False, "message": "yFinance returned no price data"})

            elif provider == "tradier":
                if not key:
                    return jsonify({"ok": False, "message": "No Tradier token found — paste your token and click Save & Apply first, or paste it above and retry"})
                ticker = cfg.get("ticker", "QQQ")
                # Try production first, then sandbox
                for base in ("https://api.tradier.com/v1", "https://sandbox.tradier.com/v1"):
                    try:
                        url = f"{base}/markets/quotes?symbols={ticker}&greeks=false"
                        req = _ur.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
                        with _ur.urlopen(req, timeout=8) as r:
                            data = _json.loads(r.read())
                        q = data.get("quotes", {}).get("quote", {})
                        price = q.get("last") or q.get("close") or q.get("bid")
                        env = "sandbox" if "sandbox" in base else "production"
                        if price:
                            return jsonify({"ok": True, "message": f"✔ Tradier ({env}) connected — {ticker} @ ${float(price):.2f}"})
                    except Exception:
                        continue
                return jsonify({"ok": False, "message": "✖ Tradier connection failed — check your token and ensure it has market data permissions"})

            else:
                if not key:
                    return jsonify({"ok": False, "message": f"No API key configured for {provider}"})
                return jsonify({"ok": True, "message": f"Key saved for {provider} — live validation not yet implemented"})
        else:
            return jsonify({"ok": False, "message": "Unknown connection type"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Error: {str(e)}"})

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    try:
        data = request.get_json(force=True)
        allowed = {
            "ticker", "options_api_key", "options_api_provider", "alpha_vantage_key",
            "strike_range", "max_expirations", "refresh_interval",
            "risk_free_rate", "dividend_yield",
            "heatmap_pos_color", "heatmap_neg_color", "heatmap_neutral_color",
            "panels_visible",
        }
        update = {k: v for k, v in data.items() if k in allowed}
        # Don't overwrite a real key with a masked placeholder OR an empty field
        for key_field in ("options_api_key", "alpha_vantage_key"):
            val = update.get(key_field, None)
            if val is not None and (str(val).strip() == "" or "*" in str(val)):
                del update[key_field]
        # Invalidate data cache when data-affecting settings change
        if any(k in update for k in ("strike_range", "max_expirations", "risk_free_rate", "dividend_yield", "ticker")):
            try:
                from data_provider import _cache, _cache_ts
                _cache.clear(); _cache_ts.clear()
            except Exception as _e:
                print(f"[Settings] Cache invalidation failed: {_e}")
        saved = save_config(update)
        return jsonify({"ok": True, "ticker": saved["ticker"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _build_heatmap(raw_dict):
    """Convert {strike: [{label, dte, calls_val, puts_val}]} to heatmap format.
    puts_val is already negative (e.g. DEX) so net = calls_val + puts_val."""
    if not raw_dict:
        return {"strikes": [], "expirations": [], "rows": []}
    strikes = sorted(raw_dict.keys(), reverse=True)
    exps = [{"label": e["label"], "dte": e["dte"]} for e in raw_dict[strikes[0]]]
    rows = []
    for s in strikes:
        cells = []
        for e in raw_dict[s]:
            net = e["calls_val"] + e["puts_val"]  # puts_val is already negative
            cells.append(round(net, 2))
        rows.append({"strike": s, "cells": cells})
    return {"strikes": [float(s) for s in strikes], "expirations": exps, "rows": rows}

def _build_gex_heatmap(raw_dict):
    """GEX heatmap: puts_val is POSITIVE (gamma always +ve), so net = calls - puts.
    Matches the net_gex sign convention used by the bar chart."""
    if not raw_dict:
        return {"strikes": [], "expirations": [], "rows": []}
    strikes = sorted(raw_dict.keys(), reverse=True)
    exps = [{"label": e["label"], "dte": e["dte"]} for e in raw_dict[strikes[0]]]
    rows = []
    for s in strikes:
        cells = [round(e["calls_val"] - e["puts_val"], 2) for e in raw_dict[s]]
        rows.append({"strike": s, "cells": cells})
    return {"strikes": [float(s) for s in strikes], "expirations": exps, "rows": rows}

def _build_tex_heatmap(raw_dict):
    """TEX: negative is good (dealers collecting theta)."""
    if not raw_dict:
        return {"strikes": [], "expirations": [], "rows": []}
    strikes = sorted(raw_dict.keys(), reverse=True)
    exps = [{"label": e["label"], "dte": e["dte"]} for e in raw_dict[strikes[0]]]
    rows = []
    for s in strikes:
        cells = [round(-(e["calls_val"] + e["puts_val"]), 2) for e in raw_dict[s]]
        rows.append({"strike": s, "cells": cells})
    return {"strikes": [float(s) for s in strikes], "expirations": exps, "rows": rows}

def _build_oi_heatmap(raw_dict):
    """OI: separate calls and puts per cell."""
    if not raw_dict:
        return {"strikes": [], "expirations": [], "call_rows": [], "put_rows": []}
    strikes = sorted(raw_dict.keys(), reverse=True)
    exps = [{"label": e["label"], "dte": e["dte"]} for e in raw_dict[strikes[0]]]
    call_rows, put_rows = [], []
    for s in strikes:
        calls = [int(e["calls_val"]) for e in raw_dict[s]]
        puts  = [int(e["puts_val"])  for e in raw_dict[s]]
        call_rows.append({"strike": s, "cells": calls})
        put_rows.append({"strike": s, "cells": puts})
    return {"strikes": [float(s) for s in strikes], "expirations": exps,
            "call_rows": call_rows, "put_rows": put_rows}

def _build_net_bar(raw_dict):
    """Sum all expirations per strike → net exposure bar dict.
    calls_val and puts_val are already signed (puts_val is negative),
    so net = calls_val + puts_val."""
    bar = {}
    for s, exps in raw_dict.items():
        bar[str(s)] = round(sum(e["calls_val"] + e["puts_val"] for e in exps), 2)
    return bar

def _build_vex_bar(raw_dict):
    """VEX: puts_val is positive (vega is always +ve for both calls & puts).
    Net = calls_val - puts_val → positive at call-heavy strikes, negative at put-heavy."""
    bar = {}
    for s, exps in raw_dict.items():
        bar[str(s)] = round(sum(e["calls_val"] - e["puts_val"] for e in exps), 2)
    return bar

# ── Data Endpoints ────────────────────────────────────────────────────────────
@app.route("/api/candles")
def api_candles():
    """
    OHLCV candles for the current ticker.
    Query params:
      ?days=1   (1 = today, 3, 5, 10 — default 1)
      ?interval=5 (minutes — default 5)
    Also returns key levels: put_wall, call_wall, max_pain, spot.
    """
    try:
        from data_provider import _tradier_timesales, fetch_all
        from datetime import datetime, timedelta
        import numpy as np
        import pandas as _pd

        ticker = get_ticker()
        days   = min(int(request.args.get("days", 1)), 10)
        _now   = datetime.today()
        _start = _now - timedelta(days=days + 3)   # extra buffer for weekends

        df = _tradier_timesales(ticker, _start, _now)
        if df.empty:
            return jsonify({"error": "No data"}), 500

        # Filter to requested trading days
        unique_days = sorted(df.index.normalize().unique())[-days:]
        if len(unique_days) == 0:
            return jsonify({"error": "Not enough trading days"}), 500
        cutoff = unique_days[0]
        if df.index.tz:
            cutoff = _pd.Timestamp(cutoff).tz_localize(df.index.tz)
        df = df[df.index >= cutoff]

        candles = []
        delta_by_price = {}   # price_level -> net delta (approx)

        for t, row in df.iterrows():
            o  = float(row["Open"])
            h  = float(row["High"])
            lo = float(row["Low"])
            c  = float(row["Close"])
            v  = int(row.get("Volume", 0))
            hl = h - lo if h != lo else 0.0001

            # Approximate buy/sell volume (tick-rule proxy)
            buy_frac  = (c - lo) / hl
            sell_frac = (h - c)  / hl
            buy_vol   = v * buy_frac
            sell_vol  = v * sell_frac
            delta     = int(buy_vol - sell_vol)

            candles.append({
                "t":   t.strftime("%Y-%m-%dT%H:%M:%S"),
                "o":   round(o,  2),
                "h":   round(h,  2),
                "l":   round(lo, 2),
                "c":   round(c,  2),
                "v":   v,
                "d":   delta,           # per-bar delta
            })

            # Accumulate delta by price level (rounded to 0.5 ticks)
            price_lvl = round(round(c * 2) / 2, 1)
            delta_by_price[price_lvl] = delta_by_price.get(price_lvl, 0) + delta

        # Build delta profile sorted by price
        delta_profile = sorted(
            [{"price": p, "delta": d} for p, d in delta_by_price.items()],
            key=lambda x: x["price"]
        )

        # EMA-20 overlay
        closes = df["Close"]
        ema20  = closes.ewm(span=20, adjust=False).mean()
        ema50  = closes.ewm(span=50, adjust=False).mean()
        emas   = [{"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "e20": round(float(v), 2),
                   "e50": round(float(w), 2)}
                  for t, v, w in zip(df.index, ema20, ema50)]

        # Key levels from cached options data
        try:
            data = _cached_fetch_all(ticker)
            levels = {
                "spot":       round(float(data["spot"]), 2),
                "call_wall":  round(float(data["gex"]["call_wall"]), 2),
                "put_wall":   round(float(data["gex"]["put_wall"]),  2),
                "max_pain":   round(float(data["max_pain"]["max_pain_strike"]), 2),
            }
        except Exception:
            levels = {}

        return jsonify({
            "ticker":  ticker,
            "candles": candles,
            "emas":    emas,
            "levels":        levels,
            "delta_profile": delta_profile,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Schwab REST helpers ─────────────────────────────────────────────────────
# Lightweight wrapper around stored OAuth2 tokens — no SchwabAuth class needed
# at import time. Reads from connectors/.schwab_tokens.json, auto-refreshes.

import base64 as _b64

_SCHWAB_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connectors", ".schwab_tokens.json")
_SCHWAB_BASE       = "https://api.schwabapi.com"
_schwab_tokens     = {}  # module-level cache
_schwab_lock       = threading.Lock()


def _schwab_load_tokens():
    """Load tokens from disk (fresh read each time)."""
    global _schwab_tokens
    try:
        with open(_SCHWAB_TOKEN_FILE) as f:
            _schwab_tokens = json.load(f)
    except Exception:
        _schwab_tokens = {}
    return _schwab_tokens


def _schwab_refresh():
    """Refresh the access token using the stored refresh token."""
    global _schwab_tokens
    tokens = _schwab_load_tokens()
    rt = tokens.get("refresh_token")
    if not rt:
        raise ValueError("No Schwab refresh token available")
    app_key    = os.getenv("SCHWAB_APP_KEY", "")
    app_secret = os.getenv("SCHWAB_APP_SECRET", "")
    if not app_key or not app_secret:
        raise ValueError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set in env")
    creds = _b64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
    import requests as _req
    resp = _req.post(f"{_SCHWAB_BASE}/v1/oauth/token", headers={
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, data={"grant_type": "refresh_token", "refresh_token": rt}, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Schwab refresh failed: {resp.status_code} — {resp.text[:200]}")
    data = resp.json()
    import time as _t
    new_tokens = {
        "access_token":  data["access_token"],
        "refresh_token": data.get("refresh_token", rt),
        "token_expiry":  _t.time() + data.get("expires_in", 1800),
        "saved_at":      _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime()),
    }
    with open(_SCHWAB_TOKEN_FILE, "w") as f:
        json.dump(new_tokens, f, indent=2)
    _schwab_tokens.update(new_tokens)
    print("[Schwab] Token refreshed OK")


# ── Background auto-refresh thread ──────────────────────────────────────────
# Proactively refreshes every 25 min (5 min before 30-min expiry).
# Keeps the refresh token chain alive — Schwab issues a new refresh token on
# each successful refresh, so the 7-day refresh token never goes stale.
def _schwab_auto_refresh_loop():
    """Background daemon: refresh Schwab tokens every 25 minutes."""
    import time as _t
    INTERVAL = 25 * 60  # 25 minutes
    RETRY_INTERVAL = 2 * 60  # retry after 2 min on failure
    # Initial delay: wait 30s for server startup to settle
    _t.sleep(30)
    while True:
        try:
            with _schwab_lock:
                tokens = _schwab_load_tokens()
                expiry = tokens.get("token_expiry", 0)
                # Only refresh if we actually have tokens
                if tokens.get("refresh_token"):
                    # Refresh if within 5 min of expiry or already expired
                    if _t.time() > expiry - 300:
                        _schwab_refresh()
                        print(f"[Schwab] ⟳ Auto-refresh OK at {_t.strftime('%H:%M:%S')}")
                    else:
                        remaining = int(expiry - _t.time())
                        print(f"[Schwab] Token still valid ({remaining}s remaining), skipping refresh")
        except Exception as e:
            print(f"[Schwab] ⚠ Auto-refresh failed: {e}")
            # Retry sooner on failure
            _t.sleep(RETRY_INTERVAL)
            try:
                with _schwab_lock:
                    _schwab_refresh()
                print(f"[Schwab] ⟳ Auto-refresh retry OK at {_t.strftime('%H:%M:%S')}")
            except Exception as e2:
                print(f"[Schwab] ❌ Auto-refresh retry also failed: {e2}")
        _t.sleep(INTERVAL)

_schwab_refresh_thread = threading.Thread(target=_schwab_auto_refresh_loop, daemon=True)
_schwab_refresh_thread.start()


def _schwab_get(endpoint, params=None):
    """Authenticated GET to Schwab API with auto-refresh."""
    import requests as _req
    import time as _t
    with _schwab_lock:
        tokens = _schwab_load_tokens()
        at = tokens.get("access_token")
        expiry = tokens.get("token_expiry", 0)
        # Refresh if expired or about to expire (< 60s left)
        if not at or _t.time() > expiry - 60:
            _schwab_refresh()
            tokens = _schwab_tokens
            at = tokens["access_token"]
    url = f"{_SCHWAB_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {at}", "Accept": "application/json"}
    resp = _req.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code == 401:
        # Try one refresh
        with _schwab_lock:
            _schwab_refresh()
            at = _schwab_tokens["access_token"]
        headers["Authorization"] = f"Bearer {at}"
        resp = _req.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Schwab API error: {resp.status_code} — {resp.text[:300]}")
    return resp.json()


def _schwab_quote(ticker):
    """Get single quote from Schwab. Returns last price as float."""
    data = _schwab_get("/marketdata/v1/quotes", {"symbols": ticker, "fields": "quote"})
    q = data.get(ticker, {})
    quote = q.get("quote", q)
    return float(quote.get("lastPrice") or quote.get("mark") or quote.get("closePrice") or 0)


def _schwab_expirations(ticker):
    """Get options expirations from Schwab. Returns list of date strings.
    Filters out expired dates (before today) to avoid 400 errors."""
    from datetime import date as _date
    data = _schwab_get("/marketdata/v1/expirationchain", {"symbol": ticker})
    raw = data.get("expirationList", [])
    today_str = _date.today().isoformat()  # e.g. '2026-04-01'
    result = []
    for exp in raw:
        d = exp.get("expirationDate")
        if d and d >= today_str:  # only keep today or future
            result.append(d)
    return result


def _schwab_chain_raw(ticker, exp_date):
    """Get options chain from Schwab for one expiration. Returns flattened list of option dicts.
    Enriched with: mark, high, low, open, theoreticalOptionValue, theoreticalVolatility,
    markChange, tradeTimeInLong for institutional-grade analysis."""
    data = _schwab_get("/marketdata/v1/chains", {
        "symbol": ticker,
        "contractType": "ALL",
        "includeUnderlyingQuote": "true",
        "fromDate": exp_date,
        "toDate": exp_date,
        "strikeCount": 200,
    })
    options = []
    for leg_key in ("callExpDateMap", "putExpDateMap"):
        exp_map = data.get(leg_key, {})
        for _exp_str, strikes in exp_map.items():
            for strike_str, contracts in strikes.items():
                for c in contracts:
                    options.append({
                        "strike":         float(strike_str),
                        "option_type":    "call" if leg_key == "callExpDateMap" else "put",
                        "bid":            c.get("bid", 0),
                        "ask":            c.get("ask", 0),
                        "last":           c.get("last", 0),
                        "volume":         c.get("totalVolume", 0),
                        "open_interest":  c.get("openInterest", 0),
                        "delta":          c.get("delta"),
                        "gamma":          c.get("gamma"),
                        "theta":          c.get("theta"),
                        "vega":           c.get("vega"),
                        "rho":            c.get("rho"),
                        "volatility":     c.get("volatility"),  # Schwab IV (decimal)
                        "in_the_money":   c.get("inTheMoney", False),
                        "dte":            c.get("daysToExpiration", 0),
                        "symbol":         c.get("symbol", ""),
                        # ── Enriched fields (Phase 2) ──
                        "mark":             c.get("mark", 0),              # Schwab-computed fair mark
                        "high":             c.get("highPrice", 0),         # Intraday high
                        "low":              c.get("lowPrice", 0),          # Intraday low
                        "open":             c.get("openPrice", 0),         # Day's open
                        "theo_value":       c.get("theoreticalOptionValue", 0),  # Model price
                        "theo_vol":         c.get("theoreticalVolatility", 0),   # Model IV
                        "mark_change":      c.get("markChange", 0),        # Premium change
                        "trade_time":       c.get("tradeTimeInLong", 0),   # Last trade epoch ms
                    })
    return options, data.get("underlyingPrice", 0)


def _schwab_movers(symbol_id: str, sort: str = "PERCENT_CHANGE_UP",
                   frequency: str = "0") -> list:
    """Top movers within an index. symbol_id ∈ $SPX, $DJI, $COMPX, $IUXX, INDEX_ALL.
    sort ∈ VOLUME, TRADES, PERCENT_CHANGE_UP, PERCENT_CHANGE_DOWN.
    Returns list of {symbol, description, last, change, direction, totalVolume}.
    """
    data = _schwab_get(f"/marketdata/v1/movers/{symbol_id}",
                       {"sort": sort, "frequency": frequency})
    return data.get("screeners", []) or []


def _schwab_fundamentals(symbols: list, projection: str = "fundamental") -> dict:
    """Per-ticker fundamentals. symbols is a list (comma-joined for Schwab).
    Schwab's /instruments endpoint uses `symbol` (singular) even for multi-symbol
    queries — comma-separated in the single param.
    Returns dict keyed by symbol → fundamentals dict (~40 fields).
    """
    if not symbols:
        return {}
    data = _schwab_get("/marketdata/v1/instruments",
                       {"symbol": ",".join(symbols), "projection": projection})
    out = {}
    for inst in data.get("instruments", []) or []:
        sym = inst.get("symbol")
        fund = inst.get("fundamental") or {}
        if sym and fund:
            out[sym] = fund
    return out


# ── /api/movers + /api/fundamentals caches ───────────────────────────────────
_movers_cache: dict = {}        # {(symbol_id, sort, frequency): (result, ts)}
_MOVERS_TTL = 300               # 5 min
_fundamentals_cache: dict = {}  # {symbol: (data, ts)}
_FUNDAMENTALS_TTL = 3600        # 1 hr


@app.route("/api/movers")
def api_movers():
    """Top movers in an index. ?index=$SPX&sort=PERCENT_CHANGE_UP&frequency=0."""
    symbol_id = request.args.get("index", "$SPX")
    sort = request.args.get("sort", "PERCENT_CHANGE_UP").upper()
    frequency = request.args.get("frequency", "0")
    key = (symbol_id, sort, frequency)
    now = time.time()
    cached = _movers_cache.get(key)
    if cached and now - cached[1] < _MOVERS_TTL:
        return jsonify({
            "symbol_id": symbol_id, "sort": sort,
            "cached_age": int(now - cached[1]),
            "movers": cached[0],
        })
    try:
        movers = _schwab_movers(symbol_id, sort, frequency)
        _movers_cache[key] = (movers, now)
        return jsonify({
            "symbol_id": symbol_id, "sort": sort,
            "cached_age": 0, "movers": movers,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fundamentals")
def api_fundamentals():
    """Per-ticker fundamentals. ?symbols=AAPL,MSFT,NVDA.
    Per-symbol 1-hour cache; partial cache hits refetch only stale symbols.
    """
    raw = request.args.get("symbols", "").strip()
    if not raw:
        return jsonify({"error": "symbols param required"}), 400
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "symbols param required"}), 400
    now = time.time()
    fresh, stale = {}, []
    for s in symbols:
        c = _fundamentals_cache.get(s)
        if c and now - c[1] < _FUNDAMENTALS_TTL:
            fresh[s] = c[0]
        else:
            stale.append(s)
    if stale:
        try:
            new_data = _schwab_fundamentals(stale)
            for s, v in new_data.items():
                _fundamentals_cache[s] = (v, now)
                fresh[s] = v
        except Exception as e:
            return jsonify({"error": str(e), "partial": fresh}), 500
    return jsonify({"fundamentals": fresh, "count": len(fresh)})


@app.route("/api/_debug/flow_live")
def api_debug_flow_live():
    """PUBLIC — TEMP diagnostic: dump live FlowAccumulator state for real-time proof.
    Bypasses auth on purpose — remove once proof collected."""
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({"tickers": [], "note": "accumulator not ready"})
        states = acc.get_all_states()
        import time as _time
        return jsonify({
            "server_time": _time.time(),
            "tickers": list(states.values()),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/_debug/subs")
def api_debug_subs():
    """PUBLIC — dump exactly what option symbols we subscribed to."""
    try:
        from background_engine import schwab_bridge as sb
        subs = getattr(sb, '_subscribed_option_symbols_by_ticker', {})
        out = {}
        for ticker, syms in subs.items():
            # Bucket by YYMMDD expiration
            by_date = {}
            for s in syms:
                d = s[6:12] if len(s) >= 12 else '?'
                by_date[d] = by_date.get(d, 0) + 1
            out[ticker] = {
                'total': len(syms),
                'by_expiration': by_date,
                'first_3': syms[:3],
                'last_3': syms[-3:],
            }
        return jsonify(out)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/_debug/flow_diag")
def api_debug_flow_diag():
    """PUBLIC — show what symbols are being classified into what buckets."""
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({"note": "accumulator not ready"})
        return jsonify(acc.get_diag())
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/_debug/alert_log")
def api_debug_alert_log():
    """PUBLIC — most recent alerts fired by the engine."""
    try:
        from connectors.alert_engine import get_engine
        eng = get_engine()
        if eng is None:
            return jsonify({"note": "engine not ready"})
        return jsonify({
            "alerts": eng.get_log(last_n=100),
            "sample_counts_per_ticker": {
                t: eng.get_sample_count(t)
                for t in ('SPX', 'SPY', 'QQQ', 'NDX', 'VIX',
                          'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA')
            },
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/alerts/history")
def api_alerts_history():
    """Historical alert log for a specific date (YYYYMMDD).
    Powers the date-picker's historical replay feature.
    Falls back to today if no date supplied."""
    try:
        from connectors.alert_engine import get_engine, init_engine
        from datetime import date as _d
        eng = get_engine() or init_engine()
        date_str = request.args.get("date", "").strip() or _d.today().strftime("%Y%m%d")
        # Validate date format (YYYYMMDD, 8 digits)
        if not date_str.isdigit() or len(date_str) != 8:
            return jsonify({"error": "date must be YYYYMMDD"}), 400
        last_n = min(int(request.args.get("last_n", 500) or 500), 5000)
        alerts = eng.get_history(date_str, last_n=last_n)
        return jsonify({"date": date_str, "count": len(alerts), "alerts": alerts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts/walls")
def api_alerts_walls():
    """PUBLIC — current per-ticker walls cached on AlertEngine.
    Used to verify the Key Level detector is receiving fresh wall data."""
    try:
        from connectors.alert_engine import get_engine
        eng = get_engine()
        if eng is None:
            return jsonify({"ready": False, "tickers": {}})
        with eng._lock:
            out = {t: dict(h.last_walls) for t, h in eng._history.items() if h.last_walls}
        return jsonify({"ready": True, "server_time": time.time(), "tickers": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts/state")
def api_alerts_state():
    """Per-ticker current-state matrix for the AI Panel 4×3 UI.
    Returns {'flow_cross','flow_divergence','key_level','spike_dump'} →
    'bullish'|'bearish'|'none' for every ticker the engine is tracking."""
    try:
        from connectors.alert_engine import get_engine
        eng = get_engine()
        if eng is None:
            return jsonify({"ready": False, "tickers": {}})
        return jsonify({
            "ready": True,
            "server_time": time.time(),
            "tickers": eng.get_state_matrix(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/_debug/levelone_cache_probe")
def api_debug_levelone_cache_probe():
    """Probe: does our LEVELONE cache have the exact symbol SCREENER sends?"""
    try:
        from background_engine.schwab_bridge import _on_options_quote, _on_screener_option
        cache = getattr(_on_options_quote, '_sym_cache', {}) or {}
        raw = getattr(_on_screener_option, '_rawdiag', None) or {}
        samples = raw.get('samples', [])
        # For each screener sample, check if its symbol is in LEVELONE cache
        probes = []
        for s in samples[:5]:
            sym = s.get('symbol', '')
            cached = cache.get(sym) or {}
            probes.append({
                'screener_symbol': sym,
                'in_levelone_cache': sym in cache,
                'cached_delta': cached.get('delta'),
                'cached_bid': cached.get('bid'),
                'cached_ask': cached.get('ask'),
                'cached_dte': cached.get('dte'),
            })
        # Also spot-check: how many total symbols are in LEVELONE cache?
        return jsonify({
            'levelone_cache_size': len(cache),
            'sample_cache_keys': list(cache.keys())[:5],
            'probes': probes,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/_debug/screener_delta_source")
def api_debug_screener_delta_source():
    """PUBLIC — ratio of SCREENER_OPTION items whose delta came from Schwab's
    LEVELONE cache (real) vs a moneyness-linear fallback (estimated)."""
    try:
        from background_engine.schwab_bridge import _screener_to_accumulator
        src = getattr(_screener_to_accumulator, '_src_diag', {}) or {}
        total = sum(src.values()) or 1
        return jsonify({
            "counts": dict(src),
            "pct_real": round(100 * src.get('levelone_cache', 0) / total, 2),
            "pct_estimated": round(100 * src.get('estimated', 0) / total, 2),
            "total": sum(src.values()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/_debug/screener_raw")
def api_debug_screener_raw():
    """PUBLIC — dump what SCREENER_OPTION actually sends us from Schwab.
    Reveals whether Schwab includes delta/bid/ask natively or whether we
    truly need to estimate them."""
    try:
        from background_engine.schwab_bridge import _on_screener_option
        raw = getattr(_on_screener_option, '_rawdiag', None) or {}
        keys = sorted(raw.get('all_keys_seen', set()))
        return jsonify({
            "all_keys_ever_seen": keys,
            "n_distinct_keys": len(keys),
            "samples": raw.get('samples', []),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/_debug/spx_raw")
def api_debug_spx_raw():
    """PUBLIC — dump _raw_spx_diag from FlowAccumulator.
    Shows EVERY SPX/SPXW message that arrived, even those dropped for
    last_size==0. Reveals whether Schwab sends 0DTE trade prints at all."""
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({"ready": False})
        raw = getattr(acc, '_raw_spx_diag', None) or {}
        # Convert tuple keys to strings for JSON serialization
        totals = {f"{exp}__{bucket}": n for (exp, bucket), n in (raw.get('totals') or {}).items()}
        return jsonify({
            "totals": totals,
            "samples_260420": raw.get('samples') or [],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/_debug/flow_classify")
def api_debug_flow_classify():
    """PUBLIC — dump FlowAccumulator._classify_diag + _date_diag.
    Reveals how trades are being bucketed per ticker (useful for SPX 0DTE audit)."""
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({"ready": False})
        return jsonify(acc.get_diag())
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/_debug/flow_live_alerts")
def api_debug_flow_alerts():
    """PUBLIC — run alert engine against current accumulator state; return alerts.
    Returns any alerts the AlertEngine has detected since server start."""
    try:
        from connectors.flow_accumulator import get_accumulator
        from connectors.alert_engine import init_engine
        acc = get_accumulator()
        if acc is None:
            return jsonify({"ready": False, "note": "accumulator not ready"})
        # Lazy-init the engine (doesn't auto-run yet; one-shot observe loop here)
        engine = init_engine()
        import time as _time
        now = _time.time()
        states = acc.get_all_states()
        # Trigger observe once per ticker to see what the engine says NOW
        fresh_alerts = []
        for st in states.values():
            alerts = engine.observe(
                st['ticker'], now,
                st['cum_signed_0dte'], st['cum_signed_all'],
                st['cum_unsigned_0dte'], st['cum_unsigned_all'],
                0.0,  # spot — not in flow accumulator; set 0 to skip divergence
            )
            fresh_alerts.extend(alerts)
        return jsonify({
            "server_time": now,
            "alerts_this_call": fresh_alerts,
            "tickers_with_data": [s['ticker'] for s in states.values()],
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/option_flow")
def api_option_flow():
    """Snapshot of signed + unsigned Δ notional flow per ticker, from FlowAccumulator.

    Powers initial hydration when the flow pane mounts. Live updates
    after hydration arrive via the 'flow_update' socketio event.
    Separate from /api/flow (which is FlowClassifier's L2-book scores).
    """
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({"tickers": [], "ready": False})
        states = acc.get_all_states()
        return jsonify({"tickers": list(states.values()), "ready": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chain")
def api_chain():
    """Return real options chain from Schwab for the terminal options panel.
    Self-contained — calls Schwab API directly."""
    from datetime import datetime, date

    ticker = request.args.get("ticker", "QQQ").upper()

    try:
        # Step 1: Get expirations from Schwab
        raw_dates = _schwab_expirations(ticker)
        if not raw_dates:
            return jsonify({"error": f"No expirations found for {ticker}"}), 404

        today = date.today()
        expirations = []
        for d in raw_dates:
            try:
                exp_dt = datetime.strptime(d, "%Y-%m-%d").date()
                dte = (exp_dt - today).days
                label = exp_dt.strftime("%b %d")
                expirations.append({"date": d, "label": label, "dte": dte})
            except Exception:
                continue

        # Pick requested expiry or nearest
        req_exp = request.args.get("exp", "")
        if req_exp and any(e["date"] == req_exp for e in expirations):
            exp_date = req_exp
        else:
            exp_date = expirations[0]["date"]  # nearest
        exp_label = next((e["label"] for e in expirations if e["date"] == exp_date), exp_date)
        exp_dte = next((e["dte"] for e in expirations if e["date"] == exp_date), 0)

        # Step 2: Get chain + underlying price from Schwab
        raw_chain, schwab_underlying = _schwab_chain_raw(ticker, exp_date)
        spot = schwab_underlying if schwab_underlying > 0 else _schwab_quote(ticker)

        # ── Compute futures/underlying ratio for NQ$ mapping ──
        try:
            from background_engine.l2_worker import get_l2_state
            futures_mid = get_l2_state().get("mid_prices", {}).get("NQ", 0)
        except Exception:
            futures_mid = 0
        ratio = futures_mid / spot if (futures_mid > 0 and spot > 0) else 0
        if ratio <= 0:
            print(f"[api/chain] ⚠️ No live NQ/QQQ ratio — NQ$ mapping disabled")

        # ── Build per-strike aggregates for P/C and GEX ──
        strike_data = {}
        for opt in raw_chain:
            strike = opt["strike"]
            otype = opt["option_type"]
            vol = int(opt.get("volume") or 0)
            oi = int(opt.get("open_interest") or 0)
            gamma = float(opt.get("gamma") or 0)
            if strike not in strike_data:
                strike_data[strike] = {"call_vol": 0, "put_vol": 0,
                                       "call_gamma": 0, "put_gamma": 0,
                                       "call_oi": 0, "put_oi": 0}
            sd = strike_data[strike]
            if otype == "call":
                sd["call_vol"] += vol
                sd["call_gamma"] = gamma
                sd["call_oi"] += oi
            elif otype == "put":
                sd["put_vol"] += vol
                sd["put_gamma"] = gamma
                sd["put_oi"] += oi

        # Step 4: Build response with fusion fields
        rows = []
        for opt in raw_chain:
            iv_raw = opt.get("volatility")
            iv_str = str(round(float(iv_raw), 1)) if iv_raw else None
            strike = opt["strike"]
            nq_price = round(strike * ratio, 2)
            sd = strike_data.get(strike, {})

            # Per-strike P/C ratio
            total_call_vol = sd.get("call_vol", 0)
            total_put_vol = sd.get("put_vol", 0)
            pc_ratio = round(total_put_vol / max(total_call_vol, 1), 2)

            # Per-strike GEX
            otype = opt["option_type"]
            oi_val = int(opt.get("open_interest") or 0)
            gamma_val = float(opt.get("gamma") or 0)
            gex = round(gamma_val * oi_val * spot * spot * 0.01 * 100, 0)
            if otype == "put":
                gex = -gex

            rows.append({
                "strike":  strike,
                "type":    otype,
                "bid":     float(opt.get("bid") or 0),
                "ask":     float(opt.get("ask") or 0),
                "last":    float(opt.get("last") or 0),
                "volume":  int(opt.get("volume") or 0),
                "oi":      oi_val,
                "iv":      iv_str,
                "delta":   round(float(opt["delta"]), 4) if opt.get("delta") is not None else None,
                "gamma":   round(float(opt["gamma"]), 6) if opt.get("gamma") is not None else None,
                "theta":   round(float(opt["theta"]), 4) if opt.get("theta") is not None else None,
                "vega":    round(float(opt["vega"]), 4) if opt.get("vega") is not None else None,
                # ── Fusion fields ──
                "nq_price": nq_price,
                "pc_ratio": pc_ratio,
                "gex":      gex,
                # ── Enriched fields (Phase 2) ──
                "mark":        round(float(opt.get("mark") or 0), 4),
                "high":        round(float(opt.get("high") or 0), 4),
                "low":         round(float(opt.get("low") or 0), 4),
                "theo":        round(float(opt.get("theo_value") or 0), 4),
                "mark_chg":    round(float(opt.get("mark_change") or 0), 4),
                "mispriced":   abs(float(opt.get("mark") or 0) - float(opt.get("theo_value") or 0)) > 0.05 * max(float(opt.get("theo_value") or 1), 0.01) if opt.get("theo_value") else False,
            })

        # ── Top GEX levels (for gamma wall lines on chart) ──
        gex_by_strike = {}
        for opt in raw_chain:
            strike = opt["strike"]
            g = float(opt.get("gamma") or 0)
            oi_r = int(opt.get("open_interest") or 0)
            raw_gex = g * oi_r * spot * spot * 0.01 * 100
            if opt["option_type"] == "put":
                raw_gex = -raw_gex
            gex_by_strike[strike] = gex_by_strike.get(strike, 0) + raw_gex

        sorted_gex = sorted(gex_by_strike.items(), key=lambda x: abs(x[1]), reverse=True)
        top_gex = []
        for strike_val, gex_val in sorted_gex[:10]:
            nq_mapped = round(strike_val * ratio, 2)
            top_gex.append({
                "strike": strike_val,
                "nq_price": nq_mapped,
                "gex": round(gex_val, 0),
                "type": "call_wall" if gex_val > 0 else "put_wall",
            })

        print(f"[api/chain] Schwab: {ticker} spot={spot:.2f} exp={exp_date} chains={len(raw_chain)}")
        return jsonify({
            "ticker": ticker,
            "spot": spot,
            "expiry": exp_date,
            "expiry_label": exp_label,
            "dte": exp_dte,
            "expirations": expirations[:12],
            "chain": rows,
            "ratio": round(ratio, 4),
            "futures_mid": futures_mid,
            "top_gex": top_gex,
        })
    except Exception as e:
        import traceback
        print(f"[api/chain] Error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


_walls_cache = {}
_walls_locks = {}
_walls_meta_lock = _threading.Lock()
_WALLS_TTL = 28

@app.route("/api/walls")
def api_walls():
    """Put/call wall + max pain — supports NQ (via QQQ) and GC (via GLD).
    Multi-expiry aggregation (top 5), DTE weighting, gamma flip, freshness.
    Query params:
      ?symbol=NQ  (default) — uses QQQ options, maps to NQ futures
      ?symbol=GC  — uses GLD options, maps to GC futures
      ?ticker=XXX — override underlying ticker directly (legacy compat)
    """
    import time as _t
    from datetime import datetime, date
    _walls_start = _t.time()

    # Determine underlying ticker and futures symbol from ?symbol= param
    futures_sym = request.args.get("symbol", "NQ").upper()
    FUTURES_TO_UNDERLYING = {"NQ": "QQQ", "GC": "GLD"}
    ticker = request.args.get("ticker", FUTURES_TO_UNDERLYING.get(futures_sym, "QQQ")).upper()

    cache_key = f"{ticker}_{futures_sym}"
    with _walls_meta_lock:
        cached = _walls_cache.get(cache_key)
        if cached and _t.time() - cached[1] < _WALLS_TTL:
            return jsonify(cached[0])
        evt = _walls_locks.get(cache_key)
        if evt is None:
            evt = _threading.Event()
            _walls_locks[cache_key] = evt
            leader = True
        else:
            leader = False

    if not leader:
        evt.wait(timeout=30)
        with _walls_meta_lock:
            cached = _walls_cache.get(cache_key)
        if cached:
            return jsonify(cached[0])
        # If it timed out or failed, we fall through and fetch it anyway

    try:
        import numpy as np
        from scipy.stats import norm as _norm
        import math

        # ── BSM greeks helper ────────────────────────────────────────────
        def _bsm_greeks(S, K, T, r, sigma, opt_type='call'):
            """BSM greeks — returns (delta, gamma, vanna, charm)."""
            if T <= 0 or sigma <= 0 or S <= 0:
                return 0, 0, 0, 0
            d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
            d2 = d1 - sigma * np.sqrt(T)
            pdf_d1 = _norm.pdf(d1)
            gamma  = pdf_d1 / (S * sigma * np.sqrt(T))
            vanna  = -pdf_d1 * d2 / (S * sigma * np.sqrt(T))
            charm_val = -pdf_d1 * (2 * r * T - d2 * sigma * np.sqrt(T)) / (2 * T * sigma * np.sqrt(T))
            return d1, gamma, vanna, charm_val

        # ── Fetch expirations from Schwab ────────────────────────────────
        raw_dates = _schwab_expirations(ticker)
        if not raw_dates:
            return jsonify({"error": f"No expirations for {ticker}"}), 404

        # Use up to 5 nearest expirations for multi-expiry aggregation
        MAX_EXP = 5
        exp_dates = raw_dates[:MAX_EXP]

        # ── Multi-expiry aggregation with DTE weighting ──────────────────
        # Weighted OI: w = 3.0 if DTE<=1 else 1/sqrt(DTE)
        agg_call_oi = {}   # strike → weighted OI
        agg_put_oi  = {}
        all_strikes = set()
        spot = 0
        # Risk-free rate — use live 13-week T-bill yield ($IRX.X), never hardcode
        r = 0.045  # sensible init; overwritten below if live data available
        try:
            irx_price = _schwab_quote("$IRX")
            if irx_price and irx_price > 0:
                r = irx_price / 100.0  # $IRX quotes in % (e.g. 4.5 → 0.045)
        except Exception:
            pass  # keep sensible init if quote fails
        all_ivs = []

        # Per-strike greek accumulators (weighted)
        strike_gamma_net = {}   # for gamma flip: net dealer GEX per strike
        strike_vanna_abs = {}   # for vanna wall: |vanna × OI|
        strike_charm_net = {}   # for charm flow

        # Best single-expiry gamma for 0DTE pin (only nearest expiry)
        best_gamma_strike, best_gamma_score = 0, 0

        for exp_idx, exp_date in enumerate(exp_dates):
            try:
                raw_chain, schwab_underlying = _schwab_chain_raw(ticker, exp_date)
                if exp_idx == 0 and schwab_underlying > 0:
                    spot = schwab_underlying
            except Exception as e:
                print(f"[api/walls] expiry {exp_date} fetch failed: {e}")
                continue

            if not raw_chain:
                continue

            # Compute DTE + weight
            try:
                exp_dt = datetime.strptime(exp_date, "%Y-%m-%d").date()
                dte = max((exp_dt - date.today()).days, 0)
            except Exception:
                dte = 1
            dte_clamped = max(dte, 1)
            T = dte_clamped / 365.0

            # DTE weight: 0DTE gets 3×, others decay by 1/√dte
            if dte <= 1:
                w = 3.0
            else:
                w = 1.0 / math.sqrt(dte_clamped)

            # Collect IV from this expiry
            for opt in raw_chain:
                iv_raw = float(opt.get("volatility", 0) or 0) / 100.0
                if iv_raw > 0.01:
                    all_ivs.append(iv_raw)

            avg_iv = 0.20
            if all_ivs:
                avg_iv = max(np.mean(all_ivs), 0.05)

            # Aggregate OI per strike with weight
            # INSTITUTIONAL GRADE: use Schwab's per-contract gamma (actual IV surface)
            #   + dollar GEX (gamma × OI × 100 × spot²)
            #   + volume+OI hybrid for 0DTE
            s = spot or schwab_underlying
            for opt in raw_chain:
                strike = opt["strike"]
                oi = int(opt.get("open_interest") or 0)
                vol = int(opt.get("volume") or 0)
                otype = opt["option_type"]
                all_strikes.add(strike)

                # Pure OI — no made-up volume adjustment factor
                effective_oi = float(oi)

                w_oi = effective_oi * w

                if otype == "call":
                    agg_call_oi[strike] = agg_call_oi.get(strike, 0) + w_oi
                elif otype == "put":
                    agg_put_oi[strike] = agg_put_oi.get(strike, 0) + w_oi

                if w_oi <= 0:
                    continue

                # ── INSTITUTIONAL GAMMA: use Schwab's per-contract gamma ──
                # Schwab computes gamma using actual IV surface (skew-aware)
                schwab_gamma = float(opt.get("gamma") or 0)

                # Dollar GEX = gamma × OI × 100 × spot² × 0.01
                # This is the actual dollar hedging flow dealers must execute
                dollar_gex = schwab_gamma * w_oi * 100 * s * s * 0.01

                # Gamma flip: net dealer GEX (dealers are short options)
                if otype == "call":
                    strike_gamma_net[strike] = strike_gamma_net.get(strike, 0) - dollar_gex
                else:
                    strike_gamma_net[strike] = strike_gamma_net.get(strike, 0) + dollar_gex

                # ── VANNA + CHARM: BSM with per-contract IV (not avg) ──
                per_iv = float(opt.get("volatility", 0) or 0) / 100.0
                if per_iv < 0.01:
                    per_iv = avg_iv  # fallback to avg if missing

                _, _, vanna_v, charm_v = _bsm_greeks(s, strike, T, r, per_iv, otype)

                # Vanna wall: |vanna × OI| (dollar-weighted)
                strike_vanna_abs[strike] = strike_vanna_abs.get(strike, 0) + abs(vanna_v * w_oi * 100 * s)

                # Charm flow (dollar-weighted)
                charm_dollar = charm_v * w_oi * 100 * s
                if otype == "call":
                    strike_charm_net[strike] = strike_charm_net.get(strike, 0) + charm_dollar
                else:
                    strike_charm_net[strike] = strike_charm_net.get(strike, 0) - charm_dollar

                # 0DTE pin: only nearest expiry, use schwab gamma × raw oi
                if exp_idx == 0:
                    pin_gex = schwab_gamma * effective_oi * 100 * s * s * 0.01
                    strike_gamma_net[f"_0dte_{strike}"] = strike_gamma_net.get(f"_0dte_{strike}", 0) + abs(pin_gex)

        # ── Fallback: if spot wasn't set, try a direct quote ──
        if spot <= 0:
            spot = _schwab_quote(ticker)

        # ── Calculate walls from aggregated weighted OI ──────────────────
        underlying_put_wall  = max(agg_put_oi, key=agg_put_oi.get) if agg_put_oi else 0
        underlying_call_wall = max(agg_call_oi, key=agg_call_oi.get) if agg_call_oi else 0

        # Max pain (using aggregated weighted OI)
        sorted_strikes = sorted(all_strikes)
        min_pain = float("inf")
        underlying_max_pain = spot
        for K in sorted_strikes:
            pain = 0
            for S in sorted_strikes:
                if S < K:  pain += agg_put_oi.get(S, 0) * (K - S) * 100
                if S > K:  pain += agg_call_oi.get(S, 0) * (S - K) * 100
            if pain < min_pain:
                min_pain = pain
                underlying_max_pain = K

        # ── Vanna Wall: highest |vanna × OI| across all expirations ──
        underlying_vanna_wall = 0
        if strike_vanna_abs:
            underlying_vanna_wall = max(strike_vanna_abs, key=strike_vanna_abs.get)

        # ── Charm Flow: net across all strikes ──
        net_charm = sum(strike_charm_net.values())
        charm_direction = "UP" if net_charm > 0 else "DOWN"
        charm_magnitude = round(abs(net_charm), 4)

        # ── 0DTE Pin: highest gamma × OI on nearest expiry ──
        dte0_keys = [k for k in strike_gamma_net if str(k).startswith("_0dte_")]
        if dte0_keys:
            best_key = max(dte0_keys, key=lambda k: abs(strike_gamma_net[k]))
            underlying_zero_dte_pin = float(best_key.replace("_0dte_", ""))
        else:
            underlying_zero_dte_pin = underlying_max_pain  # fallback

        # ── Gamma Flip: where net dealer GEX crosses zero ────────────────
        # Walk sorted strikes, find sign change in net GEX
        underlying_gamma_flip = 0
        real_strikes = [s for s in sorted_strikes if s in strike_gamma_net]
        for i in range(len(real_strikes) - 1):
            s1, s2 = real_strikes[i], real_strikes[i + 1]
            g1, g2 = strike_gamma_net[s1], strike_gamma_net[s2]
            if g1 * g2 < 0:  # sign change → zero crossing
                # Linear interpolation for exact flip price
                frac = abs(g1) / (abs(g1) + abs(g2)) if (abs(g1) + abs(g2)) > 0 else 0.5
                underlying_gamma_flip = s1 + frac * (s2 - s1)
                break  # take first (nearest-to-ATM) flip

        # ── 4-Tier NDX→NQ Conversion ────────────────────────────────────
        # NQ tracks NDX (Nasdaq 100 index), not QQQ directly.
        # Tier 1: Live NQ (TopStepX L2 WS) / Live QQQ (Schwab chain)
        # Tier 2: NDX bridge — fetch $NDX.X from Schwab, use NQ/NDX ratio
        # Tier 3: Cached ratio from last successful call
        # Tier 4: Hardcoded fallback
        try:
            from background_engine.l2_worker import get_l2_state
            futures_mid = get_l2_state().get("mid_prices", {}).get(futures_sym, 0)
        except Exception:
            futures_mid = 0

        ratio = 0
        conversion_tier = 4  # track which tier was used

        # Tier 1: Direct ratio from live NQ mid / live QQQ spot
        if futures_mid > 0 and spot > 0:
            ratio = futures_mid / spot
            conversion_tier = 1

        # Tier 2: NDX bridge — NQ futures track NDX, so use NDX as intermediary
        if ratio == 0 and futures_sym == "NQ":
            try:
                ndx_price = _schwab_quote("$NDX")  # Schwab: $NDX or $NDX.X
                if ndx_price and ndx_price > 0:
                    if futures_mid > 0:
                        # NQ/NDX ratio × NDX/QQQ ratio
                        ratio = (futures_mid / ndx_price) * (ndx_price / spot) if spot > 0 else 0
                        conversion_tier = 2
                    elif spot > 0:
                        # Use NDX/QQQ ratio (NDX ≈ QQQ × 41.15)
                        ratio = ndx_price / spot
                        conversion_tier = 2
            except Exception as e:
                print(f"[api/walls] NDX bridge failed: {e}")

        # Tier 3: Cached ratio from previous successful call
        if ratio == 0:
            _cached_ratio = getattr(api_walls, '_cached_ratio', {})
            if futures_sym in _cached_ratio:
                ratio = _cached_ratio[futures_sym]
                conversion_tier = 3

        # Tier 4: No data available — refuse to guess
        if ratio == 0:
            print(f"[api/walls] ❌ No live ratio data for {futures_sym} — cannot convert levels")
            with _walls_meta_lock:
                _walls_locks.pop(cache_key, None)
            try:
                evt.set()
            except Exception:
                pass
            return jsonify({"error": f"No live NQ/QQQ ratio available. Tiers 1-3 all failed."}), 503

        # Cache successful ratio for tier 3 future fallback
        if conversion_tier <= 2:
            if not hasattr(api_walls, '_cached_ratio'):
                api_walls._cached_ratio = {}
            api_walls._cached_ratio[futures_sym] = ratio

        # ── Freshness indicator ──────────────────────────────────────────
        data_age = round(_t.time() - _walls_start, 1)
        if data_age < 30:
            freshness = "⚡"      # fresh
        elif data_age < 90:
            freshness = "📊"     # ok
        else:
            freshness = "⚠️"     # stale

        result = {
            "put_wall":              round(underlying_put_wall * ratio, 2),
            "call_wall":             round(underlying_call_wall * ratio, 2),
            "max_pain":              round(underlying_max_pain * ratio, 2),
            "gamma_flip":            round(underlying_gamma_flip * ratio, 2) if underlying_gamma_flip else 0,
            "underlying_ticker":     ticker,
            "underlying_spot":       spot,
            "underlying_put_wall":   underlying_put_wall,
            "underlying_call_wall":  underlying_call_wall,
            "underlying_max_pain":   underlying_max_pain,
            "underlying_gamma_flip": round(underlying_gamma_flip, 2) if underlying_gamma_flip else 0,
            "futures_symbol":        futures_sym,
            "futures_mid":           futures_mid,
            "ratio":                 round(ratio, 4),
            "expiry":                exp_dates[0],
            "expirations_used":      len(exp_dates),
            # Elite Quant Levels
            "vanna_wall":            round(underlying_vanna_wall * ratio, 2),
            "underlying_vanna_wall": underlying_vanna_wall,
            "zero_dte_pin":          round(underlying_zero_dte_pin * ratio, 2),
            "underlying_zero_dte_pin": underlying_zero_dte_pin,
            "charm_direction":       charm_direction,
            "charm_magnitude":       charm_magnitude,
            # Conversion
            "conversion_tier":       conversion_tier,
            # Freshness
            "data_age_sec":          data_age,
            "freshness":             freshness,
            # Legacy compat
            "qqq_spot":              spot if ticker == "QQQ" else None,
            "qqq_put_wall":          underlying_put_wall if ticker == "QQQ" else None,
            "qqq_call_wall":         underlying_call_wall if ticker == "QQQ" else None,
            "qqq_max_pain":          underlying_max_pain if ticker == "QQQ" else None,
            "nq_mid":                futures_mid if futures_sym == "NQ" else None,
        }
        tier_names = {1: 'L2+Chain', 2: 'NDX bridge', 3: 'cached'}  # no tier 4 — we refuse, not guess
        print(f"[api/walls] {ticker} PW={underlying_put_wall} CW={underlying_call_wall} "
              f"MP={underlying_max_pain} GF={underlying_gamma_flip:.1f} | "
              f"{futures_sym} r={ratio:.2f} T{conversion_tier}({tier_names.get(conversion_tier,'?')}) | "
              f"{len(exp_dates)} expirations | {freshness} {data_age:.1f}s")
        with _walls_meta_lock:
            _walls_cache[cache_key] = (result, _t.time())
            _walls_locks.pop(cache_key, None)
        try:
            evt.set()
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        with _walls_meta_lock:
            _walls_locks.pop(cache_key, None)
        try:
            evt.set()
        except Exception:
            pass
        import traceback
        print(f"[api/walls] Error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/spot")
def api_spot():
    """Lightweight quote-only endpoint — single Tradier call, no options processing."""
    try:
        from data_provider import _fetch_quote, _cached
        ticker = get_ticker()
        # Bypass cache so we always get the freshest price
        spot = _fetch_quote(ticker)
        return jsonify({"ticker": ticker, "spot": round(float(spot), 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/data")
def api_data():
    ticker = get_ticker()
    data = _cached_fetch_all(ticker)
    if data is None:
        return jsonify({"error": "Data fetch failed"}), 500

    oi_bar = {}
    for strike, exps in data["oi"].items():
        oi_bar[str(strike)] = {
            "calls": sum(e["calls_val"] for e in exps),
            "puts":  sum(e["puts_val"]  for e in exps),
        }

    result = {
        "ticker":    data["ticker"],
        "spot":      data["spot"],
        "timestamp": data["timestamp"].isoformat(),
        "call_wall":  data["gex"]["call_wall"],
        "put_wall":   data["gex"]["put_wall"],
        "major_wall": data["gex"]["major_wall"],
        "max_pain":   data["max_pain"]["max_pain_strike"],
        # OI
        "oi_bar":    oi_bar,
        "oi_hm":     _build_oi_heatmap(data["oi"]),
        # Strike bar charts
        "gex_bar":   {str(s): round(v, 2) for s, v in data["gex"]["net_gex"].items()},
        "dex_bar":   _build_net_bar(data["dex"]),
        "vex_bar":   _build_vex_bar(data["vex"]),
        "rex_bar":   _build_vex_bar(data["rex"]),
        # Heatmaps
        "gex_hm":    _build_gex_heatmap(data["gex"]["per_exp"]),
        "dex_hm":    _build_heatmap(data["dex"]),
        "vannex_hm": _build_heatmap(data["vannex"]),
        "cex_hm":    _build_tex_heatmap(data["cex"]),
    }

    # ── Convert QQQ wall prices → NQ-equivalent for chart overlay ──
    # The chart shows NQ futures (~24500) but walls are QQQ (~495).
    # Use live NQ mid price from L2 state for accurate conversion.
    try:
        from background_engine.l2_worker import get_l2_state
        l2 = get_l2_state()
        nq_mid = l2.get("mid_prices", {}).get("NQ", 0)
        qqq_spot = data["spot"]
        if nq_mid > 0 and qqq_spot > 0:
            ratio = nq_mid / qqq_spot
        else:
            # No live ratio data — refuse to guess
            ratio = 0
            print(f"[api/data] ⚠️ No live NQ/QQQ ratio — wall conversion skipped")
        # Preserve QQQ values for toolbar display
        result["qqq_spot"] = qqq_spot
        result["qqq_put_wall"] = result["put_wall"]
        result["qqq_call_wall"] = result["call_wall"]
        result["qqq_max_pain"] = result["max_pain"]
        result["nq_mid"] = nq_mid
        result["ratio"] = round(ratio, 4) if ratio > 0 else 0
        # Overwrite wall prices with NQ-equivalent ONLY if live ratio available
        if ratio > 0:
            result["put_wall"] = round(result["qqq_put_wall"] * ratio, 2)
            result["call_wall"] = round(result["qqq_call_wall"] * ratio, 2)
            result["max_pain"] = round(result["qqq_max_pain"] * ratio, 2)
    except Exception as e:
        print(f"[api/data] NQ conversion warning: {e}")

    # ── Regime update: handled by schwab_bridge WS (single source of truth) ──
    # Previously this path computed gamma_flip from data["gex"]["net_gex"] which
    # differs from the schwab_bridge WS computation → regime contradiction.
    # Now schwab_bridge calls update_regime() every time zones are emitted,
    # so l2_worker and edge_detector share the same flip level.
    return jsonify(result)


@app.route("/api/vol_skew_multi")
def api_vol_skew_multi():
    """Return IV by strike for multiple expirations for the smile diagram."""
    try:
        from data_provider import calculate_iv_surface
        ticker = get_ticker()
        surf = calculate_iv_surface(ticker=ticker)
        strikes = surf["strikes"]
        result = []
        for row in surf["surface"]:
            pairs = []
            for s, iv in zip(strikes, row["ivs"]):
                if iv is not None and iv > 0:
                    pairs.append({"strike": s, "iv": round(iv * 100, 2)})
            result.append({"label": row["label"], "dte": row["dte"], "data": pairs})
        return jsonify({"strikes": strikes, "expirations": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/charm_overlay")
def api_charm_overlay():
    """Charm + Vanna gradient data + today's intraday OHLC for overlay chart."""
    try:
        from data_provider import _tradier_timesales
        from datetime import datetime, timedelta
        import numpy as np

        ticker = get_ticker()
        data = fetch_all(ticker)
        spot  = data["spot"]

        # ── Build net exposure per strike ─────────────────────────────────
        def net_by_strike(raw):
            result = {}
            for s, exps in raw.items():
                net = sum(e["calls_val"] + e["puts_val"] for e in exps)
                result[round(float(s), 1)] = round(net, 4)
            return result

        charm_map = net_by_strike(data["cex"])
        vanna_map = net_by_strike(data["vannex"])

        strikes = sorted(set(charm_map) | set(vanna_map))

        # ── Today's real-time 5-min OHLC from Tradier ────────────────────
        _now   = datetime.today()
        _start = _now.replace(hour=9, minute=0, second=0, microsecond=0)
        hist = _tradier_timesales(ticker, _start, _now)
        ohlc = []
        for t, row in hist.iterrows():
            ohlc.append({
                "time":  t.strftime("%Y-%m-%dT%H:%M:%S"),
                "open":  round(float(row["Open"]),  2),
                "high":  round(float(row["High"]),  2),
                "low":   round(float(row["Low"]),   2),
                "close": round(float(row["Close"]), 2),
            })

        return jsonify({
            "spot":       spot,
            "strikes":    strikes,
            "charm_vals": [charm_map.get(s, 0) for s in strikes],
            "vanna_vals": [vanna_map.get(s, 0) for s in strikes],
            "ohlc":       ohlc,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/topology")
def api_topology():
    try:
        from data_provider import build_topology
        ticker = get_ticker()
        return jsonify(build_topology(ticker))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/entropy")
def api_entropy():
    try:
        from data_provider import build_entropy
        ticker = get_ticker()
        return jsonify(build_entropy(ticker))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/hiro")
def api_hiro():
    """
    HIRO = cumulative intraday MM delta-hedging flow.
    HIRO(t) = total_net_GEX  ×  (price(t) − open_price)
    Positive → MMs bought (price-supportive), Negative → MMs sold (price-drag).
    """
    try:
        from data_provider import _tradier_timesales
        from datetime import datetime
        ticker  = get_ticker()
        data    = fetch_all(ticker)

        # Total net dollar-gamma per $1 move in spot
        net_gex = data["gex"]["net_gex"]          # {strike: net $ per point}
        total_gex = sum(net_gex.values())          # $ per $1 price move

        # Real-time 5-min intraday bars from Tradier
        _now   = datetime.today()
        _start = _now.replace(hour=9, minute=0, second=0, microsecond=0)
        hist = _tradier_timesales(ticker, _start, _now)
        if hist.empty:
            return jsonify({
                "ticker": ticker,
                "series": [],
                "total_gex_m": round(total_gex / 1e6, 2),
                "current_hiro_m": 0,
                "direction": "MARKET CLOSED",
                "open_price": 0,
            })

        open_price = float(hist.iloc[0]["Open"])
        series = []
        for t, row in hist.iterrows():
            close    = float(row["Close"])
            delta_s  = close - open_price
            hiro_m   = round(total_gex * delta_s / 1e6, 3)   # in $M
            series.append({
                "time":  t.strftime("%Y-%m-%dT%H:%M:%S"),
                "price": round(close, 2),
                "hiro":  hiro_m,
            })

        current_hiro = series[-1]["hiro"] if series else 0
        direction = ("BUY PRESSURE" if current_hiro > 0
                     else "SELL PRESSURE" if current_hiro < 0
                     else "NEUTRAL")

        return jsonify({
            "ticker":         ticker,
            "series":         series,
            "total_gex_m":    round(total_gex / 1e6, 2),
            "current_hiro_m": current_hiro,
            "direction":      direction,
            "open_price":     round(open_price, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/oi365")
def api_oi365():
    try:
        from data_provider import build_oi365
        ticker = get_ticker()
        data = build_oi365(ticker)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/anomalies")
def api_anomalies():
    try:
        from data_provider import _tradier_timesales
        from datetime import datetime, timedelta
        import numpy as np
        ticker = get_ticker()
        _now   = datetime.today()
        _start = _now - timedelta(days=7)   # covers 5 trading days
        df = _tradier_timesales(ticker, _start, _now)
        if df.empty:
            return jsonify({"error": "No data"}), 500
        df["Log_Return"] = np.log(df["Close"] / df["Close"].shift(1))
        w = 20
        df["Mean"]    = df["Log_Return"].rolling(window=w).mean()
        df["Std"]     = df["Log_Return"].rolling(window=w).std()
        df["Z_Score"] = (df["Log_Return"] - df["Mean"]) / df["Std"]
        df = df.dropna()
        threshold = 2.09
        return jsonify({
            "ticker":     ticker,
            "threshold":  threshold,
            "times":      [str(t) for t in df.index],
            "log_returns":[float(x) for x in df["Log_Return"]],
            "z_scores":   [float(x) for x in df["Z_Score"]],
            "anomalies_up":   [{"time": str(t), "val": float(v)}
                               for t, v, z in zip(df.index, df["Log_Return"], df["Z_Score"]) if z >  threshold],
            "anomalies_down": [{"time": str(t), "val": float(v)}
                               for t, v, z in zip(df.index, df["Log_Return"], df["Z_Score"]) if z < -threshold],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/regime_score")
def api_regime_score():
    """ATR-normalised Regime Score: (Close-EMA50)/ATR50*20. >25=bullish, <-25=bearish."""
    try:
        from data_provider import _tradier_timesales
        from datetime import datetime, timedelta
        import numpy as np
        import pandas as _pd

        ticker = get_ticker()
        _now   = datetime.today()
        df = _tradier_timesales(ticker, _now - timedelta(days=14), _now)
        if df.empty:
            return jsonify({"error": "No intraday data"}), 500

        window = 50
        close  = df["Close"]
        ema    = close.ewm(span=window, adjust=False).mean()

        prev_close = close.shift(1)
        tr = _pd.Series(
            np.maximum.reduce([
                (df["High"] - df["Low"]).values,
                (df["High"] - prev_close).abs().values,
                (df["Low"]  - prev_close).abs().values,
            ]),
            index=df.index
        )
        atr   = tr.rolling(window).mean()
        score = ((close - ema) / atr.replace(0, float("nan"))) * 20
        if score.dropna().empty:
            return jsonify({"error": "Not enough data"}), 500

        cutoff = _now - timedelta(days=4)   # 4 days to cover weekends
        cutoff_ts = _pd.Timestamp(cutoff).tz_localize(df.index.tz) if df.index.tz else _pd.Timestamp(cutoff)
        idx = df.index[df.index >= cutoff_ts]

        def _ser(s):
            s2 = s.reindex(idx)
            return [round(float(v), 4) if v == v else None for v in s2]

        sc_vals = score.reindex(idx)
        if sc_vals.dropna().empty:
            return jsonify({"error": "Not enough recent data for regime score"}), 500

        sc_prev = sc_vals.shift(1)
        buys, sells = [], []
        for t, sv, pv in zip(idx, sc_vals.values, sc_prev.values):
            if sv != sv or pv != pv:
                continue
            if sv > 25 and pv <= 25:
                buys.append(str(t))
            elif sv < -25 and pv >= -25:
                sells.append(str(t))

        valid_scores = sc_vals.dropna()
        cur = float(valid_scores.iloc[-1]) if len(valid_scores) > 0 else 0.0
        regime = ("DIRECTIONAL BULLISH" if cur > 25
                  else "DIRECTIONAL BEARISH" if cur < -25
                  else "BALANCE (CHOP)")

        return jsonify({
            "ticker":        ticker,
            "times":         [str(t) for t in idx],
            "prices":        _ser(close),
            "emas":          _ser(ema),
            "scores":        _ser(score),
            "buy_times":     buys,
            "sell_times":    sells,
            "current_score": round(cur, 2),
            "regime":        regime,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/volatility")
def api_volatility():
    try:
        from data_provider import calculate_iv_surface
        ticker = get_ticker()
        iv_data = calculate_iv_surface(spot=None, ticker=ticker)
        return jsonify({
            "spot": iv_data.get("spot", 0),
            "strikes": iv_data["strikes"],
            "expirations": iv_data["expirations"],
            "surface": [{"label": s["label"], "dte": s["dte"], "ivs": s["ivs"]}
                        for s in iv_data["surface"]],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/flow")
def api_flow():
    """Real-time flow classification scores from FlowClassifier (L2 book analysis)."""
    try:
        from background_engine.schwab_bridge import _flow_classifier
        if _flow_classifier is None:
            return jsonify({"error": "Flow classifier not initialized"}), 503
        reports = _flow_classifier.get_all_reports()
        return jsonify(reports)
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/edge")
def api_edge():
    """EdgeDetector rolling distribution stats."""
    try:
        from background_engine.schwab_bridge import _edge_detector
        if _edge_detector is None:
            return jsonify({"error": "Edge detector not initialized"}), 503
        return jsonify(_edge_detector.get_stats_report())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mm")
def api_mm():
    """MMTracker venue report."""
    try:
        import background_engine.schwab_bridge as sb
        if getattr(sb, '_mm_tracker', None) is None:
            return jsonify({"error": "MM tracker not initialized"}), 503
        return jsonify({
            "report": sb._mm_tracker.get_venue_report("QQQ"),
            "stats": sb._mm_tracker.get_stats_report()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vol_stats")
def api_vol_stats():
    try:
        from data_provider import _tradier_history, _tradier_get
        from datetime import datetime, timedelta
        import numpy as np
        from data_provider import calculate_iv_surface

        ticker = get_ticker()

        # ── Real-time 1-year daily history from Tradier ──────────────────────
        _now   = datetime.today()
        _start = _now - timedelta(days=365)
        hist   = _tradier_history(ticker, _start, _now)
        closes = hist["Close"].dropna()

        log_ret = np.log(closes / closes.shift(1)).dropna()

        def hv(n):
            return float((log_ret.rolling(n).std().iloc[-1]) * np.sqrt(252) * 100)

        hv10 = round(hv(10), 2)
        hv20 = round(hv(20), 2)
        hv30 = round(hv(30), 2)

        # 90-day HV series for the chart — aligned to the SAME date axis.
        # Each rolling series has a different start date (HV30 needs 30 days warmup,
        # HV10 only 10), so naively using [-90:] on each gives DIFFERENT date ranges.
        # Fix: join into one DataFrame, align on the common (HV30) index, then slice.
        import pandas as pd
        hv30_series = (log_ret.rolling(30).std() * np.sqrt(252) * 100)
        hv20_series = (log_ret.rolling(20).std() * np.sqrt(252) * 100)
        hv10_series = (log_ret.rolling(10).std() * np.sqrt(252) * 100)
        hv_df = pd.DataFrame({
            "hv10": hv10_series,
            "hv20": hv20_series,
            "hv30": hv30_series,
        }).dropna()                     # drop any row where ANY series is NaN
        hv_slice     = hv_df.tail(90)  # last 90 trading days — same dates for all three
        hv_dates     = [str(d.date()) for d in hv_slice.index]
        hv10_dates   = hv_dates
        hv10_vals    = [round(float(v), 2) for v in hv_slice["hv10"]]
        hv20_dates   = hv_dates
        hv20_vals    = [round(float(v), 2) for v in hv_slice["hv20"]]
        hv30_dates   = hv_dates
        hv30_vals    = [round(float(v), 2) for v in hv_slice["hv30"]]

        # ── HV-Rank (proxy for IV Rank) ───────────────────────────────────
        # We rank ATM IV against the 1-year HV30 min/max range.
        # True IV Rank would need 1-year of historical ATM IV snapshots
        # (not available on Tradier free tier), so this is the best proxy.
        hv30_all  = (log_ret.rolling(30).std() * np.sqrt(252) * 100).dropna()
        hv_1y_max = float(hv30_all.max())
        hv_1y_min = float(hv30_all.min())

        # Current ATM IV from Tradier surface
        try:
            surf = calculate_iv_surface(spot=None, ticker=ticker)
            spot = surf.get("spot", 0)
            # Pick nearest expiry, find ATM IV (middle of strikes)
            if surf["surface"]:
                atm_row = surf["surface"][0]["ivs"]
                mid = len(atm_row) // 2
                # Average a few strikes around ATM
                atm_slice = [v for v in atm_row[max(0,mid-3):mid+4] if v and v > 0]
                atm_iv_pct = round(float(np.mean(atm_slice)) * 100, 2) if atm_slice else hv30
            else:
                atm_iv_pct = hv30
        except Exception:
            atm_iv_pct = hv30
            spot = 0

        # Rank ATM IV against the 1-year HV30 range
        # (positive = IV elevated vs recent realized vol history)
        ivr = round((atm_iv_pct - hv_1y_min) / max(hv_1y_max - hv_1y_min, 0.01) * 100, 1)
        ivr = max(0, min(100, ivr))
        vol_premium = round(atm_iv_pct - hv30, 2)

        # Regime label
        if   ivr >= 70 and vol_premium > 2: regime = "SELL VOL"
        elif ivr <= 30 and vol_premium < -2: regime = "BUY VOL"
        else:                                regime = "NEUTRAL"

        # ── VIX term structure ───────────────────────────────────────────
        def vix_from_tradier(sym):
            try:
                d = _tradier_get("/markets/quotes", {"symbols": sym, "greeks": "false"})
                q = d.get("quotes", {}).get("quote", {})
                # Handle list response (multiple quotes)
                if isinstance(q, list):
                    q = next((x for x in q if x.get("symbol") == sym), {})
                val = q.get("last") or q.get("close")
                return round(float(val), 2) if val else None
            except Exception as ex:
                print(f"[vol_stats] VIX fetch failed for {sym}: {ex}")
                return None

        vix9d  = vix_from_tradier("VIX9D")
        vix_   = vix_from_tradier("VIX")
        vix3m  = vix_from_tradier("VIX3M")

        # Term structure shape
        if vix9d is not None and vix_ is not None and vix3m is not None:
            if vix9d < vix_ < vix3m:
                ts_shape = "CONTANGO"       # normal / calm
            elif vix9d > vix_ > vix3m:
                ts_shape = "BACKWARDATION"  # stressed
            else:
                ts_shape = "MIXED"
        else:
            missing = [s for s, v in [("VIX9D", vix9d), ("VIX", vix_), ("VIX3M", vix3m)] if v is None]
            ts_shape = f"N/A (no {', '.join(missing)})"

        # ── 5-Day intraday (5-min) kurtosis ──────────────────────────────
        # Statistically stable version: winsorize returns at ±3σ first
        # (outlier raw bars are the #1 cause of spurious kurtosis spikes),
        # then use a 156-bar rolling window (≈ 2 trading days) for stability,
        # and clip output to [-5, 15] — values above ~10 just mean "very fat
        # tails"; distinguishing 12 from 50 adds no real information.
        try:
            from data_provider import _tradier_timesales
            from datetime import timedelta as _td
            _intra_start = _now - _td(days=10)
            intra_df = _tradier_timesales(ticker, _intra_start, _now)
            intra_log = np.log(intra_df["Close"] / intra_df["Close"].shift(1)).dropna()
            # Winsorize: clip returns to ±3σ to remove single-bar outliers
            _sigma = intra_log.std()
            intra_log_w = intra_log.clip(-3 * _sigma, 3 * _sigma)
            # Rolling 156-bar kurtosis (≈ 2 trading days) — more stable than 78
            roll_window = 156
            kurt_series = intra_log_w.rolling(roll_window).kurt().dropna()
            # Clip output to meaningful range: excess kurtosis rarely useful beyond ±15
            kurt_series = kurt_series.clip(-5, 15)
            kurt_dates = [str(t) for t in kurt_series.index[-400:]]
            kurt_vals  = [round(float(v), 3) for v in kurt_series.values[-400:]]
            kurt_now   = round(float(kurt_series.iloc[-1]), 2) if len(kurt_series) else 0
        except Exception:
            # Fallback to daily rolling kurtosis if intraday unavailable
            kurt30_series = log_ret.rolling(30).kurt().dropna().clip(-5, 15)
            kurt_dates = [str(d.date()) for d in kurt30_series.index[-90:]]
            kurt_vals  = [round(float(v), 3) for v in kurt30_series.values[-90:]]
            kurt_now   = round(float(kurt30_series.iloc[-1]), 2) if len(kurt30_series) else 0

        return jsonify({
            "ticker":      ticker,
            "spot":        spot,
            "hv10":        hv10,
            "hv20":        hv20,
            "hv30":        hv30,
            "atm_iv":      atm_iv_pct,
            "ivr":         ivr,
            "vol_premium": vol_premium,
            "regime":      regime,
            "vix9d":       vix9d,
            "vix":         vix_,
            "vix3m":       vix3m,
            "ts_shape":    ts_shape,
            "hv30_chart":  {"dates": hv30_dates, "values": hv30_vals},
            "hv20_chart":  {"dates": hv20_dates, "values": hv20_vals},
            "hv10_chart":  {"dates": hv10_dates, "values": hv10_vals},
            "kurt_chart":  {"dates": kurt_dates, "values": kurt_vals, "current": kurt_now},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/macro")
def api_macro():
    try:
        from macro_provider import get_macro_data
        cfg = load_config()
        return jsonify(get_macro_data(get_ticker(), cfg.get("alpha_vantage_key", "")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Inference Engine API ──────────────────────────────────────────────────────
import time as _time
_inference_cache = {"data": None, "ts": 0}
_INFERENCE_TTL = 60  # seconds

# ── Level 2 Live Data API ─────────────────────────────────────────────────────
@app.route("/api/l2")
def api_l2():
    """
    Returns live Level 2 DOM signals from TopStepX.
    Served by the background_engine/l2_worker.py daemon.
    """
    import json as _json
    try:
        from background_engine.l2_worker import get_l2_state
        state = get_l2_state()
        # If L2 is disconnected, attach diagnostic info for debugging
        if not state.get("connected"):
            import threading as _diag_threading
            diag = {}
            # Check startup error
            try:
                import builtins
                if hasattr(builtins, '_l2_startup_error_holder'):
                    diag["startup_error"] = builtins._l2_startup_error_holder[0]
                else:
                    diag["startup_error"] = "(builtins holder not found - gunicorn fork issue)"
            except Exception as _de:
                diag["startup_error"] = f"(check failed: {_de})"
            # Check thread status
            l2_threads = [t for t in _diag_threading.enumerate() if 'l2' in t.name.lower() or 'start_l2' in str(t)]
            diag["all_threads"] = [t.name for t in _diag_threading.enumerate()]
            diag["l2_threads"] = [t.name for t in l2_threads] if l2_threads else "(no L2 threads found)"
            diag["has_username"] = bool(os.getenv("TOPSTEPX_USERNAME"))
            diag["has_api_key"] = bool(os.getenv("TOPSTEPX_API_KEY"))
            diag["has_password"] = bool(os.getenv("TOPSTEPX_PASSWORD"))
            diag["rest_base"] = os.getenv("TOPSTEPX_REST_BASE", "(not set)")
            diag["worker_error"] = _worker_error
            diag["workers_started"] = _workers_started
            state["_diag"] = diag
        # Use json.dumps with default=str to handle any numpy types in signals
        body = _json.dumps(state, default=str)
        return make_response(body, 200, {"Content-Type": "application/json"})
    except Exception as e:
        # Include diagnostic from worker thread if available
        diag = ""
        try:
            import builtins
            if hasattr(builtins, '_l2_startup_error_holder') and builtins._l2_startup_error_holder[0]:
                diag = builtins._l2_startup_error_holder[0]
        except Exception:
            pass
        return jsonify({"connected": False, "error": str(e), "startup_error": diag}), 200




@app.route("/api/l2/diag")
def api_l2_diag():
    """Diagnostic: test TopStepX auth + connectivity from this server."""
    import os, requests as _req, traceback as _tb
    results = {"tests": {}}
    username = os.getenv("TOPSTEPX_USERNAME", "")
    api_key = os.getenv("TOPSTEPX_API_KEY", "")
    rest_base = os.getenv("TOPSTEPX_REST_BASE", "https://api.topstepx.com")
    results["env"] = {
        "username_set": bool(username),
        "api_key_set": bool(api_key),
        "api_key_len": len(api_key),
        "rest_base": rest_base,
    }
    # Test 1: Auth
    try:
        resp = _req.post(f"{rest_base}/api/Auth/loginKey",
                         json={"userName": username, "apiKey": api_key}, timeout=10)
        data = resp.json()
        results["tests"]["auth"] = {
            "status": resp.status_code,
            "success": data.get("success"),
            "error": data.get("errorMessage", ""),
            "token_len": len(data.get("token", "")) if data.get("token") else 0,
        }
        token = data.get("token", "")
    except Exception as e:
        results["tests"]["auth"] = {"error": str(e), "traceback": _tb.format_exc()}
        token = ""
    # Test 2: Contract search (if auth worked)
    if token:
        try:
            resp = _req.post(f"{rest_base}/api/Contract/search",
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json"},
                             json={"searchText": "NQ", "live": False}, timeout=10)
            cdata = resp.json()
            contracts = cdata.get("contracts", [])
            results["tests"]["contract_search"] = {
                "status": resp.status_code,
                "count": len(contracts),
                "first": contracts[0] if contracts else None,
            }
        except Exception as e:
            results["tests"]["contract_search"] = {"error": str(e)}
    # Test 3: Worker thread error
    try:
        import builtins
        if hasattr(builtins, '_l2_startup_error_holder'):
            results["tests"]["worker_error"] = builtins._l2_startup_error_holder[0]
    except Exception:
        pass
    # Test 4: WebSocket connectivity
    try:
        import websocket as _ws_test
        results["tests"]["websocket_import"] = {"ok": True, "version": getattr(_ws_test, '__version__', 'unknown')}
        # Quick connect test (3s timeout)
        if token:
            hub = os.getenv("TOPSTEPX_MARKET_HUB", "https://rtc.topstepx.com/hubs/market")
            ws_url = hub.replace("https://", "wss://").replace("http://", "ws://")
            ws_url = f"{ws_url}?access_token={token}"
            try:
                ws = _ws_test.create_connection(ws_url, timeout=5)
                # Send SignalR handshake
                ws.send('{"protocol":"json","version":1}\x1e')
                resp = ws.recv()
                ws.close()
                results["tests"]["websocket_connect"] = {"ok": True, "handshake_response": resp[:200]}
            except Exception as e:
                results["tests"]["websocket_connect"] = {"ok": False, "error": str(e)}
    except ImportError as e:
        results["tests"]["websocket_import"] = {"ok": False, "error": str(e)}
    # Test 5: Current L2 state
    try:
        from background_engine.l2_worker import get_l2_state, L2_STATE
        results["tests"]["l2_state"] = {
            "connected": L2_STATE.get("connected"),
            "last_update": L2_STATE.get("last_update"),
            "dom_symbols": list(L2_STATE.get("dom", {}).keys()),
            "quote_symbols": list(L2_STATE.get("quotes", {}).keys()),
        }
    except Exception as e:
        results["tests"]["l2_state"] = {"error": str(e)}
    return jsonify(results)

@app.route("/api/l2/dom-history")
def api_l2_dom_history():
    """DOM snapshot history for 2D passive heatmap.
    Query params:
      ?symbol=NQ   (default NQ)
      ?since=0     (Unix timestamp — snapshots at or after this time)
      ?res=auto    (resolution: auto, t0, t1, t2, t3)
    Returns: {symbol, tick, snapshots: [[ts, {bid_prices}, {ask_prices}], ...]}
    """
    import json as _json
    try:
        from background_engine.l2_worker import get_dom_history
        symbol = request.args.get("symbol", "NQ").upper()
        since = float(request.args.get("since", 0))
        res = request.args.get("res", "auto")
        history = get_dom_history(symbol, since_ts=since, resolution=res)
        # Determine tick size from symbol
        tick_map = {"NQ": 0.25, "ES": 0.25, "GC": 0.10, "YM": 1.0, "RTY": 0.10}
        tick = tick_map.get(symbol, 0.25)
        body = _json.dumps({
            "symbol": symbol,
            "tick": tick,
            "count": len(history),
            "snapshots": history,
        }, default=str)
        return make_response(body, 200, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/inference")
def api_inference():
    """Return signals from all 8 alpha frameworks (synthetic demo data)."""
    # Serve from cache if fresh
    if _inference_cache["data"] and (_time.time() - _inference_cache["ts"]) < _INFERENCE_TTL:
        return jsonify(_inference_cache["data"])
    try:
        import numpy as np
        signals = []

        # 1. Transfer Entropy
        try:
            from frameworks.transfer_entropy import TransferEntropy
            te = TransferEntropy(window_size=60, lag=1, n_bins=5)
            vix = 20 + np.cumsum(np.random.normal(0, 0.5, 100))
            nq = 15000 + np.cumsum(np.random.normal(0, 50, 100))
            for i in range(100):
                te.update(float(vix[i]), float(nq[i]))
            signals.append(te.get_signal())
        except Exception as e:
            signals.append({"name": "transfer_entropy", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # 2. Shannon Entropy
        try:
            from frameworks.shannon_entropy import ShannonEntropy
            se = ShannonEntropy(window_size=60)
            for i in range(100):
                se.update(float(np.random.uniform(-1, 1)))
            sig = se.get_signal()
            sig["name"] = sig.pop("framework", "shannon_entropy")
            if "alert_level" not in sig:
                sig["alert_level"] = sig.get("regime", "unknown")
            signals.append(sig)
        except Exception as e:
            signals.append({"name": "shannon_entropy", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # 3. Ising Magnetization
        try:
            from frameworks.ising_magnetization import IsingMagnetization
            ising = IsingMagnetization(window_size=60)
            for i in range(100):
                for sym in ["NQ", "ES", "YM", "RTY"]:
                    ising.update_trade(sym, int(np.random.choice([-1, 1])))
            sig = ising.get_signal()
            sig["name"] = sig.pop("framework", "ising_magnetization")
            if "alert_level" not in sig:
                sig["alert_level"] = sig.get("regime", "unknown")
            signals.append(sig)
        except Exception as e:
            signals.append({"name": "ising_magnetization", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # 4. Mutual Information
        try:
            from frameworks.mutual_information import MutualInformation
            mi = MutualInformation(window_size=100, n_bins=5)
            for i in range(100):
                mi.update(float(np.random.randn() * 1e9), float(np.random.randn() * 0.01))
            sig = mi.get_signal()
            sig["name"] = sig.pop("framework", "mutual_information")
            if "alert_level" not in sig:
                sig["alert_level"] = sig.get("regime", "unknown")
            signals.append(sig)
        except Exception as e:
            signals.append({"name": "mutual_information", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # 5. Reynolds Number
        try:
            from frameworks.reynolds_number import ReynoldsNumber
            rn = ReynoldsNumber(window_size=60)
            price = 20000.0
            for i in range(100):
                price += np.random.randn() * 10
                rn.update(price=float(price), spread=float(0.25 + np.random.rand() * 0.5), volume=float(10 + np.random.rand() * 50))
            sig = rn.get_signal()
            sig["name"] = sig.pop("framework", "reynolds_number")
            if "alert_level" not in sig:
                sig["alert_level"] = sig.get("regime", "unknown")
            signals.append(sig)
        except Exception as e:
            signals.append({"name": "reynolds_number", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # 6. Percolation Threshold
        try:
            from frameworks.percolation_threshold import PercolationThreshold
            pt = PercolationThreshold(window_size=60)
            assets = ["NQ", "ES", "YM", "RTY", "VIX", "ZN", "GC"]
            base = {a: 1000.0 for a in assets}
            for i in range(100):
                for a in assets:
                    base[a] += np.random.randn() * 10
                pt.update({a: float(base[a]) for a in assets})
            sig = pt.get_signal()
            sig["name"] = sig.pop("framework", "percolation_threshold")
            if "alert_level" not in sig:
                sig["alert_level"] = sig.get("regime", "unknown")
            signals.append(sig)
        except Exception as e:
            signals.append({"name": "percolation_threshold", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # 7. LPPL Sornette (instant mock for dashboard demo)
        try:
            from datetime import datetime, timedelta
            tc_date = datetime.now() + timedelta(days=21)
            sig = {
                "name": "lppl_sornette",
                "value": 0.995,  # R^2
                "alert_level": "watch",
                "interpretation": f"Bubble signature detected → tc ~{tc_date.strftime('%b %d')} ({21}d). R²=0.995",
                "critical_date": tc_date.strftime('%Y-%m-%d'),
                "days_to_tc": 21,
                "is_bubble": True,
                "r_squared": 0.9953,
                "confidence": 0.72
            }
            signals.append(sig)
        except Exception as e:
            signals.append({"name": "lppl_sornette", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # 8. Power-Law Tail (instant mock for dashboard demo)
        try:
            sig = {
                "name": "powerlaw_tail",
                "value": 4.66,
                "alert_level": "stable",
                "interpretation": "Thin tails: α=4.66 — unusually calm. Consider selling vol. Trend: stable",
                "alpha": 4.66,
                "alpha_left": 4.12,
                "alpha_right": 4.97,
                "regime": "THIN_CALM",
                "tail_trend": "stable"
            }
            signals.append(sig)
        except Exception as e:
            signals.append({"name": "powerlaw_tail", "value": 0, "alert_level": "error", "interpretation": str(e)})

        # Sanitize numpy types for JSON serialization
        def _sanitize(obj):
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_sanitize(v) for v in obj]
            if isinstance(obj, (np.bool_,)):
                return bool(obj)
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return obj

        signals = [_sanitize(s) for s in signals]
        result = {"signals": signals, "count": len(signals)}
        _inference_cache["data"] = result
        _inference_cache["ts"] = _time.time()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Level 2 Candle Chart API ─────────────────────────────────────────────────

# Response cache: avoids re-serializing identical data when multiple tabs poll.
# Key = (symbol, tf, since), value = (timestamp, json_response).
import time as _time
_candle_cache: dict[tuple, tuple] = {}
_CANDLE_CACHE_TTL = 0.5  # 500ms

@app.route("/api/l2/candles")
def api_l2_candles():
    """OHLCV candles from L2 tick data.
    Query params:
      ?symbol=NQ   (default NQ)
      ?tf=1m       (default 1m — one of: 5s,15s,30s,1m,5m,15m,30m,1h,4h)
      ?since=0     (optional Unix timestamp — only return candles at or after this time)
                   Omit or set to 0 for full history (initial load / timeframe switch).
    Returns JSON: {symbol, tf, candles: [{time, open, high, low, close, volume}, ...]}
    """
    try:
        from background_engine.l2_worker import get_candles, CANDLE_TIMEFRAMES
        symbol = request.args.get("symbol", "NQ").upper()
        tf     = request.args.get("tf", "1m")
        since  = int(request.args.get("since", 0))  # Unix timestamp filter
        if tf not in CANDLE_TIMEFRAMES:
            return jsonify({"error": f"Invalid tf '{tf}'. Use: {list(CANDLE_TIMEFRAMES.keys())}"}), 400

        # ── Check response cache ──
        cache_key = (symbol, tf, since)
        cached = _candle_cache.get(cache_key)
        if cached and (_time.time() - cached[0]) < _CANDLE_CACHE_TTL:
            return jsonify(cached[1])

        raw = get_candles(symbol, tf)

        # Convert to TradingView Lightweight Charts format (time as Unix seconds)
        candles = []
        for c in raw:
            t = int(c.get("t", 0))
            # Delta filter: skip candles before 'since' timestamp
            if since and t < since:
                continue
            candle_out = {
                "time":   t,
                "open":   c["o"],
                "high":   c["h"],
                "low":    c["l"],
                "close":  c["c"],
                "volume": c.get("v", 0),
            }
            # Include bubble profile only for live candles (backfill candles have no 'bp')
            bp = c.get("bp")
            if bp:
                candle_out["bp"] = bp
            # Include all orderflow detection data
            for key in ("sweeps", "delta_div", "ignition",
                        "spoofs", "wall_gone",
                        "absorption", "depth_deltas"):
                val = c.get(key)
                if val:
                    candle_out[key] = val
            candles.append(candle_out)

        # Deduplicate by time (keep last occurrence)
        seen = {}
        for c in candles:
            seen[c["time"]] = c
        candles = sorted(seen.values(), key=lambda x: x["time"])

        payload = {"symbol": symbol, "tf": tf, "candles": candles}
        # Cache the serialized data (not the mutable Response object)
        _candle_cache[cache_key] = (_time.time(), payload)
        # Evict stale entries (keep cache small)
        stale = [k for k, v in _candle_cache.items() if (_time.time() - v[0]) > 5.0]
        for k in stale:
            _candle_cache.pop(k, None)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vprofile")
def api_vprofile():
    """Volume profile from TopStepX 1m candle bp data.
    Query params:
      ?symbol=NQ        (default NQ)
      ?mode=session     (session|prior_day|rolling_4h|custom)
      ?from_ts=0        (Unix timestamp — custom mode start)
      ?to_ts=0          (Unix timestamp — custom mode end)
      ?row_count=0      (0=auto tick-level, or 50/100/200/500 bucket rows)
      ?va_pct=0.70      (value area %, 0.50–0.95)
    Returns JSON: {symbol, mode, poc, vah, val, total_vol, levels_count,
                   from_ts, to_ts, levels: [{price, buy, sell, total, delta}]}
    """
    try:
        from background_engine.l2_worker import get_candles
        import time as _t
        from datetime import datetime
        import pytz

        symbol = request.args.get("symbol", "NQ").upper()
        mode = request.args.get("mode", "session")
        row_count = int(request.args.get("row_count", 0))
        va_pct = float(request.args.get("va_pct", 0.70))
        va_pct = max(0.50, min(0.95, va_pct))  # clamp
        session_type = request.args.get("session_type", "all")  # all|rth|eth
        vol_filter = request.args.get("vol_filter", "total")    # total|buy|sell
        min_level_vol = int(request.args.get("min_level_vol", 0))  # filter out levels < N total vol
        step_param = request.args.get("step", "")  # 15m|30m|1h|2h|4h — step profile mode
        now = _t.time()
        et = pytz.timezone("US/Eastern")
        now_et = datetime.fromtimestamp(now, tz=et)

        # Get all 1m candles
        raw = get_candles(symbol, "1m")
        if not raw:
            return jsonify({"error": "No candle data available"}), 404

        # Determine time range based on mode
        if mode == "prior_day":
            # Prior session: find yesterday's 6PM ET → today's 6PM ET
            from datetime import timedelta
            today_6pm = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
            if now_et.hour < 18:
                session_end = today_6pm
                session_start = session_end - timedelta(days=1)
            else:
                session_start = today_6pm
                session_end = today_6pm + timedelta(days=1)
            # Go back one more day for "prior"
            session_end = session_start
            session_start = session_start - timedelta(days=1)
            from_ts = int(session_start.timestamp())
            to_ts = int(session_end.timestamp())

            # Fallback: if prior day has < 10 candles in memory, use current session
            prior_count = sum(1 for c in raw if from_ts <= int(c.get("t", 0)) <= to_ts)
            if prior_count < 10:
                if now_et.hour >= 18:
                    fallback_start = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
                else:
                    fallback_start = (now_et - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
                from_ts = int(fallback_start.timestamp())
                to_ts = int(now)

        elif mode == "rolling_1h":
            from_ts = int(now - 1 * 3600)
            to_ts = int(now)

        elif mode == "rolling_2h":
            from_ts = int(now - 2 * 3600)
            to_ts = int(now)

        elif mode == "rolling_4h":
            from_ts = int(now - 4 * 3600)
            to_ts = int(now)

        elif mode == "2day":
            from datetime import timedelta
            if now_et.hour >= 18:
                session_end = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
            else:
                session_end = (now_et - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
            session_start = session_end - timedelta(days=2)
            from_ts = int(session_start.timestamp())
            to_ts = int(now)

        elif mode == "weekly":
            from datetime import timedelta
            from_ts = int(now - 7 * 86400)
            to_ts = int(now)

        elif mode == "custom":
            from_ts = int(request.args.get("from_ts", 0))
            to_ts = int(request.args.get("to_ts", 0))
            if from_ts <= 0 or to_ts <= 0 or to_ts <= from_ts:
                return jsonify({"error": "custom mode requires valid from_ts and to_ts"}), 400

        else:  # session (current)
            # Current session: today's 6PM ET (or yesterday's if before 6PM)
            if now_et.hour >= 18:
                session_start = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
            else:
                from datetime import timedelta
                session_start = (now_et - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
            from_ts = int(session_start.timestamp())
            to_ts = int(now)

        # Aggregate volume-at-price across time range
        # Candles with bp (live tick data) use exact volume-at-price.
        # Candles without bp (backfill) distribute volume uniformly across H-L.
        TICK_SIZES = {"NQ": 0.25, "GC": 0.10, "ES": 0.25}
        tick = TICK_SIZES.get(symbol, 0.25)
        profile = {}  # {price_str: {buy, sell}}
        # Absorption tracking: per-level volume vs candle range
        abs_tracker = {}  # {price_str: {vol, candle_ranges[], candle_count}}
        candles_used = 0
        for c in raw:
            t = int(c.get("t", 0))
            if t < from_ts or t > to_ts:
                continue
            # RTH/ETH session filter
            if session_type != 'all':
                c_et = datetime.fromtimestamp(t, tz=et)
                c_hour = c_et.hour * 60 + c_et.minute  # minutes since midnight
                rth_start, rth_end = 9 * 60 + 30, 16 * 60  # 9:30-16:00
                if session_type == 'rth' and not (rth_start <= c_hour < rth_end):
                    continue
                if session_type == 'eth' and (rth_start <= c_hour < rth_end):
                    continue
            # Use bp_large if min_level_vol requests large-trade-only profile
            bp_key = "bp_large" if min_level_vol >= 10 and c.get("bp_large") else "bp"
            bp = c.get(bp_key)
            if bp:
                # Exact volume-at-price from live ticks
                candles_used += 1
                for price_str, volumes in bp.items():
                    if not isinstance(volumes, (list, tuple)) or len(volumes) < 2:
                        continue
                    if price_str not in profile:
                        profile[price_str] = {"buy": 0, "sell": 0}
                    profile[price_str]["buy"] += volumes[0]
                    profile[price_str]["sell"] += volumes[1]
                    # Track absorption: volume at this price vs candle's price range
                    c_range = max(c.get("h", 0) - c.get("l", 0), tick)
                    pv = volumes[0] + volumes[1]
                    if pv > 0:
                        if price_str not in abs_tracker:
                            abs_tracker[price_str] = {"vol_per_candle": [], "ranges": []}
                        abs_tracker[price_str]["vol_per_candle"].append(pv)
                        abs_tracker[price_str]["ranges"].append(c_range)
            else:
                # Backfill candle — distribute volume across H-L range
                h = c.get("h", 0)
                l = c.get("l", 0)
                v = c.get("v", 0)
                o = c.get("o", 0)
                cl = c.get("c", 0)
                if v <= 0 or h <= 0 or l <= 0 or h <= l:
                    continue
                candles_used += 1
                # Generate price levels from low to high at tick resolution
                n_ticks = max(int(round((h - l) / tick)), 1)
                vol_per_tick = v / (n_ticks + 1)
                # Determine buy/sell split from candle direction
                is_bull = cl >= o
                buy_ratio = 0.6 if is_bull else 0.4
                sell_ratio = 1.0 - buy_ratio
                price = l
                while price <= h + tick * 0.01:
                    ps = f"{price:.2f}"
                    if ps not in profile:
                        profile[ps] = {"buy": 0, "sell": 0}
                    profile[ps]["buy"] += vol_per_tick * buy_ratio
                    profile[ps]["sell"] += vol_per_tick * sell_ratio
                    price = round(price + tick, 10)

        if not profile:
            return jsonify({
                "symbol": symbol, "mode": mode, "from_ts": from_ts, "to_ts": to_ts,
                "poc": 0, "vah": 0, "val": 0, "total_vol": 0,
                "levels_count": 0, "candles_used": 0, "levels": []
            })

        # Build sorted levels (respecting vol_filter for POC/VA computation)
        levels = []
        for price_str, vols in profile.items():
            buy_v = vols["buy"]
            sell_v = vols["sell"]
            # vol_filter controls which volume dimension drives POC/VA
            if vol_filter == 'buy':
                total = buy_v
            elif vol_filter == 'sell':
                total = sell_v
            else:
                total = buy_v + sell_v
            lv = {
                "price": float(price_str),
                "buy": buy_v,
                "sell": sell_v,
                "total": total,
                "delta": buy_v - sell_v,
            }
            # Absorption ratio: volume / avg price displacement at this level
            at = abs_tracker.get(price_str)
            if at and len(at["vol_per_candle"]) >= 2:
                total_vol_at = sum(at["vol_per_candle"])
                avg_range = sum(at["ranges"]) / len(at["ranges"])
                if avg_range > 0:
                    n_candles = len(at["vol_per_candle"])
                    time_factor = max(1, n_candles) ** 0.5
                    lv["abs_ratio"] = round(total_vol_at / avg_range / time_factor, 1)
                # Exhaustion: only compute for levels near current price
                # Old levels naturally have declining volume because price left, not exhaustion
                # Use last candle's close as "current price"
                _last_close = raw[-1].get("c", 0) if raw else 0
                _price_dist = abs(float(price_str) - _last_close) if _last_close > 0 else 999
                if _price_dist <= 20:  # within 20 points of current price
                    vols = at["vol_per_candle"]
                    if len(vols) >= 4:
                        # Only use RECENT candles (last 60% of data) to avoid stale comparison
                        recent_start = max(0, len(vols) - int(len(vols) * 0.6))
                        recent = vols[recent_start:]
                        if len(recent) >= 4:
                            half = len(recent) // 2
                            first_half_avg = sum(recent[:half]) / half
                            second_half_avg = sum(recent[half:]) / (len(recent) - half)
                            if first_half_avg > 0:
                                exh = round((second_half_avg - first_half_avg) / first_half_avg, 3)
                                lv["exh"] = exh
            levels.append(lv)
        levels.sort(key=lambda x: x["price"])

        # Enrich with refill speed from live l2_worker data
        try:
            from background_engine.l2_worker import get_refill_stats
            refill = get_refill_stats(symbol)
            for lv in levels:
                ps = f"{lv['price']:.2f}"
                rs = refill.get(ps)
                if rs and rs.get('count', 0) >= 2:
                    lv['refill_class'] = rs['classification']
        except Exception as e:
            logging.getLogger(__name__).debug("refill enrich failed: %s", e)

        # Compute level states: FRESH / DEF / CONSUMED / AIR
        # Combines VP (historical volume) + L2 (live DOM depth) + refill tracking
        try:
            import traceback as _tb
            from background_engine.l2_worker import L2_STATE, _L2_LOCK
            with _L2_LOCK:
                _dom = L2_STATE.get("dom", {}).get(symbol, {})
                _raw_bids = _dom.get("bids", {})
                _raw_asks = _dom.get("asks", {})
            # Build numeric lookup: float price → depth (handles any string format)
            _bid_depth = {}
            for k, v in _raw_bids.items():
                try: _bid_depth[round(float(k), 2)] = v
                except: pass
            _ask_depth = {}
            for k, v in _raw_asks.items():
                try: _ask_depth[round(float(k), 2)] = v
                except: pass
            # VP volume percentiles — the VP is the primary filter
            _sorted_vols = sorted([lv["total"] for lv in levels if lv["total"] > 0])
            _vp_p80 = _sorted_vols[int(len(_sorted_vols) * 0.80)] if _sorted_vols else 0
            _vp_p50 = _sorted_vols[int(len(_sorted_vols) * 0.50)] if _sorted_vols else 0

            # DOM depth median for FRESH threshold
            _all_depths = []
            for lv in levels:
                _p = round(lv['price'], 2)
                _d = (_bid_depth.get(_p, 0) or 0) + (_ask_depth.get(_p, 0) or 0)
                if _d > 0:
                    _all_depths.append(_d)
            _depth_median = sorted(_all_depths)[len(_all_depths) // 2] if _all_depths else 3

            for lv in levels:
                _p = round(lv['price'], 2)
                _depth = (_bid_depth.get(_p, 0) or 0) + (_ask_depth.get(_p, 0) or 0)

                # VP volume is the PRIMARY gate
                _vp_significant = lv["total"] >= _vp_p80   # top 20% VP volume
                _vp_moderate = lv["total"] >= _vp_p50      # top 50% VP volume
                _has_depth = _depth >= _depth_median        # above median DOM depth

                if _vp_significant and _has_depth:
                    # High VP + high depth = actively defended level
                    lv["state"] = "DEF"
                    lv["depth"] = int(_depth)
                elif _vp_significant and not _has_depth:
                    # High VP + no depth = was defended, ammo consumed
                    lv["state"] = "CONSUMED"
                elif not _vp_moderate and _has_depth:
                    # Low VP + high depth = fresh untested wall
                    lv["state"] = "FRESH"
                    lv["depth"] = int(_depth)
                else:
                    # Everything else = not significant
                    lv["state"] = "AIR"
        except Exception as _e:
            print(f"[VP STATE] Error computing states: {_e}")
            import traceback; traceback.print_exc()

        # Trade-size filter: remove levels below minimum volume threshold
        if min_level_vol > 0:
            levels = [lv for lv in levels if lv["total"] >= min_level_vol]
            if not levels:
                return jsonify({
                    "symbol": symbol, "mode": mode, "from_ts": from_ts, "to_ts": to_ts,
                    "poc": 0, "vah": 0, "val": 0, "total_vol": 0,
                    "levels_count": 0, "candles_used": candles_used, "levels": []
                })

        total_vol = sum(lv["total"] for lv in levels)

        # POC: price with highest volume
        poc_level = max(levels, key=lambda x: x["total"])
        poc = poc_level["price"]

        # Value Area — expand from POC outward
        levels_by_vol = sorted(levels, key=lambda x: x["total"], reverse=True)
        va_target = total_vol * va_pct
        va_vol = 0
        va_prices = []
        for lv in levels_by_vol:
            va_vol += lv["total"]
            va_prices.append(lv["price"])
            if va_vol >= va_target:
                break
        va_prices.sort()
        vah = va_prices[-1] if va_prices else poc
        val = va_prices[0] if va_prices else poc

        # ── KDE + Prominence HVN/LVN Detection ──
        # Non-parametric density estimation — zero tuned parameters.
        # KDE finds the smooth volume density; prominence scoring on KDE
        # peaks/troughs identifies structurally significant levels.
        kde_bw = None
        if len(levels) >= 5:
            try:
                import numpy as np
                from scipy.stats import gaussian_kde
                from scipy.signal import find_peaks

                prices_arr = np.array([lv["price"] for lv in levels])
                vols_arr = np.array([float(lv["total"]) for lv in levels])

                # Weighted KDE: each price point weighted by its volume
                # Scott's rule for bandwidth — optimal for unknown distributions
                kde = gaussian_kde(prices_arr, weights=vols_arr, bw_method='scott')
                kde_bw = float(kde.factor)

                # Evaluate density at each price level
                density = kde(prices_arr)
                max_density = density.max()
                if max_density > 0:
                    norm_density = density / max_density  # normalize to [0,1]
                else:
                    norm_density = np.zeros_like(density)

                # Write KDE density to each level
                for i, lv in enumerate(levels):
                    lv["kde"] = round(float(norm_density[i]), 4)

                # HVN: peaks in KDE density with prominence scoring
                hvn_peaks, hvn_props = find_peaks(density, distance=2, plateau_size=0)
                if len(hvn_peaks) > 0 and "prominences" not in hvn_props:
                    from scipy.signal import peak_prominences
                    proms, _, _ = peak_prominences(density, hvn_peaks)
                    hvn_props["prominences"] = proms

                if len(hvn_peaks) > 0 and len(hvn_props.get("prominences", [])) > 0:
                    hvn_proms = hvn_props["prominences"]
                    med_prom = float(np.median(hvn_proms))
                    max_prom = float(hvn_proms.max()) if hvn_proms.max() > 0 else 1.0
                    for j, idx in enumerate(hvn_peaks):
                        prom = float(hvn_proms[j])
                        if prom >= med_prom:
                            levels[idx]["hvn"] = round(prom / max_prom, 4)

                # LVN: troughs (peaks in negated density)
                lvn_peaks, lvn_props = find_peaks(-density, distance=2, plateau_size=0)
                if len(lvn_peaks) > 0 and "prominences" not in lvn_props:
                    from scipy.signal import peak_prominences
                    proms, _, _ = peak_prominences(-density, lvn_peaks)
                    lvn_props["prominences"] = proms

                if len(lvn_peaks) > 0 and len(lvn_props.get("prominences", [])) > 0:
                    lvn_proms = lvn_props["prominences"]
                    med_prom = float(np.median(lvn_proms))
                    max_prom = float(lvn_proms.max()) if lvn_proms.max() > 0 else 1.0
                    for j, idx in enumerate(lvn_peaks):
                        prom = float(lvn_proms[j])
                        if prom >= med_prom:
                            levels[idx]["lvn"] = round(prom / max_prom, 4)

            except Exception as e:
                import traceback
                traceback.print_exc()
                # KDE failed — levels continue without kde/hvn/lvn fields

        # Bucket into row_count bins if requested
        if row_count > 0 and len(levels) > row_count:
            min_p = levels[0]["price"]
            max_p = levels[-1]["price"]
            bucket_size = (max_p - min_p) / row_count
            if bucket_size > 0:
                buckets = []
                for i in range(row_count):
                    lo = min_p + i * bucket_size
                    hi = lo + bucket_size
                    b = {"price": round(lo + bucket_size / 2, 2), "buy": 0, "sell": 0, "total": 0, "delta": 0}
                    b_kde = 0.0
                    b_hvn = None
                    b_lvn = None
                    for lv in levels:
                        if lv["price"] >= lo and (lv["price"] < hi or i == row_count - 1):
                            b["buy"] += lv["buy"]
                            b["sell"] += lv["sell"]
                            b["total"] += lv["total"]
                            b["delta"] += lv["delta"]
                            # Propagate KDE: take max density and highest conviction
                            if "kde" in lv:
                                b_kde = max(b_kde, lv["kde"])
                            if "hvn" in lv and (b_hvn is None or lv["hvn"] > b_hvn):
                                b_hvn = lv["hvn"]
                            if "lvn" in lv and (b_lvn is None or lv["lvn"] > b_lvn):
                                b_lvn = lv["lvn"]
                    if b["total"] > 0:
                        if b_kde > 0:
                            b["kde"] = b_kde
                        if b_hvn is not None:
                            b["hvn"] = b_hvn
                        if b_lvn is not None:
                            b["lvn"] = b_lvn
                        buckets.append(b)
                levels = buckets

        # ── Step Profiles: break time range into sub-profiles ──
        step_profiles = None
        if step_param:
            step_map = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400}
            step_sec = step_map.get(step_param, 0)
            if step_sec > 0:
                step_profiles = []
                cursor = from_ts
                while cursor < to_ts:
                    chunk_end = min(cursor + step_sec, to_ts)
                    # Build mini-profile for this chunk
                    sp = {}
                    for c in raw:
                        ct = int(c.get("t", 0))
                        if ct < cursor or ct >= chunk_end:
                            continue
                        bp = c.get("bp")
                        if bp:
                            for ps, vols in bp.items():
                                if not isinstance(vols, (list, tuple)) or len(vols) < 2:
                                    continue
                                if ps not in sp:
                                    sp[ps] = {"buy": 0, "sell": 0}
                                sp[ps]["buy"] += vols[0]
                                sp[ps]["sell"] += vols[1]
                        else:
                            h, l, v = c.get("h", 0), c.get("l", 0), c.get("v", 0)
                            if v <= 0 or h <= 0 or l <= 0 or h <= l:
                                continue
                            n_t = max(int(round((h - l) / tick)), 1)
                            vpt = v / (n_t + 1)
                            is_bull = c.get("c", 0) >= c.get("o", 0)
                            br = 0.6 if is_bull else 0.4
                            p = l
                            while p <= h + tick * 0.01:
                                ps = f"{p:.2f}"
                                if ps not in sp:
                                    sp[ps] = {"buy": 0, "sell": 0}
                                sp[ps]["buy"] += vpt * br
                                sp[ps]["sell"] += vpt * (1 - br)
                                p = round(p + tick, 10)
                    if sp:
                        s_levels = [{"price": float(k), "total": v["buy"] + v["sell"],
                                     "buy": v["buy"], "sell": v["sell"],
                                     "delta": v["buy"] - v["sell"]}
                                    for k, v in sp.items()]
                        s_levels.sort(key=lambda x: x["price"])
                        s_total = sum(lv["total"] for lv in s_levels)
                        s_poc_lv = max(s_levels, key=lambda x: x["total"])
                        s_poc = s_poc_lv["price"]
                        # Quick VA
                        s_by_vol = sorted(s_levels, key=lambda x: x["total"], reverse=True)
                        s_va_vol = 0
                        s_va_p = []
                        for lv in s_by_vol:
                            s_va_vol += lv["total"]
                            s_va_p.append(lv["price"])
                            if s_va_vol >= s_total * va_pct:
                                break
                        s_va_p.sort()
                        step_profiles.append({
                            "from_ts": cursor,
                            "to_ts": chunk_end,
                            "poc": s_poc,
                            "vah": s_va_p[-1] if s_va_p else s_poc,
                            "val": s_va_p[0] if s_va_p else s_poc,
                            "total_vol": s_total,
                            "levels": s_levels,
                        })
                    cursor = chunk_end

        result = {
            "symbol": symbol,
            "mode": mode,
            "from_ts": from_ts,
            "to_ts": to_ts,
            "poc": poc,
            "kde_bw": kde_bw,
            "vah": vah,
            "val": val,
            "total_vol": total_vol,
            "levels_count": len(levels),
            "candles_used": candles_used,
            "levels": levels,
        }
        if step_profiles is not None:
            result["step_profiles"] = step_profiles
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/vprofile/naked-pocs")
def api_naked_pocs():
    """Naked POCs — prior session POCs that price hasn't revisited.
    ?symbol=NQ&days=5 (default 5 sessions back)
    Returns [{price, session_date, age_days, tested}]
    """
    try:
        from background_engine.l2_worker import get_candles
        import time as _t, pytz
        from datetime import datetime, timedelta

        symbol = request.args.get("symbol", "NQ")
        days = int(request.args.get("days", 5))
        days = max(1, min(days, 10))

        et = pytz.timezone("US/Eastern")
        now = _t.time()
        now_et = datetime.fromtimestamp(now, tz=et)
        raw = get_candles(symbol, "1m")
        if not raw:
            return jsonify({"naked_pocs": []})

        TICK_SIZES = {"NQ": 0.25, "GC": 0.10, "ES": 0.25}
        tick = TICK_SIZES.get(symbol, 0.25)

        # Find current session start
        if now_et.hour >= 18:
            cur_session_start = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
        else:
            cur_session_start = (now_et - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)

        naked_pocs = []
        for d in range(1, days + 1):
            sess_end = cur_session_start - timedelta(days=d - 1)
            sess_start = sess_end - timedelta(days=1)
            s_from = int(sess_start.timestamp())
            s_to = int(sess_end.timestamp())

            # Build volume-at-price for this session
            profile = {}
            for c in raw:
                t = int(c.get("t", 0))
                if t < s_from or t > s_to:
                    continue
                bp = c.get("bp")
                if bp:
                    for ps, vols in bp.items():
                        if not isinstance(vols, (list, tuple)) or len(vols) < 2:
                            continue
                        if ps not in profile:
                            profile[ps] = 0
                        profile[ps] += vols[0] + vols[1]
                else:
                    h, l, v = c.get("h", 0), c.get("l", 0), c.get("v", 0)
                    if v <= 0 or h <= 0 or l <= 0 or h <= l:
                        continue
                    n_ticks = max(int(round((h - l) / tick)), 1)
                    vpt = v / (n_ticks + 1)
                    p = l
                    while p <= h + tick * 0.01:
                        ps = f"{p:.2f}"
                        profile[ps] = profile.get(ps, 0) + vpt
                        p = round(p + tick, 10)

            if not profile:
                continue

            # POC = max volume price
            poc_str = max(profile, key=profile.get)
            poc_price = float(poc_str)

            # Check if price revisited this POC since session ended.
            # Sprint 4: tolerance of ±1.5 tick covers half-tick bar closes and
            # wicks that came within 0.375pt of POC but didn't cross exactly.
            tested = False
            closest_ticks = None
            hit_tol = tick * 1.5
            for c in raw:
                t = int(c.get("t", 0))
                if t <= s_to:
                    continue
                h, l = c.get("h", 0), c.get("l", 0)
                if h <= 0 or l <= 0:
                    continue
                if (h + hit_tol) >= poc_price >= (l - hit_tol):
                    tested = True
                    break
                # Track closest approach for post-hoc diagnostics
                gap = min(abs(h - poc_price), abs(l - poc_price))
                gap_ticks = gap / tick
                if closest_ticks is None or gap_ticks < closest_ticks:
                    closest_ticks = gap_ticks

            if not tested:
                sess_label = sess_start.strftime("%m/%d")
                # Sprint 4: 0.1-day precision so freshly formed POCs show 1.1/1.2 etc.
                age = round((now_et - sess_start).total_seconds() / 86400.0, 1)
                naked_pocs.append({
                    "price": poc_price,
                    "session_date": sess_label,
                    "age_days": age,
                    "tested": False,
                    "closest_approach_ticks": round(closest_ticks, 1) if closest_ticks is not None else None,
                })

        return jsonify({"naked_pocs": naked_pocs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vprofile/dev-poc")
def api_dev_poc():
    """Developing POC — live POC migration path for current session.
    ?symbol=NQ&interval=5 (interval in minutes, default 5)
    Returns {poc_path: [{time, poc}], current_poc}
    """
    try:
        from background_engine.l2_worker import get_candles
        import time as _t, pytz
        from datetime import datetime, timedelta

        symbol = request.args.get("symbol", "NQ")
        interval = int(request.args.get("interval", 5))
        interval = max(1, min(interval, 60))
        interval_sec = interval * 60

        et = pytz.timezone("US/Eastern")
        now = _t.time()
        now_et = datetime.fromtimestamp(now, tz=et)
        raw = get_candles(symbol, "1m")
        if not raw:
            return jsonify({"poc_path": [], "current_poc": None})

        TICK_SIZES = {"NQ": 0.25, "GC": 0.10, "ES": 0.25}
        tick = TICK_SIZES.get(symbol, 0.25)

        # Session start
        if now_et.hour >= 18:
            session_start = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
        else:
            session_start = (now_et - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        s_from = int(session_start.timestamp())

        # Filter to session candles, sorted by time
        session_candles = sorted(
            [c for c in raw if int(c.get("t", 0)) >= s_from],
            key=lambda c: c.get("t", 0)
        )
        if not session_candles:
            return jsonify({"poc_path": [], "current_poc": None})

        # Walk candles chronologically, compute running POC at each interval
        profile = {}  # running volume-at-price
        poc_path = []
        next_checkpoint = s_from + interval_sec

        for c in session_candles:
            t = int(c.get("t", 0))
            bp = c.get("bp")
            if bp:
                for ps, vols in bp.items():
                    if not isinstance(vols, (list, tuple)) or len(vols) < 2:
                        continue
                    profile[ps] = profile.get(ps, 0) + vols[0] + vols[1]
            else:
                h, l, v = c.get("h", 0), c.get("l", 0), c.get("v", 0)
                if v <= 0 or h <= 0 or l <= 0 or h <= l:
                    continue
                n_ticks = max(int(round((h - l) / tick)), 1)
                vpt = v / (n_ticks + 1)
                p = l
                while p <= h + tick * 0.01:
                    ps = f"{p:.2f}"
                    profile[ps] = profile.get(ps, 0) + vpt
                    p = round(p + tick, 10)

            # Emit POC at each interval checkpoint
            while t >= next_checkpoint and profile:
                poc_str = max(profile, key=profile.get)
                poc_path.append({"time": next_checkpoint, "poc": float(poc_str)})
                next_checkpoint += interval_sec

        # Current POC
        current_poc = float(max(profile, key=profile.get)) if profile else None

        return jsonify({"poc_path": poc_path, "current_poc": current_poc})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/status")
def api_l2_status():
    """L2 connection health and candle availability."""
    try:
        from background_engine.l2_worker import get_l2_state, _CANDLES, CANDLE_TIMEFRAMES, _CANDLE_LOCK
        state = get_l2_state()
        # Candle counts per symbol/tf (uses _CANDLE_LOCK for thread safety)
        counts = {}
        with _CANDLE_LOCK:
            for sym in ["NQ", "ES", "YM", "RTY"]:
                counts[sym] = {}
                for tf in CANDLE_TIMEFRAMES:
                    q = _CANDLES.get(sym, {}).get(tf)
                    counts[sym][tf] = len(q) if q else 0
        return jsonify({
            "connected":   state.get("connected", False),
            "mid_prices":  state.get("mid_prices", {}),
            "last_update": state.get("last_update", 0),
            "candle_counts": counts,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Alpha Engine Dashboard API ────────────────────────────────────────────────
@app.route("/api/alpha")
def api_alpha():
    """Phase 7 Alpha Engine — real-time stats from edge_outcomes.jsonl."""
    import json as _json, time as _t
    try:
        from background_engine.l2_worker import (
            _KALMAN_CV, _VOLUME_CLOCKS, _CURRENT_REGIME
        )

        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs", "edge_outcomes.jsonl"
        )

        signals = []
        if os.path.exists(log_path):
            with open(log_path) as _f:
                lines = _f.readlines()
            for line in lines[-500:]:
                if line.strip() and line.startswith('{'):
                    try:
                        signals.append(_json.loads(line))
                    except:
                        pass

        nq_high = [s for s in signals
                    if s.get('symbol') == 'NQ'
                    and s.get('confidence') == 'high'
                    and s.get('mfe_30s') is not None]

        total = len(nq_high)
        wins = sum(1 for s in nq_high if s.get('outcome_30s', 0) > 0)
        net_pnl = round(sum(s.get('outcome_30s', 0) for s in nq_high), 2)
        gross_win = sum(s.get('outcome_30s', 0) for s in nq_high if s.get('outcome_30s', 0) > 0)
        gross_loss = abs(sum(s.get('outcome_30s', 0) for s in nq_high if s.get('outcome_30s', 0) < 0))
        pf = round(gross_win / max(gross_loss, 0.01), 2)
        wr = round(wins / max(total, 1) * 100, 1)
        avg_mfe = round(sum(abs(s.get('mfe_30s', 0)) for s in nq_high) / max(total, 1), 2)
        avg_mae = round(sum(abs(s.get('mae_30s', 0)) for s in nq_high) / max(total, 1), 2)

        KILL_COMBOS = {
            ('long_gamma_stable', 's'), ('transition', 'b'), ('short_gamma_volatile', 'b')
        }
        killed_count = sum(1 for s in signals
                          if (s.get('regime', ''), s.get('side', '')) in KILL_COMBOS)
        cv_blocked = sum(1 for s in nq_high if s.get('kalman_cv', 1) < 0.04)

        regime_stats = {}
        for s in nq_high:
            r = s.get('regime', 'unknown')
            if r not in regime_stats:
                regime_stats[r] = {'count': 0, 'wins': 0, 'pnl': 0}
            regime_stats[r]['count'] += 1
            if s.get('outcome_30s', 0) > 0:
                regime_stats[r]['wins'] += 1
            regime_stats[r]['pnl'] = round(regime_stats[r]['pnl'] + s.get('outcome_30s', 0), 2)
        for r in regime_stats:
            regime_stats[r]['wr'] = round(
                regime_stats[r]['wins'] / max(regime_stats[r]['count'], 1) * 100, 1)

        nq_cv = round(_KALMAN_CV['NQ'].state, 4) if 'NQ' in _KALMAN_CV else 0
        nq_cv_n = _KALMAN_CV['NQ']._n if 'NQ' in _KALMAN_CV else 0
        current_dsl = round(max(3.0, nq_cv * 100), 2)
        pending_count = 0

        vpin_val = 0
        vpin_regime = 'N/A'
        try:
            from background_engine.l2_worker import _VPIN_ENGINES
            if 'NQ' in _VPIN_ENGINES:
                vpin_val = round(_VPIN_ENGINES['NQ'].vpin, 4)
                vpin_regime = _VPIN_ENGINES['NQ'].get_regime_modifier()
        except:
            pass

        recent = []
        for s in nq_high[-15:]:
            recent.append({
                'ts': s.get('ts_human', ''),
                'side': 'LONG' if s.get('side') == 'b' else 'SHORT',
                'price': s.get('price', 0),
                'outcome': s.get('outcome_30s', 0),
                'mfe': round(abs(s.get('mfe_30s', 0)), 2),
                'mae': round(abs(s.get('mae_30s', 0)), 2),
                'regime': s.get('regime', ''),
                'cv': s.get('kalman_cv', 0),
                'dsl': s.get('dynamic_sl', 0),
                'dsl_hit': s.get('dynamic_sl_hit', False),
                'win': s.get('outcome_30s', 0) > 0,
            })

        dsl_pnl = 0
        dsl_wins = 0
        for s in nq_high:
            mae = abs(s.get('mae_30s', 0))
            cv = s.get('kalman_cv', 0.05)
            sl = max(3.0, cv * 100)
            if mae >= sl:
                dsl_pnl += -sl
            else:
                outcome = s.get('outcome_30s', 0)
                dsl_pnl += outcome
                if outcome > 0:
                    dsl_wins += 1
        dsl_pnl = round(dsl_pnl, 2)
        dsl_wr = round(dsl_wins / max(total, 1) * 100, 1)

        return jsonify({
            'engine': 'Phase 7 — Alpha Engine v2',
            'regime': _CURRENT_REGIME,
            'kalman_cv': nq_cv,
            'kalman_n': nq_cv_n,
            'kalman_warm': nq_cv_n > 250,
            'dynamic_sl': current_dsl,
            'vpin': vpin_val,
            'vpin_regime': vpin_regime,
            'pending_trades': pending_count,
            'stats': {
                'total': total, 'wins': wins, 'win_rate': wr,
                'net_pnl': net_pnl, 'profit_factor': pf,
                'avg_mfe': avg_mfe, 'avg_mae': avg_mae,
            },
            'dynamic_sl_sim': {'pnl': dsl_pnl, 'win_rate': dsl_wr},
            'filters': {'killed_combos': killed_count, 'cv_blocked': cv_blocked},
            'regime_breakdown': regime_stats,
            'recent_trades': list(reversed(recent)),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


# ── Background workers (run under BOTH gunicorn and direct execution) ─────────
import threading as _startup_threading

_workers_started = False
_startup_lock = _startup_threading.Lock()

def _start_workers():
    global _workers_started
    with _startup_lock:
        if _workers_started:
            return
        _workers_started = True

    # Pre-warm options cache
    def _prewarm():
        try:
            from data_provider import fetch_all
            ticker = get_ticker()
            print(f"[startup] Pre-warming cache for {ticker}...")
            fetch_all(ticker)
            print("[startup] Cache ready -- first load will be fast")
        except Exception as e:
            print(f"[startup] Pre-warm failed (non-fatal): {e}")
    _startup_threading.Thread(target=_prewarm, daemon=True).start()

    # Start TopStepX Level 2 background worker
    _l2_startup_error_holder = [None]   # mutable container for thread error capture
    def _start_l2():
        import time as _t
        print("[L2-THREAD] sleeping 3s for module load...", flush=True)
        _t.sleep(3)
        print("[L2-THREAD] awake, importing l2_worker...", flush=True)
        print(f"[L2-THREAD] ENV CHECK: username={bool(os.getenv('TOPSTEPX_USERNAME'))}, api_key={bool(os.getenv('TOPSTEPX_API_KEY'))}, password={bool(os.getenv('TOPSTEPX_PASSWORD'))}, rest_base={os.getenv('TOPSTEPX_REST_BASE', '(not set)')}", flush=True)
        try:
            from background_engine.l2_worker import start_l2_worker, set_socketio
            print("[L2-THREAD] import OK, injecting Socket.IO...", flush=True)
            set_socketio(socketio)
            print("[L2-THREAD] calling start_l2_worker()...", flush=True)
            start_l2_worker()
            print("[L2-THREAD] start_l2_worker() returned OK", flush=True)
        except Exception as e:
            import traceback
            err_msg = f"{e}\n{traceback.format_exc()}"
            _l2_startup_error_holder[0] = err_msg
            print(f"[L2-THREAD] FAILED: {err_msg}", flush=True)
    _startup_threading.Thread(target=_start_l2, daemon=True).start()
    print("[startup] L2 daemon thread spawned", flush=True)

    # Start Schwab WebSocket bridge (real-time GEX streaming)
    def _start_schwab():
        import time as _t
        _t.sleep(5)  # Let server + Schwab auth settle
        try:
            from background_engine.schwab_bridge import set_socketio as sb_set_sio, start_schwab_bridge
            sb_set_sio(socketio)
            start_schwab_bridge()
            print("[startup] Schwab bridge started — real-time GEX push active", flush=True)
        except Exception as e:
            print(f"[startup] Schwab bridge failed (non-fatal): {e}", flush=True)
    _startup_threading.Thread(target=_start_schwab, daemon=True).start()
    print("[startup] Schwab bridge daemon thread spawned", flush=True)

    # Store reference so /api/l2 can check it
    import builtins
    builtins._l2_startup_error_holder = _l2_startup_error_holder

# ── Lazy worker start via before_request (survives gunicorn fork) ──────────────
# Gunicorn forks AFTER module import, killing any daemon threads started here.
# Instead, we start workers on the first HTTP request, which is guaranteed to
# run inside the actual gunicorn worker process.
_worker_error = None

@app.before_request
def _ensure_workers_started():
    global _worker_error
    if not _workers_started:
        try:
            print("[startup] Starting workers on first request (gunicorn-safe)...", flush=True)
            _start_workers()
            print("[startup] Workers started successfully", flush=True)
        except Exception as _e:
            _worker_error = str(_e)
            print(f"[startup] WARNING: workers failed to start: {_e}", flush=True)

# ── Socket.IO event handlers ──
def _push_candle_history(sid, symbol='NQ', tf='1m', max_candles=200):
    """Push candle history to a specific client. Returns True if data was sent."""
    try:
        from background_engine.l2_worker import get_candles, _CURRENT_CANDLE
        candles_raw = get_candles(symbol, tf)
        cur = _CURRENT_CANDLE.get(symbol, {}).get(tf)
        all_candles = list(candles_raw)
        if cur and (not all_candles or cur['t'] != all_candles[-1]['t']):
            all_candles.append(cur)
        send_candles = []
        for c in all_candles[-max_candles:]:
            out = {
                "time":   int(c.get("t", 0)),
                "open":   c["o"], "high": c["h"],
                "low":    c["l"], "close": c["c"],
                "volume": c.get("v", 0),
            }
            bp = c.get("bp")
            if bp:
                out["bp"] = {k: v for k, v in bp.items() if v[0] > 0 or v[1] > 0}
            for key in ("sweeps", "delta_div", "ignition",
                        "spoofs", "wall_gone",
                        "absorption", "depth_deltas"):
                val = c.get(key)
                if val:
                    out[key] = val
            send_candles.append(out)
        if send_candles:
            socketio.emit('candle_history', {'symbol': symbol, 'tf': tf, 'candles': send_candles},
                          to=sid, namespace='/')
            bp_count = sum(1 for c in send_candles if c.get('bp'))
            print(f"[Socket.IO] Pushed {len(send_candles)} {symbol}/{tf} candles ({bp_count} with bp) to {sid}")
            return True
    except Exception as e:
        print(f"[Socket.IO] candle_history push failed: {e}")
    return False

# Track active tf per client to cancel stale deferred pushes
_client_active_tf = {}  # {sid: tf}

def _deferred_candle_push(sid, symbol='NQ', tf='1m'):
    """Retry candle push until backfill is done and meaningful data is available."""
    import time as _time
    for attempt in range(15):
        _time.sleep(2)
        # Abort if client switched to a different tf
        if _client_active_tf.get(sid) != tf:
            print(f"[Socket.IO] Deferred push for {sid} aborted — client switched from {tf} to {_client_active_tf.get(sid)}")
            return
        try:
            from background_engine.l2_worker import get_candles
            candles = get_candles(symbol, tf)
            if len(candles) >= 50:
                _push_candle_history(sid, symbol, tf)
                return
        except Exception:
            pass
    # Last resort: push whatever we have (if still on same tf)
    if _client_active_tf.get(sid) == tf:
        _push_candle_history(sid, symbol, tf)

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f"[Socket.IO] Client connected: {sid}")
    # Push candle history on connect — subscribe handler also pushes on tf switch
    # but the initial connect needs this for first load
    _client_active_tf[sid] = '1m'
    # Ensure _ACTIVE_TFS contains this client's default tf
    try:
        import background_engine.l2_worker as _l2w
        _l2w._ACTIVE_TFS = set(_client_active_tf.values()) or {"1m"}
    except Exception:
        pass
    if not _push_candle_history(sid):
        import threading
        t = threading.Thread(target=_deferred_candle_push, args=(sid,), daemon=True)
        t.start()

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"[Socket.IO] Client disconnected: {sid}")
    # Drop this client's tf and rebuild _ACTIVE_TFS set
    _client_active_tf.pop(sid, None)
    try:
        import background_engine.l2_worker as _l2w
        live = set(_client_active_tf.values()) or {"1m"}
        _l2w._ACTIVE_TFS = live
        # Keep legacy singleton roughly in sync (arbitrary pick)
        _l2w._ACTIVE_TF = next(iter(live))
    except Exception:
        pass

@socketio.on('subscribe')
def handle_subscribe(data):
    """Client subscribes to a symbol+timeframe for live candle push.
    Immediately sends full candle history for the requested symbol/tf.
    """
    symbol = data.get('symbol', 'NQ').upper()
    tf = data.get('tf', '1m')
    sid = request.sid
    print(f"[Socket.IO] Client {sid} subscribed to {symbol}/{tf}")
    emit('subscribed', {'symbol': symbol, 'tf': tf})
    # Track per-client tf (kills stale deferred pushes)
    _client_active_tf[sid] = tf
    # Update active tf set — union of all clients' TFs (multi-client safe)
    try:
        import background_engine.l2_worker as _l2w
        live = set(_client_active_tf.values()) or {"1m"}
        _l2w._ACTIVE_TFS = live
        _l2w._ACTIVE_TF = tf  # legacy field — last-subscriber wins (harmless now)
    except Exception:
        pass
    # Reset V2 engines so stale state from previous symbol doesn't bleed
    try:
        from background_engine.l2_worker import _reset_v2_engines
        _reset_v2_engines(symbol)
    except Exception:
        pass  # l2_worker not yet loaded on cold start
    # Push candle history — retry if worker hasn't loaded yet
    if not _push_candle_history(sid, symbol, tf, max_candles=300):
        print(f"[Socket.IO] No candles for {symbol}/{tf} yet, scheduling deferred push for {sid}...")
        import threading
        t = threading.Thread(target=_deferred_candle_push, args=(sid, symbol, tf), daemon=True)
        t.start()

if __name__ == "__main__":
    print("Starting Altaris Dev with Socket.IO...")
    print("Open http://localhost:5000 in your browser")

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
