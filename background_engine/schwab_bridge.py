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
import logging
import threading
from datetime import datetime

log = logging.getLogger(__name__)

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

# ── Dealer session flow tracking ────────────────────────────────────────────
_session_dealer_buys = 0.0
_session_dealer_sells = 0.0
_prev_nq_for_hedge = 0.0
_hedge_wave_count = 0
_last_hedge_dir = 0       # +1 buying, -1 selling, 0 neutral


def set_socketio(sio):
    """Inject the Flask-SocketIO instance from server.py."""
    global _socketio
    _socketio = sio


def start_schwab_bridge():
    """Start the Schwab streamer bridge in a background thread."""
    global _bridge_running
    if _bridge_running:
        log.info("[SCHWAB-BRIDGE] Already running")
        return

    _bridge_running = True
    t = threading.Thread(target=_run_bridge, daemon=True)
    t.start()
    log.info("[SCHWAB-BRIDGE] Background thread spawned")


def _run_bridge():
    """Main bridge loop — initializes auth + streamer, subscribes, and bridges."""
    global _streamer
    # Import connectors
    import sys
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Initialize EdgeDetector FIRST — works with TopStepX alone (NQ-native signals)
    global _flow_classifier, _edge_detector
    try:
        from connectors.edge_detector import EdgeDetector
        _edge_detector = EdgeDetector(None, None, _socketio)
        _edge_detector.start()
        # Wire adverse selection + NQ engine getters from l2_worker
        try:
            from background_engine.l2_worker import (
                get_adverse_selection, _VPIN_ENGINES, _V2_HAWKES, _V2_KALMAN
            )
            _edge_detector._adverse_selection_fn = get_adverse_selection
            _edge_detector._get_vpin = lambda sym: _VPIN_ENGINES.get(sym)
            _edge_detector._get_hawkes = lambda sym: _V2_HAWKES.get(sym)
            _edge_detector._get_kalman = lambda sym: _V2_KALMAN.get(sym)
            log.info("[SCHWAB-BRIDGE] AdverseSelection + NQ engines → EdgeDetector wired")
        except ImportError:
            log.info("[SCHWAB-BRIDGE] ⚠️ NQ engine wiring not available")
        log.info("[SCHWAB-BRIDGE] EdgeDetector attached")

        # Initialize MMTracker — market maker withdrawal detection
        global _mm_tracker
        from connectors.mm_tracker import MMTracker
        _mm_tracker = MMTracker(edge_detector=_edge_detector)
        _edge_detector._mm_tracker_ref = _mm_tracker
        log.info("[SCHWAB-BRIDGE] MMTracker attached → EdgeDetector")

        # Initialize 0DTE Squeeze Detector — options dealer delta hedge tracking
        global _dte0_squeeze, _greek_surface
        try:
            from connectors.dte0_squeeze import DTE0SqueezeDetector
            _dte0_squeeze = DTE0SqueezeDetector(edge_detector=_edge_detector)
            log.info("[SCHWAB-BRIDGE] DTE0 Squeeze Detector attached → EdgeDetector")
        except ImportError as e:
            log.info(f"[SCHWAB-BRIDGE] ⚠️ DTE0 Squeeze Detector not available: {e}")

        # Initialize Greek Surface Engine — full sensitivity surface
        try:
            from connectors.greek_surface import GreekSurface
            _greek_surface = GreekSurface()
            _edge_detector._greek_surface = _greek_surface
            log.info("[SCHWAB-BRIDGE] GreekSurface engine attached → EdgeDetector")
        except ImportError as e:
            log.info(f"[SCHWAB-BRIDGE] ⚠️ GreekSurface not available: {e}")

        # Initialize Vol Surface Monitor — live volatility surface state machine
        global _vol_surface
        try:
            from connectors.vol_surface import VolSurface
            _vol_surface = VolSurface()
            _edge_detector._vol_surface = _vol_surface
            log.info("[SCHWAB-BRIDGE] VolSurface monitor attached → EdgeDetector")
        except ImportError as e:
            _vol_surface = None
            log.info(f"[SCHWAB-BRIDGE] ⚠️ VolSurface not available: {e}")

        # Initialize IV Calibrator — Tradier ORATS IV surface polling
        global _iv_calibrator
        try:
            from connectors.iv_calibrator import IVCalibrator
            _iv_calibrator = IVCalibrator(ticker='QQQ', poll_interval=300)
            _iv_calibrator.start()
            log.info("[SCHWAB-BRIDGE] IVCalibrator started (Tradier ORATS, 5-min poll)")
        except ImportError as e:
            _iv_calibrator = None
            log.info(f"[SCHWAB-BRIDGE] ⚠️ IVCalibrator not available: {e}")

        # Wire NQ detection forwarding: l2_worker → EdgeDetector (works without Schwab)
        try:
            from background_engine.l2_worker import (
                set_detection_callback, set_cross_asset_provider,
                set_trade_score_callback, set_nq_signal_callback
            )
            set_detection_callback(_edge_detector.on_nq_detection)
            set_cross_asset_provider(_edge_detector.get_cross_asset_context)
            set_trade_score_callback(_edge_detector.score_trade)
            set_nq_signal_callback(_edge_detector.check_nq_signals)
            log.info("[SCHWAB-BRIDGE] NQ detection ↔ EdgeDetector bidirectional wired + NQ signals")
        except ImportError:
            log.info("[SCHWAB-BRIDGE] ⚠️ l2_worker not available for NQ forwarding")

    except Exception as e:
        import traceback
        log.info(f"[SCHWAB-BRIDGE] ❌ EdgeDetector init failed: {e}")
        log.info(traceback.format_exc())

    # ── Schwab auth + streamer (optional — NQ signals work without it) ──
    try:
        log.info("[SCHWAB-BRIDGE] Initializing Schwab auth...")
        from connectors.schwab_auth import SchwabAuth
        from connectors.schwab_streamer import SchwabStreamer

        auth = SchwabAuth()
        if not auth.is_authenticated():
            log.info("[SCHWAB-BRIDGE] ⚠️ Schwab not authenticated — NQ-only mode (TopStepX signals active)")
            return

        log.info("[SCHWAB-BRIDGE] Authenticated, starting streamer...")
        _streamer = SchwabStreamer(auth)

        # Register callbacks
        _streamer.on('LEVELONE_FUTURES', _on_futures_quote)
        _streamer.on('LEVELONE_EQUITIES', _on_equity_quote)
        _streamer.on('LEVELONE_OPTIONS', _on_options_quote)
        _streamer.on('CHART_FUTURES', _on_chart_candle)
        _streamer.on('CHART_EQUITY', _on_chart_candle)
        _streamer.on('NASDAQ_BOOK', _on_nasdaq_book)
        _streamer.on('NYSE_BOOK', _on_nyse_book)
        _streamer.on('SCREENER_OPTION', _on_screener_option)
        _streamer.on('SCREENER_EQUITY', _on_screener_equity)
        _streamer.on('ACCT_ACTIVITY', _on_acct_activity)
        _streamer.on('CHART_EQUITY', _on_chart_equity)

        # Wire FlowClassifier + streamer into EdgeDetector (Schwab-dependent)
        from connectors.flow_classifier import FlowClassifier
        _flow_classifier = FlowClassifier(_streamer)
        _flow_classifier.start()
        _edge_detector._streamer = _streamer
        _edge_detector._flow = _flow_classifier
        # Re-attach Schwab book callbacks
        _streamer.on('NASDAQ_BOOK', _edge_detector._on_book)
        _streamer.on('SCREENER_OPTION', _edge_detector._on_screener)
        log.info("[SCHWAB-BRIDGE] FlowClassifier + Schwab streams → EdgeDetector attached")

        # Start the WebSocket connection
        _streamer.start()
        time.sleep(3)  # Let connection establish

        # Subscribe to key instruments
        _streamer.subscribe_futures(['/NQ', '/ES'])
        _streamer.subscribe_equities(['QQQ', 'SPY', 'VIX', '$NDX.X'])
        _streamer.subscribe_chart_futures(['/NQ', '/ES'])

        # Subscribe to NDX options for live GEX tracking
        _subscribe_qqq_options()

        # Subscribe to SPY + Mag7 options for cross-ticker 0DTE flow tracking.
        # Streamer symbol budget: QQQ (~200) + SPY (~80) + Mag7 (~140) = ~420 / 500.
        try:
            _subscribe_spy_options()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] SPY options subscription error: {e}")
        try:
            _subscribe_mag7_options()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Mag7 options subscription error: {e}")
        try:
            _subscribe_index_options()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Index options subscription error: {e}")

        # Subscribe to QQQ equity L2 book (NASDAQ_BOOK)
        _streamer.subscribe_nasdaq_book(['QQQ'])
        log.info("[SCHWAB-BRIDGE] Subscribed to NASDAQ_BOOK for QQQ")

        # Subscribe to SPY equity L2 book (NYSE_BOOK) — cross-market divergence
        _streamer.subscribe_nyse_book(['SPY'])
        log.info("[SCHWAB-BRIDGE] Subscribed to NYSE_BOOK for SPY")

        # Subscribe to options screener (volume-based unusual activity)
        try:
            _streamer.subscribe_screener_option('VOLUME', '0')
            log.info("[SCHWAB-BRIDGE] Subscribed to SCREENER_OPTION")
        except Exception as e:
            log.info(f"[SCHWAB-BRIDGE] SCREENER_OPTION subscription failed: {e}")

        # Subscribe to equity screener (NYSE + NASDAQ most-active)
        for exch in ('NYSE', 'NASDAQ'):
            try:
                _streamer.subscribe_screener_equity(exch, 'VOLUME', '0')
                log.info(f"[SCHWAB-BRIDGE] Subscribed to SCREENER_EQUITY ({exch})")
            except Exception as e:
                log.info(f"[SCHWAB-BRIDGE] SCREENER_EQUITY ({exch}) subscription failed: {e}")

        # Subscribe to CHART_EQUITY for real-time 1-min candles on key tickers
        try:
            _streamer.subscribe_chart_equity(['QQQ', 'SPY', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA'])
            log.info("[SCHWAB-BRIDGE] Subscribed to CHART_EQUITY (9 tickers)")
        except Exception as e:
            log.info(f"[SCHWAB-BRIDGE] CHART_EQUITY subscription failed: {e}")

        # Subscribe to account activity (user's own order fills, position changes)
        try:
            _streamer.subscribe_account_activity()
            log.info("[SCHWAB-BRIDGE] Subscribed to ACCT_ACTIVITY")
        except Exception as e:
            log.info(f"[SCHWAB-BRIDGE] ACCT_ACTIVITY subscription failed: {e}")

        # Initialize signed-flow accumulator (0DT-Hero-style curves per ticker)
        try:
            from connectors.flow_accumulator import init_accumulator
            init_accumulator(_socketio)
            log.info("[SCHWAB-BRIDGE] Signed flow accumulator initialised")
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Flow accumulator init failed: {e}")

        # Initialize alert engine (fed by flow_accumulator._emit_loop)
        try:
            from connectors.alert_engine import init_engine
            init_engine(socketio=_socketio)
            log.info("[SCHWAB-BRIDGE] AlertEngine initialised (fires 'flow_alert' events)")
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] AlertEngine init failed: {e}")

        # Populate expiration-metadata cache for all subscribed tickers so
        # flow trades can be bucketed as 0DTE / weekly / monthly / quarterly /
        # LEAPS in the accumulator + alert labels.
        try:
            from connectors.expiration_cache import init_cache
            from server import _schwab_get
            _exp_cache = init_cache(refresh_interval_sec=3600)
            _subscribed_tickers = ["QQQ", "SPY"] + list(MAG7_TICKERS) + list(INDEX_OPTION_TICKERS)
            _total_exps = 0
            for _t in _subscribed_tickers:
                _total_exps += _exp_cache.refresh(_t, _schwab_get)
            log.info(f"[SCHWAB-BRIDGE] Expiration cache: {_total_exps} entries across {len(_subscribed_tickers)} tickers")
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Expiration cache init failed: {e}")

        log.info("[SCHWAB-BRIDGE] All subscriptions active")

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
        log.info(f"[SCHWAB-BRIDGE] ❌ Bridge failed: {e}")
        log.info(traceback.format_exc())


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
                log.info(f"[SCHWAB-BRIDGE] QQQ REST unavailable, using live stream QQQ={qqq_spot:.2f}")
            else:
                log.info(f"[SCHWAB-BRIDGE] ❌ No live QQQ spot available — aborting options subscription")
                return

        # Get nearest 2 expirations (0DTE + next)
        raw_dates = _schwab_expirations("QQQ")
        if not raw_dates:
            log.info("[SCHWAB-BRIDGE] ⚠️ No QQQ expirations — skipping options subscription")
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
                log.info(f"[SCHWAB-BRIDGE] ⚠️ Chain fetch failed for {exp_date}: {e}")

        if not symbols:
            log.info(f"[SCHWAB-BRIDGE] ⚠️ No QQQ options near ATM={atm}")
            return

        _ndx_option_symbols = symbols[:200]  # Cap at 200 (wider chain coverage)
        _streamer.subscribe_options(_ndx_option_symbols)
        # Also subscribe to OPTIONS_BOOK (Level 2 depth) for flow classification
        _streamer.subscribe_options_book(_ndx_option_symbols[:20])  # Cap L2 at 20 to keep bandwidth reasonable
        log.info(f"[SCHWAB-BRIDGE] 📊 Subscribed to {len(_ndx_option_symbols)} QQQ options "
              f"(ATM≈{atm}, exps={exp_dates}) + OPTIONS_BOOK L2 for top 20")

    except Exception as e:
        import traceback
        log.info(f"[SCHWAB-BRIDGE] ⚠️ QQQ options subscription failed: {e}")
        log.info(traceback.format_exc())


# Tracked symbols per ticker — used by flow accumulator for ticker classification.
# Maps ticker -> list of Schwab option symbols we've subscribed to.
_subscribed_option_symbols_by_ticker: dict[str, list] = {}


def _subscribe_options_for_ticker(
    ticker: str,
    strike_radius: float = None,
    expiries_count: int = 2,
    cap: int = 80,
) -> int:
    """
    Subscribe to LEVELONE_OPTIONS for ATM contracts on a given ticker.

    Mirrors _subscribe_qqq_options but generalized so SPY + Mag7 can reuse it.

    strike_radius defaults to 6% of spot (wide enough to capture institutional
    hedge put strikes that drive the biggest flow-dump alerts).
    expiries_count: how many nearest expirations to subscribe (3 = 0DTE +
    next weekly + next monthly for a ticker with monthly Fridays).
    cap: max contracts per ticker to avoid blowing through Schwab's symbol limit.

    Returns number of contracts subscribed.
    """
    try:
        from server import _schwab_expirations, _schwab_chain_raw, _schwab_quote

        spot = _schwab_quote(ticker)
        if not spot or spot <= 0:
            log.warning(f"[SCHWAB-BRIDGE] {ticker}: no live spot, skipping options subscription")
            return 0

        raw_dates = _schwab_expirations(ticker)
        if not raw_dates:
            log.warning(f"[SCHWAB-BRIDGE] {ticker}: no expirations, skipping")
            return 0

        exp_dates = raw_dates[:expiries_count]
        radius = strike_radius if strike_radius is not None else max(4.0, spot * 0.06)

        symbols = []
        for exp_date in exp_dates:
            try:
                chain, _ = _schwab_chain_raw(ticker, exp_date)
                for opt in chain:
                    strike = float(opt.get("strike", 0))
                    sym = opt.get("symbol", "")
                    if sym and abs(strike - spot) <= radius:
                        symbols.append(sym)
            except Exception as e:
                log.warning(f"[SCHWAB-BRIDGE] {ticker}: chain fetch failed for {exp_date}: {e}")

        if not symbols:
            log.warning(f"[SCHWAB-BRIDGE] {ticker}: no options within ±{radius:.2f} of spot={spot:.2f}")
            return 0

        symbols = symbols[:cap]
        _streamer.subscribe_options(symbols)
        _subscribed_option_symbols_by_ticker[ticker] = symbols
        log.info(
            f"[SCHWAB-BRIDGE] 📊 {ticker}: subscribed to {len(symbols)} options "
            f"(spot={spot:.2f}, radius=±{radius:.2f}, exps={exp_dates})"
        )
        return len(symbols)
    except Exception as e:
        import traceback
        log.warning(f"[SCHWAB-BRIDGE] {ticker} options subscription failed: {e}")
        log.warning(traceback.format_exc())
        return 0


def _subscribe_spy_options() -> int:
    """SPY: wide chain coverage for accurate flow-dump/cross detection.
    ATM ±$30 (~4% of spot $711), 6 expiries (0DTE + next 5 weekly/monthly),
    cap 400. Targets ~300 contracts in practice.
    """
    return _subscribe_options_for_ticker("SPY", strike_radius=30.0, expiries_count=6, cap=400)


# Mag7 — each gets a 6%-of-spot window + 3 nearest expiries (captures 0DTE +
# next weekly + next monthly). Per-ticker cap 60 → ~40-60 contracts each.
# Combined target: QQQ 200 + SPY 300 + Mag7 ~320 = ~820 symbols.
# Tests whether Schwab streamer accepts 800+ simultaneous options.
MAG7_TICKERS = ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA")


def _subscribe_mag7_options() -> int:
    """Subscribe to ATM options (3 expiries) for each Mag7 name."""
    total = 0
    for ticker in MAG7_TICKERS:
        n = _subscribe_options_for_ticker(
            ticker, strike_radius=None, expiries_count=3, cap=60
        )
        total += n
    log.info(f"[SCHWAB-BRIDGE] 📊 Mag7 total: {total} options across {len(MAG7_TICKERS)} tickers")
    return total


# Index options — entitled on the standard Schwab API tier. Verified 2026-04-20:
# $SPX (SPXW/SPX roots), $NDX (NDXP/NDX), $VIX (VIXW/VIX) all return live chains
# with full Greeks. SPX is the headline signal; NDX + VIX round out the macro picture.
INDEX_OPTION_TICKERS = ("$SPX", "$NDX", "$VIX")


def _subscribe_index_options() -> int:
    """Subscribe to ATM index options for $SPX / $NDX / $VIX.

    Strike radii are tuned to each index's tick scale:
      - SPX: ±150 pts (~2% of 7100 spot)
      - NDX: ±500 pts (~1.9% of 26500 spot)
      - VIX: ±8 pts (very wide relative to 19 spot — VIX trades wide OTM puts)
    """
    cfgs = {
        "$SPX": dict(strike_radius=150.0, expiries_count=3, cap=150),
        "$NDX": dict(strike_radius=500.0, expiries_count=3, cap=100),
        "$VIX": dict(strike_radius=8.0,   expiries_count=3, cap=40),
    }
    total = 0
    for ticker, cfg in cfgs.items():
        try:
            n = _subscribe_options_for_ticker(ticker, **cfg)
            total += n
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] {ticker} index-options subscription error: {e}")
    log.info(f"[SCHWAB-BRIDGE] 📊 Index options total: {total} contracts ($SPX + $NDX + $VIX)")
    return total


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
_last_option_update_ts = 0.0  # timestamp of most recent option quote from Schwab


