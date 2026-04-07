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
_flow_classifier = None  # FlowClassifier instance (for /api/flow)
_edge_detector = None    # EdgeDetector instance (cross-asset signal engine)
_mm_tracker = None       # MMTracker instance (market maker withdrawal detection)
_dte0_squeeze = None     # DTE0SqueezeDetector instance (0DTE options delta hedge tracking)
_greek_surface = None    # GreekSurface instance (multi-Greek exposure surface)
_vol_surface = None      # VolSurface instance (live volatility surface state machine)
_iv_calibrator = None    # IVCalibrator instance (Tradier ORATS IV surface polling)

# ── Live GEX tracking ────────────────────────────────────────────────────────
_live_gex = {}           # {strike: {"call_gamma": float, "put_gamma": float, "call_oi": int, "put_oi": int, "call_delta": float, "put_delta": float}}
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

        print("[SCHWAB-BRIDGE] Authenticated, starting streamer...")
        _streamer = SchwabStreamer(auth)

        # Register callbacks BEFORE starting (they queue until connected)
        _streamer.on('LEVELONE_FUTURES', _on_futures_quote)
        _streamer.on('LEVELONE_EQUITIES', _on_equity_quote)
        _streamer.on('LEVELONE_OPTIONS', _on_options_quote)
        _streamer.on('CHART_FUTURES', _on_chart_candle)
        _streamer.on('CHART_EQUITY', _on_chart_candle)
        _streamer.on('NASDAQ_BOOK', _on_nasdaq_book)
        _streamer.on('SCREENER_OPTION', _on_screener_option)

        # Initialize FlowClassifier — attaches to NASDAQ_BOOK/OPTIONS_BOOK callbacks
        global _flow_classifier, _edge_detector
        from connectors.flow_classifier import FlowClassifier
        _flow_classifier = FlowClassifier(_streamer)
        _flow_classifier.start()
        print("[SCHWAB-BRIDGE] FlowClassifier attached")

        # Initialize EdgeDetector — cross-asset signal engine
        from connectors.edge_detector import EdgeDetector
        _edge_detector = EdgeDetector(_streamer, _flow_classifier, _socketio)
        _edge_detector.start()
        print("[SCHWAB-BRIDGE] EdgeDetector attached")

        # Initialize MMTracker — market maker withdrawal detection
        from connectors.mm_tracker import MMTracker
        _mm_tracker = MMTracker(edge_detector=_edge_detector)
        _edge_detector._mm_tracker_ref = _mm_tracker
        print("[SCHWAB-BRIDGE] MMTracker attached → EdgeDetector")

        # Initialize 0DTE Squeeze Detector — options dealer delta hedge tracking
        global _dte0_squeeze, _greek_surface
        try:
            from connectors.dte0_squeeze import DTE0SqueezeDetector
            _dte0_squeeze = DTE0SqueezeDetector(edge_detector=_edge_detector)
            print("[SCHWAB-BRIDGE] DTE0 Squeeze Detector attached → EdgeDetector")
        except ImportError as e:
            print(f"[SCHWAB-BRIDGE] ⚠️ DTE0 Squeeze Detector not available: {e}")

        # Initialize Greek Surface Engine — full sensitivity surface
        try:
            from connectors.greek_surface import GreekSurface
            _greek_surface = GreekSurface()
            _edge_detector._greek_surface = _greek_surface
            print("[SCHWAB-BRIDGE] GreekSurface engine attached → EdgeDetector")
        except ImportError as e:
            print(f"[SCHWAB-BRIDGE] ⚠️ GreekSurface not available: {e}")

        # Initialize Vol Surface Monitor — live volatility surface state machine
        global _vol_surface
        try:
            from connectors.vol_surface import VolSurface
            _vol_surface = VolSurface()
            _edge_detector._vol_surface = _vol_surface
            print("[SCHWAB-BRIDGE] VolSurface monitor attached → EdgeDetector")
        except ImportError as e:
            _vol_surface = None
            print(f"[SCHWAB-BRIDGE] ⚠️ VolSurface not available: {e}")

        # Initialize IV Calibrator — Tradier ORATS IV surface polling
        try:
            from connectors.iv_calibrator import IVCalibrator
            _iv_calibrator = IVCalibrator(ticker='QQQ', poll_interval=300)
            _iv_calibrator.start()
            print("[SCHWAB-BRIDGE] IVCalibrator started (Tradier ORATS, 5-min poll)")
        except ImportError as e:
            _iv_calibrator = None
            print(f"[SCHWAB-BRIDGE] ⚠️ IVCalibrator not available: {e}")

        # Wire NQ detection forwarding: l2_worker → EdgeDetector
        try:
            from background_engine.l2_worker import set_detection_callback, set_cross_asset_provider, set_trade_score_callback
            set_detection_callback(_edge_detector.on_nq_detection)
            set_cross_asset_provider(_edge_detector.get_cross_asset_context)
            set_trade_score_callback(_edge_detector.score_trade)
            print("[SCHWAB-BRIDGE] NQ detection ↔ EdgeDetector bidirectional wired + tape scoring")
        except ImportError:
            print("[SCHWAB-BRIDGE] ⚠️ l2_worker not available for NQ forwarding")

        # Start the WebSocket connection
        _streamer.start()
        time.sleep(3)  # Let connection establish

        # Subscribe to key instruments
        _streamer.subscribe_futures(['/NQ', '/ES'])
        _streamer.subscribe_equities(['QQQ', 'SPY', 'VIX', '$NDX.X'])
        _streamer.subscribe_chart_futures(['/NQ', '/ES'])

        # Subscribe to NDX options for live GEX tracking
        _subscribe_qqq_options()

        # Subscribe to QQQ equity L2 book (NASDAQ_BOOK)
        _streamer.subscribe_nasdaq_book(['QQQ'])
        print("[SCHWAB-BRIDGE] Subscribed to NASDAQ_BOOK for QQQ")

        # Subscribe to options screener (volume-based unusual activity)
        try:
            _streamer.subscribe_screener_option('VOLUME', '0')
            print("[SCHWAB-BRIDGE] Subscribed to SCREENER_OPTION")
        except Exception as e:
            print(f"[SCHWAB-BRIDGE] SCREENER_OPTION subscription failed: {e}")

        print("[SCHWAB-BRIDGE] All subscriptions active")

        # Keep thread alive and log stats periodically
        while _bridge_running:
            time.sleep(5)
            _maybe_emit_zones()  # Check if zones need re-emission
            # Periodic flow divergence check (edge detector)
            if _edge_detector:
                _edge_detector.check_flow_divergence('QQQ')
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

        # Get QQQ spot — MUST be live, never guess
        qqq_spot = _schwab_quote("QQQ")
        if not qqq_spot or qqq_spot <= 0:
            # Try live cached QQQ from equity stream
            if _latest_qqq > 0:
                qqq_spot = _latest_qqq
                print(f"[SCHWAB-BRIDGE] QQQ REST unavailable, using live stream QQQ={qqq_spot:.2f}")
            else:
                print(f"[SCHWAB-BRIDGE] ❌ No live QQQ spot available — aborting options subscription")
                return

        # Get nearest 2 expirations (0DTE + next)
        raw_dates = _schwab_expirations("QQQ")
        if not raw_dates:
            print("[SCHWAB-BRIDGE] ⚠️ No QQQ expirations — skipping options subscription")
            return

        exp_dates = raw_dates[:3]  # 0DTE + next two for term structure

        # Get chain to find actual option symbols
        symbols = []
        atm = round(qqq_spot)
        for exp_date in exp_dates:
            try:
                chain, _ = _schwab_chain_raw("QQQ", exp_date)
                for opt in chain:
                    strike = float(opt.get("strike", 0))
                    sym = opt.get("symbol", "")
                    if sym and abs(strike - atm) <= 60:  # ±$60 around ATM (~120 strikes per expiry)
                        symbols.append(sym)
            except Exception as e:
                print(f"[SCHWAB-BRIDGE] ⚠️ Chain fetch failed for {exp_date}: {e}")

        if not symbols:
            print(f"[SCHWAB-BRIDGE] ⚠️ No QQQ options near ATM={atm}")
            return

        _ndx_option_symbols = symbols[:200]  # Cap at 200 (wider chain coverage)
        _streamer.subscribe_options(_ndx_option_symbols)
        # Also subscribe to OPTIONS_BOOK (Level 2 depth) for flow classification
        _streamer.subscribe_options_book(_ndx_option_symbols[:20])  # Cap L2 at 20 to keep bandwidth reasonable
        print(f"[SCHWAB-BRIDGE] 📊 Subscribed to {len(_ndx_option_symbols)} QQQ options "
              f"(ATM≈{atm}, exps={exp_dates}) + OPTIONS_BOOK L2 for top 20")

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
_nq_qqq_ratio = 0.0    # computed from first live NQ + QQQ ticks, never hardcoded
_tick_count = 0
_nq_price_history = []  # [(timestamp, price)] — for intraday realized vol
_nq_rv_last_sample = 0.0  # timestamp of last RV sample
_nq_realized_vol = 0.0    # current intraday RV (annualized %)


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
        # Sample NQ price every 60s for realized vol computation
        global _nq_rv_last_sample, _nq_realized_vol
        now_ts = time.time()
        if now_ts - _nq_rv_last_sample >= 60.0:
            _nq_rv_last_sample = now_ts
            _nq_price_history.append((now_ts, last))
            # Keep last 120 samples (2 hours of 1-min bars)
            if len(_nq_price_history) > 120:
                _nq_price_history.pop(0)
            # Compute realized vol from log returns
            if len(_nq_price_history) >= 10:
                import math
                returns = []
                for j in range(1, len(_nq_price_history)):
                    p0 = _nq_price_history[j-1][1]
                    p1 = _nq_price_history[j][1]
                    if p0 > 0:
                        returns.append(math.log(p1 / p0))
                if returns:
                    mean_r = sum(returns) / len(returns)
                    var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
                    # Annualize: 1-min bars, ~390 bars/day, 252 days/year
                    _nq_realized_vol = math.sqrt(var * 390 * 252) * 100  # as %



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
    bid = data.get('bid', 0)
    ask = data.get('ask', 0)
    last_size = data.get('last_size', 0)
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

    # ── Forward NBBO venue data + cross-asset prices to EdgeDetector ──
    if _edge_detector:
        # NBBO venue fields (fields 37-41) — tick-speed MM detection
        bid_mic = data.get('bid_mic', '')
        ask_mic = data.get('ask_mic', '')
        last_mic = data.get('last_mic', '')
        bid_time = data.get('bid_time', 0)
        ask_time = data.get('ask_time', 0)

        if symbol == 'QQQ' and (bid_mic or ask_mic):
            try:
                _edge_detector.on_nbbo_venue(symbol, last, bid_mic, ask_mic,
                                             last_mic, bid_time, ask_time)
            except Exception:
                pass

        # Cross-asset prices: SPY and VIX
        if symbol in ('SPY', 'VIX', 'QQQ'):
            try:
                _edge_detector.on_cross_asset_price(symbol, last, pct_change)
            except Exception:
                pass

        # Trade Tape + CVD Profile
        if symbol == 'QQQ' and last_size > 0:
            try:
                _edge_detector.on_equity_trade(symbol, last, last_size, bid, ask)
            except Exception:
                pass

    # Emit spot_update
    _socketio.emit('spot_update', {
        'ticker': symbol,
        'spot': round(last, 2),
        'change': round(net_change, 2),
        'pct': round(pct_change, 2),
    })


