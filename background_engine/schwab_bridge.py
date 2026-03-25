"""
Schwab WebSocket → SocketIO Bridge
Connects the Schwab real-time streamer to Flask-SocketIO for live push.

Events emitted:
  - trade_tick:    {symbol, price, volume, side}       — every Level 1 update
  - candle_update: {symbol, tf, time, o, h, l, c, vol} — every chart candle
  - spot_update:   {ticker, spot, change, pct}          — spot price for header
  - zone_update:   {put_wall, call_wall, gamma_flip, max_pain, ...} — GEX zones
"""

import os
import time
import threading
from datetime import datetime

# Module-level SocketIO reference (injected by server.py)
_socketio = None
_streamer = None
_bridge_running = False

# ── Live GEX tracking ────────────────────────────────────────────────────────
_live_gex = {}           # {strike: {"call_gamma": float, "put_gamma": float, "call_oi": int, "put_oi": int}}
_gex_dirty = False       # Flag: has GEX data changed since last emit?
_last_zone_emit = 0.0    # Timestamp of last zone_update emission
_ndx_option_symbols = [] # List of subscribed NDX option symbols


def set_socketio(sio):
    """Inject the Flask-SocketIO instance from server.py."""
    global _socketio
    _socketio = sio


def start_schwab_bridge():
    """Start the Schwab streamer bridge in a background thread."""
    global _bridge_running
    if _bridge_running:
        print("[SCHWAB-BRIDGE] Already running")
        return

    _bridge_running = True
    t = threading.Thread(target=_run_bridge, daemon=True)
    t.start()
    print("[SCHWAB-BRIDGE] Background thread spawned")


def _run_bridge():
    """Main bridge loop — initializes auth + streamer, subscribes, and bridges."""
    global _streamer
    try:
        # Import connectors
        import sys
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        from connectors.schwab_auth import SchwabAuth
        from connectors.schwab_streamer import SchwabStreamer

        print("[SCHWAB-BRIDGE] Initializing Schwab auth...")
        auth = SchwabAuth()

        if not auth.is_authenticated():
            print("[SCHWAB-BRIDGE] ❌ Not authenticated — run schwab_login.py first")
            return

        print("[SCHWAB-BRIDGE] ✅ Authenticated, starting streamer...")
        _streamer = SchwabStreamer(auth)

        # Register callbacks BEFORE starting (they queue until connected)
        _streamer.on('LEVELONE_FUTURES', _on_futures_quote)
        _streamer.on('LEVELONE_EQUITIES', _on_equity_quote)
        _streamer.on('LEVELONE_OPTIONS', _on_options_quote)
        _streamer.on('CHART_FUTURES', _on_chart_candle)
        _streamer.on('CHART_EQUITY', _on_chart_candle)

        # Start the WebSocket connection
        _streamer.start()
        time.sleep(3)  # Let connection establish

        # Subscribe to key instruments
        _streamer.subscribe_futures(['/NQ', '/ES'])
        _streamer.subscribe_equities(['QQQ', 'SPY', 'VIX', '$NDX.X'])
        _streamer.subscribe_chart_futures(['/NQ', '/ES'])

        # Subscribe to NDX options for live GEX tracking
        _subscribe_qqq_options()

        print("[SCHWAB-BRIDGE] ✅ All subscriptions active — real-time push enabled")

        # Keep thread alive and log stats periodically
        while _bridge_running:
            time.sleep(5)
            _maybe_emit_zones()  # Check if zones need re-emission
            if int(time.time()) % 30 < 5:  # Log stats every ~30s
                _log_stats()

    except Exception as e:
        import traceback
        print(f"[SCHWAB-BRIDGE] ❌ Bridge failed: {e}")
        print(traceback.format_exc())