def _get_nq_mid():
    """Get best available NQ price: TopStepX L2 (fastest) → Schwab futures → 0."""
    try:
        from background_engine.l2_worker import L2_STATE
        nq = L2_STATE["mid_prices"].get("NQ", 0)
        if nq > 0:
            return nq
    except Exception:
        pass
    return _latest_nq if _latest_nq > 0 else 0


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

    # Universal spot cache (used by screener→accumulator bridge for delta est)
    if symbol and not symbol.startswith('$'):
        _latest_spot_by_ticker[symbol] = last

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

    # Emit venue-tagged equity trade for tape routing
    # Layer 3: "604.63 x6 SELL executed at BATS | bid was ARCX | ask was XNAS"
    # Schwab streaming only sends changed fields, so bid_mic/ask_mic/last_mic
    # may arrive without last_size. Cache the latest MIC codes per symbol.
    if _socketio and symbol in ('QQQ', 'SPY'):
        _eq_mic = getattr(_on_equity_quote, '_eq_mic', {})
        sym_mic = _eq_mic.setdefault(symbol, {'bid': '', 'ask': '', 'last': '', 'size': 0})
        # Update cached values only when present (streaming sends deltas)
        raw_bid = data.get('bid_mic', '')
        raw_ask = data.get('ask_mic', '')
        raw_last = data.get('last_mic', '')
        if raw_bid:  sym_mic['bid']  = str(raw_bid)
        if raw_ask:  sym_mic['ask']  = str(raw_ask)
        if raw_last: sym_mic['last'] = str(raw_last)
        if data.get('last_size'): sym_mic['size'] = int(data['last_size'])
        _on_equity_quote._eq_mic = _eq_mic

        if sym_mic['bid'] or sym_mic['ask'] or sym_mic['last']:
            _socketio.emit('equity_tape', {
                'symbol':   symbol,
                'price':    round(last, 2),
                'size':     sym_mic['size'],
                'side':     'b' if bid > 0 and last >= ask else ('s' if ask > 0 and last <= bid else ''),
                'exec_mic': sym_mic['last'],
                'bid_mic':  sym_mic['bid'],
                'ask_mic':  sym_mic['ask'],
                'ts':       time.time(),
            })

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
    global _gex_dirty, _last_option_update_ts
    _last_option_update_ts = time.time()

    # (flow accumulator feed moved AFTER cache merge — see bottom of function)
    # Schwab sends delta-only updates; raw `data` often lacks delta/dte/spot.
    # We need to call accumulator with the MERGED cached view.

    strike = data.get('strike', 0)
    contract_type = data.get('contract_type', '')

    # ── Schwab delta-update fallback ──────────────────────────────
    # Schwab sends delta-only updates: strike/contract_type only in initial snapshot.
    # Parse from symbol (format: "QQQ   YYMMDDTSSSSSSSS") as fallback.
    if (not strike or strike <= 0 or not contract_type):
        sym = data.get('symbol', '')
        if len(sym) >= 15:
            try:
                # Strip underlying padding, last 9 chars = T + 8-digit strike*1000
                tail = sym.rstrip()  # e.g. "QQQ   260410P00567000"
                ct_char = tail[-9]   # 'C' or 'P'
                strike_raw = int(tail[-8:]) / 1000.0
                if strike_raw > 0:
                    if not strike or strike <= 0:
                        strike = strike_raw
                    if not contract_type:
                        contract_type = ct_char
            except (ValueError, IndexError):
                pass

    # ── Per-symbol cache: merge delta fields with prior full snapshot ──
    _sym_cache = getattr(_on_options_quote, '_sym_cache', {})
    _on_options_quote._sym_cache = _sym_cache
    sym_key = data.get('symbol', '')
    if sym_key:
        cached = _sym_cache.get(sym_key, {})
        # Store any new fields from this update
        for k, v in data.items():
            if v is not None and v != 0 and v != '':
                cached[k] = v
        _sym_cache[sym_key] = cached

        # Feed signed-flow accumulator with the MERGED cached view so that
        # delta/dte/underlying_price are always populated (Schwab sends
        # delta-only updates where these fields are absent on most messages).
        # Only count actual trades — last_size must be in THIS incoming delta
        # (not cached) so we don't re-process stale trades.
        if data.get('last_size', 0) > 0:
            try:
                from connectors.flow_accumulator import get_accumulator
                _acc = get_accumulator()
                if _acc is not None:
                    # Build a merged snapshot: cached fills in missing fields,
                    # but keep the current message's last_size/last/trade_time
                    # so dedup works correctly.
                    merged = dict(cached)  # cache has everything accumulated
                    # Override with fresh trade-specific fields from this msg
                    for k in ('last_size', 'last', 'trade_time', 'bid', 'ask'):
                        if data.get(k) is not None:
                            merged[k] = data[k]
                    _acc.on_option_update(merged)
            except Exception as _:
                pass
    else:
        cached = {}

    gamma = data.get('gamma', cached.get('gamma', 0))
    delta = data.get('delta', cached.get('delta', 0))
    oi = data.get('open_interest', cached.get('open_interest', 0))
    vol = data.get('total_volume', data.get('volume', cached.get('total_volume', 0)))
    mark = data.get('mark', data.get('last', cached.get('mark', 0)))
    iv = data.get('implied_vol', cached.get('implied_vol', 0))
    theta = data.get('theta', cached.get('theta', 0))
    vega = data.get('vega', cached.get('vega', 0))
    rho = data.get('rho', cached.get('rho', 0))
    dte = data.get('dte', cached.get('dte', 0))
    underlying_price = data.get('underlying_price', cached.get('underlying_price', 0))

    # ── Enriched fields (Phase 1) ──────────────────────────────────
    security_status = data.get('security_status', '')
    quote_time = data.get('quote_time', 0)
    trade_time = data.get('trade_time', cached.get('trade_time', 0))
    mark_change = data.get('mark_change', cached.get('mark_change', 0))
    mark_pct_change = data.get('mark_pct_change', 0)
    theoretical_value = data.get('theoretical_value', 0)
    intrinsic_value = data.get('intrinsic_value', 0)
    open_price = data.get('open', 0)
    indicative_bid = data.get('indicative_bid', 0)
    indicative_ask = data.get('indicative_ask', 0)
    exercise_type = data.get('exercise_type', '')
    exp_type = data.get('exp_type', '')

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

    # ── Emit option_mark_update for dealer_hedge_monitor ───────────
    # Emit on any options update with valid strike/iv. Schwab streaming sends
    # delta-only updates — mark may be absent. Fall back to cached mark.
    # Throttled: max once per 500ms per contract key to avoid socket flood.
    _emit_mark = mark
    if not _emit_mark and strike in _live_gex:
        side_key = 'call_mark' if contract_type in ('C', 'CALL', 'call') else 'put_mark'
        _emit_mark = _live_gex[strike].get(side_key, 0)
    if _socketio and (_emit_mark or iv):
        try:
            _opt_emit_last = getattr(_on_options_quote, '_emit_ts', {})
            contract_key = f"{strike}{'C' if contract_type in ('C','CALL','call') else 'P'}"
            now_t = time.time()
            if now_t - _opt_emit_last.get(contract_key, 0) >= 0.5:
                _opt_emit_last[contract_key] = now_t
                _on_options_quote._emit_ts = _opt_emit_last
                import math
                _payload = {
                    'strike':       float(strike or 0),
                    'side':         'C' if contract_type in ('C','CALL','call') else 'P',
                    'mark':         round(float(_emit_mark or 0), 4),
                    'mark_change':  round(float(mark_change or 0), 4),
                    'vol':          int(vol or 0),
                    'oi':           int(oi or 0),
                    'delta':        round(float(delta or 0), 5),
                    'gamma':        round(float(gamma or 0), 6),
                    'iv':           round(float(iv or 0), 4),
                    'theta':        round(float(theta or 0), 5),
                    'dte':          int(dte or 0),
                    'underlying':   round(float(underlying_price or _latest_qqq or 0), 4),
                    'trade_time':   int(trade_time or 0),
                    'dollar_gex':   round(float(dollar_gex or 0) / 1e6, 4),
                }
                # Sanitize NaN/Inf which break JSON serialization
                for _k, _v in _payload.items():
                    if isinstance(_v, float) and (math.isnan(_v) or math.isinf(_v)):
                        _payload[_k] = 0.0
                _socketio.emit('option_mark_update', _payload)
        except Exception:
            pass


