"""""
Altaris Dev - Flask Web Server
"""
# gevent monkey-patch must run before anything else imports threading/ssl/socket.
# Without this, background threads (Schwab bridge, TopStepX L2 worker, L2 push
# loop) make blocking syscalls that stall the gevent WS hub — every pane lags
# together. Patching converts those calls to cooperative yields.
from gevent import monkey; monkey.patch_all()

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
from flask_socketio import SocketIO, emit, join_room, leave_room
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


# ── Symbol prefix alias (added 2026-05-04) ──────────────────────────────────
# Schwab's chain/quote APIs require `$` prefix for cash indices ($SPX, $VIX,
# $NDX, $VXN, $RUT, $DJX). Frontend/CLI consumers commonly send the bare
# symbol. This helper auto-prefixes so callers don't need to know.
# Already-prefixed inputs ($SPX, etc.) and non-index tickers (QQQ, AAPL) pass
# through unchanged.
_INDEX_ALIASES = frozenset({"SPX", "VIX", "NDX", "VXN", "RUT", "DJX"})

def _resolve_index_ticker(t):
    if not t: return t
    t = t.upper()
    return "$" + t if t in _INDEX_ALIASES else t


# ── Thundering-herd guard for fetch_all ──────────────────────────────────────
# When the cache is cold and N threads hit /api/data simultaneously, only one
# actually calls fetch_all(); the rest wait on the Event and reuse the result.
_fetch_locks: dict = {}          # ticker → threading.Event
_fetch_results: dict = {}        # ticker → (data, timestamp)
_fetch_meta_lock = _threading.Lock()
_FETCH_TTL = 180                 # 2026-05-01 (post-fix audit): bumped 28→60→180.
                                 # 60s wasn't enough — /api/data still hit 10s cache-miss
                                 # spikes that drained Tradier WS Recv-Q to 7MB backlog
                                 # in 16 min. 180s = 6× UI poll cadence, so cache hits on
                                 # 5 of every 6 calls. Combined with gevent yields in
                                 # _build_exposures (data_provider.py:267), the misses no
                                 # longer monopolize the event loop.

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
_PUBLIC_EXTENSIONS = ('.css', '.js', '.ico', '.png', '.jpg', '.svg',
                      '.woff', '.woff2', '.ttf', '.map')
# .html intentionally NOT public — explicit _PUBLIC_PATHS lists allowed HTMLs
# (login.html, index.html). A blanket .html bypass risks exposing any future
# debug/partial/template that lands in web/ unauthenticated.

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

    # Check token — cookie (browser nav) OR header (API calls) OR the
    # paste-link `?token=` query parameter. The query-string path is
    # accepted to preserve paste-and-go nav from token-bearing URLs;
    # to limit URL-token exposure (server logs, browser history, Referer
    # headers — OWASP A02:2021), we PROMOTE a valid query-string token
    # to an HttpOnly cookie + 302 to a clean URL on first hit (handled
    # below in this function), so the second-and-subsequent navigation
    # carries the token only in the cookie.
    qs_tok = request.args.get("token", "")
    tok = (request.cookies.get("wm_auth")
           or request.headers.get("X-Auth-Token", "")
           or qs_tok)
    if not _valid_token(tok):
        if path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        resp = Response(status=302, headers={"Location": "/login"})
        resp.delete_cookie("wm_auth")
        return resp
    # Promote a query-string token to a cookie + redirect to a clean URL
    # so subsequent navigation no longer carries the token in URLs (which
    # leak to server logs, browser history, and Referer headers).
    if qs_tok and not request.cookies.get("wm_auth") and not path.startswith("/api/"):
        # Strip the token from the URL while preserving any other query args.
        from urllib.parse import urlencode, parse_qsl
        other = [(k, v) for (k, v) in parse_qsl(request.query_string.decode('utf-8'),
                                                 keep_blank_values=True)
                 if k != 'token']
        clean_qs = ('?' + urlencode(other)) if other else ''
        clean_url = path + clean_qs
        resp = Response(status=302, headers={"Location": clean_url})
        resp.set_cookie("wm_auth", qs_tok, max_age=_TOKEN_TTL,
                        httponly=True, samesite="Lax")
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
    # If auth came in via ?token=/header (cookie missing), persist it as cookie
    # so subsequent API calls from app.js don't 401 and bounce to /login.
    query_tok = request.args.get("token", "")
    header_tok = request.headers.get("X-Auth-Token", "")
    if not request.cookies.get("wm_auth"):
        for tok in (query_tok, header_tok):
            if tok and _valid_token(tok):
                resp.set_cookie("wm_auth", tok, max_age=_TOKEN_TTL, httponly=True, samesite="Lax")
                break
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


def _schwab_chain_range(ticker, from_date, to_date, strike_count=50):
    """Get options chain from Schwab across a date RANGE (multiple expirations
    in one REST call). Returns (flattened_options, underlying_price).

    Unlike `_schwab_chain_raw` which hits `/chains` once per expiration,
    this pulls every expiration in [from_date, to_date] in a single call —
    the Tradier single-name Greek poller uses this to cover the full 60-day
    expiration ladder with one REST roundtrip per ticker.

    Each output dict carries an `exp_date` field so callers can group/sort by
    expiration without needing a second API call.
    """
    data = _schwab_get("/marketdata/v1/chains", {
        "symbol": ticker,
        "contractType": "ALL",
        "includeUnderlyingQuote": "true",
        "fromDate": from_date,
        "toDate": to_date,
        "strikeCount": strike_count,
    })
    options = []
    for leg_key in ("callExpDateMap", "putExpDateMap"):
        exp_map = data.get(leg_key, {})
        for _exp_str, strikes in exp_map.items():
            _exp_iso = _exp_str.split(":")[0] if _exp_str else ""
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
                        "volatility":     c.get("volatility"),
                        "in_the_money":   c.get("inTheMoney", False),
                        "dte":            c.get("daysToExpiration", 0),
                        "symbol":         c.get("symbol", ""),
                        "mark":           c.get("mark", 0),
                        "mark_change":    c.get("markChange", 0),
                        "exp_date":       _exp_iso,
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

# ── /api/vprofile cache (2026-05-06) ─────────────────────────────────────────
# Frontend polls vprofile from multiple panes simultaneously. Each call is
# expensive: walks ~7000+ 1m candles, builds level-by-level profile, returns
# 200KB-900KB of JSON. Worst observed: mode=session&step=1h returning 872KB
# in 3.26s, monopolizing the gevent loop while WebSocket buffers fill →
# TopStepX RSTs. With a 3-second TTL, the first poll computes; subsequent
# polls within 3s return cached body. Profile changes ~once per minute on
# new bar close, so 3s staleness is invisible to the user.
_vprofile_cache: dict = {}       # {(query_string,): (response_body_bytes, ts)}
_VPROFILE_TTL = 15.0             # seconds (was 3.0 — frontend polls every
                                 # ~2.5s, so 3.0s TTL had borderline misses
                                 # producing 0.4-0.5s gevent blocks on the
                                 # 1MB mode=weekly response. Profile data
                                 # only changes on bar close — 15s staleness
                                 # is invisible.)


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
    """PUBLIC — dump exactly what option symbols we subscribed to.
    Includes:
      - SPX/VIX/SPY: from _subscribed_option_symbols_by_ticker (subscribed
        via _subscribe_options_for_ticker, populates the dict)
      - QQQ: from _ndx_option_symbols (subscribed via _subscribe_qqq_options,
        which doesn't populate the dict — this endpoint adds it explicitly)
      - per-ticker source breakdown of _per_ticker_gex (streaming vs REST
        rotation contributions, post-2026-04-29 chain-rotation ship)
    """
    try:
        from background_engine import schwab_bridge as sb
        subs = dict(getattr(sb, '_subscribed_option_symbols_by_ticker', {}))
        # QQQ uses a separate global (_ndx_option_symbols), not the dict —
        # inject it so the endpoint reflects ALL ticker subscriptions.
        ndx_syms = getattr(sb, '_ndx_option_symbols', None) or []
        if ndx_syms:
            subs['QQQ'] = ndx_syms

        out = {}
        for ticker, syms in subs.items():
            # Bucket by YYMMDD expiration
            by_date = {}
            for s in syms:
                d = s[6:12] if len(s) >= 12 else '?'
                by_date[d] = by_date.get(d, 0) + 1
            out[ticker] = {
                'streamed_total': len(syms),
                'by_expiration':  by_date,
                'first_3':        syms[:3],
                'last_3':         syms[-3:],
            }

        # Source breakdown for _per_ticker_gex (streaming vs REST rotation).
        # Each entry has _source='rest_rotation' if it came via the chain
        # rotation thread; otherwise it came from the LEVELONE_OPTIONS stream.
        per_ticker_gex = getattr(sb, '_per_ticker_gex', {}) or {}
        gex_breakdown = {}
        for ticker, contract_dict in per_ticker_gex.items():
            if not isinstance(contract_dict, dict): continue
            stream_n = 0
            rest_n   = 0
            for sym_key, info in contract_dict.items():
                if not isinstance(info, dict): continue
                if info.get('_source') == 'rest_rotation':
                    rest_n += 1
                else:
                    stream_n += 1
            gex_breakdown[ticker] = {
                'total_in_per_ticker_gex': stream_n + rest_n,
                'from_streaming':          stream_n,
                'from_rest_rotation':      rest_n,
            }
        return jsonify({
            'subscriptions': out,
            'per_ticker_gex_breakdown': gex_breakdown,
        })
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
        result = acc.get_diag()
        # Add Schwab _on_options_quote per-symbol call/distinct-trade counters
        # for SPY/QQQ over-count forensic. If calls > distinct_tts for many
        # symbols, Schwab is firing _on_options_quote multiple times per real
        # trade (over-count). If calls == distinct_tts, the over-count is
        # elsewhere (e.g., per-print magnitude amplification).
        try:
            from background_engine import schwab_bridge as _sb
            ooq = getattr(_sb, '_on_options_quote', None)
            if ooq is not None:
                q_diag = getattr(ooq, '_q_diag', {})
                summary = {}
                top_repeat_syms = []
                for sym, e in q_diag.items():
                    calls = e['calls']; n_tts = len(e['tt_set'])
                    if calls > n_tts:
                        top_repeat_syms.append((sym, calls, n_tts, calls - n_tts))
                top_repeat_syms.sort(key=lambda x: x[3], reverse=True)
                summary['n_symbols_seen'] = len(q_diag)
                summary['n_symbols_with_repeats'] = len(top_repeat_syms)
                summary['top_repeat_syms'] = [
                    {'sym': s, 'calls': c, 'distinct_tts': n, 'repeats': r}
                    for s, c, n, r in top_repeat_syms[:20]
                ]
                summary['totals'] = {
                    'total_calls':       sum(e['calls'] for e in q_diag.values()),
                    'total_distinct_tts': sum(len(e['tt_set']) for e in q_diag.values()),
                }
                summary['oversize_drops'] = getattr(ooq, '_oversize_drops', 0)
                result['schwab_quote_repeat_diag'] = summary
        except Exception as ex:
            result['schwab_quote_repeat_err'] = str(ex)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/_debug/kv")
def api_debug_kv():
    """Phase 19 — dump KvEstimator state per ticker.
    Shows current k_v, sample count, and source (fitted vs default).

    Optional: ?ticker=QQQ for single-ticker view.
    """
    try:
        from connectors.kv_estimator import get_kv_estimator
        from connectors.flow_accumulator import FLOW_ACC_KV_ADJUST_ENABLED
        est = get_kv_estimator()
        ticker = request.args.get('ticker')
        out = {
            'phase19_enabled': FLOW_ACC_KV_ADJUST_ENABLED,
            'state': est.get_state(ticker),
        }
        # Show comparison vs raw cum_signed if accumulator is up
        try:
            from connectors.flow_accumulator import get_accumulator
            acc = get_accumulator()
            if acc is not None:
                state = acc.get_state()
                kv_compare = []
                for t in state.get('tickers', []):
                    name = t.get('ticker')
                    adj = t.get('cum_signed_all', 0)
                    raw = t.get('cum_signed_all_raw', 0)
                    if abs(raw) > 0:
                        diff_pct = 100 * (adj - raw) / raw
                    else:
                        diff_pct = 0
                    kv_compare.append({
                        'ticker': name,
                        'cum_signed_all_adjusted': adj,
                        'cum_signed_all_raw': raw,
                        'diff_pct': round(diff_pct, 3),
                        'kv_adjusted_trades': t.get('kv_adjusted_trades', 0),
                    })
                out['flow_comparison'] = kv_compare
        except Exception:
            pass
        return jsonify(out)
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