def _on_options_quote(data):
    """Handle Level 1 options updates (NDX options) — accumulate live Greeks.
    Now captures enriched fields: security_status, quote_time, trade_time,
    mark_change, mark_pct_change, theoretical_value, indicative_bid/ask.
    """
    global _gex_dirty
    strike = data.get('strike', 0)
    gamma = data.get('gamma', 0)
    delta = data.get('delta', 0)
    oi = data.get('open_interest', 0)
    vol = data.get('total_volume', data.get('volume', 0))
    contract_type = data.get('contract_type', '')
    mark = data.get('mark', data.get('last', 0))  # option premium
    iv = data.get('implied_vol', 0)                # implied volatility
    theta = data.get('theta', 0)                   # time decay per day
    vega = data.get('vega', 0)                     # vol sensitivity
    rho = data.get('rho', 0)                       # rate sensitivity
    dte = data.get('dte', 0)                       # days to expiry
    underlying_price = data.get('underlying_price', 0)  # QQQ spot from options feed

    # ── Enriched fields (Phase 1) ──────────────────────────────────
    security_status = data.get('security_status', '')     # Trading halt detection
    quote_time = data.get('quote_time', 0)                # Quote timestamp (ms)
    trade_time = data.get('trade_time', 0)                # Last trade timestamp (ms)
    mark_change = data.get('mark_change', 0)              # Premium change from yesterday
    mark_pct_change = data.get('mark_pct_change', 0)      # Premium % change
    theoretical_value = data.get('theoretical_value', 0)  # Schwab model price
    intrinsic_value = data.get('intrinsic_value', 0)      # Schwab-computed intrinsic
    open_price = data.get('open', 0)                      # Day's opening price
    indicative_bid = data.get('indicative_bid', 0)        # Pre/post-market bid
    indicative_ask = data.get('indicative_ask', 0)        # Pre/post-market ask
    exercise_type = data.get('exercise_type', '')          # American vs European
    exp_type = data.get('exp_type', '')                    # Expiration type

    # ── Halt detection: skip halted contracts ──────────────────────
    if security_status and security_status not in ('Normal', 'normal', ''):
        return  # Contract halted — do not process

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
            "call_delta": 0, "put_delta": 0,
            "call_mark": 0, "put_mark": 0,
            "call_iv": 0, "put_iv": 0,
            "call_theta": 0, "put_theta": 0,
            # Enriched fields
            "call_mark_change": 0, "put_mark_change": 0,
            "call_theo": 0, "put_theo": 0,
        }

    # Pure OI — no made-up volume adjustment factor
    effective_oi = float(oi)

    # Dollar GEX = gamma × OI × 100 (multiplier) × spot²/100
    # MUST use live QQQ spot — these are QQQ options, spot² is QQQ-native
    if _latest_qqq <= 0:
        return  # No live QQQ spot — refuse to compute GEX with a guess
    qqq_spot = _latest_qqq
    dollar_gex = gamma * effective_oi * 100 * (qqq_spot * qqq_spot / 100)

    if contract_type in ('C', 'CALL', 'call'):
        _live_gex[strike]["call_gamma"] = dollar_gex
        _live_gex[strike]["call_oi"] = effective_oi
        _live_gex[strike]["call_vol"] = vol
        _live_gex[strike]["call_delta"] = abs(float(delta or 0))
        _live_gex[strike]["call_mark"] = abs(float(mark or 0))
        _live_gex[strike]["call_iv"] = abs(float(iv or 0))
        _live_gex[strike]["call_theta"] = float(theta or 0)
        _live_gex[strike]["call_mark_change"] = float(mark_change or 0)
        _live_gex[strike]["call_theo"] = float(theoretical_value or 0)
    elif contract_type in ('P', 'PUT', 'put'):
        _live_gex[strike]["put_gamma"] = dollar_gex
        _live_gex[strike]["put_oi"] = effective_oi
        _live_gex[strike]["put_vol"] = vol
        _live_gex[strike]["put_delta"] = abs(float(delta or 0))
        _live_gex[strike]["put_mark"] = abs(float(mark or 0))
        _live_gex[strike]["put_iv"] = abs(float(iv or 0))
        _live_gex[strike]["put_theta"] = float(theta or 0)
        _live_gex[strike]["put_mark_change"] = float(mark_change or 0)
        _live_gex[strike]["put_theo"] = float(theoretical_value or 0)

    _gex_dirty = True

    # Feed into GreekSurface engine for higher-order computations
    if _greek_surface:
        try:
            _greek_surface.update(data)
        except Exception:
            pass

    # Feed into 0DTE Squeeze Detector
    if _dte0_squeeze:
        try:
            _dte0_squeeze.on_options_update(data)
        except Exception:
            pass


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

        # QQQ spot for zone computation — MUST be live, never approximate
        if _latest_qqq > 0:
            ndx_spot = _latest_qqq
            ratio_source = "LIVE_QQQ"
        elif _latest_ndx > 0:
            ndx_spot = _latest_ndx
            ratio_source = "LIVE_NDX"
        else:
            print("[SCHWAB-BRIDGE] ⚠️ No live QQQ or NDX spot — skipping zone emit")
            return

        # NQ/QQQ ratio — MUST be computed from live feeds
        if _latest_nq > 0 and ndx_spot > 0:
            ratio = _latest_nq / ndx_spot
        elif _nq_qqq_ratio > 0:
            ratio = _nq_qqq_ratio  # cached from last live computation
            ratio_source = f"CACHED_RATIO({_nq_qqq_ratio:.4f})"
        else:
            print("[SCHWAB-BRIDGE] ⚠️ No live NQ feed — cannot compute ratio, skipping zone emit")
            return

        def _r(v):
            return round(v * ratio, 2)

        # Build dollar GEX maps (already computed in _on_options_quote)
        call_dollar_gex, put_dollar_gex = {}, {}
        total_call_oi, total_put_oi = {}, {}
        call_delta_map, put_delta_map = {}, {}
        for s, d in _live_gex.items():
            call_dollar_gex[s] = d["call_gamma"]
            put_dollar_gex[s] = d["put_gamma"]
            total_call_oi[s] = d["call_oi"]
            total_put_oi[s] = d["put_oi"]
            call_delta_map[s] = d.get("call_delta", 0)
            put_delta_map[s] = d.get("put_delta", 0)

        # Fix #3: Dealer net GEX = -call_gex + put_gex
        dealer_net_gex = {}
        for K in sorted_strikes:
            dealer_net_gex[K] = -call_dollar_gex.get(K, 0) + put_dollar_gex.get(K, 0)

        # ── Put/Call wall: strike with MAXIMUM OI concentration ──
        # Institutional definition: wall = single highest OI strike.
        # Using OI (not dollar GEX) because GEX is gamma-weighted, which biases
        # toward ATM strikes even when a far OTM strike has much more OI.
        # This matches the /api/walls REST endpoint logic.
        pw_center = max(total_put_oi, key=total_put_oi.get) if total_put_oi else ndx_spot
        cw_center = max(total_call_oi, key=total_call_oi.get) if total_call_oi else ndx_spot
        pw_bottom = pw_center - 5
        pw_top    = pw_center + 5
        cw_bottom = cw_center - 5
        cw_top    = cw_center + 5

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

        # ── DEX (Delta Exposure) per strike ──
        # DEX_strike = (Call_OI × Δ_call - Put_OI × Δ_put) × 100 (contract multiplier)
        # Positive DEX = dealers net long delta at that strike (resistance)
        # Negative DEX = dealers net short delta (support)
        dealer_dex = {}
        total_net_dex = 0.0
        dex_wall_long_strike = ndx_spot   # strike with highest positive DEX (resistance)
        dex_wall_short_strike = ndx_spot  # strike with most negative DEX (support)
        max_pos_dex = 0
        max_neg_dex = 0

        # Accumulators for Net Premium, Mean IV, Net Theta
        total_call_premium = 0.0   # Σ(call_OI × mark × 100)
        total_put_premium = 0.0    # Σ(put_OI × mark × 100)
        iv_weighted_sum = 0.0      # Σ(OI × IV) for weighted mean
        iv_weight_total = 0.0      # Σ(OI) for weighted mean
        net_theta = 0.0            # Σ(OI × theta × 100) — daily $ decay

        for K in sorted_strikes:
            d = _live_gex.get(K, {})
            c_oi = total_call_oi.get(K, 0)
            p_oi = total_put_oi.get(K, 0)
            c_delta = call_delta_map.get(K, 0)
            p_delta = put_delta_map.get(K, 0)
            # Dealer is short what client is long:
            # Client buys call → dealer short call → dealer delta = -(OI × Δ_call)
            # Client buys put → dealer short put → dealer delta = +(OI × Δ_put)
            # Net dealer DEX per strike:
            dex_k = (-(c_oi * c_delta) + (p_oi * p_delta)) * 100
            dealer_dex[K] = dex_k
            total_net_dex += dex_k
            if dex_k > max_pos_dex:
                max_pos_dex = dex_k
                dex_wall_long_strike = K
            if dex_k < max_neg_dex:
                max_neg_dex = dex_k
                dex_wall_short_strike = K

            # Net Premium: OI × mark × 100 (contract multiplier)
            c_mark = d.get("call_mark", 0)
            p_mark = d.get("put_mark", 0)
            total_call_premium += c_oi * c_mark * 100
            total_put_premium += p_oi * p_mark * 100

            # OI-weighted Mean IV
            c_iv = d.get("call_iv", 0)
            p_iv = d.get("put_iv", 0)
            if c_iv > 0 and c_oi > 0:
                iv_weighted_sum += c_oi * c_iv
                iv_weight_total += c_oi
            if p_iv > 0 and p_oi > 0:
                iv_weighted_sum += p_oi * p_iv
                iv_weight_total += p_oi

            # Net Theta: dealer collects this daily
            # Dealers are short options → they receive theta (positive for them)
            c_theta = d.get("call_theta", 0)
            p_theta = d.get("put_theta", 0)
            net_theta += (c_oi * c_theta + p_oi * p_theta) * 100

        net_premium = total_call_premium - total_put_premium
        mean_iv = (iv_weighted_sum / iv_weight_total) if iv_weight_total > 0 else 0
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

        # ── Composite Heatmap Profile: GEX + DEX weighted by proximity ──
        # This is the institutionally correct way to identify reaction levels:
        #   1. GEX (gamma) = second-order hedging pressure (what CAUSES bounces)
        #   2. DEX (delta) = first-order directional bias
        #   3. Proximity = closer to spot = more immediately relevant
        # All normalization is data-driven from the chain itself.
        dex_profile = []
        if ratio > 0 and dealer_dex and dealer_net_gex:
            # Step 1: Compute normalization denominators from the data itself
            abs_gex_vals = [abs(v) for v in dealer_net_gex.values() if abs(v) > 0]
            abs_dex_vals = [abs(v) for v in dealer_dex.values() if abs(v) > 0]
            if len(abs_gex_vals) > 2 and len(abs_dex_vals) > 2:
                gex_max = max(abs_gex_vals)
                dex_max = max(abs_dex_vals)

                # Step 2: Proximity bandwidth = standard deviation of strike distribution
                # This naturally adapts to chain width (weekly = tight, monthly = wide)
                strike_mean = sum(sorted_strikes) / len(sorted_strikes)
                strike_var = sum((s - strike_mean) ** 2 for s in sorted_strikes) / len(sorted_strikes)
                proximity_bw = math.sqrt(strike_var) if strike_var > 0 else 1.0

                # Step 3: Build composite score per strike
                composite = {}
                for K in sorted_strikes:
                    gex_k = abs(dealer_net_gex.get(K, 0))
                    dex_k = abs(dealer_dex.get(K, 0))
                    # Normalize each to [0..1] using the chain's own max
                    norm_gex = gex_k / gex_max if gex_max > 0 else 0
                    norm_dex = dex_k / dex_max if dex_max > 0 else 0
                    # Proximity weight: Gaussian decay from spot (data-driven bandwidth)
                    dist = abs(K - ndx_spot)
                    prox_weight = 1.0 / (1.0 + (dist / proximity_bw) ** 2)
                    # Composite: GEX dominates (it causes the reaction), DEX adds bias
                    composite[K] = (norm_gex + norm_dex) * prox_weight

                # Step 4: Z-score the composite (pure statistics)
                comp_vals = [v for v in composite.values() if v > 0]
                if len(comp_vals) > 2:
                    comp_mean = sum(comp_vals) / len(comp_vals)
                    comp_var = sum((x - comp_mean) ** 2 for x in comp_vals) / len(comp_vals)
                    comp_std = math.sqrt(comp_var) if comp_var > 0 else 1.0
                    for K, score in composite.items():
                        z = (score - comp_mean) / comp_std if comp_std > 0 else 0
                        if z > 0.1:  # Lowered from 0.5 to render in low volatility regimes
                            dex_profile.append({
                                "price": round(K * ratio, 2),
                                "dex": round(dealer_dex.get(K, 0), 2),
                                "gex": round(dealer_net_gex.get(K, 0), 2),
                                "z": round(z, 2),
                            })

        zone_data = {
            "put_wall": _r(pw_center), "put_wall_top": _r(pw_top), "put_wall_bottom": _r(pw_bottom),
            "call_wall": _r(cw_center), "call_wall_top": _r(cw_top), "call_wall_bottom": _r(cw_bottom),
            "gamma_flip": _r(gamma_flip), "gamma_flip_top": _r(gf_top), "gamma_flip_bottom": _r(gf_bottom),
            "max_pain": _r(max_pain_strike), "max_pain_top": _r(mp_top), "max_pain_bottom": _r(mp_bottom),
            # Raw QQQ-native levels (un-scaled) for frontend labels & toolbar
            "underlying_put_wall": round(pw_center, 2),
            "underlying_call_wall": round(cw_center, 2),
            "underlying_gamma_flip": round(gamma_flip, 2),
            "underlying_max_pain": round(max_pain_strike, 2),
            "underlying_spot": round(ndx_spot, 2),
            # DEX (Delta Exposure) — first-order dealer directional positioning
            "total_dex": round(total_net_dex, 0),
            "dex_wall_long": _r(dex_wall_long_strike),     # highest positive DEX = resistance
            "dex_wall_short": _r(dex_wall_short_strike),    # most negative DEX = support
            "dex_wall_long_qqq": round(dex_wall_long_strike, 2),
            "dex_wall_short_qqq": round(dex_wall_short_strike, 2),
            # Net Premium — dollar commitment bias (positive = call-heavy = bullish)
            "net_premium": round(net_premium, 0),
            "net_premium_m": round(net_premium / 1e6, 2),
            "call_premium_m": round(total_call_premium / 1e6, 2),
            "put_premium_m": round(total_put_premium / 1e6, 2),
            # Mean IV — OI-weighted implied volatility across chain (%)
            "mean_iv": round(mean_iv, 2),
            # Net Theta — daily time decay $ (negative = dealers collect decay = supportive)
            "net_theta": round(net_theta, 0),
            "net_theta_m": round(net_theta / 1e6, 4),
            "source": "LIVE_WS",
            "underlying_ticker": "QQQ",
            "ratio": round(ratio, 4),
            "ratio_source": ratio_source,
            "last_updated": datetime.now().isoformat(),
            "strikes_count": len(sorted_strikes),
            "dex_profile": dex_profile,
        }

        # ── Mispricing detection: theoretical vs mark spread ─────────────
        # Compares Schwab's model price to actual mark price
        # Large spread → market disagrees with model → edge opportunity
        misprice_sum = 0
        misprice_count = 0
        net_mark_change = 0
        for K in sorted_strikes:
            sd = _live_gex.get(K, {})
            for side in ('call', 'put'):
                theo = sd.get(f'{side}_theo', 0)
                mk = sd.get(f'{side}_mark', 0)
                mkc = sd.get(f'{side}_mark_change', 0)
                if theo > 0.01 and mk > 0.01:
                    misprice_pct = (mk - theo) / theo * 100
                    misprice_sum += abs(misprice_pct)
                    misprice_count += 1
                net_mark_change += mkc
        avg_misprice = round(misprice_sum / max(misprice_count, 1), 2)
        zone_data["avg_mispricing_pct"] = avg_misprice
        zone_data["net_mark_change"] = round(net_mark_change, 4)
        zone_data["mark_flow_direction"] = "CALL_ACCUMULATING" if net_mark_change > 0.5 else ("PUT_ACCUMULATING" if net_mark_change < -0.5 else "BALANCED")

        # ── Merge GreekSurface higher-order exposure data ─────────────────────
        if _greek_surface:
            try:
                greek_data = _greek_surface.compute_surface(ndx_spot, nq_ratio=ratio)
                if greek_data:
                    zone_data.update(greek_data)
            except Exception as e:
                print(f"[SCHWAB-BRIDGE] ⚠️ GreekSurface compute error: {e}")

        # ── Merge Tradier ORATS IV calibration data ───────────────────────
        if _iv_calibrator:
            try:
                iv_cal = _iv_calibrator.get_calibration()
                if iv_cal.get('timestamp'):
                    zone_data['orats_mid_iv'] = iv_cal.get('mid_iv', 0)
                    zone_data['orats_smv_vol'] = iv_cal.get('smv_vol', 0)
                    zone_data['iv_spread'] = iv_cal.get('iv_spread', 0)
                    zone_data['iv_spread_label'] = iv_cal.get('iv_spread_label', 'UNKNOWN')
                    zone_data['mm_uncertainty'] = iv_cal.get('mm_uncertainty', 0)
                    zone_data['skew_25d'] = iv_cal.get('skew_25d', 0)
            except Exception:
                pass

        # ── Feed VolSurface monitor and merge regime data ─────────────────
        if _vol_surface:
            try:
                vol_state = _vol_surface.update(zone_data, realized_vol=_nq_realized_vol)
                if vol_state:
                    zone_data['vol_regime'] = vol_state.get('regime', 'NORMAL')
                    zone_data['vol_regime_confidence'] = vol_state.get('regime_confidence', 0)
                    zone_data['vol_premium'] = vol_state.get('vol_premium', 0)
                    zone_data['iv_rank'] = vol_state.get('iv_rank', 50)
                    zone_data['skew_velocity'] = vol_state.get('skew_velocity', 0)
                    zone_data['iv_velocity'] = vol_state.get('iv_velocity', 0)
                    zone_data['vol_regime_duration'] = vol_state.get('regime_duration_s', 0)
                    # Check for vol alerts
                    alert_type, severity = _vol_surface.get_skew_alert()
                    if alert_type:
                        zone_data['vol_alert'] = alert_type
                        zone_data['vol_alert_severity'] = severity
            except Exception as e:
                print(f"[SCHWAB-BRIDGE] ⚠️ VolSurface error: {e}")

        _socketio.emit('zone_update', zone_data)
        _last_zone_emit = time.time()
        _gex_dirty = False

        # Forward zone data to EdgeDetector for GEX-weighted signals
        if _edge_detector:
            try:
                _edge_detector.update_zones(zone_data)
            except Exception:
                pass

        # ── Single source of truth for gamma flip ──
        # Push the same WS-computed flip into l2_worker so regime classifier
        # and edge_detector GEX zone check share the SAME flip level.
        # Previously l2_worker got flip from /api/data (HTTP, slower, different calc)
        # while edge_detector got it from here (WS, real-time) → contradiction.
        try:
            from background_engine.l2_worker import update_regime
            nq_spot = _latest_nq if _latest_nq > 0 else 0
            if nq_spot > 0:
                total_gex = sum(
                    -call_dollar_gex.get(k, 0) + put_dollar_gex.get(k, 0)
                    for k in sorted_strikes
                )
                update_regime(
                    spot=nq_spot,
                    gamma_flip=zone_data['gamma_flip'],   # already NQ-scaled
                    total_gex=total_gex,
                    call_wall=zone_data['call_wall'],
                    put_wall=zone_data['put_wall'],
                    iv_rv_spread=mean_iv - _nq_realized_vol if _nq_realized_vol > 0 else 0.0,
                )
        except Exception as e:
            print(f"[SCHWAB-BRIDGE] ⚠️ Regime sync error: {e}")

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