def _maybe_emit_zones():
    """Recalculate and emit GEX zones if data has changed (max every 5s)."""
    global _gex_dirty, _last_zone_emit

    if not _socketio:
        return
    # If option stream stopped (after-hours) but we have valid GEX data
    # and NQ is still moving (from TopStepX), re-emit every 30s with stale data
    if not _gex_dirty:
        if _live_gex and len(_live_gex) >= 3 and time.time() - _last_zone_emit >= 30.0:
            _gex_dirty = True  # force re-emit with existing option data
        else:
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

        # NQ price — TopStepX L2 is PRIMARY (fastest, 24/5), Schwab is secondary
        nq_live = _get_nq_mid()
        if nq_live <= 0:
            log.info("[SCHWAB-BRIDGE] ⚠️ No NQ from TopStepX or Schwab — skipping zone emit")
            return

        # Update Schwab's NQ cache so ratio stays current even if Schwab stream drops
        if nq_live > 0 and _latest_nq <= 0:
            globals()['_latest_nq'] = nq_live

        # QQQ spot — live Schwab preferred, derive from NQ if equity stream is down
        if _latest_qqq > 0:
            ndx_spot = _latest_qqq
            ratio_source = "LIVE_QQQ"
        elif _latest_ndx > 0:
            ndx_spot = _latest_ndx
            ratio_source = "LIVE_NDX"
        elif nq_live > 0 and _nq_qqq_ratio > 0:
            # After hours: derive QQQ from NQ/ratio (TopStepX NQ keeps this alive)
            ndx_spot = nq_live / _nq_qqq_ratio
            ratio_source = "DERIVED_QQQ"
        else:
            log.info("[SCHWAB-BRIDGE] ⚠️ No QQQ/NDX and no cached ratio — skipping zone emit")
            return

        # NQ/QQQ ratio — MUST be computed from live feeds
        if nq_live > 0 and ndx_spot > 0:
            ratio = nq_live / ndx_spot
        elif _nq_qqq_ratio > 0:
            ratio = _nq_qqq_ratio  # cached from last live computation
            ratio_source = f"CACHED_RATIO({_nq_qqq_ratio:.4f})"
        else:
            log.info("[SCHWAB-BRIDGE] ⚠️ No live NQ feed — cannot compute ratio, skipping zone emit")
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
                log.info(f"[SCHWAB-BRIDGE] ⚠️ GreekSurface compute error: {e}")

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
            except Exception as e:
                log.warning("IV calibration merge failed: %s", e, exc_info=True)

        # ── Feed VolSurface monitor and merge regime data ─────────────────
        if _vol_surface:
            try:
                # Feed VPIN from l2_worker into HMM regime detector
                try:
                    from background_engine.l2_worker import _VPIN_ENGINES
                    _nq_vpin = _VPIN_ENGINES.get('NQ')
                    if _nq_vpin:
                        _vol_surface.set_vpin(_nq_vpin.vpin)
                except Exception:
                    pass
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
                log.info(f"[SCHWAB-BRIDGE] ⚠️ VolSurface error: {e}")

        # ── Pull adverse selection score + copula joint confidence ───────
        try:
            from background_engine.l2_worker import get_adverse_selection
            _as = get_adverse_selection('NQ')
            if _as is not None:
                _st = _as.get_state()
                if _st:
                    zone_data['adverse_selection_score'] = _st.get('adverse_selection_score', 0)
        except Exception as e:
            log.debug("adverse_selection fetch failed: %s", e)

        try:
            if _edge_detector is not None and getattr(_edge_detector, '_copula', None):
                _cs = _edge_detector._copula.get_correlation_estimate()
                zone_data['copula_joint'] = _cs.get('last_joint', 50.0)
                zone_data['copula_rho_bar'] = _cs.get('rho_bar', 0.3)
        except Exception as e:
            log.debug("copula fetch failed: %s", e)

        _socketio.emit('zone_update', zone_data)
        _last_zone_emit = time.time()
        _gex_dirty = False

        # ── Dealer session hedge flow computation ────────────────────────
        # Uses aggregate GEX to estimate NQ hedging per zone cycle.
        # GEX > 0 = dealer long gamma: price UP → SELL, price DN → BUY (dampening)
        # GEX < 0 = dealer short gamma: price UP → BUY, price DN → SELL (amplifying)
        global _session_dealer_buys, _session_dealer_sells, _prev_nq_for_hedge
        global _hedge_wave_count, _last_hedge_dir
        nq_now = nq_live if nq_live > 0 else 0  # nq_live already has TopStepX fallback
        _ratio = zone_data.get('ratio', 41.39) or 41.39
        spot_qqq = _latest_qqq if _latest_qqq > 0 else (nq_now / _ratio if _ratio > 0 else 0)

        # Compute total GEX from dealer_net_gex (in scope from zone computation)
        _total_gex = sum(dealer_net_gex.get(k, 0) for k in sorted_strikes)
        _total_gex_m = round(_total_gex / 1e6, 2)
        _total_dex_m = round(total_net_dex / 1e6, 2)

        # Determine gamma regime from flip position
        _flip_nq = zone_data.get('gamma_flip', 0)
        if nq_now > 0 and _flip_nq > 0:
            if nq_now > _flip_nq:
                _gamma_regime = 'LONG_GAMMA'
            elif nq_now < _flip_nq:
                _gamma_regime = 'SHORT_GAMMA'
            else:
                _gamma_regime = 'AT_FLIP'
        else:
            _gamma_regime = 'UNKNOWN'

        if _prev_nq_for_hedge > 0 and nq_now > 0 and spot_qqq > 0 and abs(nq_now - _prev_nq_for_hedge) > 0.5:
            move_qqq = (nq_now - _prev_nq_for_hedge) / _ratio
            divisor = spot_qqq * 20 * _ratio
            cycle_nq = abs(_total_gex * move_qqq) / divisor if divisor > 0 else 0

            if _total_gex > 0:  # Long gamma (dampening)
                if move_qqq > 0:
                    _session_dealer_sells += cycle_nq
                else:
                    _session_dealer_buys += cycle_nq
            else:  # Short gamma (amplifying)
                if move_qqq > 0:
                    _session_dealer_buys += cycle_nq
                else:
                    _session_dealer_sells += cycle_nq

            cur_dir = 1 if move_qqq > 0 else -1
            if cur_dir != _last_hedge_dir and _last_hedge_dir != 0:
                _hedge_wave_count += 1
            _last_hedge_dir = cur_dir
        _prev_nq_for_hedge = nq_now

        net_pos = _session_dealer_buys - _session_dealer_sells
        _opt_age = round(time.time() - _last_option_update_ts, 1) if _last_option_update_ts > 0 else -1
        _socketio.emit('dealer_session_flow', {
            'session_buys': round(_session_dealer_buys, 1),
            'session_sells': round(_session_dealer_sells, 1),
            'net_position': round(net_pos, 1),
            'hedge_wave_count': _hedge_wave_count,
            'gamma_regime': _gamma_regime,
            'net_gex_m': _total_gex_m,
            'net_dex_m': _total_dex_m,
            'flip_strike': _flip_nq,
            'nq_source': 'topstepx' if _get_nq_mid() != _latest_nq else 'schwab',
            'option_age_s': _opt_age,  # seconds since last Schwab option update
            'ts': time.time(),
        })

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
            nq_spot = nq_live if nq_live > 0 else 0
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
            log.info(f"[SCHWAB-BRIDGE] ⚠️ Regime sync error: {e}")

    except Exception as e:
        import traceback
        log.info(f"[SCHWAB-BRIDGE] ⚠️ Zone emit error: {e}")
        traceback.print_exc()


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

    # NQ candles come from TopStepX l2_worker — don't duplicate from Schwab
    if chart_sym in ('NQ', 'ES'):
        return

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