def _subscribe_qqq_options():
    """Subscribe to LEVELONE_OPTIONS for ~80 QQQ contracts around ATM.
    Uses Schwab REST API directly (no data_provider dependency)."""
    global _ndx_option_symbols
    try:
        import sys, os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        # Use Schwab REST functions from server.py
        from server import _schwab_expirations, _schwab_chain_raw, _schwab_quote

        # Get QQQ spot
        qqq_spot = _schwab_quote("QQQ")
        if not qqq_spot or qqq_spot <= 0:
            qqq_spot = _latest_nq / 41.5 if _latest_nq > 0 else 580
            print(f"[SCHWAB-BRIDGE] QQQ spot unavailable, using NQ-derived≈{qqq_spot:.1f}")

        # Get nearest 2 expirations (0DTE + next)
        raw_dates = _schwab_expirations("QQQ")
        if not raw_dates:
            print("[SCHWAB-BRIDGE] ⚠️ No QQQ expirations — skipping options subscription")
            return

        exp_dates = raw_dates[:2]  # 0DTE + next weekly

        # Get chain to find actual option symbols
        symbols = []
        atm = round(qqq_spot)
        for exp_date in exp_dates:
            try:
                chain, _ = _schwab_chain_raw("QQQ", exp_date)
                for opt in chain:
                    strike = float(opt.get("strike", 0))
                    sym = opt.get("symbol", "")
                    if sym and abs(strike - atm) <= 15:  # ±$15 around ATM (~30 strikes per expiry)
                        symbols.append(sym)
            except Exception as e:
                print(f"[SCHWAB-BRIDGE] ⚠️ Chain fetch failed for {exp_date}: {e}")

        if not symbols:
            print(f"[SCHWAB-BRIDGE] ⚠️ No QQQ options near ATM={atm}")
            return

        _ndx_option_symbols = symbols[:80]  # Cap at 80 to avoid overload
        _streamer.subscribe_options(_ndx_option_symbols)
        print(f"[SCHWAB-BRIDGE] 📊 Subscribed to {len(_ndx_option_symbols)} QQQ options "
              f"(ATM≈{atm}, exps={exp_dates})")

    except Exception as e:
        import traceback
        print(f"[SCHWAB-BRIDGE] ⚠️ QQQ options subscription failed: {e}")
        print(traceback.format_exc())


# ── NQ/QQQ/NDX price mapping ──────────────────────────────────────────────────
# Cache the latest prices for accurate ratio computation
_latest_nq = 0.0
_latest_qqq = 0.0
_latest_ndx = 0.0    # Real NDX spot — critical for NQ conversion
_nq_ndx_ratio = 1.0  # NQ/NDX ratio, updates dynamically
_nq_qqq_ratio = 40.88  # default NQ/QQQ ratio, updates dynamically
_tick_count = 0


def _on_futures_quote(data):
    """Handle Level 1 futures updates (/NQ, /ES)."""
    global _latest_nq, _nq_qqq_ratio, _tick_count  # noqa: PLW0603
    if not _socketio:
        return

    symbol = data.get('symbol', '')
    last = data.get('last', 0)
    bid = data.get('bid', 0)
    ask = data.get('ask', 0)
    volume = data.get('volume', 0)
    net_change = data.get('net_change', 0)
    pct_change = data.get('pct_change', 0)

    if not last or last <= 0:
        return

    _tick_count += 1

    # Map futures symbol to chart symbol
    chart_sym = symbol.lstrip('/')  # /NQ → NQ

    # Update NQ cache for ratio
    if symbol == '/NQ':
        _latest_nq = last
        if _latest_qqq > 0:
            _nq_qqq_ratio = _latest_nq / _latest_qqq

    # Emit trade_tick (matches existing frontend listener)
    _socketio.emit('trade_tick', {
        'symbol': chart_sym,
        'price': round(last, 2),
        'volume': volume,
        'bid': round(bid, 2),
        'ask': round(ask, 2),
        'side': 'buy' if last >= ask else ('sell' if last <= bid else 'neutral'),
        'timestamp': datetime.now().isoformat(),
    })

    # Emit spot_update for header
    _socketio.emit('spot_update', {
        'ticker': chart_sym,
        'spot': round(last, 2),
        'change': round(net_change, 2),
        'pct': round(pct_change, 2),
    })


def _on_equity_quote(data):
    """Handle Level 1 equity updates (QQQ, SPY, VIX, $NDX.X)."""
    global _latest_qqq, _latest_ndx, _nq_qqq_ratio, _nq_ndx_ratio, _tick_count  # noqa: PLW0603
    if not _socketio:
        return

    symbol = data.get('symbol', '')
    last = data.get('last', 0)
    net_change = data.get('net_change', 0)
    pct_change = data.get('net_pct_change', data.get('pct_change', 0))

    if not last or last <= 0:
        return

    _tick_count += 1

    # Update NDX cache (critical for NQ conversion accuracy)
    if symbol in ('$NDX.X', 'NDX', '$NDX'):
        _latest_ndx = last
        if _latest_nq > 0:
            _nq_ndx_ratio = _latest_nq / _latest_ndx

    # Update QQQ cache
    if symbol == 'QQQ':
        _latest_qqq = last
        if _latest_nq > 0:
            _nq_qqq_ratio = _latest_nq / _latest_qqq

    # Emit spot_update
    _socketio.emit('spot_update', {
        'ticker': symbol,
        'spot': round(last, 2),
        'change': round(net_change, 2),
        'pct': round(pct_change, 2),
    })