@app.route("/api/_debug/walls_audit")
def api_debug_walls_audit():
    """PUBLIC — dump engine's cached walls + raw per-ticker GEX state so we can
    verify the _per_ticker_gex → _compute_walls_for → update_walls pipeline."""
    try:
        from connectors.alert_engine import get_engine
        from background_engine import schwab_bridge as sb
        eng = get_engine()
        ticker = _resolve_index_ticker(request.args.get("ticker", "QQQ"))
        out = {"ticker": ticker, "server_time": time.time()}
        if eng is not None:
            with eng._lock:
                h = eng._history.get(ticker)
                out["engine_walls"] = dict(h.last_walls) if (h and h.last_walls) else None
        try:
            out["computed_walls"] = sb._compute_walls_for(ticker)
        except Exception as e:
            out["computed_walls_error"] = str(e)
        per_contract = sb._per_ticker_gex.get(ticker, {}) if hasattr(sb, '_per_ticker_gex') else {}
        out["contract_count"] = len(per_contract)
        if per_contract:
            # Aggregate across expirations for the top-10 view (same as
            # _compute_walls_for). This is what the wall detector sees.
            agg = {}
            for e in per_contract.values():
                k = e.get('strike'); s = e.get('side')
                if not k or not s: continue
                a = agg.setdefault(k, {'call_oi':0,'put_oi':0,'call_gamma':0,'put_gamma':0})
                if s == 'call':
                    a['call_oi']    += e.get('oi',0) or 0
                    a['call_gamma'] += e.get('gamma_dollars',0) or 0
                else:
                    a['put_oi']    += e.get('oi',0) or 0
                    a['put_gamma'] += e.get('gamma_dollars',0) or 0
            out["strike_count"] = len(agg)
            strikes = sorted(agg.keys())
            out["strike_range"] = [strikes[0], strikes[-1]]
            out["top_call_oi"]    = sorted([(k,a['call_oi'])    for k,a in agg.items()], key=lambda x:-x[1])[:10]
            out["top_put_oi"]     = sorted([(k,a['put_oi'])     for k,a in agg.items()], key=lambda x:-x[1])[:10]
            out["top_call_gamma"] = [(k,round(g,0)) for k,g in sorted([(k,a['call_gamma']) for k,a in agg.items()], key=lambda x:-x[1])[:10]]
            out["top_put_gamma"]  = [(k,round(g,0)) for k,g in sorted([(k,a['put_gamma'])  for k,a in agg.items()], key=lambda x:-x[1])[:10]]
            out["spot"] = sb._latest_spot_by_ticker.get(ticker, 0) if hasattr(sb, '_latest_spot_by_ticker') else 0
        return jsonify(out)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/ndx_wgc")
def api_ndx_wgc():
    """Last emitted NDX Weighted Gamma Composite payload. Used by AI panel
    to hydrate the NDX regime cell on init so it's not blank until the next
    socket emit."""
    try:
        from background_engine import schwab_bridge as _sb
        return jsonify(_sb.get_latest_wgc())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/hedge_pressure/<ticker>")