# ── Venue taxonomy: slow-cancel (real depth) vs fast-cancel (HFT, evaporate on sweep) ──
# SLOW venues: NASDAQ, ARCA, PHLX, CBOE — institutional, survive aggressive sweeps
# FAST venues: EDGX, BATX, MEMX, EDGA, BATY, IEXG — HFT, pull quotes in <1ms on sweep
_SLOW_VENUES = frozenset({'NSDQ', 'arcx', 'phlx', 'cinn', 'cboe', 'bos', 'NYSE'})
_FAST_VENUES = frozenset({'edgx', 'batx', 'memx', 'edga', 'baty', 'iexg', 'drctedge'})

def _analyze_book_level(lvl):
    """Compute microstructure quality metrics for a single NASDAQ_BOOK price level.
    
    Returns a dict with:
      price         : float — price level
      size          : int   — total contracts
      mm_count      : int   — number of venues/MMs quoting this level
      avg_lot_size  : float — size / mm_count (high = institutional block)
      venue_conc    : float — 1/mm_count (high = single venue = thin/HFT)
      slow_venues   : int   — count of slow-cancel institutional venues
      fast_venues   : int   — count of fast-cancel HFT venues
      has_primary   : bool  — NSDQ or arcx present (slow venue = depth survives sweep)
      depth_quality : float — 0.0–1.0 quality score (high = institutional, low = HFT noise)
      venues        : list  — venue MPID strings
    """
    price    = lvl.get('price', 0)
    size     = lvl.get('size', 0)
    mm_count = max(lvl.get('mm_count', 1), 1)
    mms      = lvl.get('market_makers', [])
    ids      = [m.get('id', '').lower() for m in mms]
    
    slow_count = sum(1 for v in ids if v in _SLOW_VENUES or v.upper() in _SLOW_VENUES)
    fast_count = sum(1 for v in ids if v in _FAST_VENUES or v.upper() in _FAST_VENUES)
    has_primary = any(v in ('nsdq', 'arcx') or v.upper() in ('NSDQ', 'ARCX') for v in ids)
    
    avg_lot  = round(size / mm_count, 1)
    v_conc   = round(1.0 / mm_count, 3)
    
    # Quality score: penalize HFT-only levels, reward primary exchange presence
    # slow_ratio: fraction of venues that are institutional
    slow_ratio = slow_count / mm_count if mm_count > 0 else 0
    # size_score: larger avg lot = more institutional conviction
    size_score = min(avg_lot / 500.0, 1.0)  # caps at 500 shares avg
    # Primary bonus: NSDQ or arcx = this quote will NOT evaporate on sweep
    primary_bonus = 0.3 if has_primary else 0.0
    depth_quality = round(min((slow_ratio * 0.4 + size_score * 0.3 + primary_bonus), 1.0), 3)
    
    return {
        'price':         price,
        'size':          size,
        'mm_count':      mm_count,
        'avg_lot_size':  avg_lot,
        'venue_conc':    v_conc,
        'slow_venues':   slow_count,
        'fast_venues':   fast_count,
        'has_primary':   has_primary,
        'depth_quality': depth_quality,
        'venues':        [m.get('id', '?') for m in mms[:8]],
    }