_book_logged = False

def _on_nasdaq_book(data):
    """Handle NASDAQ Level 2 book updates for QQQ — push to frontend + feed EdgeDetector."""
    global _book_logged
    if not _socketio:
        return
    symbol = data.get('symbol', 'UNKNOWN')
    bids = data.get('bids', [])
    asks = data.get('asks', [])

    # One-shot log to confirm MM MPID data
    if not _book_logged and bids:
        _book_logged = True
        print(f"[SCHWAB-BRIDGE] NASDAQ_BOOK sample ({symbol}):")
        for side_label, levels in [('BID', bids[:3]), ('ASK', asks[:3])]:
            for lvl in levels:
                mms = lvl.get('market_makers', [])
                ids = [m.get('id', '?') for m in mms[:8]]
                print(f"  {side_label} {lvl.get('price')}: size={lvl.get('size')} mm_count={lvl.get('mm_count')} MMs={ids}")

    # Emit raw book data for the Equity Book panel
    _socketio.emit('eq_book_update', {
        'symbol': symbol,
        'timestamp': data.get('timestamp', 0),
        'bids': bids[:15],   # top 15 levels
        'asks': asks[:15],
    })

    # Forward to MMTracker for market maker withdrawal detection
    if _mm_tracker:
        try:
            _mm_tracker.update(data)
        except Exception:
            pass