def api_hedge_pressure(ticker):
    """Per-strike hedge-pressure snapshot (Γ · ΔS%, V · Δσpt, C · Δt_hr).

    Signed shares convention: positive = dealers must BUY, negative = SELL.
    Pure read of the GreekSurface double-buffered snapshot — no new compute.
    """
    try:
        from background_engine import schwab_bridge as _sb
        return jsonify(_sb.get_hedge_pressure_state(ticker.upper()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/hedge_pressure/<ticker>/by_exchange")
def api_hedge_pressure_by_exchange(ticker):
    """Per-exchange HP_γ rollup (posted vs caught share × dn_gamma at each
    contract's strike). Identifies which venue is carrying which side of the
    dealer-γ book."""
    try:
        from background_engine import schwab_bridge as _sb
        return jsonify(_sb.get_hedge_pressure_by_exchange(ticker.upper()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/hedge_pressure/<ticker>/alignment/<path:contract_sym>")
def api_hedge_pressure_alignment(ticker, contract_sym):
    """WITH-MM / AGAINST-MM alignment for a specific OSI contract on <ticker>.
    path:<contract_sym> handles any spacing / slashes in OSI strings."""
    try:
        from background_engine import schwab_bridge as _sb
        return jsonify(_sb.get_alignment_for_contract(contract_sym))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/sweeps")
def api_intel_sweeps():
    """Recent multi-strike option sweeps detected by connectors/sweep_detector.

    Sweeps are 3+ adjacent option-strike prints walking within 500ms, all
    aggressor-side same direction. Each record carries:
      - leg-by-leg detail (sym, strike, size, price, exch, ts_ms)
      - notional Δ exposure (DERIVED from Σ size × Δ × 100)
      - venue_sequence (institutional fingerprint)

    DESCRIPTIVE ONLY — no directional prediction. The dealer-hedging hypothesis
    was falsified (+0.27% edge vs base rate, n=15,902). expected_hedge_side
    and hf_alignment fields were stripped 2026-05-05.

    Live: a new sweep also fires Socket.IO 'intel:sweep_alert' event
    (push) — REST returns history for pane initial-load.

    Query params:
      limit: max records to return (default 50, max 200)
    """
    try:
        from connectors import sweep_detector as _swd
        limit = int(request.args.get('limit', 50))
        sweeps = _swd.get_recent_sweeps(min(max(limit, 1), 200))
        return jsonify({
            'sweeps':      sweeps,
            'server_time': time.time(),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/greek_routing")
def api_debug_greek_routing():
    """Phase 21 (2026-05-01): Diagnostic for explicit Greek source routing.

    Returns counts and samples per source so we can verify:
      - Every print resolved to ONE explicit source (Schwab WS / REST / BSM)
      - schwab_ws_cache_miss + schwab_rest_cache_miss are ~0 in steady state
        (non-zero indicates startup race or Schwab WS reconnect)
      - Routing set sizes match expected subscriptions
    """
    try:
        from background_engine import schwab_bridge as _sb
        ws_set = getattr(_sb, '_SCHWAB_WS_OSIS', set())
        rest_set = getattr(_sb, '_SCHWAB_REST_OSIS', set())
        stats = dict(getattr(_sb, '_GREEK_ROUTING_STATS', {}))
        # Compute totals
        total_routed = sum(v for k, v in stats.items()
                           if k in ('schwab_ws', 'schwab_rest', 'bsm'))
        total_misses = sum(v for k, v in stats.items() if 'cache_miss' in k)
        # Sample 5 symbols from each set for debugging
        ws_sample = list(ws_set)[:5]
        rest_sample = list(rest_set)[:5]
        # Detect overlap
        overlap = ws_set & rest_set
        return jsonify({
            'sets': {
                'schwab_ws_size':   len(ws_set),
                'schwab_rest_size': len(rest_set),
                'overlap_size':     len(overlap),
                'ws_sample':        ws_sample,
                'rest_sample':      rest_sample,
            },
            'routing_stats': stats,
            'totals': {
                'total_routed':       total_routed,
                'total_cache_misses': total_misses,
                'cache_miss_pct':     (
                    round(100 * total_misses / total_routed, 3)
                    if total_routed > 0 else 0
                ),
            },
            'health': {
                'schwab_ws_healthy':   stats.get('schwab_ws_cache_miss', 0) < total_routed * 0.01,
                'schwab_rest_healthy': stats.get('schwab_rest_cache_miss', 0) < total_routed * 0.01,
            },
            'server_time': time.time(),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/sweep_detector/stats")
def api_debug_sweep_detector_stats():
    """Diagnostic counters from connectors/sweep_detector.

    Used to verify the detector is receiving prints (prints_seen),
    classifying correctly (sweeps_detected vs dropped buckets), and
    that the per-underlying buffer is pruning properly.
    """
    try:
        from connectors import sweep_detector as _swd
        return jsonify(_swd.get_stats())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/pin/<ticker>")
def api_intel_pin(ticker):
    """End-of-day pin location prediction for <ticker>.

    Pin = strike where 0DTE price is mechanically pulled toward at expiration
    due to dealer Γ exposure. Source: connectors/pin_convergence.compute_pin_state.

    Live: Socket.IO 'intel:pin_update' is pushed every 15s during last hour
    and 60s otherwise (during RTH). REST returns the same cached state plus
    time-evolution `history` for trajectory rendering.

    Response includes per-strike pin_probability + score components
    (gamma_score, distance_score, oi_score, warehouse_strength, time_amplifier),
    expected_close + 95% CI band (ci_low/ci_high), walls overlay (max_pain,
    gamma_flip, call_wall, put_wall), and data_ts/server_time freshness.
    """
    try:
        from connectors import pin_convergence as _pc
        return jsonify(_pc.get_state(ticker.upper()))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/hedge_forecast/<ticker>")
def api_intel_hedge_forecast(ticker):
    """OBSERVABLE-ONLY hedge state for <ticker>.

    2026-05-04: stripped of directional predictions. Audit on n=1,910 paired
    records showed sign_match 53.3% vs majority-class baseline 62.7% — model
    is 9.4 pts WORSE than constantly predicting positive. Calibration ratio
    median 0.002 (3 orders of magnitude off). See /tmp/hedge_forecaster_audit.py.

    Returns observable Γ-pressure × velocity components only:
      - spot, velocity_per_sec, velocity_cv, velocity_stable
      - distance_to_flip, gamma_flip, hp_gamma_shares_1pct
      - observed_5min_actual / observed_5min_count (equity tape over [T-300, T])

    The full predictive `forecasts` dict is still computed by
    hedge_forecaster.compute_forecast and written to disk ledgers
    (hedge_forecast_outcomes_*, hedge_forecast_paired_*) for offline research.
    It is NOT exposed via REST or socket. Re-enable when a measured-edge
    model replaces the dealer-hedging hypothesis.
    """
    try:
        from connectors import hedge_forecaster as _hf
        full = _hf.get_state(ticker.upper())
        descriptive = {
            'ticker':                full.get('ticker'),
            'spot':                  full.get('spot'),
            'velocity_per_sec':      full.get('velocity_per_sec'),
            'velocity_cv':           full.get('velocity_cv'),
            'velocity_stable':       full.get('velocity_stable'),
            'distance_to_flip':      full.get('distance_to_flip'),
            'gamma_flip':            full.get('gamma_flip'),
            'hp_gamma_shares_1pct':  full.get('hp_gamma_shares_1pct'),
            'observed_5min_actual':  full.get('observed_5min_actual'),
            'observed_5min_count':   full.get('observed_5min_count'),
            'data_ts':               full.get('data_ts'),
            'server_time':           full.get('server_time'),
            'reason':                full.get('reason'),  # for empty-state messaging
            'kind':                  'observable_state',
        }
        return jsonify(descriptive)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/events")
def api_intel_events():
    """Event Calendar — earnings + macro events that drive vol regime expectation.

    Source: connectors/event_calendar.compute_state — reads
    data/event_calendar.json (operator-maintained), reloaded every 60 min.

    Live: Socket.IO 'intel:events' is pushed every 60 min during RTH.

    Response includes:
      - next_event: nearest upcoming event with time_until_sec / hours
      - in_24hr / in_7d: time-bucketed event lists
      - vol_warning: {active, event, hours} — flagged if high-impact within 24hr
      - source: 'json_file' / 'no_data'
      - last_loaded_ts, server_time, reason
    """
    try:
        from connectors import event_calendar as _ec
        return jsonify(_ec.get_state())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/dealer_warehouse")
def api_intel_dealer_warehouse():
    """Dealer Warehouse Quality — per-strike commitment scorer.

    Source: connectors/dealer_warehouse.compute_state — DERIVED from:
      mm_attribution._capture (posted_bid/ask_time, caught_at_top, caught_count)
      via Schwab OPTIONS_BOOK (≤120 contracts in budget) +
      Schwab TIMESALE_OPTIONS / Tradier prints

    Live: Socket.IO 'intel:dealer_warehouse' is pushed every 10s during RTH.

    Response includes:
      - strikes: per-K {posted_time_s, caught_at_top, catch_rate,
        commitment_score, phantom_score, classification, top_exch}
      - top_committed: 5 strikes with highest commitment_score
      - top_phantom: 5 strikes with highest phantom_score
      - totals: aggregate posted_time + caught_at_top + contract_count
      - history: last 20 min of evolution samples
    """
    try:
        from connectors import dealer_warehouse as _dw
        return jsonify(_dw.get_state())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/gamma_skyline")
def api_intel_gamma_skyline():
    """Gamma Skyline — per-strike dealer Γ$ vertical-bar visualization.

    Source: connectors/gamma_skyline.compute_state — DERIVED from:
      schwab_bridge._latest_qqq + _greek_surface.export_hedge_pressure(spot)
      + wall_signals._walls['QQQ']

    Live: Socket.IO 'intel:gamma_skyline' is pushed every 5s during RTH.

    Response includes:
      - spot, atm_strike, band_low/high (ATM ±$VIEWABLE_BAND_DOLLARS)
      - strikes: per-K {dn_gamma, dn_vanna, dn_charm, oi_call, oi_put,
        hp_gamma_shares_1pct, dist_pct, dn_gamma_norm, is_atm}
      - totals: aggregate hedge pressures + dn_gamma_max_abs (bar normalizer)
      - walls: call_wall / put_wall / gamma_flip / gamma_call_wall /
        gamma_put_wall (overlay vertical lines)
      - history: last 20 min of summary samples for evolution tracking
    """
    try:
        from connectors import gamma_skyline as _gs
        return jsonify(_gs.get_state())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/wing_tracker")
def api_intel_wing_tracker():
    """0DTE Wing Tracker — far-OTM call/put aggressor flow classifier.

    Source: connectors/wing_tracker.compute_state — DERIVED from each Tradier
    options-print event (filtered to ANALYSIS_TICKER='QQQ' AND today's DTE).

    Live: Socket.IO 'intel:wing_update' is pushed every 5s during RTH.

    Response includes:
      - spot, dte_key, session_age_sec
      - zones: per-zone {ATM/NEAR_WING/DEEP_WING/TAIL} aggregates
        (total_volume, total_premium, buy/sell counts/sizes, call/put split)
      - top_strikes: 10 most active strikes with aggressor skew
      - recent_prints: last 20 wing prints (size, price, aggressor, zone)
      - regime: NORMAL / ACTIVE / EXTREME / NO_DATA
      - regime_strength [0..1], rationale
      - net_dealer_delta_est_shares (DERIVED proxy)
    """
    try:
        from connectors import wing_tracker as _wt
        return jsonify(_wt.get_state())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/regression")
def api_debug_regression():
    """Surface latest regression-runner summary. Reads logs/regression_summary.json
    written by scripts/regression_runner.py (cron'd weekly).

    Returns the headline per-panel hit-rates so the operator can check
    CONFIGURED-constant validation status without re-running the script.
    """
    try:
        path = os.path.join(os.path.dirname(__file__), 'logs', 'regression_summary.json')
        if not os.path.exists(path):
            return jsonify({
                'available': False,
                'reason':    'no regression report yet — run scripts/regression_runner.py',
            })
        with open(path, 'r') as f:
            data = json.load(f)
        data['available'] = True
        return jsonify(data)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/wing_tracker/stats")
def api_debug_wing_tracker_stats():
    """Diagnostic counters for wing_tracker (print accept/reject rate)."""
    try:
        from connectors import wing_tracker as _wt
        return jsonify(_wt.get_stats())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/vix_term")
def api_intel_vix_term():
    """Cross-asset vol regime dashboard — VIX-family + cross-asset vol comparator.

    Source: connectors/vix_term_structure.compute_state — DERIVED from:
      schwab_bridge._latest_spot_by_ticker[VIX/VIX1D/VVIX/VXN/RVX/VXD/VXEEM/
                                            SKEW/OVX/GVZ/TNX]

    Live: Socket.IO 'intel:vix_term' is pushed every 10s during RTH.

    Response includes:
      - tickers: per-symbol live spot
      - spreads: vxn-vix, rvx-vix, vxd-vix, vxeem-vix, vix1d-vix
      - ratios: vix1d/vix (backwardation indicator), vvix/vix (institutional bid)
      - regime: classifier result {CALM_CONTANGO/NORMAL/TECH_DIVERGENCE/
                ELEVATED/STRESS_CONTANGO/STRESS_BACKWARDATION/VVIX_DIVERGENCE/
                NO_DATA}
      - regime_strength [0..1], rationale (human-readable)
      - history: last 60 min of samples for trajectory display
    """
    try:
        from connectors import vix_term_structure as _vts
        return jsonify(_vts.get_state())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/spx_qqq_divergence")
def api_intel_spx_qqq_divergence():
    """SPX-vs-QQQ option-flow regime divergence comparator.

    Source: connectors/spx_qqq_divergence.compute_state — DERIVED from:
      QQQ: greek_surface.export_hedge_pressure (totals + per-strike) + wall_signals
      SPX: _per_ticker_gex['SPX'] aggregation + _compute_walls_for('SPX')

    Live: Socket.IO 'intel:spx_qqq_divergence' is pushed every 10s during RTH.

    Response includes:
      - spx / qqq snapshot (spot, gamma_flip, distance_to_flip_pct, regime,
        hp_gamma_shares_1pct, walls, pcr_oi, net_dealer_gamma_dollars)
      - divergence: verdict (ALIGNED_BULL/ALIGNED_BEAR/DIVERGENT_REGIME/
        DIVERGENT_MAGNITUDE/NEUTRAL/NO_DATA), strength [0..1],
        regime_aligned, magnitude_ratio, flip_distance_diff_pct, rationale
      - history: last 60 min of samples for trajectory display
    """
    try:
        from connectors import spx_qqq_divergence as _sqd
        return jsonify(_sqd.get_state())
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/intel/svi/<ticker>")
def api_intel_svi(ticker):
    # Init rate-limit cache on first call (function-attr pattern; thread-safe
    # under gevent since dict assignment is atomic in CPython)
    if not hasattr(api_intel_svi, '_last_ledger_ts'):
        api_intel_svi._last_ledger_ts = {}
    """SVI volatility surface fit + per-strike residuals (Phase 20A).

    Lifts arbitrage-free SVI parametrisation (Gatheral 2004 + Gatheral &
    Jacquier 2014) from Nguyen (2025) "Regime-Adaptive Volatility Surface
    Arbitrage" with our total-variance objective fix for small-T stability.

    Query params:
        exp:  expiration date YYYY-MM-DD (default: nearest expiry)

    Source: connectors/svi_surface.compute_svi_state — feeds raw Schwab
    chain through SVI calibration. RMSE typically 18-41bp on QQQ chains
    at DTE [0, 3, 14, 21] (validated by scripts/svi_live_smoke.py).

    Response:
        ticker, exp_date, spot, T_years, dte
        params: {a, b, rho, m, sigma}
        rmse_bp, pass_rmse, butterfly_arb
        strikes: per-K {K, k, side, iv_obs, iv_fit, residual_bp, vega_weight, oi, volume, delta}
        aggregate_residual (vega-weighted mean residual in bp)
        aggregate_z (z-score over rolling 20-sample window)
        data_ts
    """
    from datetime import datetime, date
    ticker = ticker.upper()
    req_exp = request.args.get('exp', '').strip()

    try:
        # Resolve expiration
        raw_dates = _schwab_expirations(ticker)
        if not raw_dates:
            return jsonify({'error': f'No expirations found for {ticker}'}), 404

        today = date.today()
        if req_exp and req_exp in raw_dates:
            exp_date = req_exp
        else:
            exp_date = raw_dates[0]

        try:
            dte = (datetime.strptime(exp_date, '%Y-%m-%d').date() - today).days
        except Exception:
            dte = 0

        # Fetch chain
        raw_chain, schwab_underlying = _schwab_chain_raw(ticker, exp_date)
        if not raw_chain:
            return jsonify({'error': f'Empty chain for {ticker} {exp_date}'}), 404

        spot = float(schwab_underlying) if schwab_underlying else float(_schwab_quote(ticker) or 0)
        if spot <= 0:
            return jsonify({'error': f'Cannot resolve spot for {ticker}'}), 503

        # Map raw Schwab fields to our SVI input format
        chain_for_svi = []
        for c in raw_chain:
            iv_decimal = c.get('volatility')   # Schwab: decimal (e.g. 0.20 = 20%)
            if iv_decimal is None:
                continue
            try:
                iv_pct = float(iv_decimal)     # Schwab "volatility" is % (e.g. 20.0 not 0.20)
            except (TypeError, ValueError):
                continue
            chain_for_svi.append({
                'strike':  c.get('strike'),
                'type':    c.get('option_type'),
                'iv':      iv_pct,
                'oi':      c.get('open_interest', 0) or 0,
                'volume':  c.get('volume', 0) or 0,
                'delta':   c.get('delta', 0) or 0,
            })

        from connectors.svi_surface import compute_svi_state, append_outcome_record
        state = compute_svi_state(
            ticker=ticker,
            exp_date=exp_date,
            spot=spot,
            chain=chain_for_svi,
            dte=dte,
        )

        # Append to outcome ledger — rate-limited to 1 record per 30s per
        # (ticker, exp_date) key. Audit issue #11 (2026-05-01): without this,
        # multi-pane polling at 30s interval would write N×panes/min instead
        # of 2/min, bloating the ledger over a session.
        if 'error' not in state:
            _now = time.time()
            _key = f"{ticker}:{exp_date}"
            _last = api_intel_svi._last_ledger_ts.get(_key, 0)  # type: ignore[attr-defined]
            if _now - _last >= 30.0:
                api_intel_svi._last_ledger_ts[_key] = _now  # type: ignore[attr-defined]
                try:
                    append_outcome_record({
                        'ts':                     state['data_ts'],
                        'ticker':                 ticker,
                        'exp_date':               exp_date,
                        'dte':                    dte,
                        'spot':                   state['spot'],
                        'rmse_bp':                state['rmse_bp'],
                        'aggregate_residual_bp':  state['aggregate_residual'],
                        'aggregate_z':            state.get('aggregate_z'),
                        'samples_used':           state['samples_used'],
                        'butterfly_arb':          state['butterfly_arb'],
                        'params':                 state['params'],
                    })
                except Exception:
                    pass

        return jsonify(state)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/conviction/<ticker>")
def api_conviction(ticker):
    """Composite Conviction Score (CCS) for <ticker>.

    Synthesizes regime, hedge pressure, options flow, MM attribution, time-of-day
    and cross-asset confirm into a single 0–100 score with direction + size
    recommendation. Source: connectors/conviction_score.py. Live cadence 5s.
    """
    try:
        from connectors.conviction_score import get_scorer
        s = get_scorer()
        if s is None:
            return jsonify({'error': 'scorer not initialized'}), 503
        return jsonify(s.get_state(ticker.upper()))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/conviction")
def api_conviction_all():
    """All tickers' CCS state (currently QQQ only; SPY coming next)."""
    try:
        from connectors.conviction_score import get_scorer
        s = get_scorer()
        if s is None:
            return jsonify({'tickers': {}})
        return jsonify({'tickers': s.get_all_states()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route("/api/option_flow/oi_split/<ticker>")
def api_option_flow_oi_split(ticker):
    """Opening vs Closing flow split via streaming OI velocity classification.

    Returns:
      {
        ticker,
        opening_long_signed,   closing_short_signed,
        opening_short_signed,  closing_long_signed,
        unknown_signed,
        opening_signed_flow,   closing_signed_flow,
        oi_classified_share,
      }
    Pan & Poteshman (2006) — opening flow has next-day forward return alpha;
    closing flow does not. Use opening_signed_flow as the directional alpha
    component, not raw cum_signed_all.
    """
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({'error': 'flow_accumulator not initialized'}), 503
        states = acc.get_all_states() or {}
        s = states.get(ticker.upper())
        if not s:
            return jsonify({'ticker': ticker.upper(), 'note': 'no data'}), 200
        return jsonify({
            'ticker': ticker.upper(),
            'cohort_opening_long_signed':  s.get('cohort_opening_long_signed'),
            'cohort_opening_long_trades':  s.get('cohort_opening_long_trades'),
            'cohort_closing_short_signed': s.get('cohort_closing_short_signed'),
            'cohort_closing_short_trades': s.get('cohort_closing_short_trades'),
            'cohort_opening_short_signed': s.get('cohort_opening_short_signed'),
            'cohort_opening_short_trades': s.get('cohort_opening_short_trades'),
            'cohort_closing_long_signed':  s.get('cohort_closing_long_signed'),
            'cohort_closing_long_trades':  s.get('cohort_closing_long_trades'),
            'cohort_unknown_signed':       s.get('cohort_unknown_signed'),
            'cohort_unknown_trades':       s.get('cohort_unknown_trades'),
            'opening_signed_flow':         s.get('opening_signed_flow'),
            'closing_signed_flow':         s.get('closing_signed_flow'),
            'unknown_signed_flow':         s.get('unknown_signed_flow'),
            'oi_classified_share':         s.get('oi_classified_share'),
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/option_flow/by_exchange/<ticker>")
def api_option_flow_by_exchange(ticker):
    """Per-venue MIC flow attribution for a ticker.

    Returns top venues sorted by |signed flow| with their share %, plus
    concentration_score (top1 share). Concentration ≥ 0.60 = institutional
    algo single-venue routing. < 0.30 = retail spread across many brokers.
    Source: connectors/flow_accumulator.get_by_exchange.
    """
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({'error': 'flow_accumulator not initialized'}), 503
        top_n = max(1, min(50, int(request.args.get('top_n', 10))))
        return jsonify(acc.get_by_exchange(ticker.upper(), top_n=top_n))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/option_flow/mispricing/<ticker>")
def api_option_flow_mispricing(ticker):
    """Theoretical-vs-Mark mispricing snapshot for a ticker.

    Surfaces strikes where the trade-weighted average premium-to-theoretical
    is significantly non-zero. ≥+3% = institutional accumulation paying above
    BSM fair value. ≤-3% = forced/distressed selling below fair value.
    Source: connectors/flow_accumulator.get_mispricing.
    """
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return jsonify({'error': 'flow_accumulator not initialized'}), 503
        top_n = max(1, min(50, int(request.args.get('top_n', 10))))
        return jsonify(acc.get_mispricing(ticker.upper(), top_n=top_n))
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/mm_attribution/contracts")
def api_mm_attribution_contracts():
    """Ranked list of contracts tracked by mm_attribution. Ranking metric is
    chosen via `metric` query param: events | prints | formations. Result
    size bounded by `limit` (default 50) — pure display cap, not a filter
    that discards structural events.
    """
    try:
        from connectors import mm_attribution as _mma
        metric = (request.args.get("metric") or "events").lower()
        limit = max(1, min(500, int(request.args.get("limit", 50))))
        rows = _mma.rank_contracts(metric=metric, limit=limit)
        return jsonify({
            "summary": _mma.module_summary(),
            "metric": metric,
            "contracts": rows,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/mm_attribution/contract/<path:sym>")
def api_mm_attribution_contract(sym):
    """Full live state for one contract — ribbon, formations, capture, last
    impulse. Polled every ~1s by the pane while the user has a contract
    locked. Auth is automatic via `before_request`."""
    try:
        from connectors import mm_attribution as _mma
        return jsonify(_mma.contract_state(sym))
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/mm_attribution/impulse/<path:sym>")
def api_mm_attribution_impulse(sym):
    """Lookup a specific closed impulse for a contract by `print_ts`. Used
    by the pane's prev/next navigation over the impulse history. Without
    `print_ts`, returns the list of recent impulses for that contract."""
    try:
        from connectors import mm_attribution as _mma
        pts = request.args.get("print_ts")
        if pts:
            rec = _mma.impulse_for_print(sym, float(pts))
            return jsonify(rec or {})
        limit = max(1, min(500, int(request.args.get("limit", 50))))
        return jsonify({"impulses": _mma.impulse_list(sym, limit=limit)})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/wall_signals/sym_cache_counts")
def api_debug_wall_signals_sym_cache_counts():
    """Group sym_cache keys by OSI underlying prefix (first token).
    Reveals whether SPX / SPY options are polluting the _live_gex strikes."""
    try:
        from background_engine import schwab_bridge as _sb
        sym_cache = getattr(_sb._on_options_quote, '_sym_cache', {})
        counts: dict = {}
        for sym in sym_cache.keys():
            prefix = (sym.split()[0] if sym else '').upper()
            counts[prefix] = counts.get(prefix, 0) + 1
        # Also inspect the strike padding of each underlying — last 8 chars of OSI.
        # A QQQ spot-area strike prints as e.g. "00660000"; SPX prints as "07000000".
        samples = {}
        for sym in sym_cache.keys():
            prefix = (sym.split()[0] if sym else '').upper()
            if prefix not in samples:
                samples[prefix] = sym
        return jsonify({
            'counts': counts,
            'samples': samples,
            'total_symbols': len(sym_cache),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/wall_signals/symbols")
def api_debug_wall_signals_symbols():
    """Look up which Schwab option symbols feed a given strike in _live_gex.
    Query: ?strike=7000 — returns matching symbols in the sym_cache. Lets us
    tell 'SPX leakage' from 'QQQ LEAP w/ adjusted strike' at a glance."""
    try:
        from background_engine import schwab_bridge as _sb
        strike_q = request.args.get("strike")
        if not strike_q:
            return jsonify({"error": "pass ?strike=<float>"}), 400
        strike_target = float(strike_q)
        sym_cache = getattr(_sb._on_options_quote, '_sym_cache', {})
        matches = []
        for sym, rec in sym_cache.items():
            s = rec.get('strike') or 0
            if abs(float(s or 0) - strike_target) < 1e-6:
                matches.append({
                    'symbol': sym,
                    'strike': s,
                    'contract_type': rec.get('contract_type'),
                    'open_interest': rec.get('open_interest'),
                    'underlying': rec.get('underlying_symbol', rec.get('underlying')),
                    'dte': rec.get('dte'),
                    'mark': rec.get('mark'),
                })
        return jsonify({
            'strike': strike_target,
            'match_count': len(matches),
            'matches': matches[:20],
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/wall_signals/walls")
def api_debug_wall_signals_walls():
    """Dump current internal _walls state of connectors.wall_signals plus the
    top-5 OI strikes from schwab_bridge._live_gex. Lets us compare what
    wall_signals is seeing vs. what the raw GEX map says. No auth (under
    /api/_debug)."""
    try:
        from connectors import wall_signals as _ws
        from background_engine import schwab_bridge as _sb
        live_gex = getattr(_sb, '_live_gex', {}) or {}
        top_call = sorted(live_gex.items(), key=lambda kv: -(kv[1] or {}).get('call_oi', 0))[:8]
        top_put  = sorted(live_gex.items(), key=lambda kv: -(kv[1] or {}).get('put_oi', 0))[:8]
        return jsonify({
            'wall_signals_walls': _ws._walls,
            'wall_signals_spot':  _ws._spot,
            'live_gex_strike_count': len(live_gex),
            'top_call_oi': [{'strike': k, 'call_oi': v.get('call_oi'), 'put_oi': v.get('put_oi')}
                            for k, v in top_call],
            'top_put_oi':  [{'strike': k, 'call_oi': v.get('call_oi'), 'put_oi': v.get('put_oi')}
                            for k, v in top_put],
            'latest_qqq': getattr(_sb, '_latest_qqq', 0.0),
            'latest_nq':  getattr(_sb, '_latest_nq', 0.0),
            'ratio':      getattr(_sb, '_nq_qqq_ratio', 0.0),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/wall_signals/state")
def api_wall_signals_state():
    """Current wall_signals snapshot for the requested ticker (default QQQ).
    Query params: ?ticker=QQQ&proximity_pct=0.0025&lookback_sec=60 — only the
    `ticker` param is required; the others default to CONFIGURED module values
    (see connectors/wall_signals.py). All three params are runtime choices —
    no internal magnitude thresholds."""
    try:
        from connectors import wall_signals as _ws
        ticker = _resolve_index_ticker(request.args.get("ticker", "QQQ"))
        prox = request.args.get("proximity_pct")
        lookback = request.args.get("lookback_sec")
        kwargs = {}
        if prox is not None:
            kwargs["proximity_pct"] = float(prox)
        if lookback is not None:
            kwargs["lookback_sec"] = float(lookback)
        return jsonify(_ws.get_state(ticker, **kwargs))
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/wall_signals/ledger")
def api_wall_signals_ledger():
    """Ground-truth hit-rate summary for wall_signals crossings.

    Bucket 1 (actionable): fire-time C or F ≥ actionable_threshold
    Bucket 2 (baseline):   fire-time scores below threshold

    The GAP (actionable_rate − baseline_rate) is the measured edge. When
    <10 entries exist, stats are not meaningful — the UI should show the
    total and a "collecting data…" state.

    Query params (all optional, runtime-chosen, no CONFIGURED defaults baked
    into the math):
      hours                — window to compute stats over (default 24)
      actionable_threshold — score floor for "actionable" bucket (default 0.3)
      hit_delta_nq         — signed NQ move to count as a hit (default 10)
      limit                — number of recent entries to return (default 20)
    """
    try:
        from connectors import signal_ledger as _slg
        hours = float(request.args.get("hours", 24.0))
        at = request.args.get("actionable_threshold")
        hd = request.args.get("hit_delta_nq")
        limit = int(request.args.get("limit", 20))
        at_f = float(at) if at is not None else None
        hd_f = float(hd) if hd is not None else None
        return jsonify({
            "summary": _slg.get_hit_rate(
                hours=hours,
                actionable_threshold=at_f,
                hit_delta_nq=hd_f,
            ),
            "recent": _slg.get_recent(limit=limit),
            "wall_state": _slg.get_wall_state(
                _resolve_index_ticker(request.args.get("ticker", "QQQ"))
            ),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/wall_signals/ledger_raw")
def api_debug_wall_signals_ledger_raw():
    """Unfiltered ledger dump for debugging the outcome tracker. No auth
    (under /api/_debug)."""
    try:
        from connectors import signal_ledger as _slg
        return jsonify({
            "entries": list(_slg._ledger),
            "wall_state_all": {
                f"{k[0]}:{k[1]}": v for k, v in _slg._wall_state.items()
            },
            "cap": _slg.LEDGER_CAP,
            "actionable_threshold": _slg.ACTIONABLE_THRESHOLD,
            "hit_delta_nq": _slg.HIT_DELTA_NQ,
            "windows_min": list(_slg.WINDOWS_MIN),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/chain_rotation/stats")
def api_debug_chain_rotation_stats():
    """Live status of the REST chain rotation (cap-blocked tail backfill).
    Reports: enabled flag, today's request count vs daily budget, last cycle
    timestamp + per-ticker merge counts, 429 streak + last 429 ts.
    Used to verify Schwab REST polling stays under safety thresholds.
    """
    try:
        import time as _time
        from background_engine import schwab_bridge as _sb
        return jsonify({
            'enabled':                getattr(_sb, '_chain_rotation_enabled', False),
            'thread_started':         getattr(_sb, '_chain_rotation_thread_started', False),
            'is_rth_now':             _sb._is_rth_now() if hasattr(_sb, '_is_rth_now') else None,
            'cycle_interval_s':       getattr(_sb, '_CHAIN_ROTATION_INTERVAL_S', None),
            'tickers':                getattr(_sb, '_CHAIN_ROTATION_TICKERS', []),
            'requests_today':         getattr(_sb, '_chain_rotation_request_count', 0),
            'daily_budget':           getattr(_sb, '_CHAIN_ROTATION_DAILY_BUDGET', 0),
            'budget_pct_used':        round(
                100 * getattr(_sb, '_chain_rotation_request_count', 0) /
                max(getattr(_sb, '_CHAIN_ROTATION_DAILY_BUDGET', 1), 1), 2
            ),
            'last_cycle_ts':          getattr(_sb, '_chain_rotation_last_cycle_ts', 0.0),
            'last_cycle_age_s':       round(
                _time.time() - getattr(_sb, '_chain_rotation_last_cycle_ts', 0.0), 1
            ) if getattr(_sb, '_chain_rotation_last_cycle_ts', 0.0) > 0 else None,
            'last_merge_per_ticker':  dict(getattr(_sb, '_chain_rotation_last_merge_count', {})),
            'lifetime_merged':        getattr(_sb, '_chain_rotation_lifetime_merged', 0),
            '429_streak':             getattr(_sb, '_chain_rotation_429_streak', 0),
            'last_429_ts':            getattr(_sb, '_chain_rotation_last_429_ts', 0.0),
            'last_reset_date':        getattr(_sb, '_chain_rotation_last_reset_date', None),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/tradier/equity_stats")
def api_debug_tradier_equity_stats():
    """Live Tradier equity-timesale counters per ticker. Verifies the
    TIMESALE_EQUITY code=11 gap-fill is firing (QQQ + SPY equity tape
    via Tradier WS, not L1 size-delta synthesis)."""
    try:
        import time as _time
        from background_engine import schwab_bridge as _sb
        return jsonify({
            'subscribed':         list(getattr(_sb, '_TRADIER_EQUITY_TICKERS', ())),
            'counters':           dict(getattr(_sb, '_tradier_equity_timesale_stats', {})),
            'recent_buffer_sizes': {
                sym: len(buf) for sym, buf in
                getattr(_sb, '_recent_equity_prints', {}).items()
            },
            'retention_s':        getattr(_sb, 'EQUITY_PRINT_RETENTION_S', None),
            'server_ts':          _time.time(),
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/tradier/stats")
def api_debug_tradier_stats():
    """Live Tradier streamer health + timesale counters (total / enriched /
    bare / qqq). Used to verify the WS stays connected and QQQ prints flow."""
    try:
        import time as _time
        from background_engine import schwab_bridge as _sb
        streamer = getattr(_sb, '_tradier_streamer', None)
        ws = streamer.stats() if streamer else {'running': False}
        ts_stats = dict(getattr(_sb, '_tradier_timesale_stats', {}))
        last_msg = getattr(streamer, '_last_msg_ts', 0.0) if streamer else 0.0
        # Flow accumulator feed diagnostics (post-Tradier-feed fix)
        _ott = getattr(_sb, '_on_tradier_timesale', None)
        flow_diag = dict(getattr(_ott, '_flow_diag', {})) if _ott else {}
        return jsonify({
            'ws': ws,
            'last_msg_ts': last_msg,
            'silence_s': (_time.time() - last_msg) if last_msg > 0 else -1,
            'timesale_counters': ts_stats,
            'flow_accumulator_feed_diag': flow_diag,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/dealer_prints/summary")
def api_debug_dealer_prints_summary():
    """Descriptive distributions over recent dealer prints — NO thresholds.
    `window_s` query param (default 300). See connectors/dealer_print_capture.py
    for the capture schema."""
    try:
        from connectors import dealer_print_capture as _dpc
        window_s = float(request.args.get("window_s", 300))
        return jsonify(_dpc.live_summary(window_s=window_s))
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/dealer_prints/recent")
def api_debug_dealer_prints_recent():
    """Last N captured prints with top-of-book context for live panel display.
    `n` query param (default 100, max 10000 — was 500 prior to 2026-05-04)."""
    try:
        from connectors import dealer_print_capture as _dpc
        n = max(1, min(10000, int(request.args.get("n", 100))))
        return jsonify({"prints": _dpc.recent_prints(n=n)})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/capture_rate")
def api_debug_capture_rate():
    """Live capture-rate metrics for the dealer-print pipeline. Replaces the
    expensive disk-log audits as a continuous SLA monitor. See
    `connectors/dealer_print_capture.capture_rate()` for field definitions.

    Healthy session shape (steady-state, >30 min after start):
        rate ≈ 1 − pending / in_total
    A drift > 0.001 suggests prints are being dropped between input and disk.
    A non-zero stale_pending means the flush loop is failing to write entries
    whose enrichment window already closed.

    2026-05-05 — added `tradier_conns` array. Each entry exposes per-conn
    uptime/disconnect history so reconnects are visible without log-grep.
    """
    try:
        from connectors import dealer_print_capture as _dpc
        out = _dpc.capture_rate()
        try:
            from background_engine import schwab_bridge as _sb
            out['tradier_conns'] = _sb.get_tradier_conn_stats()
        except Exception as e:
            out['tradier_conns'] = {'error': str(e)[:120]}
        return jsonify(out)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/_debug/alert_samples")
def api_debug_alert_samples():
    """PUBLIC — dump AlertEngine per-ticker 1Hz samples (ts, s0, sa, u0, ua, spot).
    Used for post-hoc verification of forward spot movement after specific alerts."""
    try:
        from connectors.alert_engine import get_engine
        eng = get_engine()
        if eng is None:
            return jsonify({"ready": False})
        ticker = _resolve_index_ticker(request.args.get("ticker", "QQQ"))
        with eng._lock:
            h = eng._history.get(ticker)
            if not h:
                return jsonify({"ready": True, "ticker": ticker, "samples": []})
            # Samples are (ts, s0, sa, u0, ua, spot)
            samples = [
                {"ts": s[0], "s0": s[1], "sa": s[2], "spot": s[5]}
                for s in h.samples
            ]
        return jsonify({"ready": True, "ticker": ticker, "n": len(samples), "samples": samples})
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


@app.route("/api/alerts/outcomes")
def api_alerts_outcomes():
    """Realized hit rates for fired alerts per (ticker, type, bucket, horizon).
    Each alert's direction is checked against spot(t+300s), +900s, +1800s.
    Returns empirical win rate, avg |move%|, and expectancy (signed move).
    Query param: days=N (default 7)."""
    try:
        from connectors.alert_engine import get_engine
        eng = get_engine()
        if eng is None:
            return jsonify({"ready": False, "outcomes": {}})
        days = int(request.args.get('days', 7))
        days = max(1, min(days, 90))
        return jsonify({
            "ready": True,
            "server_time": time.time(),
            "days": days,
            "horizons_sec": [300, 900, 1800],
            "outcomes": eng.get_hit_rates(last_n_days=days),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/_debug/vix_term_structure")
def api_debug_vix_term_structure():
    """Implied VIX term structure via put-call parity on VIX options.

    Schwab does NOT support /VX futures market data (every /VX* symbol
    returns invalidSymbols). To still get the term curve we use the
    well-known dealer technique: the underlying for VIX options at a
    given expiration IS the VIX FUTURE for that expiration. Put-call
    parity at the ATM strike recovers the forward:

        F(T) ≈ C(K_atm, T) − P(K_atm, T) + K_atm × e^(−r·T)

    where r ≈ short-rate (we use $IRX). For short-dated VIX options
    (DTE < 60), the discount factor is ~1.000 to 0.997 — sub-cent
    impact on F. We carry it for completeness.

    Returns a curve [{exp, dte, atm_strike, call_mark, put_mark,
    implied_forward, vs_spot}] sorted by DTE.
    """
    try:
        import math, time as _t
        spot_vix = float(_schwab_quote('$VIX') or 0)
        if spot_vix <= 0:
            return jsonify({'error': 'spot VIX unavailable', 'spot': 0}), 503
        # Risk-free rate from $IRX (CBOE 13-week T-bill).
        # CBOE convention: $IRX is published as yield × 10 (e.g. 35.9 = 3.59%
        # actual yield). Same for $TNX (43.62 = 4.362% 10-year). To get the
        # decimal rate for e^(-r·T) discounting: IRX / 1000.
        # Earlier code used /100.0 which gave a 10× inflated rate (35.9%) and
        # over-discounted the back-month implied forwards by ~10%.
        try:
            irx = float(_schwab_quote('$IRX') or 0)
            r = irx / 1000.0  # CBOE × 10 convention
            # Sanity clamp: real short rates 0–10%; reject obviously bad data
            if not (0.0 < r < 0.10):
                r = 0.045
        except Exception:
            r = 0.045  # fallback
        try:
            exps = _schwab_expirations('$VIX') or []
        except Exception as _e:
            return jsonify({'error': f'expirations fetch failed: {_e}'}), 500
        rows = []
        # Walk every expiration Schwab returns (typically ~13 weeklies out
        # to 9 months). Each chain fetch is one REST call (~50-150ms), so
        # the full curve takes ~1-2 seconds — acceptable for a debug endpoint
        # that's polled occasionally rather than per-tick.
        for exp_date in exps:
            try:
                chain, underlying = _schwab_chain_raw('$VIX', exp_date)
            except Exception:
                continue
            if not chain:
                continue
            # Find ATM strike (closest to spot VIX)
            strikes = sorted({float(c['strike']) for c in chain if c.get('strike', 0) > 0})
            if not strikes:
                continue
            atm = min(strikes, key=lambda k: abs(k - spot_vix))
            call = next((c for c in chain
                         if abs(float(c.get('strike', 0)) - atm) < 0.01
                         and c.get('option_type') == 'call'), None)
            put  = next((c for c in chain
                         if abs(float(c.get('strike', 0)) - atm) < 0.01
                         and c.get('option_type') == 'put'), None)
            if not call or not put:
                continue
            call_mark = float(call.get('mark') or call.get('last') or 0)
            put_mark  = float(put.get('mark')  or put.get('last')  or 0)
            dte = int(call.get('dte') or 0)
            # ── Book-staleness check via streamer cache ──
            # If the displayed bid/ask is wide but indicative is tight,
            # Schwab's `mark` from REST may be a stale midpoint. Pull the
            # latest streamer-side `book_indicative_mid` and use it
            # instead when book_stale=True. Falls back to mark cleanly.
            try:
                from background_engine import schwab_bridge as _sb
                _cache = getattr(_sb._on_options_quote, '_sym_cache', {}) or {}
                _csym = call.get('symbol', '')
                _psym = put.get('symbol', '')
                _ccache = _cache.get(_csym) or {}
                _pcache = _cache.get(_psym) or {}
                if _ccache.get('book_stale') and _ccache.get('book_indicative_mid'):
                    call_mark = float(_ccache['book_indicative_mid'])
                if _pcache.get('book_stale') and _pcache.get('book_indicative_mid'):
                    put_mark = float(_pcache['book_indicative_mid'])
                _book_stale = bool(_ccache.get('book_stale') or _pcache.get('book_stale'))
            except Exception:
                _book_stale = False
            # Discount factor e^(-r·T) where T = dte/365
            T = dte / 365.0
            disc = math.exp(-r * T)
            implied_fwd = call_mark - put_mark + atm * disc
            rows.append({
                'exp':             exp_date,
                'dte':             dte,
                'atm_strike':      round(atm, 2),
                'call_mark':       round(call_mark, 4),
                'put_mark':        round(put_mark, 4),
                'discount_factor': round(disc, 6),
                'implied_forward': round(implied_fwd, 4),
                'vs_spot':         round(implied_fwd - spot_vix, 4),
                'book_stale':      _book_stale,  # marks expirations whose ATM book was gapped
            })
        rows.sort(key=lambda x: x['dte'])
        # Spreads
        spreads = {}
        if len(rows) >= 2:
            spreads['front_minus_second'] = round(rows[0]['implied_forward'] - rows[1]['implied_forward'], 4)
        if len(rows) >= 4:
            spreads['front_minus_fourth'] = round(rows[0]['implied_forward'] - rows[3]['implied_forward'], 4)
        # Regime tag
        regime = 'unknown'
        if len(rows) >= 2:
            if rows[0]['implied_forward'] < rows[1]['implied_forward']:
                regime = 'contango'  # back > front = normal
            elif rows[0]['implied_forward'] > rows[1]['implied_forward']:
                regime = 'backwardation'  # front > back = stress
            else:
                regime = 'flat'
        return jsonify({
            'spot_vix':       round(spot_vix, 2),
            'risk_free_rate': round(r, 4),
            'curve':          rows,
            'spreads':        spreads,
            'regime':         regime,
            'ts':             _t.time(),
            'note':           'Implied via put-call parity on VIX options chain. /VX futures unavailable on Schwab.',
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/vol_indices")
def api_debug_vol_indices():
    """Live dashboard of every CBOE vol-related index Schwab is streaming.

    Surfaces each index with its current value + structural interpretation:
      VIX     CBOE 30-day SPX vol             (the headline number)
      VXN     CBOE 30-day NDX vol             (more relevant for QQQ hedging)
      VVIX    Vol-of-VIX-options              (institutional tail-hedge demand)
      SKEW    OTM-put crash premium           (>135 = elevated tail risk)
      VIX1D   1-day VIX (overnight gap vol)   (front-of-curve)

    Plus computed derivatives:
      VXN/VIX ratio   structural NDX-vs-SPX vol bias (typically 1.20-1.40)
      VIX1D/VIX       overnight discount/premium
      VVIX regime     calm <90, normal 90-100, stressed 100-120, panic >120
      SKEW regime     normal <130, elevated 130-140, crash-hedge >140
    """
    try:
        import time as _t
        from background_engine import schwab_bridge as _sb
        cache = getattr(_sb, '_latest_spot_by_ticker', {}) or {}
        vix    = float(cache.get('VIX', 0) or 0)
        vxn    = float(cache.get('VXN', 0) or 0)
        vvix   = float(cache.get('VVIX', 0) or 0)
        skew   = float(cache.get('SKEW', 0) or 0)
        vix1d  = float(cache.get('VIX1D', 0) or 0)

        # Regime labels (structural, not magic-number tuned — these are
        # CBOE-published regime bands).
        def _vvix_regime(v):
            if v <= 0: return 'unknown'
            if v < 90: return 'calm'
            if v < 100: return 'normal'
            if v < 120: return 'stressed'
            return 'panic'
        def _skew_regime(s):
            if s <= 0: return 'unknown'
            if s < 130: return 'normal'
            if s < 140: return 'elevated'
            return 'crash_hedge_bid'

        return jsonify({
            'ts':    _t.time(),
            'indices': {
                'VIX':   {'last': vix,   'desc': 'CBOE SPX 30-day implied vol'},
                'VXN':   {'last': vxn,   'desc': 'CBOE NDX 30-day implied vol'},
                'VVIX':  {'last': vvix,  'desc': 'Vol-of-VIX (vol on VIX options)',
                          'regime': _vvix_regime(vvix)},
                'SKEW':  {'last': skew,  'desc': 'OTM-put tail-risk premium',
                          'regime': _skew_regime(skew)},
                'VIX1D': {'last': vix1d, 'desc': '1-day VIX (overnight gap vol)'},
            },
            'derived': {
                'vxn_minus_vix':    round(vxn - vix, 3) if (vxn and vix) else None,
                'vxn_over_vix':     round(vxn / vix, 3) if (vxn and vix) else None,
                'vix1d_minus_vix':  round(vix1d - vix, 3) if (vix1d and vix) else None,
                'vix1d_over_vix':   round(vix1d / vix, 3) if (vix1d and vix) else None,
            },
            'note':  'All 5 indices stream from Schwab LEVELONE_EQUITIES with '
                     'realtime=true. VVIX/SKEW/VIX1D added 2026-04-28.',
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/vxn_history")
def api_debug_vxn_history():
    """Historical VXN bars via Schwab /pricehistory.

    Schwab carries spot $VXN (we stream it live) but not VXN options.
    /pricehistory works for the index symbol — pulls daily/intraday bars
    for realized-vol-of-VXN computations + IV-RV spread tracking.

    Query params:
      ?period=1   period count (default 1)
      ?period_type=month   day/month/year/ytd (default month)
      ?freq=daily          minute/daily/weekly/monthly (default daily)
      ?freq_count=1        (default 1)
    """
    try:
        period       = int(request.args.get('period', 1))
        period_type  = request.args.get('period_type', 'month')
        freq         = request.args.get('freq', 'daily')
        freq_count   = int(request.args.get('freq_count', 1))
        # /pricehistory accepts $VXN since the spot streams (verified)
        data = _schwab_get('/marketdata/v1/pricehistory', {
            'symbol':              '$VXN',
            'periodType':          period_type,
            'period':              period,
            'frequencyType':       freq,
            'frequency':           freq_count,
            'needExtendedHoursData': 'false',
        })
        if not data or 'candles' not in data:
            return jsonify({'error': 'no candles returned', 'raw': data}), 503
        candles = data.get('candles', []) or []
        # Compute realized vol from log returns of close prices
        import math as _m
        rv_pct = None
        if len(candles) >= 5:
            closes = [c.get('close', 0) for c in candles if c.get('close', 0) > 0]
            if len(closes) >= 5:
                log_rets = [_m.log(closes[i] / closes[i-1])
                            for i in range(1, len(closes))]
                mean_r = sum(log_rets) / len(log_rets)
                var = sum((r - mean_r) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
                # Annualize: daily bars → ×sqrt(252); minute bars → ×sqrt(252×6.5×60)
                if freq == 'daily':
                    annualizer = _m.sqrt(252)
                elif freq == 'minute':
                    annualizer = _m.sqrt(252 * 6.5 * 60 / freq_count)
                else:
                    annualizer = _m.sqrt(252)
                rv_pct = _m.sqrt(var) * annualizer * 100
        return jsonify({
            'symbol':         '$VXN',
            'count':          len(candles),
            'period':         f"{period}{period_type}",
            'frequency':      f"{freq_count} {freq}",
            'realized_vol_pct': round(rv_pct, 3) if rv_pct else None,
            'first_close':    candles[0].get('close') if candles else None,
            'last_close':     candles[-1].get('close') if candles else None,
            'candles':        candles[-50:],   # last 50 bars only (response size)
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/qqq_iv_term_structure")
def api_debug_qqq_iv_term_structure():
    """QQQ implied-volatility term structure — substitute for VXN options.

    Schwab does not carry VXN options (verified — see comments in
    schwab_bridge.py). However QQQ tracks NDX directly (~25× ratio,
    correlation ~1), so QQQ option IVs ARE NDX vol expressed in QQQ
    terms. Per-strike IV is on the chain already (Schwab field 'volatility').

    Methodology:
      1. For each subscribed QQQ expiration, find the strike closest
         to spot (ATM). Take the average of the ATM call IV and ATM
         put IV — those should be equal under put-call parity but
         averaging smooths the bid-ask noise.
      2. Build the curve [{exp, dte, atm_iv_pct}] sorted by DTE.
      3. Interpolate to a fixed 30-day tenor (linear between the two
         bracketing expirations). That number IS the QQQ-equivalent
         of VXN — they should track within a few vol-points.
      4. Return alongside live spot VIX (CBOE SPX-vol) and spot VXN
         (CBOE NDX-vol from Schwab equity stream) for context.

    The 30-day interpolated IV vs spot VXN spread tells you whether
    QQQ option market is pricing more or less vol than the official
    CBOE NDX-vol calc — useful for cross-checking and arb signals.
    """
    try:
        import math, time as _t
        spot_qqq = float(_schwab_quote('QQQ') or 0)
        if spot_qqq <= 0:
            return jsonify({'error': 'spot QQQ unavailable'}), 503
        # Pull live VIX + VXN spots from streamer cache
        try:
            from background_engine import schwab_bridge as _sb
            _spot_cache = getattr(_sb, '_latest_spot_by_ticker', {}) or {}
            spot_vix = float(_spot_cache.get('VIX', 0) or 0)
            spot_vxn = float(_spot_cache.get('VXN', 0) or 0)
        except Exception:
            spot_vix = spot_vxn = 0.0
        # Get every QQQ expiration Schwab returns (we subscribed to the
        # roughly 30 closest to spot — chain endpoint enumerates all).
        exps = _schwab_expirations('QQQ') or []
        if not exps:
            return jsonify({'error': 'no QQQ expirations'}), 503
        rows = []
        # Cap at first ~15 expirations to keep response time reasonable
        # (each /chains call is one REST hit). Front 15 covers daily +
        # weekly + monthly out to ~3 months — the meaningful tenor range.
        for exp_date in exps[:15]:
            try:
                chain, _ = _schwab_chain_raw('QQQ', exp_date)
            except Exception:
                continue
            if not chain:
                continue
            # Find ATM strike (closest available to spot)
            strikes = sorted({float(c.get('strike', 0)) for c in chain
                              if c.get('strike', 0) > 0})
            if not strikes:
                continue
            atm = min(strikes, key=lambda k: abs(k - spot_qqq))
            atm_call = next((c for c in chain
                             if abs(float(c.get('strike', 0)) - atm) < 0.01
                             and c.get('option_type') == 'call'), None)
            atm_put  = next((c for c in chain
                             if abs(float(c.get('strike', 0)) - atm) < 0.01
                             and c.get('option_type') == 'put'), None)
            if not (atm_call and atm_put):
                continue
            # Schwab's `volatility` field is annualized IV in percent.
            iv_call = float(atm_call.get('volatility') or 0)
            iv_put  = float(atm_put.get('volatility') or 0)
            # Skip expirations where IV isn't populated (e.g. 0DTE
            # with one side stale)
            if iv_call <= 0 and iv_put <= 0:
                continue
            atm_iv = (iv_call + iv_put) / 2 if (iv_call > 0 and iv_put > 0) else max(iv_call, iv_put)
            dte = int(atm_call.get('dte') or atm_put.get('dte') or 0)
            rows.append({
                'exp':           exp_date,
                'dte':           dte,
                'atm_strike':    round(atm, 2),
                'iv_call':       round(iv_call, 3),
                'iv_put':        round(iv_put, 3),
                'atm_iv_pct':    round(atm_iv, 3),
            })
        rows.sort(key=lambda r: r['dte'])
        # Linear-interpolate to 30-day fixed tenor — that's the
        # canonical "QVX" reading directly comparable to VXN.
        qvx_30d = None
        if len(rows) >= 2:
            target_dte = 30
            below = next((r for r in reversed(rows) if r['dte'] <= target_dte), None)
            above = next((r for r in rows if r['dte'] > target_dte), None)
            if below and above and above['dte'] > below['dte']:
                w = (target_dte - below['dte']) / (above['dte'] - below['dte'])
                qvx_30d = below['atm_iv_pct'] + w * (above['atm_iv_pct'] - below['atm_iv_pct'])
            elif below:
                qvx_30d = below['atm_iv_pct']
            elif above:
                qvx_30d = above['atm_iv_pct']
        # Spreads vs the official CBOE indexes
        vs_vxn = round(qvx_30d - spot_vxn, 3) if (qvx_30d and spot_vxn > 0) else None
        vs_vix = round(qvx_30d - spot_vix, 3) if (qvx_30d and spot_vix > 0) else None
        return jsonify({
            'spot_qqq':           round(spot_qqq, 2),
            'spot_vix':           round(spot_vix, 2) if spot_vix > 0 else None,
            'spot_vxn':           round(spot_vxn, 2) if spot_vxn > 0 else None,
            'qvx_30d':            round(qvx_30d, 3) if qvx_30d else None,
            'qvx_30d_minus_vxn':  vs_vxn,
            'qvx_30d_minus_vix':  vs_vix,
            'curve':              rows,
            'ts':                 _t.time(),
            'note':               'QQQ implied vol term structure. ATM IV per expiration. '
                                  'qvx_30d is linear-interpolated 30-day fixed-tenor IV — '
                                  'the QQQ-derived equivalent of CBOE VXN. Should track '
                                  'spot VXN within ~2 vol points; large divergence = '
                                  'NDX option market disagrees with the CBOE calc.',
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/equity_tape/<ticker>")
def api_debug_equity_tape(ticker):
    """Per-print equity tape from Schwab TIMESALE_EQUITY for QQQ/SPY.

    Returns:
      - Recent N prints (default 50) — ts, price, size, mic, side
      - Per-venue cumulative buy/sell volume
      - Net signed volume in last 60s / 300s
      - Bull/bear ratio across the rolling window
    """
    try:
        from background_engine import schwab_bridge as sb
        sym = (ticker or '').upper()
        if sym not in ('QQQ', 'SPY'):
            return jsonify({'error': 'ticker must be QQQ or SPY'}), 400
        n = max(1, min(500, int(request.args.get('n', 50))))
        with sb._timesale_equity_lock:
            buf = sb._timesale_equity_prints.get(sym)
            ven = sb._timesale_equity_by_venue.get(sym, {}) or {}
            prints_recent = list(buf)[-n:] if buf else []
            all_prints = list(buf) if buf else []
            venues_snapshot = {k: dict(v) for k, v in ven.items()}
        import time as _t
        now_ms = int(_t.time() * 1000)
        # Compute rolling windows
        cutoff_60   = now_ms -   60_000
        cutoff_300  = now_ms -  300_000
        net_60s_buy = sum(p[2] for p in all_prints if p[0] >= cutoff_60 and p[4] > 0)
        net_60s_sell= sum(p[2] for p in all_prints if p[0] >= cutoff_60 and p[4] < 0)
        net_300s_buy  = sum(p[2] for p in all_prints if p[0] >= cutoff_300 and p[4] > 0)
        net_300s_sell = sum(p[2] for p in all_prints if p[0] >= cutoff_300 and p[4] < 0)
        # Per-venue summary sorted by total volume
        venue_rows = []
        for mic, v in venues_snapshot.items():
            tot = v['buy_sz'] + v['sell_sz'] + v['neutral_sz']
            net = v['buy_sz'] - v['sell_sz']
            venue_rows.append({
                'mic':         mic,
                'buy_sz':      v['buy_sz'],
                'sell_sz':     v['sell_sz'],
                'neutral_sz':  v['neutral_sz'],
                'total_sz':    tot,
                'net_signed':  net,
                'trades':      v['trades'],
                'last_age_s':  round((now_ms - v['last_ts']) / 1000.0, 1),
                'share_pct':   round(100.0 * tot / max(sum(x['buy_sz']+x['sell_sz']+x['neutral_sz']
                                                          for x in venues_snapshot.values()), 1), 2),
            })
        venue_rows.sort(key=lambda r: -r['total_sz'])
        return jsonify({
            'ticker':              sym,
            'total_prints':        len(all_prints),
            'recent_prints':       [
                {'ts_ms': p[0], 'price': p[1], 'size': p[2], 'mic': p[3],
                 'side': p[4], 'sequence': p[5]}
                for p in prints_recent
            ],
            'venues':              venue_rows[:20],
            'rolling_60s': {
                'buy':  net_60s_buy, 'sell': net_60s_sell,
                'net':  net_60s_buy - net_60s_sell,
                'bull_ratio': round(net_60s_buy / max(net_60s_sell, 1), 3),
            },
            'rolling_300s': {
                'buy':  net_300s_buy, 'sell': net_300s_sell,
                'net':  net_300s_buy - net_300s_sell,
                'bull_ratio': round(net_300s_buy / max(net_300s_sell, 1), 3),
            },
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/book_health")
def api_debug_book_health():
    """Survey indicative-quote book staleness across all subscribed options.

    Filters: ?ticker=$VIX (or QQQ/$SPX/SPY) limits to one underlying.
             ?stale_only=1 returns only contracts flagged stale.

    Each row shows displayed bid/ask vs indicative bid/ask + the staleness
    ratio (disp_spread / max(ind_spread, 0.01)) so you can see how
    extreme the gap is. Use this to debug why a particular VIX term
    structure point is reading off — book_stale=True on the ATM strikes
    means the implied forward used the indicative midpoint instead of
    Schwab's `mark`.
    """
    try:
        from background_engine import schwab_bridge as _sb
        cache = getattr(_sb._on_options_quote, '_sym_cache', {}) or {}
        ticker_filter = (request.args.get('ticker', '') or '').upper()
        stale_only = request.args.get('stale_only') in ('1', 'true', 'yes')
        rows = []
        stale_count = 0
        for sym, c in cache.items():
            if ticker_filter:
                root = (c.get('option_root') or sym[:6]).strip().upper()
                if not root.startswith(ticker_filter.lstrip('$')) and ticker_filter not in sym:
                    continue
            stale = bool(c.get('book_stale'))
            if stale:
                stale_count += 1
            if stale_only and not stale:
                continue
            rows.append({
                'sym':                  sym,
                'strike':               c.get('strike'),
                'side':                 c.get('contract_type'),
                'bid':                  c.get('bid'),
                'ask':                  c.get('ask'),
                'displayed_mid':        c.get('book_displayed_mid'),
                'indicative_bid':       c.get('indicative_bid'),
                'indicative_ask':       c.get('indicative_ask'),
                'indicative_mid':       c.get('book_indicative_mid'),
                'mark':                 c.get('mark'),
                'stale_ratio':          c.get('book_stale_ratio'),
                'book_stale':           stale,
            })
        # Sort: stale first, then by stale_ratio desc
        rows.sort(key=lambda r: (-int(bool(r['book_stale'])),
                                  -(r.get('stale_ratio') or 0)))
        return jsonify({
            'total_cached':   len(cache),
            'matched':        len(rows),
            'stale_count':    stale_count,
            'stale_pct':      round(100.0 * stale_count / max(len(cache), 1), 2),
            'contracts':      rows[:100],
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/vix_chain")
def api_debug_vix_chain():
    """Snapshot of the live $VIX option chain — strike, mark, IV, OI, delta.
    Useful for skew analysis (call IV vs put IV at equidistant strikes from
    spot) and identifying VIX walls (high-OI strikes)."""
    try:
        from background_engine import schwab_bridge as sb
        per_ticker = getattr(sb, '_per_ticker_gex', {}) or {}
        vix = per_ticker.get('VIX', {}) or per_ticker.get('$VIX', {}) or {}
        rows = []
        for sym_key, info in vix.items():
            rows.append({
                'sym':           sym_key,
                'strike':        info.get('strike'),
                'side':          info.get('side'),
                'oi':            info.get('oi'),
                'delta':         info.get('delta'),
                'gamma_dollars': info.get('gamma_dollars'),
            })
        rows.sort(key=lambda r: (r.get('strike') or 0, r.get('side') or ''))
        return jsonify({
            'count':       len(rows),
            'spot_vix':    sb._latest_spot_by_ticker.get('VIX', 0) if hasattr(sb, '_latest_spot_by_ticker') else 0,
            'contracts':   rows[:50],
            'note':        'Live VIX option chain from LEVELONE_OPTIONS stream. Sorted by strike+side.',
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


@app.route("/api/_debug/spots")
def api_debug_spots():
    """Dump the universal spot cache populated by Schwab LEVELONE_EQUITIES.
    Surfaces VIX, $NDX.X, SPY, QQQ live levels + last-update age in seconds."""
    try:
        from background_engine import schwab_bridge as sb
        cache = getattr(sb, '_latest_spot_by_ticker', {}) or {}
        # Schwab streamer's per-symbol last-update timestamps (if tracked)
        eq_mic = getattr(sb._on_equity_quote, '_eq_mic', {}) if hasattr(sb, '_on_equity_quote') else {}
        rows = []
        for sym, last in sorted(cache.items()):
            rows.append({
                'symbol': sym,
                'last':   round(float(last or 0), 4),
                'mic_cached': bool(eq_mic.get(sym)),
            })
        return jsonify({
            'count':  len(cache),
            'spots':  rows,
            'note':   'Populated by _on_equity_quote (LEVELONE_EQUITIES) + _on_options_quote (per-strike underlying_price echoes)',
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()}), 500


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


@app.route("/api/_debug/sym_cache_sample")
def api_debug_sym_cache_sample():
    """Inspect the live Schwab streaming cache for a sample of symbols matching
    a prefix, returning the exact field values Schwab is sending us. Used to
    audit whether Schwab actually delivers fields like `exp_type` (field 36)
    for non-index roots like QQQ.
    Args: prefix (str) — symbol prefix to match (e.g. "QQQ", "SPXW", "$NDX").
          limit (int)  — max samples to return (default 5)
    """
    try:
        from background_engine.schwab_bridge import _on_options_quote
        prefix = (request.args.get('prefix') or 'QQQ').upper()
        limit = int(request.args.get('limit', 5))
        cache = getattr(_on_options_quote, '_sym_cache', {}) or {}
        keys = [k for k in cache.keys() if k.startswith(prefix)]
        sample = {}
        for k in keys[:limit]:
            v = cache.get(k) or {}
            # Return ALL fields so we can see exp_type, settlement_type, etc.
            sample[k] = {fk: fv for fk, fv in v.items() if not fk.startswith('_')}
        return jsonify({
            "prefix": prefix,
            "total_matches": len(keys),
            "first_keys": keys[:limit],
            "sample": sample,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/_debug/timesale")
def api_debug_timesale():
    """PUBLIC — TIMESALE_OPTIONS feed stats. Confirms the raw trade stream
    is alive and trades are being merged with LEVELONE cache."""
    try:
        from background_engine import schwab_bridge
        stats = getattr(schwab_bridge, '_timesale_stats', None)
        if stats is None:
            return jsonify({"ready": False, "reason": "bridge not imported yet"})
        return jsonify({"ready": True, **stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/intel/signal_quality")
def api_intel_signal_quality():
    """Signal Quality Dashboard backend.

    Reads outcome ledgers (logs/dealer_prints, sweep_outcomes, pin_outcomes,
    hmm_ab, hedge_forecast, spx_qqq_divergence) and returns per-signal
    quality metrics: hit_rate, sample_size, edge_$, verdict.

    Cached for 60s (file reads are expensive — dealer_prints today is
    181MB / 533K lines). Use ?force=1 to bypass cache.
    """
    try:
        from connectors import signal_audit
        force = (request.args.get('force') or '').lower() in ('1', 'true', 'yes')
        return jsonify(signal_audit.get_signal_audit(force=force))
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/option_flow/history")
def api_option_flow_history():
    """Return rolling 2h history buffer for a ticker — used by flow_pane.js
    to hydrate the chart on page-load so users see the past 2 hours of
    cumulative flow without waiting for live ticks to populate.

    Query params:
      ticker: 'QQQ' | 'SPX' | 'SPY' | etc. (defaults: QQQ)
      since:  optional ms-epoch — return only snapshots AFTER this time

    Each snapshot has compact field names (matched to flow_pane.js render):
      t, s0, sa, u0, ua,                    legacy 2-way + unsigned
      cb, cs, pb, ps,                       calls/puts decomposition
      c_0am, c_0pm, c_wk, c_mo, c_qt, c_lp  6-cohort drill-down

    Cadence: snapshotted every 30s, maxlen 240 = 2h.
    Persists to logs/flow_history_buffer.json — survives server restarts
    within the same trading day.
    """
    try:
        from connectors.flow_accumulator import get_accumulator
        ticker = (request.args.get('ticker', 'QQQ') or 'QQQ').upper()
        since_str = request.args.get('since', '0')
        try:
            since_ms = int(since_str) if since_str else 0
        except Exception:
            since_ms = 0
        acc = get_accumulator()
        if acc is None:
            return jsonify({"ticker": ticker, "snapshots": [], "ready": False})
        snapshots = acc.get_history(ticker, since_ts_ms=since_ms)
        return jsonify({
            "ticker": ticker,
            "snapshots": snapshots,
            "ready": True,
            "interval_s": 30,
            "maxlen_s": 7200,  # 2h
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "tb": traceback.format_exc()}), 500


@app.route("/api/chain")
def api_chain():
    """Return real options chain from Schwab for the terminal options panel.
    Self-contained — calls Schwab API directly."""
    from datetime import datetime, date

    ticker = _resolve_index_ticker(request.args.get("ticker", "QQQ"))

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
# 2026-05-01 fix (Tradier flap diagnosis): /api/walls compute is 10-17s and
# was firing every 28s, monopolising the gevent event loop and starving the
# Tradier WS reader greenlet (10.5MB Recv-Q backlog observed in production).
# Bumped TTL to 300s. Also added gevent.sleep(0) yields inside the heavy
# loops below so a single compute can no longer hold the loop for >200 ms.
_WALLS_TTL = 300

@app.route("/api/_debug/single_name")
def api_single_name_debug():
    """DEBUG: per-ticker breakdown of the 8-name Greeks cache.

    Returns unique DTEs, strike ranges, min/max DTE, and total contract counts
    so we can verify the /chains poll is returning the full 60-day expiration
    ladder for each ticker (not just front-month).
    """
    try:
        from background_engine import schwab_bridge as _sb
    except Exception as e:
        return jsonify({"err": str(e)})
    out = {}
    with _sb._single_name_greeks_lock:
        for osi, entry in _sb._single_name_greeks_cache.items():
            root = (osi[:6] or '').strip()
            if not root:
                continue
            slot = out.setdefault(root, {
                'n_contracts': 0, 'dtes': set(), 'strikes': set(),
                'calls': 0, 'puts': 0, 'oi_sum': 0, 'vol_sum': 0,
                'spot': 0.0, 'oldest_ms': 0, 'newest_ms': 0,
            })
            slot['n_contracts'] += 1
            slot['dtes'].add(int(entry.get('dte') or 0))
            slot['strikes'].add(float(entry.get('strike') or 0))
            if entry.get('contract_type') == 'C':
                slot['calls'] += 1
            else:
                slot['puts'] += 1
            slot['oi_sum'] += int(entry.get('oi') or 0)
            slot['vol_sum'] += int(entry.get('vol') or 0)
            if not slot['spot']:
                slot['spot'] = float(entry.get('underlying_price') or 0)
            ts = int(entry.get('updated_ms') or 0)
            if not slot['oldest_ms'] or ts < slot['oldest_ms']:
                slot['oldest_ms'] = ts
            if ts > slot['newest_ms']:
                slot['newest_ms'] = ts
    # Serialize sets + summarise
    result = {}
    for tk, s in out.items():
        dtes = sorted(s['dtes'])
        strikes = sorted(s['strikes'])
        result[tk] = {
            'n_contracts':   s['n_contracts'],
            'n_expirations': len(dtes),
            'n_strikes':     len(strikes),
            'dte_min':       dtes[0] if dtes else 0,
            'dte_max':       dtes[-1] if dtes else 0,
            'dte_list':      dtes,
            'strike_min':    strikes[0] if strikes else 0.0,
            'strike_max':    strikes[-1] if strikes else 0.0,
            'calls':         s['calls'],
            'puts':          s['puts'],
            'oi_sum':        s['oi_sum'],
            'vol_sum':       s['vol_sum'],
            'spot':          s['spot'],
            'oldest_ms':     s['oldest_ms'],
            'newest_ms':     s['newest_ms'],
        }
    return jsonify({
        'tickers':       result,
        'total_cached':  sum(s['n_contracts'] for s in result.values()),
    })


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
    ticker = _resolve_index_ticker(request.args.get("ticker", FUTURES_TO_UNDERLYING.get(futures_sym, "QQQ")))

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
        # gevent yield helper — used inside hot loops so the WS reader greenlet
        # gets cycles. Without these, a single /api/walls call holds the event
        # loop for 10-17s which back-pressures Tradier WS and kills conns.
        try:
            import gevent as _gevent
            _yield = _gevent.sleep
        except Exception:
            def _yield(t=0):  # fallback no-op
                pass

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

        # ── Parallelize the 5 chain fetches using gevent.spawn ──────────────
        # 2026-05-01 fix (bulletproof): sequential fetches were the dominant
        # /api/walls latency contributor (~5-7s for 5 REST calls). Spawning
        # them in parallel via gevent reduces total fetch time to ~max(call)
        # instead of sum(calls). Each greenlet yields naturally during socket
        # I/O so the Tradier WS reader keeps draining its kernel buffer.
        try:
            import gevent as _gevent_mod
            _greenlets = [_gevent_mod.spawn(_schwab_chain_raw, ticker, exp_date)
                          for exp_date in exp_dates]
            _gevent_mod.joinall(_greenlets, timeout=15)
            _fetched_chains = []
            for i, g in enumerate(_greenlets):
                if g.successful() and g.value:
                    _fetched_chains.append((exp_dates[i], g.value[0], g.value[1]))
                else:
                    _fetched_chains.append((exp_dates[i], [], 0))
        except Exception as e:
            # Fallback to sequential if gevent.spawn fails for any reason
            _fetched_chains = []
            for exp_date in exp_dates:
                try:
                    rc, su = _schwab_chain_raw(ticker, exp_date)
                    _fetched_chains.append((exp_date, rc, su))
                except Exception:
                    _fetched_chains.append((exp_date, [], 0))

        for exp_idx, (exp_date, raw_chain, schwab_underlying) in enumerate(_fetched_chains):
            # Yield once per expiry to give other greenlets a slot
            _yield(0)
            if exp_idx == 0 and schwab_underlying > 0:
                spot = schwab_underlying

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
            for _opt_i, opt in enumerate(raw_chain):
                # Yield every 50 contracts (was 200; tightened 2026-05-01 for
                # bulletproof gevent reader-friendliness). BSM vanna/charm math
                # uses numpy + scipy.stats.norm.pdf which can each take 5-15µs.
                # 50 contracts × ~10µs = 500µs per batch, then yield. Each
                # /api/walls compute now yields ~200 times (5 expiries × 40
                # batches) keeping Tradier WS reader fully responsive.
                if _opt_i and (_opt_i % 50 == 0):
                    _yield(0)
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
        # O(N²) over strikes — for ~200 strikes that's 40K iterations of pure
        # Python multiplication + dict lookup. Yield every 10 outer iterations
        # (tightened from 25 for bulletproof responsiveness — was contributing
        # to /api/walls compute time during cache miss; with 200 strikes that's
        # 20 yields scattered through the max-pain compute).
        sorted_strikes = sorted(all_strikes)
        min_pain = float("inf")
        underlying_max_pain = spot
        for _k_i, K in enumerate(sorted_strikes):
            if _k_i and (_k_i % 10 == 0):
                _yield(0)
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
    """Lightweight quote-only endpoint — single Tradier call, no options processing.

    2026-05-01 fix (audit issue #8): now honors `?ticker=` query param. Previously
    returned the configured default ticker regardless of query string.
    """
    try:
        from data_provider import _fetch_quote, _cached
        # Honor ?ticker= override; fall back to default if not specified
        ticker = _resolve_index_ticker(request.args.get("ticker") or get_ticker())
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

        # ── 3-second TTL cache (2026-05-06) ──────────────────────────────
        # Without this, multiple frontend panes polling vprofile cause
        # repeated 0.4-3.3s computes and 200KB-900KB JSON serializations
        # that monopolize the gevent loop and trigger WS buffer pile-up
        # → TopStepX RSTs. Cache key = full query string (different params
        # get different entries). Profile changes ~once per minute on new
        # bar close, so 3s staleness is invisible.
        _cache_key = request.query_string  # bytes including all params
        _now_t = _t.time()
        _cached = _vprofile_cache.get(_cache_key)
        if _cached and (_now_t - _cached[1]) < _VPROFILE_TTL:
            from flask import Response
            return Response(_cached[0], mimetype='application/json')

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
        # Cache serialized body so subsequent polls within TTL skip the
        # entire compute path. Cap cache size at 32 entries (well above
        # the ~4 distinct query patterns observed).
        from flask import Response
        import json as _json_mod
        _body = _json_mod.dumps(result).encode('utf-8')
        if len(_vprofile_cache) > 32:
            # drop oldest entries (rare — only if param explosion)
            for _k in list(_vprofile_cache.keys())[:-16]:
                _vprofile_cache.pop(_k, None)
        _vprofile_cache[_cache_key] = (_body, _now_t)
        return Response(_body, mimetype='application/json')
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
    # 2026-05-07 (Phase 3 of multiprocess split): if BRIDGE_PROCESS=1 env
    # var is set, SKIP starting the in-server schwab_bridge — expect a
    # separate bridge.py process to be running instead. This lets us roll
    # out the multiprocess architecture without forcing it. Default unset =
    # current single-process behavior preserved.
    if os.environ.get('BRIDGE_PROCESS', '').strip() in ('1', 'true', 'yes', 'on'):
        print("[startup] BRIDGE_PROCESS env set — skipping in-server schwab_bridge", flush=True)
        print("[startup] Expecting bridge.py to be running as a separate process", flush=True)
    else:
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
def _push_candle_history(sid, symbol='NQ', tf='1m', max_candles=5000):
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
    # Ensure _ACTIVE_TFS contains this client's default tf + always-enriched preferred TFs
    try:
        import background_engine.l2_worker as _l2w
        _l2w._ACTIVE_TFS = set(_client_active_tf.values()) | _l2w._PREFERRED_ENRICHED_TFS
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
    # Floor at _PREFERRED_ENRICHED_TFS so disconnects don't strand the user's
    # default chart-TFs (30s/1m/3m/200s/5m) un-enriched.
    _client_active_tf.pop(sid, None)
    # Clean up the candle_history dedupe tracker for this sid
    for _k in [k for k in _subscribe_last_push if k[0] == sid]:
        _subscribe_last_push.pop(_k, None)
    try:
        import background_engine.l2_worker as _l2w
        live = set(_client_active_tf.values()) | _l2w._PREFERRED_ENRICHED_TFS
        _l2w._ACTIVE_TFS = live
        # Keep legacy singleton roughly in sync (arbitrary pick)
        _l2w._ACTIVE_TF = next(iter(live))
    except Exception:
        pass
    # Clean up any MM-Attribution watch this client had.
    sym = _mma_sid_sym.pop(sid, None)
    if sym:
        try:
            from connectors import mm_attribution as _mma
            _mma.unwatch(sym)
        except Exception:
            pass

# 2026-05-07: dedupe redundant candle_history pushes. Frontend bug: chart's
# attach() function (called on every layout re-mount) resets _histApplied=false
# and re-emits subscribe. Without dedupe the server pushes 5000 bars × N
# re-mounts per session = 12-17MB redundant payload to the same browser tab.
# Symptom: chart felt sluggish during page interactions (sidebar toggle, resize).
_subscribe_last_push: dict = {}  # {(sid, sym, tf): last_push_ts}
_SUBSCRIBE_DEDUPE_SEC = 10.0

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
    # Update active tf set — union of all clients' TFs ∪ preferred-enriched defaults
    # so chart-TF switching never leaves a bar un-enriched on the user's
    # commonly-viewed timeframes (30s/1m/3m/200s/5m).
    try:
        import background_engine.l2_worker as _l2w
        live = set(_client_active_tf.values()) | _l2w._PREFERRED_ENRICHED_TFS
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
    # Dedupe: skip the 5000-bar push if we just sent one to this sid/sym/tf
    # in the last 10s. Frontend will keep getting live candle_update events;
    # only the heavy history payload is suppressed.
    _now_ts = time.time()
    _key = (sid, symbol, tf)
    _last_ts = _subscribe_last_push.get(_key, 0)
    if _now_ts - _last_ts < _SUBSCRIBE_DEDUPE_SEC:
        print(f"[Socket.IO] DEDUPE — skip candle_history push to {sid} ({_now_ts-_last_ts:.1f}s since last)")
        return
    _subscribe_last_push[_key] = _now_ts
    # Push candle history — retry if worker hasn't loaded yet
    if not _push_candle_history(sid, symbol, tf, max_candles=5000):
        print(f"[Socket.IO] No candles for {symbol}/{tf} yet, scheduling deferred push for {sid}...")
        import threading
        t = threading.Thread(target=_deferred_candle_push, args=(sid, symbol, tf), daemon=True)
        t.start()


# ── MM Attribution socket subscriptions ────────────────────────────────────
# Per-client watch map: sid -> sym. Lets us cleanly decrement refcounts on
# disconnect/switch without the client having to send `unwatch`.
_mma_sid_sym: dict = {}

@socketio.on('mm_attribution:watch')
def handle_mma_watch(data):
    """Client declares interest in a contract. Joins room `mma:<sym>`, bumps
    server-side refcount, and sends initial state immediately so the pane
    has something to render without waiting for the next flush tick."""
    sid = request.sid
    sym = (data or {}).get('sym')
    if not sym:
        return
    prev = _mma_sid_sym.get(sid)
    if prev == sym:
        return  # idempotent
    try:
        from connectors import mm_attribution as _mma
        if prev:
            try:
                leave_room(f'mma:{prev}')
                _mma.unwatch(prev)
            except Exception:
                pass
        _mma_sid_sym[sid] = sym
        join_room(f'mma:{sym}')
        _mma.watch(sym)
        emit('mm_contract_state', _mma.contract_state(sym))
    except Exception as e:
        print(f"[Socket.IO] mma:watch error sid={sid} sym={sym}: {e}")

@socketio.on('mm_attribution:unwatch')
def handle_mma_unwatch(_data=None):
    """Client explicitly stops watching (pane destroy)."""
    sid = request.sid
    sym = _mma_sid_sym.pop(sid, None)
    if not sym:
        return
    try:
        leave_room(f'mma:{sym}')
        from connectors import mm_attribution as _mma
        _mma.unwatch(sym)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# BRIDGE RELAY (Phase 2 of multiprocess split, 2026-05-07)
# ════════════════════════════════════════════════════════════════════
# bridge.py runs in its own process and emits Socket.IO events with the
# 'relay:' prefix. This catch-all relay handler re-broadcasts each one
# to all connected browser clients as the original event name.
#
# Why a single catch-all instead of per-event handlers:
# python-socketio supports a wildcard handler via @socketio.on('*'), but
# the dispatch is per-namespace. Simpler: register one handler per known
# bridge event. Easier to maintain — adds ~5 lines per new event.
#
# Backpressure: if browsers can't keep up with the firehose (50K+ events/min
# at RTH peak), the server's emit() will buffer in Socket.IO's internal queue.
# Worst case: chart appears stale, but server doesn't crash.

# All event types emitted by bridge.py (from grep on schwab_bridge.py):
_BRIDGE_RELAY_EVENTS = [
    'acct_activity',
    'big_print',
    'book_microstructure',
    'candle_enriched',
    'candle_history',
    'candle_update',
    'chart_equity_update',
    'dealer_session_flow',
    'eq_book_update',
    'eq_context',
    'equity_tape',
    'flow_alert',
    'flow_update',
    'intel:dealer_warehouse',
    'intel:events',
    'intel:gamma_skyline',
    'intel:hedge_forecast',
    'intel:pin_update',
    'intel:spx_qqq_divergence',
    'intel:sweep_alert',
    'intel:vix_term',
    'intel:wing_update',
    'l2_update',
    'mm_event_batch',
    'ndx_wgc',
    'option_mark_batch',
    'option_trade_batch',
    'screener_equity_update',
    'screener_option_update',
    'single_name_walls',
    'spot_update',
    'tape_alert',
    'trade_tick',
    'wall_signals_update',
    'zone_update',
]

# Aggregate relay activity into a 30s rolling counter so we can verify
# the relay path without printing every event (would be too noisy).
_relay_counts: dict = {}
_relay_last_log: list = [time.time()]

def _make_relay_handler(event_name: str):
    """Build a relay handler closure that re-broadcasts to all browsers."""
    def _handler(data):
        try:
            socketio.emit(event_name, data)
            _relay_counts[event_name] = _relay_counts.get(event_name, 0) + 1
            now = time.time()
            if now - _relay_last_log[0] >= 30.0:
                ranked = sorted(_relay_counts.items(), key=lambda x: -x[1])
                summary = ', '.join(f"{n}={c}" for n, c in ranked[:8])
                total = sum(_relay_counts.values())
                print(f"[RELAY] 30s: total={total} events | {summary}", flush=True)
                _relay_counts.clear()
                _relay_last_log[0] = now
        except Exception as e:
            print(f"[RELAY] {event_name} broadcast failed: {e}", flush=True)
    return _handler

# Register one relay handler per event type. The @socketio.on decorator
# would conflict with our dynamic registration, so we use the lower-level
# `on` API directly.
for _ev_name in _BRIDGE_RELAY_EVENTS:
    socketio.on_event(f'relay:{_ev_name}', _make_relay_handler(_ev_name))

print(f"[RELAY] Registered {len(_BRIDGE_RELAY_EVENTS)} bridge relay handlers", flush=True)


if __name__ == "__main__":
    print("Starting Altaris Dev with Socket.IO...")
    print("Open http://localhost:5000 in your browser")

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