_book_logged = False
_last_book_ms_emit = 0.0  # throttle microstructure emit to 2Hz

def _on_nasdaq_book(data):
    """Handle NASDAQ Level 2 book updates for QQQ — push to frontend + feed EdgeDetector."""
    global _book_logged, _last_book_ms_emit
    if not _socketio:
        return
    symbol = data.get('symbol', 'UNKNOWN')
    bids = data.get('bids', [])
    asks = data.get('asks', [])

    # One-shot log to confirm MM MPID data
    if not _book_logged and bids:
        _book_logged = True
        log.info(f"[SCHWAB-BRIDGE] NASDAQ_BOOK sample ({symbol}):")
        for side_label, levels in [('BID', bids[:3]), ('ASK', asks[:3])]:
            for lvl in levels:
                mms = lvl.get('market_makers', [])
                ids = [m.get('id', '?') for m in mms[:8]]
                log.info(f"  {side_label} {lvl.get('price')}: size={lvl.get('size')} mm_count={lvl.get('mm_count')} MMs={ids}")

    # ── Book Microstructure Analysis ──────────────────────────────────────────
    # Compute quality metrics per level and aggregate into BBO-level signals.
    # Throttled to 2Hz to avoid flooding the frontend.
    now_ms = time.time()
    if now_ms - _last_book_ms_emit >= 0.5:
        _last_book_ms_emit = now_ms
        
        bid_levels = [_analyze_book_level(lvl) for lvl in bids[:10]]
        ask_levels = [_analyze_book_level(lvl) for lvl in asks[:10]]
        
        # BBO quality (best bid / best ask)
        bbo_bid = bid_levels[0] if bid_levels else {}
        bbo_ask = ask_levels[0] if ask_levels else {}
        
        # Aggregate quality: weighted by size (larger levels matter more)
        def _weighted_quality(levels):
            total_size = sum(l['size'] for l in levels) or 1
            return round(sum(l['depth_quality'] * l['size'] / total_size for l in levels), 3)
        
        bid_q = _weighted_quality(bid_levels) if bid_levels else 0
        ask_q = _weighted_quality(ask_levels) if ask_levels else 0
        
        # HFT ratio: fraction of BBO dominated by fast-cancel venues only
        bbo_hft = (bbo_bid.get('fast_venues', 0) > 0 and bbo_bid.get('slow_venues', 0) == 0) or                   (bbo_ask.get('fast_venues', 0) > 0 and bbo_ask.get('slow_venues', 0) == 0)
        
        # Quality-adjusted imbalance: size * quality (filters HFT phantom depth)
        bid_q_size = sum(l['size'] * l['depth_quality'] for l in bid_levels)
        ask_q_size = sum(l['size'] * l['depth_quality'] for l in ask_levels)
        total_q = bid_q_size + ask_q_size
        qa_imbalance = round((bid_q_size - ask_q_size) / total_q, 3) if total_q > 0 else 0.0
        # +1.0 = all quality depth on bid side (strong buy wall)
        # -1.0 = all quality depth on ask side (strong sell wall)
        
        _socketio.emit('book_microstructure', {
            'symbol':       symbol,
            'ts':           now_ms,
            # BBO quality
            'bbo_bid':      bbo_bid,
            'bbo_ask':      bbo_ask,
            'bbo_hft_only': bbo_hft,  # True = BBO quotes will evaporate on any sweep
            # Aggregate depth quality
            'bid_quality':  bid_q,
            'ask_quality':  ask_q,
            # Quality-adjusted imbalance: the REAL order flow signal (not phantom HFT depth)
            'qa_imbalance': qa_imbalance,
            # Top levels with full venue detail
            'bid_levels':   bid_levels[:5],
            'ask_levels':   ask_levels[:5],
        })
        
        # Forward quality-adjusted imbalance to EdgeDetector
        if _edge_detector:
            try:
                _edge_detector.on_book_microstructure(symbol, qa_imbalance, bid_q, ask_q, bbo_hft)
            except AttributeError:
                pass  # EdgeDetector may not have this method yet
            except Exception:
                pass

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