_screener_logged = False

def _on_screener_option(data):
    """Handle real-time options screener updates — unusual activity radar.
    
    Data from _process_screener now has:
      symbol: screener key (e.g., 'OPTION_ALL_VOLUME_0')
      items: list of dicts with named keys (symbol, description, lastPrice, 
             totalVolume, netChange, netPercentChange)
      sort_field, frequency, _timestamp
    """
    global _screener_logged
    if not _socketio:
        return

    # Log first raw response for field debugging
    if not _screener_logged:
        print(f"[SCHWAB-BRIDGE] SCREENER_OPTION data: items={len(data.get('items', []))}")
        items_sample = data.get('items', [])[:2]
        print(f"[SCHWAB-BRIDGE] SCREENER_OPTION sample items: {str(items_sample)[:500]}")
        _screener_logged = True

    items = data.get('items', [])
    if not items:
        return

    # Items have named keys from Schwab: symbol, description, lastPrice, 
    # totalVolume, netChange, netPercentChange
    alerts = []
    for item in items[:20]:
        if isinstance(item, dict):
            alerts.append({
                'symbol': item.get('symbol', ''),
                'description': item.get('description', ''),
                'volume': _safe_num(item.get('totalVolume', 0)),
                'percentChange': _safe_num(item.get('netPercentChange', 0)),
                'lastPrice': _safe_num(item.get('lastPrice', 0)),
                'netChange': _safe_num(item.get('netChange', 0)),
            })

    if alerts:
        _socketio.emit('screener_option_update', {
            'type': data.get('sort_field', 'VOLUME'),
            'alerts': alerts,
            'timestamp': data.get('_timestamp', 0),
        })


def _safe_num(val):
    """Safely convert a value to float, returning 0 on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0


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