def _on_options_quote(data):
    """Handle Level 1 options updates (NDX options) — accumulate live Greeks."""
    global _gex_dirty
    strike = data.get('strike', 0)
    gamma = data.get('gamma', 0)
    oi = data.get('open_interest', 0)
    vol = data.get('total_volume', data.get('volume', 0))
    contract_type = data.get('contract_type', '')

    if not strike or strike <= 0:
        return

    strike = float(strike)
    gamma = abs(float(gamma or 0))
    oi = int(oi or 0)
    vol = int(vol or 0)

    if oi <= 0:
        return

    # Initialize strike entry if needed
    if strike not in _live_gex:
        _live_gex[strike] = {
            "call_gamma": 0, "put_gamma": 0,
            "call_oi": 0, "put_oi": 0,
            "call_vol": 0, "put_vol": 0,
        }

    # Fix #6: Volume-adjusted effective OI
    effective_oi = oi + (vol * 0.3)

    # Fix #1: Dollar GEX = gamma × effectiveOI × 100 (multiplier) × spot²/100
    # Use NDX spot approximation from strikes or NQ price
    ndx_approx = _latest_nq if _latest_nq > 0 else 20000
    dollar_gex = gamma * effective_oi * 100 * (ndx_approx * ndx_approx / 100)

    if contract_type in ('C', 'CALL', 'call'):
        _live_gex[strike]["call_gamma"] = dollar_gex
        _live_gex[strike]["call_oi"] = effective_oi
        _live_gex[strike]["call_vol"] = vol
    elif contract_type in ('P', 'PUT', 'put'):
        _live_gex[strike]["put_gamma"] = dollar_gex
        _live_gex[strike]["put_oi"] = effective_oi
        _live_gex[strike]["put_vol"] = vol

    _gex_dirty = True