# ── NYSE_BOOK handler for SPY — same analysis pipeline, separate throttle ──
_last_nyse_ms_emit = 0.0
_nyse_book_logged = False
# Latest SPY microstructure for cross-market divergence
_spy_ms = {'qa_imbalance': 0.0, 'bid_quality': 0.0, 'ask_quality': 0.0,
           'bbo_hft_only': False, 'ts': 0.0}

def _on_nyse_book(data):
    """Handle NYSE Level 2 book updates for SPY — same analysis as QQQ NASDAQ_BOOK."""
    global _nyse_book_logged, _last_nyse_ms_emit
    if not _socketio:
        return
    symbol = data.get('symbol', 'UNKNOWN')
    bids = data.get('bids', [])
    asks = data.get('asks', [])

    if not _nyse_book_logged and bids:
        _nyse_book_logged = True
        log.info(f"[SCHWAB-BRIDGE] NYSE_BOOK sample ({symbol}):")
        for side_label, levels in [('BID', bids[:3]), ('ASK', asks[:3])]:
            for lvl in levels:
                mms = lvl.get('market_makers', [])
                ids = [m.get('id', '?') for m in mms[:8]]
                log.info(f"  {side_label} {lvl.get('price')}: size={lvl.get('size')} "
                      f"mm_count={lvl.get('mm_count')} MMs={ids}")

    now_ms = time.time()
    if now_ms - _last_nyse_ms_emit >= 0.5:
        _last_nyse_ms_emit = now_ms

        bid_levels = [_analyze_book_level(lvl) for lvl in bids[:10]]
        ask_levels = [_analyze_book_level(lvl) for lvl in asks[:10]]

        bbo_bid = bid_levels[0] if bid_levels else {}
        bbo_ask = ask_levels[0] if ask_levels else {}

        def _weighted_quality(levels):
            total_size = sum(l['size'] for l in levels) or 1
            return round(sum(l['depth_quality'] * l['size'] / total_size for l in levels), 3)

        bid_q = _weighted_quality(bid_levels) if bid_levels else 0
        ask_q = _weighted_quality(ask_levels) if ask_levels else 0

        bbo_hft = ((bbo_bid.get('fast_venues', 0) > 0 and bbo_bid.get('slow_venues', 0) == 0) or
                   (bbo_ask.get('fast_venues', 0) > 0 and bbo_ask.get('slow_venues', 0) == 0))

        bid_q_size = sum(l['size'] * l['depth_quality'] for l in bid_levels)
        ask_q_size = sum(l['size'] * l['depth_quality'] for l in ask_levels)
        total_q = bid_q_size + ask_q_size
        qa_imbalance = round((bid_q_size - ask_q_size) / total_q, 3) if total_q > 0 else 0.0

        # Cache for divergence computation
        _spy_ms['qa_imbalance'] = qa_imbalance
        _spy_ms['bid_quality'] = bid_q
        _spy_ms['ask_quality'] = ask_q
        _spy_ms['bbo_hft_only'] = bbo_hft
        _spy_ms['ts'] = now_ms

        # Emit SPY microstructure on same event (symbol-tagged)
        _socketio.emit('book_microstructure', {
            'symbol':       symbol,
            'ts':           now_ms,
            'bbo_bid':      bbo_bid,
            'bbo_ask':      bbo_ask,
            'bbo_hft_only': bbo_hft,
            'bid_quality':  bid_q,
            'ask_quality':  ask_q,
            'qa_imbalance': qa_imbalance,
            'bid_levels':   bid_levels[:5],
            'ask_levels':   ask_levels[:5],
        })

        if _edge_detector:
            try:
                _edge_detector.on_book_microstructure(symbol, qa_imbalance, bid_q, ask_q, bbo_hft)
            except (AttributeError, Exception):
                pass

    # Raw book for frontend
    _socketio.emit('eq_book_update', {
        'symbol': symbol,
        'timestamp': data.get('timestamp', 0),
        'bids': bids[:15],
        'asks': asks[:15],
    })

    if _mm_tracker:
        try:
            _mm_tracker.update(data)
        except Exception:
            pass


# Per-contract state cached between screener snapshots. Used to compute
# incremental volume (delta trades since last update) and tick direction
# (side inference: price up = buyer, price down = seller).
_screener_prev: dict[str, dict] = {}  # symbol -> {volume, lastPrice}

# Ticker → latest spot price. Populated from CHART_EQUITY bars + LEVELONE_EQUITIES.
# Used by the screener→accumulator bridge to estimate delta for 0DTE contracts.
_latest_spot_by_ticker: dict[str, float] = {}

_screener_logged = False

def _tape_spot_for(ticker: str) -> float:
    """Latest underlying spot for a ticker. Fallback chain:
      1. _latest_spot_by_ticker (populated by CHART_EQUITY + LEVELONE_EQUITIES)
      2. Hard-coded _latest_qqq legacy cache
    """
    sp = _latest_spot_by_ticker.get(ticker, 0.0)
    if sp > 0:
        return sp
    if ticker == 'QQQ':
        return _latest_qqq
    return 0.0


def _estimate_delta_and_dte(symbol: str, last_price: float, spot: float) -> tuple:
    """Best-effort delta + DTE + bucket from a Schwab option symbol.

    Schwab symbol layout: `TICKER(6) YYMMDD C|P strike(8-digit × 1000)`
      e.g. 'SPY   260420C00710000' → SPY, 2026-04-20, call, strike 710.00

    Delta estimate: very simple moneyness proxy (better: use greek_surface
    but it's only populated for QQQ).
      ATM call: ~0.5, deep ITM call: ~0.9, deep OTM call: ~0.1
      Linear interpolation over ±5% of spot.
    """
    try:
        if len(symbol) < 15:
            return (0.0, -1)
        yy, mm, dd = symbol[6:8], symbol[8:10], symbol[10:12]
        ct = symbol[12]  # 'C' or 'P'
        strike_raw = int(symbol[13:21]) / 1000.0
        from datetime import date as _date
        exp = _date(2000 + int(yy), int(mm), int(dd))
        dte = (exp - _date.today()).days
        if spot <= 0 or strike_raw <= 0:
            return (0.0, dte)
        # Moneyness ratio — for calls: ITM when spot > strike, ATM at 1.0
        if ct == 'C':
            ratio = (spot - strike_raw) / max(spot, 1e-9)  # positive = ITM
            # Map ratio → delta: ATM (0) = 0.5, +5% ITM = 0.85, -5% OTM = 0.15
            delta = 0.5 + ratio * 7  # scale 1% moneyness ≈ 7pp delta
            delta = max(0.02, min(0.98, delta))
        else:  # put
            ratio = (strike_raw - spot) / max(spot, 1e-9)  # positive = ITM put
            delta = -(0.5 + ratio * 7)
            delta = min(-0.02, max(-0.98, delta))
        return (delta, dte)
    except Exception:
        return (0.0, -1)


def _screener_to_accumulator(items: list, market_ts_ms: int) -> int:
    """Synthesize LEVELONE_OPTIONS-like messages from screener items and
    feed them into FlowAccumulator. Fills the 0DTE gap that LEVELONE
    silently drops.

    Per-item math (since screener gives cumulative session stats):
      new_volume = curr.volume - prev.volume   # incremental contracts traded
      side = +1 if lastPrice > prev.lastPrice  # tick-direction proxy
              -1 if lastPrice < prev.lastPrice
               0 if equal (skip signed, count unsigned)
      delta = estimated from moneyness (spot vs strike)
      signed_dn = side × new_volume × delta × spot × 100

    Only processes contracts that are 0DTE (today's expiration) — we
    already have LEVELONE for non-0DTE so no need to double-count.
    """
    if not items:
        return 0
    global _screener_prev
    try:
        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is None:
            return 0
    except Exception:
        return 0

    processed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        sym = item.get('symbol', '') or ''
        if len(sym) < 15:
            continue
        ticker = sym[:6].strip()
        if not ticker:
            continue

        # Skip if we don't have a live spot for this ticker (needed for delta)
        spot = _tape_spot_for(ticker)
        if spot <= 0:
            # Try pulling from flow_accumulator's cached spot
            try:
                spot = acc._latest_spot.get(ticker, 0.0)
            except Exception:
                spot = 0.0
        if spot <= 0:
            continue

        curr_vol = _safe_num(item.get('volume', 0))
        curr_px  = _safe_num(item.get('lastPrice', 0))
        if curr_vol <= 0 or curr_px <= 0:
            continue

        delta, dte = _estimate_delta_and_dte(sym, curr_px, spot)
        if dte != 0:
            continue  # only process 0DTE from screener (non-0DTE we get via LEVELONE)

        prev = _screener_prev.get(sym)
        new_vol = curr_vol - (prev['volume'] if prev else curr_vol)
        px_diff = curr_px - (prev['lastPrice'] if prev else curr_px)
        if new_vol <= 0 or not prev:
            # First-observation: cache + skip (need prior to compute delta)
            _screener_prev[sym] = {'volume': curr_vol, 'lastPrice': curr_px}
            continue

        if px_diff > 0:   # tick up → buyer-initiated
            bid, ask = curr_px - 0.01, curr_px
        elif px_diff < 0: # tick down → seller-initiated
            bid, ask = curr_px, curr_px + 0.01
        else:
            bid, ask = curr_px, curr_px  # ambiguous; accumulator will skip signed

        fake_msg = {
            'symbol':           sym,
            'bid':              bid,
            'ask':              ask,
            'last':             curr_px,
            'last_size':        new_vol,
            'delta':            delta,
            'dte':              0,
            'underlying_price': spot,
            'trade_time':       int(market_ts_ms),
        }
        try:
            acc.on_option_update(fake_msg)
            processed += 1
        except Exception:
            pass
        _screener_prev[sym] = {'volume': curr_vol, 'lastPrice': curr_px}

    return processed