def _maybe_emit_zones():
    """Recalculate and emit GEX zones if data has changed (max every 5s)."""
    global _gex_dirty, _last_zone_emit

    if not _gex_dirty or not _socketio:
        return
    if time.time() - _last_zone_emit < 5.0:
        return
    if not _live_gex:
        return

    try:
        import math
        from datetime import datetime

        sorted_strikes = sorted(_live_gex.keys())
        if len(sorted_strikes) < 3:
            return

        # Use real NDX spot if available, else approximate from strikes
        if _latest_ndx > 0:
            ndx_spot = _latest_ndx
            ratio_source = "LIVE_NDX"
        else:
            ndx_spot = sorted_strikes[len(sorted_strikes) // 2]
            ratio_source = "STRIKE_APPROX"

        # Compute NQ/NDX ratio dynamically
        if _latest_nq > 0 and ndx_spot > 0:
            ratio = _latest_nq / ndx_spot
        elif _nq_ndx_ratio != 1.0:
            ratio = _nq_ndx_ratio  # cached from last update
            ratio_source = f"CACHED_RATIO({_nq_ndx_ratio:.6f})"
        else:
            ratio = 1.0
            ratio_source = "DEFAULT_1.0"

        def _r(v):
            return round(v * ratio, 2)

        # Build dollar GEX maps (already computed in _on_options_quote)
        call_dollar_gex, put_dollar_gex = {}, {}
        total_call_oi, total_put_oi = {}, {}
        for s, d in _live_gex.items():
            call_dollar_gex[s] = d["call_gamma"]
            put_dollar_gex[s] = d["put_gamma"]
            total_call_oi[s] = d["call_oi"]
            total_put_oi[s] = d["put_oi"]

        # Fix #3: Dealer net GEX = -call_gex + put_gex
        dealer_net_gex = {}
        for K in sorted_strikes:
            dealer_net_gex[K] = -call_dollar_gex.get(K, 0) + put_dollar_gex.get(K, 0)

        # Fix #5: σ-based zone computation
        def _compute_zone_sigma(gex_map):
            valid = {s: v for s, v in gex_map.items() if v > 0}
            if not valid:
                return (ndx_spot, ndx_spot, ndx_spot)
            total = sum(valid.values())
            if total <= 0:
                return (ndx_spot, ndx_spot, ndx_spot)
            mean = sum(s * v for s, v in valid.items()) / total
            variance = sum(v * (s - mean) ** 2 for s, v in valid.items()) / total
            sigma = math.sqrt(variance) if variance > 0 else 25.0
            return (round(mean, 2), round(mean - sigma, 2), round(mean + sigma, 2))

        # Put/Call wall zones
        pw_center, pw_bottom, pw_top = _compute_zone_sigma(put_dollar_gex)
        cw_center, cw_bottom, cw_top = _compute_zone_sigma(call_dollar_gex)

        # Fix #3: Gamma flip — where DEALER net GEX crosses zero
        gamma_flip = ndx_spot
        for i in range(1, len(sorted_strikes)):
            s0, s1 = sorted_strikes[i-1], sorted_strikes[i]
            g0 = dealer_net_gex.get(s0, 0)
            g1 = dealer_net_gex.get(s1, 0)
            if g0 * g1 < 0:
                frac = abs(g0) / (abs(g0) + abs(g1)) if (abs(g0) + abs(g1)) > 0 else 0.5
                gamma_flip = s0 + frac * (s1 - s0)
                break

        gf_band = gamma_flip * 0.005
        gf_bottom = gamma_flip - gf_band
        gf_top = gamma_flip + gf_band

        # Max pain
        max_pain_strike = ndx_spot
        min_pain = float("inf")
        for K in sorted_strikes:
            pain = sum(
                (total_put_oi.get(S, 0) * max(K - S, 0) + total_call_oi.get(S, 0) * max(S - K, 0))
                for S in sorted_strikes
            )
            if pain < min_pain:
                min_pain = pain
                max_pain_strike = K

        ss = sorted_strikes[1] - sorted_strikes[0] if len(sorted_strikes) > 1 else 25
        mp_bottom = max_pain_strike - ss
        mp_top = max_pain_strike + ss

        zone_data = {
            "put_wall": _r(pw_center), "put_wall_top": _r(pw_top), "put_wall_bottom": _r(pw_bottom),
            "call_wall": _r(cw_center), "call_wall_top": _r(cw_top), "call_wall_bottom": _r(cw_bottom),
            "gamma_flip": _r(gamma_flip), "gamma_flip_top": _r(gf_top), "gamma_flip_bottom": _r(gf_bottom),
            "max_pain": _r(max_pain_strike), "max_pain_top": _r(mp_top), "max_pain_bottom": _r(mp_bottom),
            "source": "LIVE_WS",
            "underlying_ticker": "NDX",
            "ratio": round(ratio, 4),
            "ratio_source": ratio_source,
            "last_updated": datetime.now().isoformat(),
            "strikes_count": len(sorted_strikes),
        }

        _socketio.emit('zone_update', zone_data)
        _last_zone_emit = time.time()
        _gex_dirty = False

    except Exception as e:
        print(f"[SCHWAB-BRIDGE] ⚠️ Zone emit error: {e}")


def _on_chart_candle(data):
    """Handle real-time chart candle updates."""
    if not _socketio:
        return

    symbol = data.get('symbol', data.get('key', ''))
    chart_time = data.get('chart_time', 0)
    o = data.get('open', 0)
    h = data.get('high', 0)
    l = data.get('low', 0)
    c = data.get('close', 0)
    v = data.get('volume', 0)

    if not chart_time or not c:
        return

    # Convert ms timestamp to seconds if needed
    ts = chart_time / 1000 if chart_time > 1e12 else chart_time

    chart_sym = symbol.lstrip('/')  # /NQ → NQ

    # Emit candle_update (matches existing frontend listener exactly)
    _socketio.emit('candle_update', {
        'symbol': chart_sym,
        'tf': '1m',  # Schwab chart stream is 1-min candles
        'time': int(ts),
        'open': round(o, 2),
        'high': round(h, 2),
        'low': round(l, 2),
        'close': round(c, 2),
        'volume': v,
    })


def _log_stats():
    """Periodic stats logging."""
    global _tick_count
    if _streamer and _streamer.is_connected:
        nq = _streamer.get_latest('LEVELONE_FUTURES', '/NQ')
        nq_price = nq.get('last', 0) if nq else 0
        opts_count = len(_live_gex)
        print(f"[SCHWAB-BRIDGE] 📊 {_tick_count} ticks | NQ={nq_price:.2f} | "
              f"ratio={_nq_qqq_ratio:.2f} | options_strikes={opts_count} | connected=True")
    else:
        print(f"[SCHWAB-BRIDGE] ⚠️  {_tick_count} ticks | connected=False (reconnecting...)")
    _tick_count = 0


def stop_schwab_bridge():
    """Stop the bridge cleanly."""
    global _bridge_running
    _bridge_running = False
    if _streamer:
        _streamer.stop()
    print("[SCHWAB-BRIDGE] Stopped")