def _on_screener_option(data):
    """Handle real-time options screener updates — unusual activity radar.

    Data from _process_screener now has:
      symbol: screener key (e.g., 'OPTION_ALL_VOLUME_0')
      items: list of dicts with named keys (symbol, description, lastPrice,
             totalVolume, netChange, netPercentChange)
      sort_field, frequency, _timestamp

    Also bridges into FlowAccumulator for 0DTE contracts (Schwab's
    LEVELONE_OPTIONS silently drops same-day-exp trade updates, so
    screener is the only way we see 0DTE flow).
    """
    global _screener_logged
    if not _socketio:
        return

    # Log first raw response for field debugging
    if not _screener_logged:
        log.info(f"[SCHWAB-BRIDGE] SCREENER_OPTION data: items={len(data.get('items', []))}")
        items_sample = data.get('items', [])[:2]
        log.info(f"[SCHWAB-BRIDGE] SCREENER_OPTION sample items: {str(items_sample)[:500]}")
        _screener_logged = True

    items = data.get('items', [])
    if not items:
        return

    # Items from Schwab carry:
    #   symbol, description, lastPrice, netChange, netPercentChange,
    #   marketShare, totalVolume (market-wide total — same on every item),
    #   volume (per-contract), trades (per-contract trade count)
    alerts = []
    market_total = 0
    for item in items[:20]:
        if isinstance(item, dict):
            mt = _safe_num(item.get('totalVolume', 0))
            if mt > market_total:
                market_total = mt
            alerts.append({
                'symbol': item.get('symbol', ''),
                'description': item.get('description', ''),
                'volume': _safe_num(item.get('volume', 0)),
                'trades': _safe_num(item.get('trades', 0)),
                'marketShare': _safe_num(item.get('marketShare', 0)),
                'percentChange': _safe_num(item.get('netPercentChange', 0)),
                'lastPrice': _safe_num(item.get('lastPrice', 0)),
                'netChange': _safe_num(item.get('netChange', 0)),
            })

    if alerts:
        _socketio.emit('screener_option_update', {
            'type': data.get('sort_field', 'VOLUME'),
            'alerts': alerts,
            'market_total_volume': market_total,
            'timestamp': data.get('_timestamp', 0),
        })

    # Feed 0DTE contracts into FlowAccumulator (LEVELONE doesn't push them)
    try:
        n = _screener_to_accumulator(items, data.get('_timestamp', int(time.time() * 1000)))
        if n > 0 and not getattr(_on_screener_option, '_bridge_logged', False):
            log.info(f"[SCHWAB-BRIDGE] Screener→FlowAccumulator bridge active (first batch: {n} 0DTE messages synthesized)")
            _on_screener_option._bridge_logged = True
    except Exception as e:
        log.debug(f"[SCHWAB-BRIDGE] screener→accumulator bridge error: {e}")


# ── Equity screener handler (NYSE + NASDAQ most-active stocks) ────────────────
_screener_equity_logged = False


def _on_screener_equity(data):
    """Handle SCREENER_EQUITY updates — top-N most-active stocks per exchange.
    Same envelope as screener_option but for equities (e.g., NYSE_VOLUME_0).
    """
    global _screener_equity_logged
    if not _socketio:
        return

    items = data.get('items', []) or []
    key = data.get('sort_field') or data.get('symbol', 'UNKNOWN')

    if not _screener_equity_logged and items:
        log.info(f"[SCHWAB-BRIDGE] SCREENER_EQUITY key={key} items={len(items)}")
        log.info(f"[SCHWAB-BRIDGE] SCREENER_EQUITY sample: {str(items[:2])[:400]}")
        _screener_equity_logged = True

    alerts = []
    for item in items[:20]:
        if isinstance(item, dict):
            alerts.append({
                'symbol':        item.get('symbol', ''),
                'description':   item.get('description', ''),
                'volume':        _safe_num(item.get('volume', 0)),
                'trades':        _safe_num(item.get('trades', 0)),
                'lastPrice':     _safe_num(item.get('lastPrice', 0)),
                'netChange':     _safe_num(item.get('netChange', 0)),
                'percentChange': _safe_num(item.get('netPercentChange', 0)),
                'marketShare':   _safe_num(item.get('marketShare', 0)),
            })
    if alerts:
        _socketio.emit('screener_equity_update', {
            'key':       key,
            'alerts':    alerts,
            'timestamp': data.get('_timestamp', 0),
        })


# ── Account activity handler (user's own order fills + position updates) ──────
def _on_acct_activity(data):
    """Handle ACCT_ACTIVITY — order events, fills, position changes, balances.
    Schwab payload: {SubscriptionKey, AccountNumber, MessageType, MessageData}.
    MessageType examples: OrderActivity, SubscribedEvent, etc.
    """
    if not _socketio:
        return

    try:
        msg_type = data.get('MessageType') or data.get('2', '')
        msg_data = data.get('MessageData') or data.get('3', '')
        account  = data.get('AccountNumber') or data.get('1', '')
        log.info(f"[SCHWAB-BRIDGE] ACCT_ACTIVITY type={msg_type} account={account[:4]}***")
        _socketio.emit('acct_activity', {
            'type':      msg_type,
            'account':   account,
            'data':      msg_data,
            'timestamp': data.get('_timestamp', 0),
        })
    except Exception as e:
        log.debug(f"[SCHWAB-BRIDGE] acct_activity handler error: {e}")


# ── Chart equity handler (real-time 1-min equity candles) ─────────────────────
_chart_equity_logged = False


def _on_chart_equity(data):
    """Handle CHART_EQUITY — real-time 1-minute equity bars.
    Schwab sends OHLCV for each minute close.
    """
    global _chart_equity_logged
    if not _socketio:
        return
    # Skip futures (they go through _on_chart_candle as CHART_FUTURES)
    symbol = data.get('symbol') or data.get('key', '')
    if not symbol or symbol.startswith('/'):
        return

    if not _chart_equity_logged:
        log.info(f"[SCHWAB-BRIDGE] CHART_EQUITY first bar: {symbol} data_keys={list(data.keys())[:10]}")
        _chart_equity_logged = True

    # Cache close price as spot (used by screener bridge for delta estimation)
    close = data.get('close')
    if close:
        try:
            _latest_spot_by_ticker[symbol] = float(close)
        except (TypeError, ValueError):
            pass

    _socketio.emit('chart_equity_update', {
        'symbol': symbol,
        'open':   data.get('open'),
        'high':   data.get('high'),
        'low':    data.get('low'),
        'close':  data.get('close'),
        'volume': data.get('volume'),
        'chart_time': data.get('chart_time'),
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
    nq_topstep = _get_nq_mid()
    nq_schwab = _latest_nq
    opts_count = len(_live_gex)
    opt_age = round(time.time() - _last_option_update_ts, 0) if _last_option_update_ts > 0 else -1
    src = "TSX" if nq_topstep > 0 and nq_topstep != nq_schwab else "SCH"
    nq_show = nq_topstep if nq_topstep > 0 else nq_schwab
    if _streamer and _streamer.is_connected:
        log.info(f"[SCHWAB-BRIDGE] 📊 {_tick_count} ticks | NQ={nq_show:.2f}({src}) | "
              f"ratio={_nq_qqq_ratio:.2f} | strikes={opts_count} | opt_age={opt_age}s")
    else:
        log.info(f"[SCHWAB-BRIDGE] ⚠️  {_tick_count} ticks | NQ={nq_show:.2f}({src}) | connected=False (reconnecting...)")
    _tick_count = 0


def stop_schwab_bridge():
    """Stop the bridge cleanly."""
    global _bridge_running
    _bridge_running = False
    if _streamer:
        _streamer.stop()
    log.info("[SCHWAB-BRIDGE] Stopped")

