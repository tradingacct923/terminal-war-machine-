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
import json
import logging
import threading
from collections import deque, defaultdict
from datetime import datetime

log = logging.getLogger(__name__)

# Module-level SocketIO reference (injected by server.py)
_socketio = None
_streamer = None        # Connection A — primary streamer
_streamer_b = None      # Connection B — secondary streamer for option-chain overflow
                        # Each Schwab connection has a 3,000-symbol LEVELONE_OPTIONS cap.
                        # Splitting subscriptions across two concurrent connections from the
                        # SAME app/credentials gives 6,000 effective. Confirmed empirically:
                        # second WS login is accepted, second connection's SUBS gets its own
                        # 3,000 quota. PING keepalive (30s, set in SchwabStreamer) keeps both
                        # alive; auto-reconnect handles any forced disconnect.
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

# ── Per-ticker GEX (parallel to _live_gex — used to compute per-ticker walls
#    for the AI Panel's Key Level row, without disrupting the QQQ zone_update
#    pipeline the rest of the terminal consumes).
#    Shape: { ticker: { strike: {call_oi, put_oi, call_gamma, put_gamma,
#                                 call_delta, put_delta} } }
_per_ticker_gex: dict = {}
_last_key_level_push = 0.0
_KEY_LEVEL_INDEX_TICKERS = ("SPX", "SPY", "QQQ")

# ── Dealer session flow tracking ────────────────────────────────────────────
_session_dealer_buys = 0.0
_session_dealer_sells = 0.0
_prev_nq_for_hedge = 0.0
_hedge_wave_count = 0
_last_hedge_dir = 0       # +1 buying, -1 selling, 0 neutral

# ── Option mark batch buffer ────────────────────────────────────────────────
# Coalesce per-contract option_mark_update emits into one `option_mark_batch`
# frame every 50ms. Peak emit count drops ~10x. Latest update per contract
# wins within the window (older overwrites are just stale Greek noise).
_opt_mark_batch = {}            # contract_key → payload
_opt_mark_batch_lock = threading.Lock()
_opt_mark_last_flush = 0.0
_OPT_MARK_FLUSH_SEC = 0.05      # 50ms wall-clock cadence (driven by _flush_loop)

# ── Option trade batch buffer (TIMESALE real prints) ────────────────────────
# Append-only (NOT latest-wins) — every print is its own tape row.
_opt_trade_batch: list = []
_opt_trade_batch_lock = threading.Lock()
_opt_trade_last_flush = 0.0
_OPT_TRADE_FLUSH_SEC = 0.05     # 50ms cadence, same timer as mark batch

# ── BBO ring buffer (for accurate aggressor-at-trade-time) ──────────────────
# symbol → deque[(ts_ms, bid, ask)], bounded so memory stays well under 2MB
# across all 3000 subscribed contracts.
_bbo_history: dict = defaultdict(lambda: deque(maxlen=50))
_bbo_history_lock = threading.Lock()

# ── Dedicated flush loop guard ──────────────────────────────────────────────
_flush_loop_started = False

# ── Tradier streamer (fills the TIMESALE_OPTIONS gap Schwab rejects) ────────
_tradier_streamer = None            # TradierStreamer instance (Conn A — QQQ ATM + equity)
_tradier_streamer_b = None          # Phase 14 — Conn B (SPX + VIX + Mag-8)
_tradier_streamer_c = None          # Phase 15 — Conn C (QQQ LEAPS + far wings)
_tradier_streamer_d = None          # Phase 15 — Conn D (QQQ 31-365 DTE wings)
_tradier_streamer_e = None          # Phase 15 — Conn E (QQQ 0-30 DTE wings)
_tradier_single_names = ("NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AVGO")

# ── Single-name Greek cache (Schwab REST /chains poll, NOT on LEVELONE stream)
# These tickers aren't part of the 3000-symbol LEVELONE budget, so the regular
# `_sym_cache` built by `_on_options_quote` never sees them. We refresh via
# REST every 15s to populate delta/gamma/iv/theta for the Tradier timesale tape.
# Shape: { osi_21char : {delta, gamma, theta, iv, strike, contract_type,
#                        dte, oi, vol, mark, underlying_price, updated_ms} }
_single_name_greeks_cache: dict = {}
_single_name_greeks_lock = threading.Lock()
_single_name_refresh_started = False
_qqq_full_chain_refresh_started = False    # Phase 22 (2026-05-05) — guards _qqq_full_chain_refresh_loop spawn
_SINGLE_NAME_REFRESH_SEC = 30.0  # Phase 13: Schwab streaming covers ATM real-time, REST is now LEAPS/tail backfill only

# ── single-name Greeks cache disk persistence (added 2026-05-06) ────
# Reason: bridge re-init wipes the in-memory cache, blocking
# `_compute_single_name_walls` (returns empty until re-fetched), which in
# turn silences `single_name_walls`, `ndx_wgc`, AI-panel NDX cells, and
# any downstream consumer until the 5-10 min Schwab REST warmup completes.
# Disk persistence eliminates that cold-start window across restarts.
_SINGLE_NAME_CACHE_DIR              = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'state')
_SINGLE_NAME_CACHE_FILE             = os.path.join(_SINGLE_NAME_CACHE_DIR, 'single_name_greeks.json')
_SINGLE_NAME_CACHE_TMP              = _SINGLE_NAME_CACHE_FILE + '.tmp'
_SINGLE_NAME_CACHE_PERSIST_SEC      = 30.0
_SINGLE_NAME_CACHE_RESTORE_MAX_AGE  = 600.0   # 10 min — Greeks acceptable for warmup seed; refresh loop overwrites within 30s anyway
_SINGLE_NAME_CACHE_VERSION          = 1
_single_name_cache_persist_started  = False

# ── REST CHAIN ROTATION (cap-blocked tail backfill, added 2026-04-29) ────
# Streaming gives 200ms-fresh data on 2,940 contracts (3,000 cap, 1,500 QQQ).
# This rotation pulls the FULL chain via REST every 30s for QQQ/SPX/VIX/SPY
# and merges contracts NOT in the streaming cache into _per_ticker_gex —
# so walls/max_pain/GEX/hedge_pressure see the cap-blocked within-±$100
# tail (4,970 QQQ contracts holding 27% of today's volume) at 30s lag.
#
# Account-safety guards:
#  - 8 req/min steady = 6.7% of Schwab's 120 req/min limit
#  - Market-hours gate (9:30-15:00 ET, weekdays only — see below)
#  - 7.5s stagger between underlyings (no burst)
#  - 429 detection → exponential backoff → auto-disable on 4-streak
#  - Daily request budget cap (5,000) as final safety net
#
# 2026-04-30 — Option B deployment (adaptive cadence + trade-window-only):
#   - SPY dropped from rotation (no panel consumes its options data;
#     SPY equity microstructure stays alive via LEVELONE_EQUITIES + NYSE_BOOK
#     + Tradier equity tape).
#   - Adaptive cadence via _chain_rotation_interval_now():
#       9:30–12:00 ET → 30s (opening drive — walls migrate fast)
#       12:00–15:00 ET → 60s (afternoon — slow trends, walls sticky)
#   - End at 15:00 ET (user's trade-window cutoff, not full RTH 16:00).
#     Saves the last hour of reqs (no actionable signal post-15:00 for this
#     workflow). Layer 1 streaming keeps running through 16:00.
#   - Projected daily: ~4,800 reqs (4% headroom under 5K budget).
#   - _CHAIN_ROTATION_INTERVAL_S kept as a legacy default; the loop reads
#     _chain_rotation_interval_now() per cycle.
_CHAIN_ROTATION_INTERVAL_S       = 60.0   # legacy default (afternoon cadence)
_CHAIN_ROTATION_STAGGER_S        = 7.5
_CHAIN_ROTATION_DAILY_BUDGET     = 50000   # 2026-05-01: bumped 5000→50000 after empirical
                                            # cap test (30 sequential /chains @ 2.6/sec OK, no
                                            # 429). Schwab's actual burst limit is 100/sec,
                                            # daily theoretical max 8.64M. 50K is 0.6% of
                                            # theoretical, gives 10× headroom for new panes
                                            # that need fresher OI data. Phase 18 still uses
                                            # ~65 reqs/day = 0.13% utilization either way.
_CHAIN_ROTATION_TICKERS          = ['QQQ', '$SPX', '$VIX', 'SPY']  # SPY restored Phase 17B (deep-wing OI for walls/max_pain)
_CHAIN_ROTATION_STRIKE_COUNT     = 400  # captures full ±$100 radius easily

# Per-ticker date-chunk specifications. Schwab returns 502 "TooBigBody" when a
# /chains response payload exceeds ~50MB, which happens for SPX (massive chain)
# even on a 30-day window. Splitting SPX into 6 finer chunks fits each response
# under the size limit. Other tickers stay at 3 chunks (default-equivalent).
# Format: [(from_offset_days, to_offset_days), ...] from today.
_CHAIN_ROTATION_CHUNKS = {
    'QQQ':   [(0, 30),  (31, 180), (181, 1100)],                               # 3 chunks
    'SPY':   [(0, 30),  (31, 180), (181, 1100)],                               # 3 chunks
    '$VIX':  [(0, 1100)],                                                      # 1 chunk (small chain)
    '$SPX':  [(0, 7),   (8, 30),   (31, 90),   (91, 180),  (181, 365), (366, 1100)],  # 6 chunks
}
# Per-ticker strike_count overrides — finer for huge chains to keep payload small.
_CHAIN_ROTATION_STRIKE_COUNT_BY_TICKER = {
    '$SPX': 250,   # smaller per-call window; 6 chunks compensate
}

_chain_rotation_thread_started   = False
# Phase 18 (2026-04-30) — Strategic-schedule chain rotation.
# OI is fundamentally EOD-stable (OCC publishes once daily), so polling
# every 30-60s during RTH was wasteful. New design: fire 5x daily at
# strategic times — each fire captures the OI/Greeks/mark snapshot for
# downstream walls/max_pain/hedge_pressure use. Reduces ~4,800 reqs/day
# to ~50 reqs/day. _CHAIN_ROTATION_DAILY_BUDGET stays at 5K as safety.
_chain_rotation_fire_times_et_min = [
    # 2026-05-01: increased cadence from 5/day → 14/day (every 30 min during RTH)
    # after empirical Schwab cap proof: 100/sec burst, no daily limit.
    # New budget 50K/day → still <0.5% utilization at 14 fires × ~13 reqs = ~180/day.
    # Benefit: walls/max_pain/hedge_pressure refresh every 30min instead of 90min.
    570,   # 09:30 ET — open
    600,   # 10:00 ET
    630,   # 10:30 ET
    660,   # 11:00 ET
    690,   # 11:30 ET
    720,   # 12:00 ET — noon
    750,   # 12:30 ET
    780,   # 13:00 ET
    810,   # 13:30 ET
    840,   # 14:00 ET
    870,   # 14:30 ET
    900,   # 15:00 ET
    930,   # 15:30 ET
    955,   # 15:55 ET — pre-close snapshot
]
_chain_rotation_fired_today: set = set()  # ET-minutes that already fired today
# Intelligent-panels compute loop — pin_convergence (Phase 2) +
# hedge_forecaster (Phase 3). Spawned alongside the chain rotation thread.
_intel_loop_thread_started       = False
_kv_loop_thread_started          = False  # Phase 19 — k_v sample collector
_kv_last_sample_date: str        = ''     # tracks last sample date (YYYY-MM-DD)
_INTEL_PIN_LAST_HOUR_INTERVAL_S  = 15.0   # CONFIGURED — 15s during last hour of RTH
_INTEL_PIN_OFF_INTERVAL_S        = 60.0   # CONFIGURED — 60s otherwise during RTH
_intel_pin_last_compute_ts       = 0.0
_intel_hedge_last_compute_ts     = 0.0
_INTEL_HEDGE_INTERVAL_S          = 5.0    # CONFIGURED — Phase 3 forecaster cadence
_intel_div_last_compute_ts       = 0.0
_INTEL_DIV_INTERVAL_S            = 10.0   # CONFIGURED — Phase 4 SPX-vs-QQQ divergence cadence
_intel_vix_last_compute_ts       = 0.0
_INTEL_VIX_INTERVAL_S            = 10.0   # CONFIGURED — Phase 5 VIX regime cadence (vol indices update at ~5-10s)
_intel_wing_last_compute_ts      = 0.0
_INTEL_WING_INTERVAL_S           = 5.0    # CONFIGURED — Phase 6 Wing Tracker cadence (matches hedge_fc — wings move fast on 0DTE)
_intel_skyline_last_compute_ts   = 0.0
_INTEL_SKYLINE_INTERVAL_S        = 5.0    # CONFIGURED — Phase 7 Gamma Skyline cadence (matches zone_update emit cadence)
_intel_warehouse_last_compute_ts = 0.0
_INTEL_WAREHOUSE_INTERVAL_S      = 10.0   # CONFIGURED — Phase 8 Dealer Warehouse cadence (warehouse evolves slower than skyline)
_intel_events_last_compute_ts    = 0.0
_INTEL_EVENTS_INTERVAL_S         = 3600.0 # CONFIGURED — Phase 10B Event Calendar cadence (60 min — events change rarely)
_chain_rotation_enabled          = True
_chain_rotation_request_count    = 0           # this-day total
_chain_rotation_last_reset_date  = None        # ISO date of last counter reset
_chain_rotation_429_streak       = 0           # consecutive 429 count
_chain_rotation_last_429_ts      = 0.0
_chain_rotation_last_cycle_ts    = 0.0
_chain_rotation_last_merge_count = {}          # {ticker: int}
_chain_rotation_lifetime_merged  = 0           # total contracts merged since start


class _ChainRotationRateLimit(Exception):
    """Raised when /chains REST returns 429 — triggers backoff path."""
    pass

# NDX constituent weights for the Weighted Gamma Composite (WGC).
# Source: Nasdaq-100 index factsheet (public). Weights drift daily with price
# moves and are rebalanced quarterly on the 3rd Friday of Mar/Jun/Sep/Dec.
# These 8 names cover ~46% of NDX by weight. Refresh at each rebalance.
NDX_WEIGHTS = {
    'NVDA':  0.088,
    'AAPL':  0.084,
    'MSFT':  0.076,
    'AMZN':  0.056,
    'META':  0.047,
    'AVGO':  0.041,
    'GOOGL': 0.032,
    'TSLA':  0.035,
}
NDX_WEIGHTS_REFRESHED = '2026-03-21'  # Q1 2026 rebalance

# State for WGC regime-flip + cluster alerts.
_ndx_wgc_state = {
    'prev_sign':          0.0,
    'prev_regime':        None,
    'flip_history':       [],    # [{ts_ms, ticker, old, new}] rolling 5-min window
    'last_cross_alert':   0.0,
    'last_cluster_alert': 0.0,
}
_ndx_wgc_prev_above = {}  # ticker -> last above_flip bool, for flip detection
_latest_wgc: dict = {}    # last emitted WGC payload; consumed by REST hydrate


def set_socketio(sio):
    """Inject the Flask-SocketIO instance from server.py.

    2026-05-06: also wraps sio.emit with timing instrumentation. Every
    emit() call records per-event-type latency. Logs distribution every
    30s. Goal: identify which emit's JSON serialization is pinning gevent
    long enough to cause HTTP truncation + WebSocket buffer pile-up.
    """
    global _socketio
    # Wrap emit() with timing aggregator.
    _orig_emit = sio.emit
    _emit_perf: dict = {}  # event_name -> list of latencies in ms
    _emit_last_log = [time.time()]

    def _timed_emit(event, *args, **kwargs):
        _t0 = time.perf_counter()
        try:
            return _orig_emit(event, *args, **kwargs)
        finally:
            _dt_ms = (time.perf_counter() - _t0) * 1000.0
            _emit_perf.setdefault(event, []).append(_dt_ms)
            if (time.time() - _emit_last_log[0]) >= 30.0 and _emit_perf:
                # Log per-event summary, sorted by total time spent
                ranked = sorted(
                    [(name, samples) for name, samples in _emit_perf.items()],
                    key=lambda x: -sum(x[1])
                )
                wall_dt = max(time.time() - _emit_last_log[0], 1.0)
                lines = []
                total_emit_cpu_pct = 0.0
                for name, samples in ranked[:10]:
                    n = len(samples)
                    total_ms = sum(samples)
                    avg = total_ms / n
                    p99 = sorted(samples)[int(n * 0.99)] if n > 1 else samples[0]
                    cpu_pct = 100.0 * total_ms / 1000.0 / wall_dt
                    total_emit_cpu_pct += cpu_pct
                    lines.append(f"{name}={cpu_pct:.1f}%(n={n},avg={avg:.2f}ms,p99={p99:.2f}ms)")
                log.info(f"[EMIT-PERF] total={total_emit_cpu_pct:.1f}% of one core | " + ", ".join(lines))
                _emit_perf.clear()
                _emit_last_log[0] = time.time()

    sio.emit = _timed_emit
    _socketio = sio


def get_latest_wgc() -> dict:
    """Return last emitted NDX WGC payload, or empty dict if none yet."""
    return dict(_latest_wgc) if _latest_wgc else {}


# ── VIX futures front-month rotation ─────────────────────────────────────────
# CBOE VIX futures settle on the Wednesday 30 days before the 3rd-Friday SPX
# expiration of the FOLLOWING calendar month. Symbol format: /VX{code}{YY}
# where {code} is the SETTLEMENT month code (F=Jan,G=Feb,H=Mar,J=Apr,K=May,
# M=Jun,N=Jul,Q=Aug,U=Sep,V=Oct,X=Nov,Z=Dec). Example: /VXK26 = May 2026
# settlement (~May 20).
_VIX_MONTH_CODES = {1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
                    7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'}


def _vix_settle_date(contract_year: int, contract_month: int):
    """Settlement date for the VIX future labelled by (year, contract_month).

    CBOE rule: a VIX future for contract month M (where M is the SETTLEMENT
    month) settles on the Wednesday 30 days before the 3rd Friday of the
    FOLLOWING calendar month (M+1). Example: the April VIX future (/VXJ26)
    settles 30 days before the May 2026 3rd-Friday SPX expiry.

    Holiday adjustment (when that Wed is a CBOE holiday, settle moves to
    Tuesday) is not applied — Schwab accepts the symbol regardless and
    the date is only used here for "is it past?" filtering.
    """
    from datetime import date as _d, timedelta as _td
    # The SPX 3rd Friday lives in the FOLLOWING month
    spx_y, spx_m = contract_year, contract_month + 1
    if spx_m > 12:
        spx_m = 1
        spx_y += 1
    first = _d(spx_y, spx_m, 1)
    # Python weekday: Mon=0..Sun=6. Friday=4
    days_to_fri = (4 - first.weekday()) % 7
    third_fri = first + _td(days=days_to_fri + 14)
    return third_fri - _td(days=30)


def _next_n_vix_futures(n: int = 6) -> list:
    """Return the next `n` monthly /VX* symbols whose settle date >= today.

    Rotates automatically as front-months expire — no hardcoded symbols
    that go stale. The result is deterministic per calendar day, so a
    server restart on the same day gives the same subscription.
    """
    from datetime import date as _d
    today = _d.today()
    out = []
    # Walk forward up to 24 months — way more than n=6 we ever need
    y, m = today.year, today.month
    for _ in range(24):
        settle = _vix_settle_date(y, m)
        if settle >= today:
            yy = str(y)[-2:]
            code = _VIX_MONTH_CODES[m]
            out.append(f"/VX{code}{yy}")
            if len(out) >= n:
                break
        # advance one month
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def start_schwab_bridge():
    """Start the Schwab streamer bridge in a background thread."""
    global _bridge_running, _flush_loop_started
    if _bridge_running:
        log.info("[SCHWAB-BRIDGE] Already running")
        return

    # ── Pending-queue persistence (Improvement #1, 2026-05-04) ──────────
    # Recover in-flight prints from previous process, then start the snapshot
    # daemon so the next restart can do the same. Done BEFORE the bridge
    # thread spawns so flush_pending() picks up restored entries naturally.
    try:
        from connectors import dealer_print_capture as _dpc
        _dpc.set_default_spot_lookup(_tape_spot_for)
        n_restored = _dpc.restore_pending(spot_lookup=_tape_spot_for)
        if n_restored > 0:
            log.info(f"[SCHWAB-BRIDGE] Recovered {n_restored} pending dealer prints from previous run")
        _dpc.start_persistence()
    except Exception as e:
        log.warning(f"[SCHWAB-BRIDGE] dealer_print_capture persistence init failed: {e}")

    # ── Single-name Greeks cache disk seed (added 2026-05-06) ───────────
    # Skips the 5-10 min Schwab REST warmup so single_name_walls / ndx_wgc /
    # AI-panel NDX cells emit immediately on restart instead of staying dark.
    try:
        n_sn = _restore_single_name_cache()
        if n_sn > 0:
            log.info(f"[SCHWAB-BRIDGE] Seeded {n_sn} single-name Greeks entries from disk")
        _start_single_name_cache_persistence()
    except Exception as e:
        log.warning(f"[SCHWAB-BRIDGE] single-name cache persistence init failed: {e}")

    _bridge_running = True
    t = threading.Thread(target=_run_bridge, daemon=True)
    t.start()
    log.info("[SCHWAB-BRIDGE] Background thread spawned")

    # Spawn the fixed-cadence flush loop via gevent-aware primitive.
    # server.py uses `gevent` monkeypatch + async_mode="gevent", so
    # start_background_task is the canonical way to cooperate with the hub.
    if not _flush_loop_started and _socketio is not None:
        _socketio.start_background_task(_flush_loop)
        _flush_loop_started = True
        log.info("[SCHWAB-BRIDGE] Flush loop background task spawned (gevent)")

    # Spawn REST chain rotation loop — backfills cap-blocked-tail OI/Greeks/
    # volume for QQQ/SPX/VIX/SPY into _per_ticker_gex at 30s cadence.
    # Account-safe: 8 req/min (6.7% of Schwab's 120/min limit), market-hours-
    # only, 429 backoff, daily budget cap. See _full_chain_rotation_loop docstring.
    global _chain_rotation_thread_started
    if not _chain_rotation_thread_started:
        _chain_rotation_thread_started = True
        _ct = threading.Thread(target=_full_chain_rotation_loop, daemon=True,
                               name="schwab-chain-rotation")
        _ct.start()
        log.info("[SCHWAB-BRIDGE] Chain-rotation thread spawned (cap-blocked tail backfill)")

    # Spawn Intelligent-Panels compute loop — recomputes pin_convergence (15s
    # last hour, 60s otherwise) + emits Socket.IO 'intel:pin_update' push.
    # Phase 3 (hedge_forecaster, 5s cadence) wires into the same loop later.
    # RTH-gated like chain rotation. See _intel_compute_loop docstring.
    global _intel_loop_thread_started
    if not _intel_loop_thread_started:
        _intel_loop_thread_started = True
        _it = threading.Thread(target=_intel_compute_loop, daemon=True,
                               name="schwab-intel-compute")
        _it.start()
        log.info("[SCHWAB-BRIDGE] Intel-compute thread spawned (pin_convergence + hedge_forecaster)")

    # Phase 19 (2026-05-01) — k_v sample collector
    # Records daily (spot, ATM_IV) samples for Kobayashi (2025) Volatility-Delta.
    # Fires once per day at 15:30 ET (end of trade window — pre-close stable IV).
    global _kv_loop_thread_started
    if not _kv_loop_thread_started:
        _kv_loop_thread_started = True
        _kt = threading.Thread(target=_kv_sample_loop, daemon=True,
                               name="schwab-kv-sampler")
        _kt.start()
        log.info("[SCHWAB-BRIDGE] k_v sample-collector thread spawned (Phase 19, Kobayashi 2025)")

    # Wire the canonical NQ mid getter into signal_ledger so outcome
    # tracking can snapshot the live NQ price at fire-time and at each
    # 5/10/15-min window. Per CLAUDE.md, _get_nq_mid() is the single source
    # of truth (TopStepX L2 primary, Schwab level-one fallback). If the
    # ledger module isn't present, silently skip — the ledger is optional.
    try:
        from connectors import signal_ledger as _slg
        _slg.set_nq_mid_getter(_get_nq_mid)
    except Exception:
        pass


def _run_bridge():
    """Main bridge loop — initializes auth + streamer, subscribes, and bridges."""
    global _streamer, _streamer_b
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

        log.info("[SCHWAB-BRIDGE] Authenticated, starting streamer (single WS — emergency rollback)...")
        # ROLLBACK 2026-04-28: dual-WS produced churn loop (~67 reconnects/
        # min per conn). Schwab cleanly closes the WS shortly after we
        # complete the dense ~12-service SUBS burst (LEVELONE_OPTIONS 3000 +
        # OPTIONS_BOOK 120 + NASDAQ_BOOK + NYSE_BOOK + SCREENER_OPTION +
        # SCREENER_EQUITY + CHART_EQUITY + ACCT_ACTIVITY). Kept reconnecting
        # immediately on clean-close path → rate-limit risk.
        # Reverting to single-WS at 3,000 cap until proper rate-pacing fix.
        _streamer = SchwabStreamer(auth, n_connections=1)

        # Register callbacks
        _streamer.on('LEVELONE_FUTURES', _on_futures_quote)
        _streamer.on('LEVELONE_EQUITIES', _on_equity_quote)
        _streamer.on('LEVELONE_OPTIONS', _on_options_quote)
        _streamer.on('TIMESALE_OPTIONS', _on_options_timesale)
        _streamer.on('TIMESALE_EQUITY',  _on_equity_timesale)
        _streamer.on('CHART_FUTURES', _on_chart_candle)
        _streamer.on('CHART_EQUITY', _on_chart_candle)
        _streamer.on('NASDAQ_BOOK', _on_nasdaq_book)
        _streamer.on('NYSE_BOOK', _on_nyse_book)
        _streamer.on('SCREENER_OPTION', _on_screener_option)
        _streamer.on('SCREENER_EQUITY', _on_screener_equity)
        _streamer.on('ACCT_ACTIVITY', _on_acct_activity)
        _streamer.on('CHART_EQUITY', _on_chart_equity)
        _streamer.on('OPTIONS_BOOK', _on_options_book)

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

        # Subscribe to key instruments.
        # NOTE — VIX futures (/VX*) are NOT subscribed because Schwab's market
        # data API does not support CFE-listed futures. Tested formats:
        #   /VX, /VXK26, /VXM26, /VX:XCBF, VX, VXM26, /VX1 — all return
        #   {"errors": {"invalidSymbols": [...]}} from REST. The streamer
        #   silently accepts SUBS without streaming any data. Term structure
        #   is built instead from VIX options put-call parity (see
        #   _vix_implied_term_structure below) once VIX options stream lands.
        #
        # /NQ + /ES futures are NOT subscribed via Schwab — TopStepX is the
        # primary futures provider (faster Level-2 + tape, 24/5). Schwab's
        # LEVELONE_FUTURES + CHART_FUTURES for these symbols was redundant:
        #   • _get_nq_mid() prefers TopStepX (line 908) and only falls back
        #     to _latest_nq when TopStepX is empty
        #   • The zone emit at ~line 2648 auto-copies TopStepX's NQ mid into
        #     _latest_nq so downstream code sees a populated value
        #   • _on_chart_candle filters NQ/ES out (CLAUDE.md: "TopStepX is
        #     sole NQ candle source")
        # Dropping the subscription frees 4 streamer keys + ~40 msg/sec of
        # handler overhead. ES is a separate concern (no TopStepX equivalent)
        # but is not actively consumed by any signal/alert/CCS path today.
        #
        # $VXN — CBOE NASDAQ-100 Volatility Index (NDX-native vol).
        # $VVIX — vol-of-VIX-options. The single best read of institutional
        #         tail-hedge demand: when VVIX rips, even VIX options become
        #         expensive = desks paying up for protection-on-protection.
        # $SKEW — CBOE Skew Index. >135 = elevated tail-risk premium in
        #         OTM SPX puts (crash-hedge bid).
        # $VIX1D — 1-day VIX (overnight gap-vol). Front-of-curve indicator
        #         that decouples from $VIX during weekend / event windows.
        # All four are realtime=true, assetMainType=INDEX (verified live),
        # stream identically to VIX. Total LEVELONE_EQUITIES keys: 8.
        # LEVELONE_EQUITIES — primary spots + curated vol/breadth/yield indices.
        # Schwab's documented cap on this service is undocumented but ample
        # (community reports 50-500+). Each new key is FREE relative to the
        # 3,000 LEVELONE_OPTIONS budget. Going from 8 → 19 here.
        #
        # Added 2026-04-29:
        #   $RVX        Russell 2000 volatility       (small-cap vol regime)
        #   $OVX        Crude oil volatility          (commodity stress)
        #   $GVZ        Gold volatility               (safe-haven demand)
        #   $VXEEM      Emerging-markets volatility   (EM risk premium)
        #   $VXD        DJIA volatility               (cyclicals vs tech split)
        #   $XSP        Mini-SPX (SPX/10) cash        (cleaner spot for retail-sized)
        #   $RUT        Russell 2000 cash             (small-cap pulse)
        #   $TICK       NYSE up-tick / down-tick      (breadth)
        #   $TRIN       Arms index                     (breadth thrust signal)
        #   $TNX        10-year Treasury yield × 10   (rates-vs-equity proxy)
        _streamer.subscribe_equities([
            # Primary spots (8 keys)
            # 2026-05-04: replaced bare 'VIX' with '$VIX' (cash index — bare
            # 'VIX' was silently dropped or aliased) and added '$SPX' (was
            # entirely missing despite SPX being used in walls/HP/divergence
            # pane/alerts). Net: 19 → 20 keys.
            'QQQ', 'SPY', '$SPX', '$VIX',
            '$VXN', '$VVIX', '$SKEW', '$VIX1D',
            '$NDX',  # FIXED 2026-04-30: was '$NDX.X' which Schwab silently dropped
                     # (TD-legacy artifact). Schwab accepts plain `$NDX` like `$VIX`/`$SPX`.
                     # Handler at _on_equity_quote still accepts '$NDX.X' defensively.
            # Vol-of-other-assets (5 keys)
            '$RVX', '$OVX', '$GVZ', '$VXEEM', '$VXD',
            # Cross-asset cash indices (3 keys)
            '$XSP', '$RUT', '$DJX',
            # Market-internals breadth (2 keys)
            '$TICK', '$TRIN',
            # Treasury yield (1 key)
            '$TNX',
        ])

        # TIMESALE_EQUITY would give per-print equity tape with venue MIC,
        # but Schwab's market data product DOES NOT entitle this service on
        # our account (returns code=11 "Service not available" — same gating
        # as TIMESALE_OPTIONS for QQQ/SPY).
        # Probed 2026-04-28: tried TIMESALE_EQUITY, EQUITY_TIMESALE, TRADES,
        # TRADE, ACTIVES_*, LEVELTWO_EQUITY — all either not entitled or
        # require an undocumented format. TRADES was recognized (code=21
        # "Bad command formatting") suggesting it exists but the params
        # aren't TIMESALE-shaped — likely a deprecated TD legacy service.
        # The _on_equity_timesale handler stays wired (harmless without
        # data) so if entitlement is ever granted or we add Polygon/IB as
        # an alt feed, it activates automatically.
        try:
            _streamer.subscribe_timesale_equity(['QQQ', 'SPY'])
            log.info("[SCHWAB-BRIDGE] 📊 TIMESALE_EQUITY attempt: QQQ + SPY (will be code=11 if not entitled)")
        except Exception:
            pass

        # Full-chain options budget: 3,000 total (Schwab account cap).
        # QQQ 1,500 + SPX 900 + SPY 600 = 3,000.
        # SPY added 2026-04-21 for SPY/SPX flow-divergence cross (retail ETF vs
        # institutional index). Streamer chunks SUBS at 2,000 keys to stay
        # under Schwab's 65,535-byte WS frame limit.
        # Allocation 2026-04-29 (post-SPY-drop):
        #   QQQ 1,500 + SPX 1,100 + VIX 400 = 3,000 (exactly at cap)
        #   SPY options DROPPED — equity-side coverage retained via
        #   NYSE_BOOK + Tradier equity tape + chain rotation REST (30s lag).
        #   The 240 freed keys + 60 reserve = 300 reallocated to SPX (was 800).
        #   Rationale: SPX drives VIX and systemic γ regime; rotation can't
        #   pull SPX front-month chunks (TooBigBody 502s), so streaming
        #   IS the only way to fill that gap.
        # ── Phase 17B (2026-04-30) — Streaming cap reallocation ──
        # Old (pre-Phase 17B): QQQ 1,412 + SPX 800 + VIX 254 + Mag-8 320 = 2,786
        # New (Phase 17B):     QQQ 0DTE 150 + SPX 800 + VIX 254 + Mag-7 490
        #                       + SPY 800 + IWM 200 + sector ETFs 200 = 2,894
        # Frees ~1,260 QQQ ATM ±$100 slots (BSM solver Phase 17 covers Greeks)
        # Adds:    SPY (restored), IWM, sector ETFs, Mag-7 wider radius
        # ── BOOT-THROTTLING (added 2026-05-07) ──────────────────────────────
        # Each option subscribe fires 100-1500 SUBS msgs; Schwab responds with
        # initial state for every contract → inbound burst that drives CPU to
        # 200%+ on a single core. Combined with parallel L2 tick backfill
        # (~60K ticks across 5 chunks), the gevent hub starves and the OS
        # kills the process. Solution: 1.5s sleep between heavy subscribes
        # so each burst can drain before the next fires. Total added boot
        # latency: ~10s. Peak CPU: <100% sustained.
        _BOOT_THROTTLE_SEC = 1.5
        try:
            _subscribe_qqq_0dte_only()  # was _subscribe_qqq_options() — non-0DTE Greeks via BSM
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] QQQ 0DTE-only subscription error: {e}")
        time.sleep(_BOOT_THROTTLE_SEC)
        try:
            _subscribe_index_options()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Index options subscription error: {e}")
        time.sleep(_BOOT_THROTTLE_SEC)
        # Phase 17B — Mag-7 expanded (AVGO dropped, 7 names × 70 strikes)
        # Replaces Phase 13 Mag-8 (8 × 40). Captures ATM ±10% per stock.
        try:
            _subscribe_mag7_expanded()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Mag-7 expanded subscription error: {e}")
        time.sleep(_BOOT_THROTTLE_SEC)
        # Phase 17B — SPY RESTORED to streaming (was dropped 2026-04-29)
        try:
            _subscribe_spy_options_full()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] SPY options subscription error: {e}")
        time.sleep(_BOOT_THROTTLE_SEC)
        # Phase 17B — IWM (Russell 2000) for small-cap risk detection
        try:
            _subscribe_iwm_options()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] IWM options subscription error: {e}")
        time.sleep(_BOOT_THROTTLE_SEC)
        # Phase 17B — Sector ETFs (XLK/XLE/XLF) for sector rotation
        try:
            _subscribe_sector_etf_options()
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Sector ETF options subscription error: {e}")
        time.sleep(_BOOT_THROTTLE_SEC)
        log.info(f"[SCHWAB-BRIDGE] Option-subscribe phase complete (boot-throttled {_BOOT_THROTTLE_SEC}s/step)")

        # NASDAQ_BOOK — Level 2 NBBO depth on NASDAQ-listed names.
        # Cap is undocumented but community-reported 50+. Each key is free
        # relative to LEVELONE_OPTIONS. QQQ stays primary; Mag-8 added so
        # we can see dealer-equity hedging on NDX-component single names
        # (NVDA single-stock option dealers hedge NVDA shares, NOT QQQ).
        # Going from 1 → 9 keys.
        _streamer.subscribe_nasdaq_book([
            'QQQ',                                                  # primary
            'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO',  # Mag-8
        ])
        log.info("[SCHWAB-BRIDGE] Subscribed to NASDAQ_BOOK for QQQ + Mag-8")

        # NYSE_BOOK — Level 2 NBBO on NYSE-listed names. SPY stays primary;
        # IWM (Russell 2000 ETF) + DIA (Dow ETF) added for cross-asset
        # confirmation of regime moves. Going from 1 → 3 keys.
        _streamer.subscribe_nyse_book(['SPY', 'IWM', 'DIA'])
        log.info("[SCHWAB-BRIDGE] Subscribed to NYSE_BOOK for SPY + IWM + DIA")

        # SCREENER_OPTION — cap=10 (documented). Use multiple sort/freq
        # combos to capture different unusual-flow signatures simultaneously.
        # Going from 1 → 5 keys (5 reserve still in place for future probes).
        # Added 2026-04-29:
        #   VOLUME 0      session-cumulative volume     (already present)
        #   VOLUME 5      last-5-min vol surge          (intraday burst signal)
        #   TRADES 5      last-5-min trade-count surge  (retail activity proxy)
        #   PERCENT_CHANGE_UP 5      premium pop (last 5 min)   (rip detection)
        #   PERCENT_CHANGE_DOWN 5    premium dump (last 5 min)  (panic detection)
        for sort_field, freq in (
            ('VOLUME',                '0'),
            ('VOLUME',                '5'),
            ('TRADES',                '5'),
            ('PERCENT_CHANGE_UP',     '5'),
            ('PERCENT_CHANGE_DOWN',   '5'),
        ):
            try:
                _streamer.subscribe_screener_option(sort_field, freq)
                log.info(f"[SCHWAB-BRIDGE] Subscribed to SCREENER_OPTION {sort_field}_{freq}")
            except Exception as e:
                log.info(f"[SCHWAB-BRIDGE] SCREENER_OPTION {sort_field}_{freq} failed: {e}")

        # Subscribe to equity screener (NYSE + NASDAQ most-active)
        for exch in ('NYSE', 'NASDAQ'):
            try:
                _streamer.subscribe_screener_equity(exch, 'VOLUME', '0')
                log.info(f"[SCHWAB-BRIDGE] Subscribed to SCREENER_EQUITY ({exch})")
            except Exception as e:
                log.info(f"[SCHWAB-BRIDGE] SCREENER_EQUITY ({exch}) subscription failed: {e}")

        # CHART_EQUITY — real-time 1-min OHLCV bars. Cap is undocumented but
        # ample (50+). Each key is free vs the LEVELONE_OPTIONS budget.
        # Going from 4 → 13 keys.
        # Added 2026-04-29:
        #   NVDA, AAPL, MSFT, AMZN: Mag-4 single-name 1-min bars for
        #     constituent-vs-index divergence detection
        #   $RUT: Russell 2000 — small-cap rotation
        #   IWM, DIA: ETF reflections (matches NYSE_BOOK additions)
        #   TQQQ, SQQQ: 3× leveraged QQQ — gamma-amplified moves are visible
        try:
            _streamer.subscribe_chart_equity([
                'QQQ', 'SPY', 'VIX', '$VXN',                # original 4
                'NVDA', 'AAPL', 'MSFT', 'AMZN',             # Mag-4 single names
                '$RUT', 'IWM', 'DIA', 'TQQQ', 'SQQQ',       # cross-asset + leveraged
            ])
            log.info("[SCHWAB-BRIDGE] Subscribed to CHART_EQUITY (13 keys: QQQ/SPY/VIX/$VXN + Mag-4 + cross-asset)")
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

        # Initialize Composite Conviction Scorer — the directional-bias
        # framework that synthesizes greek_surface + wall_signals + flow_acc
        # + mm_attribution into one CCS score per ticker. 5s cadence.
        try:
            from connectors.conviction_score import init_scorer
            # Both QQQ and SPY — _on_equity_quote feeds prints for both
            # tickers via _ccs.feed_equity_print(); previously SPY prints
            # silently no-op'd because tickers=['QQQ'] left _vol_history
            # without a 'SPY' deque so the early-return at the top of
            # feed_equity_print fired on every SPY tick.
            # 2026-05-08: SPY scoring is INTERNALLY GATED inside ConvictionScorer
            # (greek_surface is QQQ-only, so SPY hedge_pressure can't be
            # computed). Volume tracking still flows for cross-asset use.
            init_scorer(socketio=_socketio, tickers=['QQQ', 'SPY'])
            log.info("[SCHWAB-BRIDGE] ConvictionScorer initialised (emits 'conviction_update')")
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] ConvictionScorer init failed: {e}")

        # Initialize Sweep Detector (Phase 1 of intelligent panels).
        # Wires Socket.IO so the detector can emit 'intel:sweep_alert' events
        # when a multi-strike sweep completes. Detector is fed by the existing
        # _on_tradier_timesale path (hook added at end of that handler).
        try:
            from connectors import sweep_detector as _swd
            _swd.init(_socketio)
            log.info("[SCHWAB-BRIDGE] Sweep Detector initialised (emits 'intel:sweep_alert')")
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Sweep Detector init failed: {e}")

        # Populate expiration-metadata cache for all subscribed tickers so
        # flow trades can be bucketed as 0DTE / weekly / monthly / quarterly /
        # LEAPS in the accumulator + alert labels.
        try:
            from connectors.expiration_cache import init_cache
            from server import _schwab_get
            _exp_cache = init_cache(refresh_interval_sec=3600)
            _subscribed_tickers = ["QQQ"] + list(INDEX_OPTION_TICKERS)
            _total_exps = 0
            for _t in _subscribed_tickers:
                _total_exps += _exp_cache.refresh(_t, _schwab_get)
            log.info(f"[SCHWAB-BRIDGE] Expiration cache: {_total_exps} entries across {len(_subscribed_tickers)} tickers")
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] Expiration cache init failed: {e}")

        # ── Start Tradier streamer (fills the TIMESALE_OPTIONS gap) ────────
        # Tradier's consolidated WS feed gives us real trade prints for any
        # OCC option symbol. We stream:
        #   1. Every Schwab-subscribed option (QQQ/SPX/SPY) — for Greek-enriched
        #      rows in the tape (price + Δ/IV/GEX).
        #   2. Top-8 NQ weights (NVDA/AAPL/MSFT/AMZN/META/GOOGL/TSLA/AVGO) near
        #      their ATM strikes — for single-name tape rows WITHOUT Greeks
        #      (price + size + bid/ask only). Same coverage 0DTHero shows.
        try:
            _start_tradier_streamer()
        except Exception as e:
            import traceback
            log.warning(f"[SCHWAB-BRIDGE] Tradier streamer failed to start: {e}")
            log.warning(traceback.format_exc())

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


def _pick_expirations(raw_dates: list, count: int = 15) -> list:
    """Curated expiration picks with RESERVED budget per category.

    Old behaviour exhausted budget on dailies (e.g. 25-slot SPX with daily
    chain filled all 25 slots before reaching monthly OPEX/quarterly logic),
    leaving structural dates uncovered. This version reserves budget upfront:

      Category budgets (proportional to count, with hard caps):
        Dailies (next 14 days):       40% (cap 12)
        Monthly OPEX (next 6 months): 20% (cap 6)
        Quarterly EOQ (next 4):       15% (cap 4)
        LEAPS (Jan year+1, year+2):   10% (cap 3)
        Weekly Fri (out 30+ days):    fill remaining (cap 6)

    For Friday-settled tickers (QQQ/SPX/SPY) this guarantees that critical
    structural dates (June OPEX, September EOQ, December EOY, Jan LEAPS) are
    always represented in the returned list — even when count is tight.

    raw_dates are sorted ISO date strings (YYYY-MM-DD).
    """
    from datetime import datetime, date, timedelta
    import calendar as _cal
    if count <= 3 or len(raw_dates) <= count:
        return raw_dates[:count]

    today = date.today()
    raw_set = set(raw_dates)

    def third_friday(y, m):
        cal = _cal.Calendar()
        fridays = [dd for dd in cal.itermonthdates(y, m) if dd.month == m and dd.weekday() == 4]
        return fridays[2] if len(fridays) >= 3 else None

    # Reserved per-category budgets
    daily_budget   = min(int(count * 0.40) or 1, 12)
    opex_budget    = min(int(count * 0.20) or 1, 6)
    eoq_budget     = min(int(count * 0.15) or 1, 4)
    leaps_budget   = min(int(count * 0.10) or 1, 3)
    weekly_budget  = max(0, count - daily_budget - opex_budget - eoq_budget - leaps_budget)

    picks_list = []
    used = set()

    def _add(iso):
        if iso in raw_set and iso not in used:
            picks_list.append(iso)
            used.add(iso)
            return True
        return False

    # 1. Dailies in next 14 days (capped at daily_budget)
    daily_count = 0
    for d in raw_dates:
        if daily_count >= daily_budget:
            break
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            if (dt - today).days > 14:
                break
            if _add(d):
                daily_count += 1
        except Exception:
            pass

    # 2. Monthly OPEXes (3rd Friday of next 6 months)
    opex_count = 0
    for offset in range(0, 6):
        if opex_count >= opex_budget:
            break
        m = ((today.month - 1 + offset) % 12) + 1
        y = today.year + ((today.month - 1 + offset) // 12)
        opex = third_friday(y, m)
        if opex and opex > today:
            if _add(opex.isoformat()):
                opex_count += 1

    # 3. Quarterly EOQs (3rd Fri of Mar/Jun/Sep/Dec for year+0 and year+1)
    eoq_count = 0
    for y_offset in [0, 1]:
        if eoq_count >= eoq_budget:
            break
        y = today.year + y_offset
        for q_month in [3, 6, 9, 12]:
            if eoq_count >= eoq_budget:
                break
            eoq = third_friday(y, q_month)
            if eoq and eoq > today:
                if _add(eoq.isoformat()):
                    eoq_count += 1

    # 4. LEAPS — January 3rd Fridays for year+1, year+2, year+3
    leaps_count = 0
    for y_offset in [1, 2, 3]:
        if leaps_count >= leaps_budget:
            break
        y = today.year + y_offset
        leap = third_friday(y, 1)
        if leap and leap > today:
            if _add(leap.isoformat()):
                leaps_count += 1

    # 5. Fill remaining with weekly Fridays (out 14+ days from today)
    weekly_count = 0
    for d in raw_dates:
        if weekly_count >= weekly_budget or len(picks_list) >= count:
            break
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            if dt.weekday() == 4 and (dt - today).days > 14:
                if _add(d):
                    weekly_count += 1
        except Exception:
            pass

    # 6. Top up with any remaining nearest dates (keeps picks_list at exactly count)
    for d in raw_dates:
        if len(picks_list) >= count:
            break
        _add(d)

    # Sort chronologically for predictable per-expiration tier dispatch
    picks_list.sort()
    return picks_list[:count]


def _subscribe_qqq_options():
    """Subscribe to LEVELONE_OPTIONS for all available QQQ contracts around ATM.
    TEST MODE: pushing all ~33 expirations + ±$100 radius to probe Schwab's
    actual per-service limit. Startup log will show what Schwab accepts (code=0)
    vs rejects/truncates. If this works, Schwab's limit is much higher than the
    previously assumed 1,700 total."""
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

        # Full coverage: all available QQQ expirations (~33, through LEAPS
        # 2028-12-15). Streamer chunks the SUBS message into ≤2,000-key frames
        # to stay under Schwab's 65,535-byte WS limit, so we can push the full
        # chain without truncation.
        exp_dates = _pick_expirations(raw_dates, count=100)

        # ── TIER-BASED ALLOCATION (2026-04-29) ─────────────────────────────
        # Replaces uniform round-robin with intraday-weighted tiers:
        #   Layer A (0-9 DTE): max coverage on 0DTE, deep on 1-9
        #   Layer B (12-30 DTE): moderate near-term weeklies
        #   Layer C (31-180 DTE): standard mid-monthly coverage
        #   Layer D (180-365 DTE quarterlies): light coverage
        #   Layer E (365-700 DTE 2027 LEAPS): light coverage
        #   Layer F (700+ DTE 2028 LEAPS): minimal coverage
        # Net: 1,412 keys QQQ + 1,100 SPX + 400 VIX = 2,912 / 3,000 (88-key buffer)
        # 0DTE goes from 44 → 102 contracts (max ±$25, 95.7% volume capture)
        from datetime import datetime, date as _date_cls
        atm = round(qqq_spot)
        today_d = _date_cls.today()

        # Per-tier quota: how many strikes per expiration in each tier.
        # See _categorize_qqq_expiration() for tier definitions.
        TIER_QUOTAS = {
            'L_0DTE':       102,  # max possible (±$25 covers 95.7% of vol)
            'L_1DTE':        80,
            'L_2DTE':        60,
            'L_3_7DTE':      40,  # default for 3-7 DTE; overridable per-expiration below
            'L_8_9DTE':      45,
            'L_10_30DTE':    40,
            'L_31_180DTE':   50,
            'L_QUARTERLY':   30,
            'L_LEAPS_2YR':   25,  # 2027 LEAPS — KEPT not sentinel
            'L_LEAPS_3YR':   15,  # 2028 LEAPS — KEPT not sentinel
        }
        # Per-day-of-week tweaks for L_3_7DTE — earlier days get slightly more
        # since they're closer to expiry impact. Order: 5DTE, 6DTE, 7DTE
        L_3_7_OVERRIDES = {3: 40, 4: 40, 5: 40, 6: 35, 7: 35, 8: 40, 9: 50}

        def _tier_for(exp_iso: str) -> str:
            try:
                d = datetime.strptime(exp_iso, "%Y-%m-%d").date()
                dte = (d - today_d).days
            except Exception:
                return 'L_31_180DTE'  # safe default
            if dte == 0:    return 'L_0DTE'
            if dte == 1:    return 'L_1DTE'
            if dte == 2:    return 'L_2DTE'
            if dte <= 7:    return 'L_3_7DTE'
            if dte <= 9:    return 'L_8_9DTE'
            if dte <= 30:   return 'L_10_30DTE'
            if dte <= 180:  return 'L_31_180DTE'
            if dte <= 365:  return 'L_QUARTERLY'
            if dte <= 700:  return 'L_LEAPS_2YR'
            return 'L_LEAPS_3YR'

        def _quota_for(exp_iso: str) -> int:
            try:
                d = datetime.strptime(exp_iso, "%Y-%m-%d").date()
                dte = (d - today_d).days
            except Exception:
                return 50
            tier = _tier_for(exp_iso)
            # Special per-DTE override for 3-9 DTE band (smoother slope)
            if 3 <= dte <= 9 and dte in L_3_7_OVERRIDES:
                return L_3_7_OVERRIDES[dte]
            return TIER_QUOTAS.get(tier, 40)

        # Collect symbols PER expiration, sorted nearest-ATM first.
        per_exp_symbols: list[tuple[str, list[str]]] = []
        tier_count_log: dict = {}
        for exp_date in exp_dates:
            try:
                chain, _ = _schwab_chain_raw("QQQ", exp_date)
                row = []
                for opt in chain:
                    strike = float(opt.get("strike", 0))
                    sym = opt.get("symbol", "")
                    if sym and abs(strike - atm) <= 100:
                        row.append((abs(strike - atm), sym))
                row.sort(key=lambda x: x[0])  # nearest-ATM first
                # Apply per-expiration quota
                quota = _quota_for(exp_date)
                selected_syms = [s[1] for s in row[:quota]]
                per_exp_symbols.append((exp_date, selected_syms))
                tier = _tier_for(exp_date)
                tier_count_log[tier] = tier_count_log.get(tier, 0) + len(selected_syms)
            except Exception as e:
                log.info(f"[SCHWAB-BRIDGE] ⚠️ Chain fetch failed for {exp_date}: {e}")
                per_exp_symbols.append((exp_date, []))

        # Flatten — preserve expiration order (0DTE first, then 1DTE, etc.)
        # so if we hit cap mid-list, far-dated LEAPS get truncated, not 0DTE.
        symbols = []
        for exp_date, syms in per_exp_symbols:
            symbols.extend(syms)

        if not symbols:
            log.info(f"[SCHWAB-BRIDGE] ⚠️ No QQQ options near ATM={atm}")
            return

        # Total cap: 1,412 keys keeps us at 2,912/3,000 with 88-key buffer
        # (was 1,500 round-robin — see post-2026-04-29 tier allocation note above)
        _QQQ_CAP = 1412
        log.info(f"[SCHWAB-BRIDGE] QQQ tier allocation: {tier_count_log}")
        _ndx_option_symbols = symbols[:_QQQ_CAP]
        _streamer.subscribe_options(_ndx_option_symbols, conn_idx=0)
        # OPTIONS_BOOK (Level 2 depth) — Phase 1 data-collection uncap.
        # Round-robin interleave means symbols[:120] = ~3-4 closest-ATM strikes
        # across all ~33 QQQ expirations. Raw snapshots logged to disk by
        # _on_options_book; no UI signal until calibration from captures.
        _OB_BUDGET = 120
        # OPTIONS_BOOK pinned to conn 0 same as the QQQ LEVELONE subscription
        _streamer.subscribe_options_book(_ndx_option_symbols[:_OB_BUDGET], conn_idx=0)
        # TIMESALE source: Tradier WebSocket (Schwab's TIMESALE_OPTIONS returns
        # code=11 — service not entitled on this account). Subscription
        # forwarding to _tradier_streamer happens at the end of start_schwab_bridge.
        log.info(f"[SCHWAB-BRIDGE] 📊 Subscribed to {len(_ndx_option_symbols)} QQQ options "
              f"(ATM≈{atm}, radius=±100, exps={exp_dates}) + OPTIONS_BOOK L2 on {_OB_BUDGET} contracts")

    except Exception as e:
        import traceback
        log.info(f"[SCHWAB-BRIDGE] ⚠️ QQQ options subscription failed: {e}")
        log.info(traceback.format_exc())


# Tracked symbols per ticker — used by flow accumulator for ticker classification.
# Maps ticker -> list of Schwab option symbols we've subscribed to.
_subscribed_option_symbols_by_ticker: dict[str, list] = {}

# ── Phase 21 (2026-05-01): EXPLICIT Greek source routing ──────────────────
# Each option symbol (OSI 21-char) is in EXACTLY ONE of these sets, deciding
# its Greek source — replaces the implicit cache-fall-through pattern.
#
#   SCHWAB_WS_OSIS    — subscribed via _subscribe_options() to LEVELONE_OPTIONS
#                       Live Greeks pushed sub-200ms by Schwab to _sym_cache
#
#   SCHWAB_REST_OSIS  — covered by Mag-8 single-name REST refresher (15s poll)
#                       Greeks live in _single_name_greeks_cache
#                       NOTE: A symbol can be in BOTH SCHWAB_WS_OSIS and
#                       SCHWAB_REST_OSIS (Mag-8 ATM is streamed AND polled).
#                       Routing prefers WS (fresher).
#
#   Symbols in NEITHER set → BSM solver computes Greeks per-print (explicit).
#
# Symbols are added at subscribe time. Routing decision in _on_tradier_timesale
# is a single set-membership check — no fall-through, no implicit lookups.
_SCHWAB_WS_OSIS:   set = set()       # populated by _subscribe_options_for_ticker
_SCHWAB_REST_OSIS: set = set()       # populated by _fetch_single_name_chain
# Routing telemetry — incremented per print to monitor source distribution.
_GREEK_ROUTING_STATS: dict = {
    'schwab_ws':   0,
    'schwab_rest': 0,
    'bsm':         0,
    # Bug detector: subscribed but cache empty (should be ~0 in steady state)
    'schwab_ws_cache_miss':   0,
    'schwab_rest_cache_miss': 0,
}


def _subscribe_options_for_ticker(
    ticker: str,
    strike_radius: float = None,
    expiries_count: int = 2,
    cap: int = 80,
    streamer_override=None,
    conn_idx: int = 0,
    tier_quotas: dict = None,
) -> int:
    """
    Subscribe to LEVELONE_OPTIONS for ATM contracts on a given ticker.

    Mirrors _subscribe_qqq_options but generalized so SPY + Indices can reuse it.

    strike_radius defaults to 6% of spot (wide enough to capture institutional
    hedge put strikes that drive the biggest flow-dump alerts).
    expiries_count: how many nearest expirations to subscribe (3 = 0DTE +
    next weekly + next monthly for a ticker with monthly Fridays).
    cap: max contracts per ticker to avoid blowing through Schwab's symbol limit.
    streamer_override: route SUBS to a specific SchwabStreamer instance (used
        to split LEVELONE_OPTIONS budget across two concurrent connections —
        each has its own 3,000 cap, so SPY+VIX go on conn B while QQQ+SPX
        stay on conn A). Defaults to None = use primary `_streamer`.
    tier_quotas: optional dict mapping DTE tier label → strikes-per-expiration.
        When provided, switches from uniform round-robin to TIERED allocation:
        more strikes for 0DTE/short-term, fewer for LEAPS. LEAPS coverage is
        backfilled by the REST chain rotation (30s lag — fine for slow-moving
        far-dated Greeks). Tier labels: L_0DTE / L_1DTE / L_2DTE / L_3_7DTE /
        L_8_9DTE / L_10_30DTE / L_31_180DTE / L_QUARTERLY / L_LEAPS_2YR /
        L_LEAPS_3YR. Each tier defaults to 30 if missing from dict.

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

        # Detect "weekday-settled" tickers (e.g. $VIX expires Wednesdays).
        # _pick_expirations is biased toward Friday-anchored OPEX cycles, so
        # for tickers WITHOUT any Friday expirations in the first 60 days,
        # we fall back to nearest-N instead.
        #
        # Phase 11b fix: SPX has DAILY expirations (only ~20% are Fridays),
        # but it STILL needs the picker to capture monthly OPEX + quarterly
        # EOQ + LEAPS Januaries — those structural dates are crucial for
        # cross-asset regime detection. So we sample a larger window (60
        # entries) instead of 8 — if we find ANY Fridays at all, we treat
        # the ticker as "Friday-aware" and let the picker run.
        from datetime import datetime as _dt
        _fri_count = 0
        _sample = raw_dates[:60]
        for _d in _sample:
            try:
                if _dt.strptime(_d, "%Y-%m-%d").weekday() == 4:
                    _fri_count += 1
            except Exception:
                pass
        # Threshold lowered: ≥3 Fridays in first 60 dates = Friday-aware.
        # VIX which has Wednesday-only expirations stays under threshold.
        _is_friday_aware = _fri_count >= 3

        # For ≥4 expiries on Friday-aware tickers (QQQ/SPX/SPY/dailies-with-
        # Fridays), hand-pick dailies + Fridays + structural OPEX/EOQ/LEAPS.
        # For Wednesday-only tickers (VIX) or ≤3 expiries, take nearest-N.
        if expiries_count >= 4 and _is_friday_aware:
            exp_dates = _pick_expirations(raw_dates, count=expiries_count)
        else:
            exp_dates = raw_dates[:expiries_count]
        radius = strike_radius if strike_radius is not None else max(4.0, spot * 0.06)

        # ── Per-expiration strike selection ─────────────────────────────
        # Two modes:
        #   tier_quotas=None  → uniform round-robin (legacy behaviour)
        #   tier_quotas=dict  → TIERED allocation (Phase 11):
        #     Each expiration gets a quota based on its DTE bucket. 0DTE gets
        #     the most, LEAPS get the least. Far-dated coverage gap is
        #     backfilled by REST chain rotation at 30s cadence (acceptable
        #     lag for charm/vanna decay which moves on σ-shocks not spot).
        from datetime import date as _date_cls, datetime as _dt_cls
        _today_d = _date_cls.today()

        def _tier_for(exp_iso: str) -> str:
            try:
                d = _dt_cls.strptime(exp_iso, "%Y-%m-%d").date()
                dte = (d - _today_d).days
            except Exception:
                return 'L_31_180DTE'
            if dte == 0:    return 'L_0DTE'
            if dte == 1:    return 'L_1DTE'
            if dte == 2:    return 'L_2DTE'
            if dte <= 7:    return 'L_3_7DTE'
            if dte <= 9:    return 'L_8_9DTE'
            if dte <= 30:   return 'L_10_30DTE'
            if dte <= 180:  return 'L_31_180DTE'
            if dte <= 365:  return 'L_QUARTERLY'
            if dte <= 700:  return 'L_LEAPS_2YR'
            return 'L_LEAPS_3YR'

        per_exp: list[list[tuple[float, str]]] = []
        tier_log: dict = {}
        for exp_date in exp_dates:
            try:
                chain, _ = _schwab_chain_raw(ticker, exp_date)
                row = []
                for opt in chain:
                    strike = float(opt.get("strike", 0))
                    sym = opt.get("symbol", "")
                    if sym and abs(strike - spot) <= radius:
                        row.append((abs(strike - spot), sym))
                row.sort(key=lambda x: x[0])
                per_exp.append(row)
            except Exception as e:
                log.warning(f"[SCHWAB-BRIDGE] {ticker}: chain fetch failed for {exp_date}: {e}")
                per_exp.append([])

        symbols = []
        if tier_quotas:
            # TIERED MODE — apply per-expiration quota based on DTE tier
            for i, exp_date in enumerate(exp_dates):
                tier = _tier_for(exp_date)
                quota = tier_quotas.get(tier, 30)
                taken = [r[1] for r in per_exp[i][:quota]]
                symbols.extend(taken)
                tier_log[tier] = tier_log.get(tier, 0) + len(taken)
        else:
            # LEGACY — uniform round-robin interleave
            max_len = max((len(r) for r in per_exp), default=0)
            for i in range(max_len):
                for row in per_exp:
                    if i < len(row):
                        symbols.append(row[i][1])

        if not symbols:
            log.warning(f"[SCHWAB-BRIDGE] {ticker}: no options within ±{radius:.2f} of spot={spot:.2f}")
            return 0

        symbols = symbols[:cap]
        if tier_quotas:
            log.info(f"[SCHWAB-BRIDGE] {ticker} TIERED allocation: {tier_log}")
        # Route to the selected WebSocket connection on the (single) streamer.
        # conn_idx selects which of the streamer's N parallel WSs receives
        # the SUBS. Each WS has its own 3,000-symbol Schwab quota.
        _streamer.subscribe_options(symbols, conn_idx=conn_idx)
        # TIMESALE for these symbols flows via Tradier (see start_schwab_bridge
        # tail; Schwab's TIMESALE_OPTIONS service is not entitled).
        _subscribed_option_symbols_by_ticker[ticker] = symbols
        # Phase 21: register every Schwab-streamed symbol in routing set so
        # _on_tradier_timesale can dispatch directly to Schwab cache without
        # an empty-cache fall-through.
        _SCHWAB_WS_OSIS.update(symbols)
        log.info(
            f"[SCHWAB-BRIDGE] 📊 {ticker}: subscribed to {len(symbols)} options "
            f"(spot={spot:.2f}, radius=±{radius:.2f}, exps={exp_dates}) [conn-{conn_idx}]"
        )
        return len(symbols)
    except Exception as e:
        import traceback
        log.warning(f"[SCHWAB-BRIDGE] {ticker} options subscription failed: {e}")
        log.warning(traceback.format_exc())
        return 0


def _subscribe_spy_options() -> int:
    """SPY: ATM-focused coverage for SPY/SPX retail-vs-institutional flow cross.
    ATM ±$30 (~4% of spot $707), 12 hand-picked expirations (dailies + weekly
    Fridays + monthly OPEX + quarterly EOQ), cap 600. Budget slot of the
    3,000-symbol Schwab account cap (QQQ 1,500 + SPX 900 + SPY 600).
    """
    # Single-connection rollback: SPY at cap=300 ATM ±$15 × 4 expirations.
    return _subscribe_options_for_ticker("SPY", strike_radius=15.0,
                                          expiries_count=4, cap=300,
                                          conn_idx=0)


# Index options — entitled on the standard Schwab API tier. Verified 2026-04-20:
# $SPX (SPXW/SPX roots) returns live chains with full Greeks.
# $VIX dropped 2026-04-21: low marginal signal for QQQ/NQ dealer hedging;
# freed 40 slots redirected to Dec 18 OPEX coverage on QQQ + SPX.
# $NDX re-added 2026-05-04: previously REST-only via chain rotation (30s lag).
# Now streamed for parity with SPX. Cap 400 fits in 3,000 streaming budget
# headroom (current usage ≈2,584; budget headroom ≈416).
INDEX_OPTION_TICKERS = ("$SPX", "$NDX")


def _subscribe_index_options() -> int:
    """Subscribe to ATM index options for $SPX and $VIX.

    SPX: ±250 pts (~3.5% of 7100 spot) — wide enough to capture crash-hedge puts.
    VIX: ±20 pts integer-strikes — covers the liquid skew from low-teens through
         high-30s. Front 6 expirations capture weekly + monthly cycle. The cap
         100 fits inside the 6,000-budget headroom (3,000 currently used by
         QQQ/SPX/SPY).
    """
    # ── Phase 11 TIERED ALLOCATION (2026-04-30) ─────────────────────────
    # Old uniform allocation: SPX 1,100 + VIX 400 = 1,500 keys at uniform
    # radius. 0DTE got only 44 strikes (1100/25 expirations) — undersized.
    #
    # New tiered allocation: per-expiration strike count varies by DTE bucket.
    # 0DTE gets full coverage; LEAPS get sparse ATM-only sample. LEAPS gap
    # is backfilled by REST chain rotation (30s cadence). LEAPS Greeks move
    # on σ-shocks not spot-moves, so 30s lag is acceptable.
    #
    # Net savings for headroom: ~600 keys freed from streaming budget.
    # ── Phase 11b: tier_quotas balanced for 35-expiration SPX coverage ─────
    # Targets: 0DTE full + dailies + weeklies + 6 monthly OPEX + 4 quarterly
    # EOQ + 3 LEAPS Januaries. Total budget ≈ 800 (uses 100 of headroom).
    SPX_TIER_QUOTAS = {
        'L_0DTE':       100,   # full ATM band ±$200
        'L_1DTE':        60,
        'L_2DTE':        50,
        'L_3_7DTE':      35,   # × ~5 = 175
        'L_8_9DTE':      30,   # × ~2 = 60
        'L_10_30DTE':    20,   # × ~5 = 100
        'L_31_180DTE':   25,   # × ~6 monthly OPEX = 150 (CRITICAL — Jun/Jul/Aug/Sep/Oct/Nov OPEX)
        'L_QUARTERLY':   20,   # × ~2 (Sep+Dec EOQ overlap with monthly = no double-count) = 40
        'L_LEAPS_2YR':   12,   # × ~2 (Jan 2027, Jan 2028) = 24
        'L_LEAPS_3YR':    8,   # × ~1 (Jan 2029) = 8
    }
    VIX_TIER_QUOTAS = {
        # VIX has weekly Wednesday + monthly Wednesday — no daily 0DTE cycle.
        # With expiries_count=17, captures through ~May 2027 vs current Jan 2027.
        'L_0DTE':        60,
        'L_1DTE':        50,
        'L_2DTE':        40,
        'L_3_7DTE':      35,
        'L_8_9DTE':      30,
        'L_10_30DTE':    25,
        'L_31_180DTE':   18,   # × 6 monthlies = 108
        'L_QUARTERLY':   12,   # × ~3 (Nov/Dec/Jan 2027) = 36
        'L_LEAPS_2YR':   10,   # × ~3 (Feb-May 2027) = 30
        'L_LEAPS_3YR':    6,
    }
    # NDX (added 2026-05-04): scaled-down SPX equivalent. NDX trades at ~28000
    # (vs SPX ~7200) so dealer-hedging-relevant ATM band is wider in absolute
    # dollars but proportionally similar. Cap 400 fits headroom; would require
    # rebalancing if we add another major ticker.
    NDX_TIER_QUOTAS = {
        'L_0DTE':        60,   # ATM ±$300 dense
        'L_1DTE':        40,
        'L_2DTE':        30,
        'L_3_7DTE':      20,   # × ~5 = 100
        'L_8_9DTE':      20,
        'L_10_30DTE':    15,
        'L_31_180DTE':   18,   # × ~6 monthlies = 108
        'L_QUARTERLY':   12,   # × ~2 = 24
        'L_LEAPS_2YR':    8,
        'L_LEAPS_3YR':    6,
    }
    cfgs = {
        # SPX: expiries_count 25→35 captures all 6 monthly OPEX + 4 quarterly
        # EOQ + 3 LEAPS Januaries (Phase 11b). Cap 700→800 uses 100 of headroom.
        # Was: 0DTE 44 strikes uniform. Now: 0DTE 100, monthlies 25 each,
        # LEAPS 8-12 each — full structural backbone in real-time streaming.
        "$SPX": dict(strike_radius=400.0, expiries_count=35, cap=800,
                     conn_idx=0, tier_quotas=SPX_TIER_QUOTAS),
        # VIX: expiries_count 13→17 catches Feb-May 2027 expirations.
        # Cap 250→300 supports the 4 added expirations.
        "$VIX": dict(strike_radius=15.0, expiries_count=17, cap=300,
                     conn_idx=0, tier_quotas=VIX_TIER_QUOTAS),
        # NDX (re-added 2026-05-04, was REST-only): cap 400 = ~13% of available
        # streaming budget. strike_radius 600 = ±2.1% of 28000 spot — covers
        # full liquid skew band. expiries_count 15 catches weekly + monthly
        # cycle including front-end OPEX.
        "$NDX": dict(strike_radius=600.0, expiries_count=15, cap=400,
                     conn_idx=0, tier_quotas=NDX_TIER_QUOTAS),
    }
    total = 0
    for ticker, cfg in cfgs.items():
        try:
            n = _subscribe_options_for_ticker(ticker, **cfg)
            total += n
            # Add OPTIONS_BOOK L2 on the top ~30 ATM contracts for VIX so we
            # get dealer gamma posture on volatility itself. SPX already has
            # this through QQQ/equity bookflow channels; VIX is new ground.
            if ticker == "$VIX" and n > 0:
                try:
                    syms = _subscribed_option_symbols_by_ticker.get(ticker, [])
                    _vix_ob_budget = min(30, len(syms))
                    if _vix_ob_budget > 0:
                        _streamer.subscribe_options_book(syms[:_vix_ob_budget], conn_idx=0)
                        log.info(f"[SCHWAB-BRIDGE] 📊 $VIX OPTIONS_BOOK L2 on {_vix_ob_budget} ATM contracts")
                except Exception as _ob_e:
                    log.warning(f"[SCHWAB-BRIDGE] $VIX OPTIONS_BOOK subscription failed: {_ob_e}")
                # Try Schwab TIMESALE_OPTIONS for VIX (different product class
                # than QQQ/SPY where Schwab returns code=11). If entitled, this
                # gives us per-print VIX option tape; if not, the SUBS response
                # will log the failure and we move on.
                try:
                    _streamer.subscribe_timesale_options(syms[:_vix_ob_budget])
                    log.info(f"[SCHWAB-BRIDGE] 📊 $VIX TIMESALE_OPTIONS attempt on {_vix_ob_budget} contracts")
                except Exception as _ts_e:
                    log.warning(f"[SCHWAB-BRIDGE] $VIX TIMESALE_OPTIONS subscription failed: {_ts_e}")
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] {ticker} index-options subscription error: {e}")
    log.info(f"[SCHWAB-BRIDGE] 📊 Index options total: {total} contracts ($SPX + $VIX)")
    return total


# ── Phase 13 (2026-04-30) — Mag-8 single-name options on Schwab streaming ──
# Replaces Tradier ORATS-based REST polling (30-60 min stale Greeks) with
# real-time Schwab LEVELONE_OPTIONS push. Schwab provides full Greeks + IV +
# OI on single-name options at the standard tier (verified live: NVDA chain
# returns delta/gamma/volatility populated). Each ticker gets ~32 ATM
# contracts; budget = 8 × 32 = 256 keys total (uses Schwab headroom from 534).
MAG8_TICKERS = ('NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO')


def _subscribe_mag8_options() -> int:
    """Subscribe Mag-8 ATM contracts to Schwab LEVELONE_OPTIONS for real-time Greeks.

    Why: Tradier ORATS Greeks are 30-60 min stale (verified live: updated_at
    ~54 min ago). For Mag-8 walls panel and any signal that uses single-name
    Greeks, real-time push from Schwab is strictly better.

    Per-ticker tier quotas are intentionally tight — we only need ATM-band
    Greeks for walls/regime detection, not full chains. ~32 contracts per
    ticker × 8 tickers = 256 keys. Schwab budget post-this: 2,722 / 3,000.
    """
    MAG8_TIER_QUOTAS = {
        # Tighter than QQQ/SPX — only ATM-band needed for walls computation
        'L_0DTE':        12,   # 0DTE single-names rare but high-impact
        'L_1DTE':         8,
        'L_2DTE':         6,
        'L_3_7DTE':       6,
        'L_8_9DTE':       5,
        'L_10_30DTE':     5,
        'L_31_180DTE':    4,
        'L_QUARTERLY':    3,
        'L_LEAPS_2YR':    2,
        'L_LEAPS_3YR':    2,
    }
    total = 0
    for ticker in MAG8_TICKERS:
        try:
            # Most Mag-8 names use ~$5 strikes near ATM, so ±$15 = ~6 strikes
            # per side. Tier quotas above will limit each expiration to fit.
            n = _subscribe_options_for_ticker(
                ticker,
                strike_radius=15.0,
                expiries_count=10,
                cap=40,                       # ~32 actual contracts on average
                conn_idx=0,
                tier_quotas=MAG8_TIER_QUOTAS,
            )
            total += n
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] {ticker} Mag-8 options subscription error: {e}")
    log.info(f"[SCHWAB-BRIDGE] 📊 Mag-8 single-name options total: {total} contracts "
             f"(Phase 13 — replaces stale Tradier ORATS with real-time Schwab Greeks)")
    return total


# ────────────────────────────────────────────────────────────────────────────
# Phase 17B (2026-04-30) — Streaming cap reallocation
# ────────────────────────────────────────────────────────────────────────────
# Goal: Free QQQ ATM-±$100 streaming slots (1,412 → 150 0DTE-only) and
# redirect to SPY restoration + IWM + sector ETFs + Mag-7 expansion.
# Non-0DTE QQQ Greeks now come from Phase 17 BSM solver (sub-microsec).
# Final allocation target: ~2,894 / 3,000 (106 free for chain growth).
# ────────────────────────────────────────────────────────────────────────────

MAG7_TICKERS = ('NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA')   # AVGO dropped
SECTOR_ETF_TICKERS = ('XLK', 'XLE', 'XLF')


def _subscribe_qqq_0dte_only() -> int:
    """Phase 21 (2026-05-01): Schwab streams QQQ short-DTE — 0/1/2/3-7 DTE.
    DTE>=8 still uses BSM (accurate at longer T, saves Schwab cap).

    Old name kept for backward compat; this is now multi-DTE.

    Why short-DTE on Schwab WS, not BSM:
      - 0-7 DTE Δ swings hardest as T → 0; Schwab's institutional IV
        smoothing handles this materially better than textbook BSM
      - Sub-200ms Schwab updates ensure FlowAccumulator and pin_convergence
        see fresh Greeks during fast moves
      - 0-7 DTE captures ~80% of QQQ option flow

    Why DTE>=8 stays BSM:
      - BSM and Schwab Greeks converge to within ±1% Δ at T > 8/365
      - Saves Schwab cap slots (290 vs 1,400 for full chain)
      - BSM at sub-microsecond per-print is essentially free

    Phase 21 adds:
      - 60 contracts × 1DTE  (tomorrow expiry)
      - 50 contracts × 2DTE  (day after)
      - 80 contracts × 3-7 DTE (weekly hot strikes)
      total +190 → 290 QQQ contracts streamed

    Cap: 290 / 3,000 (after this) — leaves 593 free slots for emergencies.
    """
    QQQ_TIER_QUOTAS = {
        'L_0DTE':       100,  # ATM ±$25 (was 150 — trimmed to make room)
        'L_1DTE':        60,  # NEW — Phase 21
        'L_2DTE':        50,  # NEW — Phase 21
        'L_3_7DTE':      80,  # NEW — Phase 21 (weekly hot strikes)
        'L_8_9DTE':       0,  # BSM
        'L_10_30DTE':     0,  # BSM
        'L_31_180DTE':    0,  # BSM
        'L_QUARTERLY':    0,  # BSM
        'L_LEAPS_2YR':    0,  # BSM
        'L_LEAPS_3YR':    0,  # BSM
    }
    n = _subscribe_options_for_ticker(
        "QQQ",
        strike_radius=25.0,
        expiries_count=5,    # nearest 5 expirations covers 0/1/2/3-7 DTE
        cap=290,             # 100 + 60 + 50 + 80 = 290
        conn_idx=0,
        tier_quotas=QQQ_TIER_QUOTAS,
    )
    # 2026-05-01: restore QQQ OPTIONS_BOOK L2 (regressed away from Phase 17B
    # rewrite of _subscribe_qqq_options→_subscribe_qqq_0dte_only). Subscribes
    # the top ATM-most 60 contracts to OPTIONS_BOOK for per-strike book
    # microstructure (dealer warehouse, posted/caught analytics).
    # Cap: Schwab OPTIONS_BOOK service limit is 100; $VIX takes 30, leaving 70.
    if n > 0:
        try:
            qqq_syms = _subscribed_option_symbols_by_ticker.get('QQQ', [])
            qqq_ob_budget = min(60, len(qqq_syms))
            if qqq_ob_budget > 0:
                _streamer.subscribe_options_book(qqq_syms[:qqq_ob_budget], conn_idx=0)
                log.info(f"[SCHWAB-BRIDGE] 📊 QQQ OPTIONS_BOOK L2 on {qqq_ob_budget} ATM 0DTE contracts (Phase 17B regression fix)")
        except Exception as _ob_e:
            log.warning(f"[SCHWAB-BRIDGE] QQQ OPTIONS_BOOK subscription failed: {_ob_e}")
    return n


def _subscribe_mag7_expanded() -> int:
    """Phase 17B: Mag-7 (AVGO dropped) with EXPANDED coverage per name.
    7 names × 70 strikes ≈ 490 contracts (was 8 × 40 = 320).
    Captures ATM ±10% per stock — institutional rotation positioning.
    """
    MAG7_TIER_QUOTAS = {
        'L_0DTE':        16,   # 0DTE single-name moves
        'L_1DTE':        12,
        'L_2DTE':        10,
        'L_3_7DTE':       9,
        'L_8_9DTE':       8,
        'L_10_30DTE':     7,
        'L_31_180DTE':    6,
        'L_QUARTERLY':    4,
        'L_LEAPS_2YR':    2,
        'L_LEAPS_3YR':    2,
    }
    total = 0
    for ticker in MAG7_TICKERS:
        try:
            n = _subscribe_options_for_ticker(
                ticker,
                strike_radius=30.0,    # wider radius = ATM ±10% on $300 stock
                expiries_count=10,
                cap=70,                 # 70 per name × 7 = 490 total
                conn_idx=0,
                tier_quotas=MAG7_TIER_QUOTAS,
            )
            total += n
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] {ticker} Mag-7 options subscription error: {e}")
    log.info(f"[SCHWAB-BRIDGE] 📊 Mag-7 expanded total: {total} contracts "
             f"(Phase 17B — 7 names × 70 strikes, AVGO dropped from streaming)")
    return total


def _subscribe_spy_options_full() -> int:
    """Phase 17B: SPY restored at cap=800 (was dropped 2026-04-29).
    ATM ±$30 across ~10 expirations covers retail-vs-institutional flow cross.
    Restores SPY tab on FLOW chart with live per-print events.
    """
    SPY_TIER_QUOTAS = {
        'L_0DTE':       80,
        'L_1DTE':       60,
        'L_2DTE':       50,
        'L_3_7DTE':     40,
        'L_8_9DTE':     35,
        'L_10_30DTE':   30,
        'L_31_180DTE':  25,
        'L_QUARTERLY':  15,
        'L_LEAPS_2YR':   8,
        'L_LEAPS_3YR':   5,
    }
    return _subscribe_options_for_ticker(
        "SPY",
        strike_radius=30.0,
        expiries_count=10,
        cap=800,
        conn_idx=0,
        tier_quotas=SPY_TIER_QUOTAS,
    )


def _subscribe_iwm_options() -> int:
    """Phase 17B: IWM (Russell 2000 ETF) — small-cap risk thermometer.
    ~200 contracts, ATM ±$10 across 8 expirations.
    Used for: small-cap put surge detection (de-risking signal).
    """
    return _subscribe_options_for_ticker(
        "IWM",
        strike_radius=10.0,
        expiries_count=8,
        cap=200,
        conn_idx=0,
    )


def _subscribe_sector_etf_options() -> int:
    """Phase 17B: Sector ETF options for cross-sector rotation detection.
    XLK (tech), XLE (energy), XLF (financials) — ~70 contracts each = ~210 total.
    """
    total = 0
    for ticker in SECTOR_ETF_TICKERS:
        try:
            n = _subscribe_options_for_ticker(
                ticker,
                strike_radius=8.0,
                expiries_count=6,
                cap=70,
                conn_idx=0,
            )
            total += n
        except Exception as e:
            log.warning(f"[SCHWAB-BRIDGE] {ticker} sector ETF options subscription error: {e}")
    log.info(f"[SCHWAB-BRIDGE] 📊 Sector ETF options total: {total} contracts "
             f"({','.join(SECTOR_ETF_TICKERS)})")
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

# ── Phase D: equity print ring buffer ────────────────────────────────────
# Time-bound (not count-bound) so retention self-adjusts with tape density.
# Each entry is (ts_ms, price, size, exec_mic, side_sign) where side_sign is
# the tick-rule already computed in the equity_tape emit path.
# CONFIGURED infra value; documented in MEASURED_VALUES.md.
EQUITY_PRINT_RETENTION_S = 60.0
import threading as _threading_mod
from collections import deque as _deque
_recent_equity_prints: dict = {}   # symbol -> deque[(ts_ms, price, size, mic, sign)]
_equity_prints_lock = _threading_mod.Lock()


# ── TIMESALE_EQUITY ring buffer ───────────────────────────────────────
# Per-print equity tape from Schwab's TIMESALE_EQUITY service. Distinct
# from `_recent_equity_prints` (above) which is populated by the
# LEVELONE_EQUITIES last_size-inferred path. TIMESALE_EQUITY gives us the
# TRUE per-print stream — every trade, not just the ones that happened to
# coincide with a NBBO change.
#
# Each entry: (ts_ms, price, size, mic, side_sign, sequence)
#   - mic: looked up from _on_equity_quote._eq_mic[symbol]['last'] cache
#   - side_sign: tick rule against most recent cached NBBO bid/ask
TIMESALE_EQUITY_RETENTION_S = 300.0   # 5-min window — denser than LEVELONE because every print
_timesale_equity_prints: dict = {}      # symbol -> deque[(ts_ms, price, size, mic, sign, seq)]
_timesale_equity_lock   = _threading_mod.Lock()
# Per-venue cumulative volume aggregator (resets per session via 24h prune)
_timesale_equity_by_venue: dict = {}    # symbol -> {mic: {buy_sz, sell_sz, neutral_sz, trades}}


def _on_equity_timesale(data: dict) -> None:
    """Per-print equity tape handler.

    Schwab TIMESALE_EQUITY fields: symbol, trade_time, last_price, last_size,
    last_sequence. Schwab does NOT include the execution venue in the
    timesale message, so we pull the most recent `last_mic` from the
    LEVELONE_EQUITIES cache (`_on_equity_quote._eq_mic[symbol]['last']`).
    Tick-rule for aggressor side uses cached bid/ask from the same path.
    """
    try:
        symbol = (data.get('symbol') or '').strip()
        price  = float(data.get('last_price') or 0)
        size   = int(data.get('last_size') or 0)
        ts_ms  = int(data.get('trade_time') or (time.time() * 1000))
        seq    = int(data.get('last_sequence') or 0)
        if not symbol or price <= 0 or size <= 0:
            return

        # Pull cached NBBO + last MIC from LEVELONE_EQUITIES path.
        # _on_equity_quote stores its NBBO on the function attribute
        # `_last_nbbo[symbol]` — fall back to 0 if missing (first prints).
        nbbo_cache = getattr(_on_equity_quote, '_last_nbbo', {}) or {}
        mic_cache  = getattr(_on_equity_quote, '_eq_mic', {}) or {}
        nbbo = nbbo_cache.get(symbol) or {}
        bid  = float(nbbo.get('bid') or 0)
        ask  = float(nbbo.get('ask') or 0)
        last_mic_dict = mic_cache.get(symbol) or {}
        mic = (last_mic_dict.get('last') or '').strip().upper()
        # Tick rule
        if ask > 0 and price >= ask:
            side = 1
        elif bid > 0 and price <= bid:
            side = -1
        else:
            side = 0  # ambiguous mid

        with _timesale_equity_lock:
            buf = _timesale_equity_prints.get(symbol)
            if buf is None:
                buf = _deque()
                _timesale_equity_prints[symbol] = buf
            buf.append((ts_ms, price, size, mic, side, seq))
            cutoff = ts_ms - int(TIMESALE_EQUITY_RETENTION_S * 1000)
            while buf and buf[0][0] < cutoff:
                buf.popleft()

            # Per-venue aggregator
            ven = _timesale_equity_by_venue.setdefault(symbol, {})
            row = ven.setdefault(mic or '?', {
                'buy_sz': 0, 'sell_sz': 0, 'neutral_sz': 0,
                'trades': 0, 'last_ts': 0,
            })
            if side > 0:
                row['buy_sz']     += size
            elif side < 0:
                row['sell_sz']    += size
            else:
                row['neutral_sz'] += size
            row['trades']  += 1
            row['last_ts']  = ts_ms
    except Exception as _e:
        log.debug(f"[TIMESALE-EQ] err: {_e}")


def lookup_equity_window(symbol: str, ts_start_ms: int, ts_end_ms: int) -> list:
    """Return list of (ts_ms, price, size, exec_mic, side_sign) in
    [ts_start_ms, ts_end_ms). Thread-safe snapshot under _equity_prints_lock.

    Called from mm_attribution when an IMPULSE_CLOSED record is about to be
    written — enriches the record with the equity prints that fell in its
    structural window [prev_option_print, curr_option_print].
    """
    with _equity_prints_lock:
        buf = _recent_equity_prints.get(symbol)
        if not buf:
            return []
        return [t for t in buf if ts_start_ms <= t[0] < ts_end_ms]


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

    # NOTE: /VX* futures handler intentionally absent — Schwab's market
    # data API doesn't support CFE-listed VIX futures (every /VX* symbol
    # returns "invalidSymbols" from REST; streamer accepts SUBS silently
    # but never delivers ticks). VIX term structure is built instead via
    # put-call parity on VIX options — see /api/_debug/vix_term_structure.

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

    # 2026-05-04: cash indices ($VIX, $SPX, $TICK, $NDX, $TNX, $TRIN, etc.)
    # have no actual trades — Schwab pushes the index VALUE in field 33
    # (`mark`), not field 3 (`last`). Without this fallback, 14 of 19
    # subscribed equity symbols silently dropped (verified via /api/_debug/
    # spots). For ETF tickers (QQQ, SPY) `last` is always populated, so the
    # fallback only kicks in when `last` is 0/null.
    if not last or last <= 0:
        mark = data.get('mark', 0) or 0
        if mark > 0:
            last = mark
        else:
            return

    _tick_count += 1

    # Universal spot cache (used by screener→accumulator bridge for delta est).
    # Indices with `$` prefix (e.g. $VIX, $VXN, $NDX.X) get stored under
    # their stripped name so consumers can look them up by ticker without
    # needing to know the prefix convention.
    # 2026-04-28 BUG FIX: previous version did `rstrip('.X')` which strips
    # ANY trailing character in the set {'.', 'X'} — turning $VVIX into 'VVI'.
    # Use explicit suffix check instead.
    if symbol:
        if symbol.startswith('$'):
            _key = symbol[1:]  # drop the leading '$'
            if _key.endswith('.X'):
                _key = _key[:-2]   # $NDX.X → NDX
            if _key:
                _latest_spot_by_ticker[_key] = last
        else:
            _latest_spot_by_ticker[symbol] = last

    # Update NDX cache (critical for NQ conversion accuracy)
    if symbol in ('$NDX.X', 'NDX', '$NDX'):
        _latest_ndx = last
        if _latest_nq > 0:
            _nq_ndx_ratio = _latest_nq / _latest_ndx

    # ── Phase 20B: feed VIX-family spots into VolSurface VIX-term HMM ──
    # Three symbols of interest: $VIX (30d), $VVIX (vol-of-vol), $VIX1D (1d).
    # Each tick updates the corresponding feature; HMM observes on next
    # zone_update emission cycle.
    if symbol in ('$VIX', '$VVIX', '$VIX1D') and _vol_surface is not None:
        try:
            if symbol == '$VIX':
                _vol_surface.set_vix_family(vix=last)
            elif symbol == '$VVIX':
                _vol_surface.set_vix_family(vvix=last)
            elif symbol == '$VIX1D':
                _vol_surface.set_vix_family(vix1d=last)
        except Exception:
            pass

    # Update QQQ cache
    if symbol == 'QQQ':
        _latest_qqq = last
        if _latest_nq > 0:
            _nq_qqq_ratio = _latest_nq / _latest_qqq

    # Wall-signals: feed spot for any ticker we have walls for. QQQ is the
    # only ticker with active OPTIONS_BOOK subscription (Phase 1) so pulls/
    # prints only land on QQQ, but pushing spot for SPY as well lets future
    # expansions "just work" without another code change.
    if symbol in ('QQQ', 'SPY'):
        try:
            from connectors import wall_signals as _ws
            _ws.update_spot(symbol, last)
        except Exception:
            pass

    # ── Cache latest NBBO per symbol so TIMESALE_EQUITY can apply tick
    # rule. Schwab's TIMESALE_EQUITY only carries (price, size, time, seq);
    # the bid/ask required for aggressor classification come from
    # LEVELONE_EQUITIES updates. We store the most-recent quote per symbol
    # here so the timesale handler reads a consistent snapshot.
    if symbol in ('QQQ', 'SPY'):
        try:
            _nbbo_cache = getattr(_on_equity_quote, '_last_nbbo', None)
            if _nbbo_cache is None:
                _nbbo_cache = {}
                _on_equity_quote._last_nbbo = _nbbo_cache
            _b = data.get('bid')
            _a = data.get('ask')
            row = _nbbo_cache.setdefault(symbol, {'bid': 0.0, 'ask': 0.0, 'ts_ms': 0})
            if _b is not None and _b > 0:
                row['bid'] = float(_b)
            if _a is not None and _a > 0:
                row['ask'] = float(_a)
            row['ts_ms'] = int(time.time() * 1000)
        except Exception:
            pass

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

        # ── Conviction Scorer volume feed ──
        # Track every equity print so the rolling 5-min volume baseline
        # (the denominator of amplification_factor = hp_γ_shares / vol_5m)
        # reflects live tape rather than the configured fallback.
        if symbol in ('QQQ', 'SPY') and last_size > 0:
            try:
                from connectors.conviction_score import get_scorer
                _ccs = get_scorer()
                if _ccs is not None:
                    _ccs.feed_equity_print(symbol, int(last_size), time.time())
            except Exception:
                pass

        # ── Phase D: equity print ring buffer ──
        # Tick-rule for side_sign: +1 buyer-initiated, -1 seller-initiated, 0 unknown.
        # Matches the side inference already used in the equity_tape emit below.
        if symbol == 'QQQ' and last_size > 0:
            try:
                ts_ms = int(time.time() * 1000)
                if bid > 0 and last >= ask:
                    side_sign = 1
                elif ask > 0 and last <= bid:
                    side_sign = -1
                else:
                    side_sign = 0
                # Grab the latest MIC we've cached (raw_last may not be in this
                # delta). Safe default: empty string.
                _mic_cache = getattr(_on_equity_quote, '_eq_mic', {}).get(symbol, {})
                exec_mic = _mic_cache.get('last', '') or str(data.get('last_mic', '') or '')
                with _equity_prints_lock:
                    buf = _recent_equity_prints.get(symbol)
                    if buf is None:
                        buf = _deque()
                        _recent_equity_prints[symbol] = buf
                    buf.append((ts_ms, float(last), int(last_size), str(exec_mic), side_sign))
                    # Prune entries older than the retention window — time-bound.
                    cutoff = ts_ms - int(EQUITY_PRINT_RETENTION_S * 1000)
                    while buf and buf[0][0] < cutoff:
                        buf.popleft()
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


_timesale_stats = {'trades': 0, 'no_cache': 0, 'fed': 0}


def _on_options_timesale(data):
    """Handle TIMESALE_OPTIONS trade prints — raw trade-by-trade feed.

    TIMESALE gives exact prints (symbol, trade_time ms, last_price, last_size,
    sequence). Unlike LEVELONE (where trades are inferred from last_size on
    quote-tick updates), TIMESALE is the raw print stream — no missed blocks,
    no aggregation lag.

    Strategy: merge each print with the cached LEVELONE snapshot (which has
    bid/ask/delta/underlying_price) and feed the combined view to the flow
    accumulator. accumulator dedups by (symbol, trade_time), so running
    LEVELONE's last_size path and TIMESALE in parallel is safe.
    """
    try:
        symbol = data.get('symbol') or ''
        if not symbol:
            return
        size = int(data.get('last_size') or 0)
        if size <= 0:
            return
        price = float(data.get('last_price') or 0.0)
        if price <= 0:
            return
        trade_time = int(data.get('trade_time') or 0)

        _timesale_stats['trades'] += 1

        # Merge with cached LEVELONE quote snapshot so delta/bid/ask/spot
        # are populated. Without cached context we can't classify aggressor
        # or compute signed delta-notional.
        _sym_cache = getattr(_on_options_quote, '_sym_cache', {})
        cached = _sym_cache.get(symbol)
        if not cached:
            _timesale_stats['no_cache'] += 1
            return

        merged = dict(cached)
        merged['symbol'] = symbol
        merged['last'] = price
        merged['last_size'] = size
        if trade_time > 0:
            merged['trade_time'] = trade_time

        from connectors.flow_accumulator import get_accumulator
        acc = get_accumulator()
        if acc is not None:
            acc.on_option_update(merged)
            _timesale_stats['fed'] += 1

        # ── Tape emit path: queue this print for option_trade_batch ────────
        # Real trade print (TIMESALE) — classify aggressor against the BBO
        # that was freshest at trade_time, not against whatever's cached now.
        try:
            _b, _a = _lookup_bbo_at(symbol, trade_time)
            if _b <= 0 or _a <= 0 or _b >= _a:
                # Fallback to cached snapshot
                _b = float(cached.get('bid') or 0)
                _a = float(cached.get('ask') or 0)
            _aggr = ''
            if price > 0 and _b > 0 and _a > 0 and _b < _a:
                _mid = (_b + _a) / 2
                _spread = _a - _b
                if price >= _a - 0.01 or price >= _mid + _spread * 0.35:
                    _aggr = 'BUY'
                elif price <= _b + 0.01 or price <= _mid - _spread * 0.35:
                    _aggr = 'SELL'
                else:
                    _aggr = 'MID'

            _ct = cached.get('contract_type', '')
            _side = 'C' if _ct in ('C', 'CALL', 'call') else 'P'
            # Infer ticker root from the OSI symbol (chars up to first digit/space)
            _root = ''
            for _ch in symbol:
                if _ch.isdigit() or _ch == ' ':
                    break
                _root += _ch
            _root = _root.strip() or symbol[:6].strip()

            _tape_payload = {
                'ticker':        _root,
                'symbol':        symbol,
                'strike':        float(cached.get('strike') or 0),
                'side':          _side,
                'dte':           int(cached.get('dte') or 0),
                'last':          round(price, 4),
                'last_size':     size,
                'trade_time_ms': trade_time,
                'sequence':      int(data.get('last_sequence') or 0),
                'delta':         round(float(cached.get('delta') or 0), 5),
                'gamma':         round(float(cached.get('gamma') or 0), 6),
                'iv':            round(float(cached.get('iv') or 0), 4),
                'theta':         round(float(cached.get('theta') or 0), 5),
                'bid':           round(_b, 4),
                'ask':           round(_a, 4),
                'aggressor':     _aggr,
                'underlying':    round(float(cached.get('underlying_price') or 0), 4),
                'mark':          round(float(cached.get('mark') or price), 4),
                'mark_change':   round(float(cached.get('mark_change') or 0), 4),
                'oi':            int(cached.get('oi') or 0),
                'vol':           int(cached.get('vol') or 0),
                'premium':       round(price * size * 100, 2),
                'dollar_gex':    round(float(cached.get('dollar_gex') or 0) / 1e6, 4),
            }
            with _opt_trade_batch_lock:
                _opt_trade_batch.append(_tape_payload)
        except Exception:
            pass
    except Exception:
        pass


def _on_options_quote(data):
    """Handle Level 1 options updates (NDX options) — accumulate live Greeks.
    Now captures enriched fields: security_status, quote_time, trade_time,
    mark_change, mark_pct_change, theoretical_value, indicative_bid/ask.
    """
    # 2026-05-06 PERF INSTRUMENTATION
    _perf_t0 = time.perf_counter()
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
        # Store any new fields from this update.
        # 2026-05-05: `dte` (and similar zero-meaningful fields) are special-cased
        # because the generic `v != 0` filter would reject dte=0 (today's
        # expiration). That bug caused 0DTE prints to be classified as 1DTE
        # since the cache never updated past day-1. Verified live: IWM 260505
        # was logging dte_field=1 in flow_accumulator on 2026-05-05.
        ALLOW_ZERO_FIELDS = {'dte', 'days_to_exp'}
        for k, v in data.items():
            if v is None or v == '':
                continue
            if v == 0 and k not in ALLOW_ZERO_FIELDS:
                continue
            cached[k] = v
        _sym_cache[sym_key] = cached

        # ── BBO ring buffer: record (arrival_ms, bid, ask) for accurate
        # aggressor-at-trade-time classification in _on_options_timesale.
        # Use arrival wall clock (not Schwab's quote_time which can drift).
        try:
            _b = float(cached.get('bid') or 0)
            _a = float(cached.get('ask') or 0)
            if _b > 0 and _a > 0 and _b < _a:
                _ts_ms = int(time.time() * 1000)
                with _bbo_history_lock:
                    _bbo_history[sym_key].append((_ts_ms, _b, _a))
        except Exception:
            pass

        # ── Book-staleness flag from indicative quotes ────────────────
        # Schwab streamer fields 52/53 (indicative_ask/bid) carry the
        # exchange's calculated quote when the displayed inside is wide
        # or stale. When the displayed spread is more than 2× the
        # indicative spread (and the indicative spread itself is positive),
        # the book is gapped — there's no live order resting tight, but
        # the contract still has a true value the indicative reflects.
        # Downstream consumers (VIX term structure, mispricing ranker)
        # use this flag to fall back to the indicative midpoint or
        # filter the strike out entirely.
        try:
            _b   = float(cached.get('bid') or 0)
            _a   = float(cached.get('ask') or 0)
            _ib  = float(cached.get('indicative_bid') or 0)
            _ia  = float(cached.get('indicative_ask') or 0)
            disp_spread = (_a - _b) if (_a > 0 and _b > 0) else 0.0
            ind_spread  = (_ia - _ib) if (_ia > 0 and _ib > 0) else 0.0
            # Floor the indicative-spread comparison at 5¢ — for ATM
            # contracts indicative is often 1-2¢, which would flag every
            # contract as "stale" even when displayed is 4¢. We only
            # consider the book stale when the displayed gap is >2×
            # indicative AND the indicative spread is non-trivial OR the
            # displayed spread is >25¢ (objectively wide).
            if ind_spread > 0:
                cached['book_stale'] = bool(
                    disp_spread > 2.0 * max(ind_spread, 0.05)
                    or disp_spread > 0.25
                )
                cached['book_indicative_mid'] = round((_ia + _ib) / 2.0, 4)
                cached['book_displayed_mid']  = round((_a + _b) / 2.0, 4)
                cached['book_stale_ratio']    = round(
                    disp_spread / max(ind_spread, 0.01), 2)
            else:
                cached['book_stale'] = False
                cached['book_indicative_mid'] = None
                cached['book_displayed_mid']  = round((_a + _b) / 2.0, 4) if (_a > 0 and _b > 0) else None
                cached['book_stale_ratio']    = None
        except Exception as _e:
            log.debug(f"[BOOK-STALE] sym={sym_key} err={_e}")

        # Feed signed-flow accumulator with the MERGED cached view so that
        # delta/dte/underlying_price are always populated (Schwab sends
        # delta-only updates where these fields are absent on most messages).
        # Only count actual trades — last_size must be in THIS incoming delta
        # (not cached) so we don't re-process stale trades.
        #
        # SPY OVER-COUNT FIX (2026-05-01):
        # Schwab occasionally streams `last_size` values of 100K-600K for SPY
        # options — these are 100-1000× larger than any real institutional
        # 0DTE block trade (~5K contract max). Empirically observed in SPY's
        # top-15 signed prints (400K, 562K, 606K size values). Likely a Schwab
        # data quirk where field 18 (last_size) gets a cumulative-volume value
        # on certain message types — not seen in QQQ/SPX/Mag-8 streams.
        # Guard: drop any print with last_size > 100,000 contracts. Even the
        # largest documented institutional blocks (e.g. JPM 50K-contract SPY
        # collar in 2024) stayed under 100K. Logged for forensic, not silent.
        _ls_in = data.get('last_size', 0) or 0
        _SCHWAB_OVERSIZE_GUARD = 100_000  # contracts
        if _ls_in > _SCHWAB_OVERSIZE_GUARD:
            _ovs = getattr(_on_options_quote, '_oversize_drops', 0)
            _on_options_quote._oversize_drops = _ovs + 1
            if _ovs < 5:  # log first 5 so we see the data shape
                log.warning(
                    f"[SCHWAB-OPT-OVERSIZE] DROPPED last_size={_ls_in} "
                    f"sym={data.get('symbol')} last={data.get('last')} "
                    f"trade_time={data.get('trade_time')} "
                    f"(possible field 8/18 swap) — drop #{_ovs+1}"
                )
            # Don't process this print — fall through past accumulator feed
        elif data.get('last_size', 0) > 0:
            # SPY/QQQ over-count diagnostic (added 2026-05-01):
            # Track per-symbol (call_count, distinct_trade_times). If call_count
            # > distinct_trade_times, Schwab is repeating the same trade across
            # multiple last_size>0 updates (broken dedup → over-count). If they
            # match, the gate-fire pattern is correct and over-count is elsewhere.
            _sym_dbg = data.get('symbol', '')
            _root_dbg = _sym_dbg[:6].strip() if len(_sym_dbg) >= 6 else ''
            if _root_dbg in ('SPY', 'QQQ'):
                _q_diag = getattr(_on_options_quote, '_q_diag', None)
                if _q_diag is None:
                    _q_diag = {}
                    _on_options_quote._q_diag = _q_diag
                _entry = _q_diag.setdefault(_sym_dbg, {
                    'calls': 0, 'tt_set': set(),
                })
                _entry['calls'] += 1
                _tt = data.get('trade_time')
                if _tt is not None:
                    _entry['tt_set'].add(_tt)

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

    # ── Per-ticker GEX (for Key Level detector / AI Panel) ────────────
    # Mirror the write into a per-ticker dict so we can compute walls for
    # SPX/SPY/QQQ independently. Normalize SPXW→SPX etc. (same rule as
    # flow_accumulator).
    _sym_root = sym_key[:6].strip() if sym_key else ''
    if _sym_root in ('SPXW',): _sym_root = 'SPX'
    elif _sym_root in ('NDXP',): _sym_root = 'NDX'
    elif _sym_root in ('RUTW',): _sym_root = 'RUT'
    elif _sym_root in ('VIXW',): _sym_root = 'VIX'
    if _sym_root:
        # Key by full OCC sym_key (includes expiration), not by strike alone.
        # Aggregating latest-value per CONTRACT lets _compute_walls_for sum
        # across all expirations at a given strike — previously each new
        # expiration's message overwrote the prior one at the same strike,
        # causing wall_proximity to fire on walls that flapped 30+ points
        # every few seconds. Proven 2026-04-23 via /api/_debug/walls_audit.
        _pt = _per_ticker_gex.setdefault(_sym_root, {})
        # Per-ticker dollar-GEX: use the underlying_price from this message
        # (SPX/SPY/QQQ each have their own spot — don't reuse QQQ's).
        _pt_spot = underlying_price or 0
        if _pt_spot > 0:
            _pt_dollar_gex = gamma * effective_oi * 100 * (_pt_spot * _pt_spot / 100)
        else:
            _pt_dollar_gex = 0
        _side = 'call' if contract_type in ('C', 'CALL', 'call') else (
                'put'  if contract_type in ('P', 'PUT', 'put')   else None)
        if _side:
            _pt[sym_key] = {
                'strike':   strike,
                'side':     _side,
                'gamma_dollars': _pt_dollar_gex,
                'oi':       effective_oi,
                'delta':    abs(float(delta or 0)),
            }
        # Also stash the live spot per-ticker for reuse elsewhere
        if _pt_spot > 0:
            _latest_spot_by_ticker[_sym_root] = _pt_spot

    # Feed into GreekSurface engine for higher-order computations
    # Surface is QQQ-dealer-only; filter by underlying root so SPX/NDX/RUT/VIX
    # options don't pollute the QQQ dealer book. _sym_root normalized above
    # (SPXW→SPX, NDXP→NDX, etc.).
    #
    # ⚠ Schwab delta-only updates omit `strike` and `contract_type` from the
    # raw `data` dict (only the changed fields are sent). GreekSurface.update()
    # reads them via `data.get('strike')` / `data.get('contract_type')` and
    # returns early if missing → `_strikes` never accumulates → `_surface`
    # stays empty → /api/hedge_pressure/QQQ returns no rows. We've already
    # parsed `strike` and `contract_type` from the OSI symbol above (lines
    # 1129–1143) and `cached` holds the merged accumulated view from prior
    # snapshots. Pass the merged dict with the parsed locals injected so
    # update() always sees valid strike/CT and the latest Greeks.
    if _greek_surface and _sym_root == 'QQQ':
        try:
            _greek_data = dict(cached) if cached else dict(data)
            _greek_data['strike'] = strike
            _greek_data['contract_type'] = contract_type
            _greek_surface.update(_greek_data)
        except Exception as _gs_e:
            log.debug(f"[SCHWAB-BRIDGE] greek_surface.update err sym={sym_key!r}: {_gs_e}")

    # Feed into 0DTE Squeeze Detector
    if _dte0_squeeze:
        try:
            _dte0_squeeze.on_options_update(data)
        except Exception:
            pass

    # ── Queue option_mark_update for batched emit ──────────────────
    # Per-contract latest-wins buffer; flushed once every _OPT_MARK_FLUSH_SEC
    # as a single `option_mark_batch` frame. Cuts WS overhead ~10x vs one
    # emit per contract.
    _emit_mark = mark
    if not _emit_mark and strike in _live_gex:
        side_key = 'call_mark' if contract_type in ('C', 'CALL', 'call') else 'put_mark'
        _emit_mark = _live_gex[strike].get(side_key, 0)
    if _socketio and (_emit_mark or iv):
        try:
            import math
            contract_key = f"{_sym_root}:{strike}{'C' if contract_type in ('C','CALL','call') else 'P'}"
            _bid = float(data.get('bid', cached.get('bid', 0)) or 0)
            _ask = float(data.get('ask', cached.get('ask', 0)) or 0)
            _last = float(data.get('last', cached.get('last', 0)) or 0)
            _last_sz = int(data.get('last_size', 0) or 0)
            _aggr = ''
            if _last > 0 and _bid > 0 and _ask > 0 and _bid < _ask:
                _mid = (_bid + _ask) / 2
                _spread = _ask - _bid
                if _last >= _ask - 0.01 or _last >= _mid + _spread * 0.35:
                    _aggr = 'BUY'
                elif _last <= _bid + 0.01 or _last <= _mid - _spread * 0.35:
                    _aggr = 'SELL'
                else:
                    _aggr = 'MID'
            _premium = round(_last * _last_sz * 100, 2) if (_last > 0 and _last_sz > 0) else 0.0
            _payload = {
                'ticker':       _sym_root or '',
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
                'trade_time_ms': int(trade_time or 0),
                'dollar_gex':   round(float(dollar_gex or 0) / 1e6, 4),
                'bid':          round(_bid, 4),
                'ask':          round(_ask, 4),
                'last':         round(_last, 4),
                'last_size':    _last_sz,
                'aggressor':    _aggr,
                'premium':      _premium,
            }
            for _k, _v in _payload.items():
                if isinstance(_v, float) and (math.isnan(_v) or math.isinf(_v)):
                    _payload[_k] = 0.0
            with _opt_mark_batch_lock:
                _opt_mark_batch[contract_key] = _payload
        except Exception:
            pass

    # 2026-05-06 PERF INSTRUMENTATION: aggregate per-call timings.
    # Schwab options quote handler is the prime suspect for the remaining
    # ~56% sustained CPU we couldn't explain after Tradier proved innocent
    # (1% of one core). Logs distribution every 30s as p50/p95/p99/max +
    # call rate + total CPU%.
    _perf_dt_ms = (time.perf_counter() - _perf_t0) * 1000.0
    global _options_quote_perf_samples, _options_quote_perf_last_log
    if _options_quote_perf_last_log == 0.0:
        _options_quote_perf_last_log = time.time()
    _options_quote_perf_samples.append(_perf_dt_ms)
    if (time.time() - _options_quote_perf_last_log) >= 30.0 and _options_quote_perf_samples:
        s = sorted(_options_quote_perf_samples)
        n = len(s)
        p50 = s[n // 2]
        p95 = s[int(n * 0.95)] if n > 1 else s[0]
        p99 = s[int(n * 0.99)] if n > 1 else s[0]
        mx = s[-1]
        avg = sum(s) / n
        wall_dt = max(time.time() - _options_quote_perf_last_log, 1.0)
        rate = n / wall_dt
        cpu_pct = 100.0 * sum(s) / 1000.0 / wall_dt
        log.info(
            f"[OPTIONS-QUOTE-PERF] n={n} ({rate:.0f}/s) | p50={p50:.3f}ms p95={p95:.3f}ms "
            f"p99={p99:.3f}ms max={mx:.2f}ms | avg={avg:.3f}ms | CPU on this handler: "
            f"{cpu_pct:.1f}% of one core"
        )
        _options_quote_perf_samples = []
        _options_quote_perf_last_log = time.time()


def _flush_option_mark_batch():
    """Emit queued option mark updates as one `option_mark_batch` frame.
    Driven by `_flush_loop` on a fixed 50ms cadence (wall clock) — independent
    of Schwab's ~1Hz inbound burst rhythm.
    """
    global _opt_mark_last_flush
    now = time.time()
    if now - _opt_mark_last_flush < _OPT_MARK_FLUSH_SEC:
        return
    with _opt_mark_batch_lock:
        if not _opt_mark_batch:
            _opt_mark_last_flush = now
            return
        updates = list(_opt_mark_batch.values())
        _opt_mark_batch.clear()
        _opt_mark_last_flush = now
    if _socketio and updates:
        try:
            _socketio.emit('option_mark_batch', {'updates': updates, '_emit_ms': int(now * 1000)})
        except Exception:
            pass


def _flush_option_trade_batch():
    """Emit queued TIMESALE trade prints as one `option_trade_batch` frame.
    Append-only buffer (every print is its own tape row — no latest-wins dedup).
    Driven by `_flush_loop` on a fixed 50ms cadence.
    """
    global _opt_trade_last_flush
    now = time.time()
    if now - _opt_trade_last_flush < _OPT_TRADE_FLUSH_SEC:
        return
    with _opt_trade_batch_lock:
        if not _opt_trade_batch:
            _opt_trade_last_flush = now
            return
        trades = list(_opt_trade_batch)
        _opt_trade_batch.clear()
        _opt_trade_last_flush = now
    if _socketio and trades:
        try:
            _socketio.emit('option_trade_batch', {'trades': trades, '_emit_ms': int(now * 1000)})
        except Exception:
            pass


def _lookup_bbo_at(sym_key: str, trade_time_ms: int):
    """Return the (bid, ask) that was freshest at-or-before `trade_time_ms`.

    Used to classify TIMESALE aggressor against the quote the trade actually
    fired against, not whatever the cache holds at emit time (which may have
    moved several ms later). Falls back to the most-recent snapshot if the
    trade is older than any stored quote, or (0, 0) if the symbol has no
    history yet — caller then falls back to its own cached bid/ask.
    """
    try:
        with _bbo_history_lock:
            hist = _bbo_history.get(sym_key)
            if not hist:
                return (0.0, 0.0)
            # Right-to-left scan: most recent first
            for ts, b, a in reversed(hist):
                if ts <= trade_time_ms:
                    return (b, a)
            # Trade older than entire buffer — use oldest known quote
            first = hist[0]
            return (first[1], first[2])
    except Exception:
        return (0.0, 0.0)


_tradier_timesale_stats = {'trades': 0, 'enriched': 0, 'bare': 0, 'qqq': 0}
# 2026-05-06 PERF: per-call timing samples for _on_tradier_timesale.
# Logged every 30s as p50/p95/p99/max + total CPU%. Identifies if the
# per-print handler is the CPU bottleneck.
_tradier_perf_samples: list = []
_tradier_perf_last_log: float = 0.0
# Same pattern for _on_options_quote (Schwab options L1 stream — prime
# suspect for the residual CPU pin since Tradier handler proved innocent).
_options_quote_perf_samples: list = []
_options_quote_perf_last_log: float = 0.0
# Equity-timesale counters (separate from option counters above). Populated
# by _on_tradier_equity_timesale — fills the Schwab TIMESALE_EQUITY code=11
# gap with TRUE per-print prints from Tradier WS instead of L1 size-delta
# synthesis. Per-symbol counts let us verify each subscription is firing.
_tradier_equity_timesale_stats = {
    'trades': 0,
    # Primary ETFs
    'QQQ': 0, 'SPY': 0,
    # VIX-tracking ETNs/ETFs
    'VXX': 0, 'UVXY': 0, 'SVXY': 0,
    # Mag-8 single-name equity tape
    'NVDA': 0, 'AAPL': 0, 'MSFT': 0, 'AMZN': 0,
    'META': 0, 'GOOGL': 0, 'TSLA': 0, 'AVGO': 0,
    # Leveraged QQQ ETFs
    'TQQQ': 0, 'SQQQ': 0,
}


def _on_tradier_timesale(evt: dict):
    """Route Tradier option time-and-sales prints into the tape pipeline.

    Tradier fills the TIMESALE_OPTIONS gap Schwab rejects (code=11). For
    symbols we also track via Schwab LEVELONE (QQQ/SPX/SPY), we merge with
    the cached Greek snapshot so the tape row shows Δ/IV/GEX. For symbols
    on Tradier-only (NVDA/AAPL/TSLA/etc.), the row has price/size/bid/ask
    but empty Greeks — same coverage 0DTHero shows.

    Tradier's OCC symbol is the un-padded form ('AAPL260425C00175000').
    Schwab's cache key is the 21-char OSI with underlying-root space-padding
    ('AAPL  260425C00175000'), so we convert before the cache lookup.
    """
    # 2026-05-06 PERF INSTRUMENTATION: measure per-call duration. With 52
    # prints/sec and ~80% sustained 1-core CPU, the budget is ~15ms/print
    # max before back-pressure builds. Aggregates dumped every 30s.
    _perf_t0 = time.perf_counter()
    try:
        occ = evt.get('symbol') or ''
        if not occ:
            return
        # ROUTE GUARD: equity tickers (QQQ/SPY/etc.) reach the same WS but
        # are handled by _on_tradier_equity_timesale. OCC option symbols
        # always contain YYMMDD digits; equity tickers are pure letters
        # (or $-prefixed indices). Skip non-option symbols here.
        if not any(c.isdigit() for c in occ):
            return
        size = int(evt.get('size') or 0)
        if size <= 0:
            return
        price = float(evt.get('price') or 0.0)
        if price <= 0:
            return
        if evt.get('cancel') or evt.get('correction'):
            return

        _tradier_timesale_stats['trades'] += 1
        if occ.startswith('QQQ'):
            _tradier_timesale_stats['qqq'] += 1

        # 2026-05-08 FIX: clamp stale Tradier timestamps to "now" if more than
        # 60s in the past. Tradier sometimes replays prints from previous days
        # (subscription handoff / reconnect cache), and the stale ts routes
        # mm_events into wrong-date files (mm_events_20260507.jsonl on 05/08).
        # Live prints don't legitimately arrive >60s late.
        _now_ms_t = int(time.time() * 1000)
        _ev_ts = int(evt.get('timestamp_ms') or 0)
        if not _ev_ts or _ev_ts < _now_ms_t - 60_000:
            _ev_ts = _now_ms_t
        trade_time_ms = _ev_ts
        t_bid = float(evt.get('bid') or 0.0)
        t_ask = float(evt.get('ask') or 0.0)

        # Convert Tradier OCC → Schwab OSI for cache lookup.
        from connectors.tradier_streamer import tradier_occ_to_schwab_osi
        osi = tradier_occ_to_schwab_osi(occ)

        _sym_cache = getattr(_on_options_quote, '_sym_cache', {})

        # Phase 21 (2026-05-01): EXPLICIT Greek source routing.
        # Each OSI is in EXACTLY ONE source set (or none → BSM).
        # See _SCHWAB_WS_OSIS / _SCHWAB_REST_OSIS declarations near top.
        _route_source = 'bsm'   # default; overridden if cache hits
        cached = {}
        if osi in _SCHWAB_WS_OSIS:
            cached = _sym_cache.get(osi) or {}
            if cached:
                _route_source = 'schwab_ws'
                _GREEK_ROUTING_STATS['schwab_ws'] += 1
            else:
                # Subscribed but cache empty — only happens during boot warm-up
                # or Schwab WS reconnect. Fall back to BSM but log so we know.
                _GREEK_ROUTING_STATS['schwab_ws_cache_miss'] += 1
                # Try REST cache as secondary (for Mag-8 ATM that's in both)
                if osi in _SCHWAB_REST_OSIS:
                    with _single_name_greeks_lock:
                        cached = _single_name_greeks_cache.get(osi) or {}
                    if cached:
                        _route_source = 'schwab_rest'
        elif osi in _SCHWAB_REST_OSIS:
            with _single_name_greeks_lock:
                cached = _single_name_greeks_cache.get(osi) or {}
            if cached:
                _route_source = 'schwab_rest'
                _GREEK_ROUTING_STATS['schwab_rest'] += 1
            else:
                _GREEK_ROUTING_STATS['schwab_rest_cache_miss'] += 1
        else:
            # Not in either set → BSM (counted later, only when BSM actually fires)
            pass

        # Aggressor classification. Tradier provides bid/ask atomically with
        # the trade — so we don't need the BBO ring buffer here. Fall back to
        # cached quote if Tradier's embedded BBO is absent.
        _b, _a = t_bid, t_ask
        if _b <= 0 or _a <= 0 or _b >= _a:
            _b = float(cached.get('bid') or 0)
            _a = float(cached.get('ask') or 0)
        _aggr = ''
        if price > 0 and _b > 0 and _a > 0 and _b < _a:
            _mid = (_b + _a) / 2
            _spread = _a - _b
            if price >= _a - 0.01 or price >= _mid + _spread * 0.35:
                _aggr = 'BUY'
            elif price <= _b + 0.01 or price <= _mid - _spread * 0.35:
                _aggr = 'SELL'
            else:
                _aggr = 'MID'

        # Parse root/strike/side/dte from the OCC symbol directly so Tradier-
        # only symbols (no Schwab cache) still get a fully-populated row.
        # OCC format: {root}{YYMMDD}{C|P}{strike*1000 as 8-digit int}
        _root = ''
        _digits_start = 0
        for i, ch in enumerate(occ):
            if ch.isdigit():
                _digits_start = i
                break
            _root += ch
        _side = 'C'
        _strike = 0.0
        _exp_yy, _exp_mm, _exp_dd = 0, 0, 0
        try:
            _tail = occ[_digits_start:]
            _exp_yy = int(_tail[0:2])
            _exp_mm = int(_tail[2:4])
            _exp_dd = int(_tail[4:6])
            _side = _tail[6]  # 'C' or 'P'
            _strike = int(_tail[7:15]) / 1000.0
        except Exception:
            pass

        # DTE: ALWAYS compute from OCC + today's date.
        # 2026-05-05 BUG FIX: previously preferred cached value, but the cache
        # is populated at subscription time and never invalidated at midnight.
        # Result: when server runs across a day boundary, today's 0DTE
        # contracts show cached `dte=1` (stale), so flow_accumulator's
        # `is_0dte = int(dte) == 0` evaluates False and 0DTE flow is misrouted
        # into the 'weekly' bucket. Verified live via /api/_debug/flow_diag
        # showing IWM 260505 with dte_field=1 instead of 0 on 2026-05-05.
        # Recomputing per-print is cheap (single date subtraction).
        _dte = 0
        if _exp_yy > 0:
            try:
                from datetime import date
                _exp_date = date(2000 + _exp_yy, _exp_mm, _exp_dd)
                _dte = max(0, (_exp_date - date.today()).days)
            except Exception:
                _dte = int(cached.get('dte') or 0)  # fallback only on parse error
        else:
            _dte = int(cached.get('dte') or 0)

        if cached:
            _tradier_timesale_stats['enriched'] += 1
        else:
            _tradier_timesale_stats['bare'] += 1

        # Venue + session come directly off the Tradier event. `tradex` events
        # carry session='pre'|'post'; plain `timesale` defaults to 'regular'.
        _exchange = str(evt.get('exchange') or '').strip()
        _session  = str(evt.get('session')  or 'regular').strip().lower()

        _tape_payload = {
            'ticker':        _root,
            'symbol':        osi or occ,
            'strike':        float(cached.get('strike') or _strike),
            'side':          'C' if (cached.get('contract_type') in ('C', 'CALL', 'call') or _side == 'C') else 'P',
            'dte':           _dte,
            'last':          round(price, 4),
            'last_size':     size,
            'trade_time_ms': trade_time_ms,
            'sequence':      int(evt.get('seq') or 0),
            'delta':         round(float(cached.get('delta') or 0), 5),
            'gamma':         round(float(cached.get('gamma') or 0), 6),
            'iv':            round(float(cached.get('iv') or 0), 4),
            'theta':         round(float(cached.get('theta') or 0), 5),
            'bid':           round(_b, 4),
            'ask':           round(_a, 4),
            'aggressor':     _aggr,
            'underlying':    round(float(cached.get('underlying_price') or 0), 4),
            'mark':          round(float(cached.get('mark') or price), 4),
            'mark_change':   round(float(cached.get('mark_change') or 0), 4),
            'oi':            int(cached.get('oi') or 0),
            'vol':           int(cached.get('vol') or 0),
            'premium':       round(price * size * 100, 2),
            'dollar_gex':    round(float(cached.get('dollar_gex') or 0) / 1e6, 4),
            'exchange':      _exchange,            # venue code (e.g. 'P'=PHLX, 'C'=CBOE, 'I'=ISE)
            'session':       _session,             # 'regular' | 'pre' | 'post'
            'source':        'TRADIER',  # tape-side flag — distinguishes from SCHWAB-LEVELONE prints
        }
        with _opt_trade_batch_lock:
            _opt_trade_batch.append(_tape_payload)

        # ⚠ Feed FLOW ACCUMULATOR — Schwab's TIMESALE_OPTIONS service is NOT
        # entitled for QQQ/SPY (line 684), so we have zero option trade prints
        # from Schwab. Tradier IS the primary option-trade source, but
        # historically only the tape ribbon and mm_attribution were fed from
        # it. The flow_accumulator (powering AI Panel + AlertEngine) was only
        # fed via _on_options_quote's last_size>0 path which never fires for
        # QQQ. Result: AI Panel matrix empty, AlertEngine 0 cells active for
        # entire sessions. Build the same `merged` dict the Schwab path uses
        # and call on_option_update so 0DTE/all-exp accumulators tick.
        try:
            from connectors.flow_accumulator import get_accumulator
            _acc = get_accumulator()
            if _acc is not None:
                # Diagnostic counters — tell us why feeds drop or use BSM
                _diag = getattr(_on_tradier_timesale, '_flow_diag',
                                {'no_cache':0, 'no_delta':0, 'no_dte':0, 'no_spot':0,
                                 'fed':0, 'qqq_fed':0,
                                 'bsm_filled':0, 'bsm_qqq_filled':0,
                                 'bsm_failed':0})
                _on_tradier_timesale._flow_diag = _diag

                # ── Phase 17 (2026-04-30) — BSM fallback for missing Greeks ──
                # When Tradier print fires for a contract NOT in Schwab L1
                # streaming cache (i.e., wing/LEAPS contracts on Conn C/D/E
                # outside QQQ ATM ±$100), we previously dropped the print or
                # used stale 60s REST Greeks. Now we compute Δ via BSM in ~5µs
                # using the print's market price as input to inverse-BSM.
                #
                # Validated: ±0.013 Δ vs Schwab at 0DTE ATM, ±0.026 weekly,
                # ±0.007 quarterly. Boundary handler returns ±1/0 for deep
                # ITM/OTM where IV is undefined (price ≈ intrinsic).
                # Speed: 5µs/print, 198K ops/sec on single thread.
                # See scripts/bsm_validate_vs_schwab.py for proof.

                # Start with cache (may be empty for wings/LEAPS)
                _flow_merged = dict(cached) if cached else {}
                _flow_merged['symbol']     = osi or occ
                _flow_merged['last']       = price
                _flow_merged['last_size']  = size
                _flow_merged['bid']        = _b if _b > 0 else (cached or {}).get('bid')
                _flow_merged['ask']        = _a if _a > 0 else (cached or {}).get('ask')
                _flow_merged['trade_time'] = trade_time_ms
                if _exchange:
                    _flow_merged['exchange'] = _exchange

                # Backfill structural fields from OCC parse if missing.
                # 0DTE FIX (2026-05-01): use `is None` check instead of `not get()`
                # because dte=0 is a valid 0DTE value (was being treated as missing
                # → silently dropping every 0DTE print on Tradier-only contracts
                # i.e. SPX wings outside Schwab LEVELONE 800-key window). Combined
                # with `_dte > 0` filter (now `>= 0`) this drops the no_dte gate
                # for 0DTE index options. Empirical proof in flow_diag counter:
                # SPX 38,549 0DTE trades but signed_0dte=+0.02B → 99%+ rejected
                # at the no_dte pre-flight check.
                if _flow_merged.get('dte') is None and _exp_yy > 0:
                    _flow_merged['dte'] = _dte   # _dte already clamped to max(0, days) above
                if not _flow_merged.get('strike') and _strike > 0:
                    _flow_merged['strike'] = _strike
                if not _flow_merged.get('contract_type'):
                    _flow_merged['contract_type'] = ('CALL' if _side == 'C' else 'PUT')
                if not _flow_merged.get('underlying'):
                    _flow_merged['underlying'] = _root

                # Backfill underlying_price from tape if missing
                if not _flow_merged.get('underlying_price'):
                    _ts_spot = _tape_spot_for(_root)
                    if _ts_spot > 0:
                        _flow_merged['underlying_price'] = _ts_spot

                # ── BSM Δ computation (Phase 21: explicit BSM path) ──
                # When _route_source == 'bsm' (osi not in either Schwab routing
                # set), this is the EXPECTED Greek source — not a fallback.
                # When _route_source != 'bsm' but delta is still None, we hit
                # a cache-empty edge case (warm-up / reconnect); BSM stops the
                # bleed and the cache_miss telemetry above flagged it.
                if _flow_merged.get('delta') is None and _dte is not None and _strike > 0:
                    _spot = _flow_merged.get('underlying_price') or 0
                    _mkt = price if price > 0 else (
                        ((_b or 0) + (_a or 0)) / 2.0 if (_b or 0) > 0 and (_a or 0) > 0 else 0
                    )
                    if _spot > 0 and _mkt > 0:
                        try:
                            from connectors.bsm_solver import compute_greeks_from_market
                            # Risk-free rate from $TNX (yield × 10 → decimal /1000)
                            _tnx = float(_latest_spot_by_ticker.get('TNX', 43.96) or 43.96)
                            _r = _tnx / 1000.0
                            # Dividend yield: rough constants per underlying
                            _q = {'QQQ': 0.005, 'SPY': 0.013, 'IWM': 0.015,
                                  'SPX': 0.0, 'VIX': 0.0, 'XLK': 0.005,
                                  'XLE': 0.03, 'XLF': 0.018}.get(_root, 0.005)
                            # Time-to-expiration in years
                            if _dte == 0:
                                from datetime import datetime as _dtnow
                                try:
                                    from zoneinfo import ZoneInfo
                                    _et = _dtnow.now(ZoneInfo("America/New_York"))
                                except Exception:
                                    _et = _dtnow.now()
                                _hrs_to_close = max(0.25, (16*60 - (_et.hour*60 + _et.minute)) / 60.0)
                                _T = _hrs_to_close / (365.0 * 24.0)
                            else:
                                _T = _dte / 365.0

                            _bsm_result = compute_greeks_from_market(
                                _spot, _strike, _T, _r, _q, _mkt, _side
                            )
                            if _bsm_result is not None:
                                # Inject computed Greeks into _flow_merged
                                _flow_merged['delta'] = _bsm_result['delta']
                                _flow_merged['gamma'] = _bsm_result['gamma']
                                _flow_merged['theta'] = _bsm_result['theta']
                                _flow_merged['vega']  = _bsm_result['vega'] / 100.0  # per 1% conv
                                if _bsm_result.get('iv', 0) > 0:
                                    _flow_merged['implied_vol'] = _bsm_result['iv'] * 100.0
                                _diag['bsm_filled'] += 1
                                if _root == 'QQQ':
                                    _diag['bsm_qqq_filled'] += 1
                                # Phase 21: count BSM as the routing source
                                # only when it was the EXPECTED source (not a
                                # cache-empty fallback for a subscribed sym).
                                if _route_source == 'bsm':
                                    _GREEK_ROUTING_STATS['bsm'] += 1
                            else:
                                _diag['bsm_failed'] += 1
                        except Exception as _bsm_e:
                            _diag['bsm_failed'] += 1
                            log.debug(f"[BSM-FALLBACK] {osi} err: {_bsm_e}")

                # Pre-flight check before calling — record drop reasons
                if not cached and _flow_merged.get('delta') is None:
                    _diag['no_cache'] += 1
                if _flow_merged.get('delta') is None:
                    _diag['no_delta'] += 1
                elif _flow_merged.get('dte') is None:
                    _diag['no_dte'] += 1
                elif not _flow_merged.get('underlying_price'):
                    _diag['no_spot'] += 1
                else:
                    _diag['fed'] += 1
                    if _root == 'QQQ':
                        _diag['qqq_fed'] += 1
                _acc.on_option_update(_flow_merged)
        except Exception as _flow_e:
            log.debug(f"[FLOW-ACC tradier feed] err: {_flow_e}")

        # Feed dealer_print_capture — joins this print with the OPTIONS_BOOK
        # snapshot that was live at print time, then samples forward spot
        # + book at t+5s/30s + t+5m/15m/30m. Purely descriptive capture;
        # no classification, no thresholds. See connectors/dealer_print_capture.py.
        try:
            from connectors import dealer_print_capture as _dpc
            _dpc.on_print(
                {
                    'ts':       trade_time_ms / 1000.0,
                    'symbol':   osi or occ,
                    'root':     _root,
                    'strike':   float(cached.get('strike') or _strike),
                    'side_cp':  'C' if (cached.get('contract_type') in ('C', 'CALL', 'call') or _side == 'C') else 'P',
                    'dte':      _dte,
                    'price':    price,
                    'size':     size,
                    'exchange': _exchange,
                    'session':  _session,
                    'aggressor': _aggr,                                # 'BUY'|'SELL'|'MID'|''
                    'delta':    float(cached.get('delta') or 0),
                    'spot':     float(cached.get('underlying_price') or _tape_spot_for(_root)),
                },
                spot_lookup=_tape_spot_for,
            )
        except Exception:
            pass

        # MM Attribution — PRINT_MATCH + start/close impulse capture.
        try:
            from connectors import mm_attribution as _mma
            _mma.on_print({
                'ts':        trade_time_ms / 1000.0,
                'symbol':    osi or occ,
                'exchange':  _exchange,
                'price':     price,
                'size':      size,
                'aggressor': _aggr,
            })
        except Exception:
            pass

        # Sweep Detector — multi-strike sweep alerts (Phase 1 of intelligent panels).
        # Reads pre-parsed _root/_strike/_side/_aggr/_exchange/trade_time_ms and
        # forwards each pure-aggressor print to sweep_detector.on_print. The
        # detector window-buffers prints per-underlying and emits an Socket.IO
        # 'intel:sweep_alert' event when 3+ adjacent strikes walk within 500ms.
        # All thresholds CONFIGURED/MEASURED — see connectors/sweep_detector.py.
        _swd_strike = 0.0
        _swd_side = 'P'
        _swd_dte_key = ''
        try:
            _swd_strike = float(cached.get('strike') or _strike or 0)
            _swd_side = 'C' if (cached.get('contract_type') in ('C', 'CALL', 'call')
                                 or _side == 'C') else 'P'
            if _exp_yy > 0 and _exp_mm > 0 and _exp_dd > 0:
                _swd_dte_key = f"{_exp_yy:02d}{_exp_mm:02d}{_exp_dd:02d}"
            from connectors import sweep_detector as _swd
            if _swd_strike > 0 and _root and _aggr in ('BUY', 'SELL'):
                _swd.on_print(
                    underlying=_root,
                    occ_symbol=occ,
                    strike=_swd_strike,
                    option_side=_swd_side,
                    size=size,
                    price=price,
                    ts_ms=trade_time_ms,
                    exchange=_exchange,
                    aggressor=_aggr,
                    dte_key=_swd_dte_key or None,
                )
        except Exception:
            pass

        # 0DTE Wing Tracker — far-OTM call/put aggressor flow (Phase 6).
        # Reuses the same _swd_* parsed values. Filters internally to
        # ANALYSIS_TICKER='QQQ' AND today's DTE. Each print is bucketed by
        # zone (ATM/NEAR_WING/DEEP_WING/TAIL) and aggressor side.
        try:
            from connectors import wing_tracker as _wt
            if _swd_strike > 0 and _root and _aggr in ('BUY', 'SELL'):
                _wt.on_print(
                    underlying=_root,
                    occ_symbol=occ,
                    strike=_swd_strike,
                    option_side=_swd_side,
                    size=size,
                    price=price,
                    ts_ms=trade_time_ms,
                    exchange=_exchange,
                    aggressor=_aggr,
                    dte_key=_swd_dte_key or None,
                )
        except Exception:
            pass
    except Exception as e:
        log.debug(f"[TRADIER] timesale handler error: {e}")
    finally:
        # 2026-05-06 PERF INSTRUMENTATION: aggregate per-call timings.
        # Logs distribution every 30s. Identifies if the per-print handler
        # is the CPU bottleneck — at 52 prints/sec, a 15ms median already
        # consumes 78% of one core. Anything above that = back-pressure risk.
        _perf_dt_ms = (time.perf_counter() - _perf_t0) * 1000.0
        global _tradier_perf_samples, _tradier_perf_last_log
        if _tradier_perf_last_log == 0.0:
            _tradier_perf_last_log = time.time()
        _tradier_perf_samples.append(_perf_dt_ms)
        if (time.time() - _tradier_perf_last_log) >= 30.0 and _tradier_perf_samples:
            s = sorted(_tradier_perf_samples)
            n = len(s)
            p50 = s[n // 2]
            p95 = s[int(n * 0.95)] if n > 1 else s[0]
            p99 = s[int(n * 0.99)] if n > 1 else s[0]
            mx = s[-1]
            avg = sum(s) / n
            tot_s = sum(s) / 1000.0
            cpu_pct = 100.0 * tot_s / max(time.time() - _tradier_perf_last_log, 1.0)
            log.info(
                f"[TRADIER-PERF] n={n} prints/30s | p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms max={mx:.2f}ms | "
                f"avg={avg:.2f}ms | total CPU on this handler: {cpu_pct:.1f}% of one core"
            )
            _tradier_perf_samples = []
            _tradier_perf_last_log = time.time()


# ── Tradier EQUITY timesale handler (fills Schwab TIMESALE_EQUITY code=11) ──
# Schwab refuses to deliver per-print equity tape (returns code=11). The
# old fallback was synthesizing trades from LEVELONE_EQUITIES last_size
# deltas — lossy because it conflates trades with cancels and only fires
# when L1 changes. Tradier's WS streams TRUE OPRA/CTA per-print prints with
# bid/ask context inline (no NBBO ring-buffer lookup needed for tick rule).
#
# This handler writes to the SAME `_recent_equity_prints` ring buffer that
# the L1-inferred path writes to, so mm_attribution's IMPULSE_CLOSED equity-
# join (lookup_equity_window) automatically picks up the truer data without
# needing a code change downstream. Also emits `equity_tape` for UI consumers.
_TRADIER_EQUITY_TICKERS = (
    # Primary ETFs (original)
    'QQQ', 'SPY',
    # VIX-tracking ETNs/ETFs (added 2026-04-29) — give us tradeable vol-product
    # tape since Schwab's $VIX index doesn't have per-print equity tape:
    'VXX',                  # iPath VIX short-term futures ETN
    'UVXY',                 # 1.5x leveraged VIX short-term ETN
    'SVXY',                 # -0.5x inverse VIX short-term ETN
    # Mag-8 single-name equity tape — fills out the equity side of the
    # option-chain coverage we already have on Tradier:
    'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO',
    # Leveraged QQQ ETFs — gamma-amplified moves visible in tape:
    'TQQQ', 'SQQQ',
)
# Going from 2 → 13 equity tickers. Tradier's documented soft cap is
# ~"several hundred to ~1,800". Currently at 422 (424 with these adds) —
# still well within their comfort zone.

def _on_tradier_equity_timesale(evt: dict):
    """Per-print equity tape from Tradier. Routes around Schwab's code=11
    rejection of TIMESALE_EQUITY. Same _recent_equity_prints buffer as the
    L1-inferred path — downstream consumers auto-upgrade to true data.
    """
    try:
        symbol = (evt.get('symbol') or '').strip()
        if not symbol:
            return
        # ROUTE GUARD: skip OCC option symbols (handled by _on_tradier_timesale).
        # Option symbols ALWAYS contain digits (YYMMDD + strike); equity tickers
        # don't. This is the inverse check from the options handler.
        if any(c.isdigit() for c in symbol):
            return
        # Only process tickers we actually use for analytics — avoids garbage
        # from any test/debug subscriptions that might leak through.
        if symbol not in _TRADIER_EQUITY_TICKERS:
            return

        size = int(evt.get('size') or 0)
        price = float(evt.get('price') or 0.0)
        if size <= 0 or price <= 0:
            return
        if evt.get('cancel') or evt.get('correction'):
            return

        ts_ms = int(evt.get('timestamp_ms') or int(time.time() * 1000))
        bid   = float(evt.get('bid') or 0.0)
        ask   = float(evt.get('ask') or 0.0)
        mic   = (evt.get('exchange') or '').strip().upper()

        # Tick-rule for aggressor side. Tradier embeds bid/ask atomically with
        # the print, so we don't need the NBBO ring buffer lookup the Schwab
        # path would have used (had Schwab not been code=11). Falls through to
        # neutral-mid if bid/ask both 0 (rare, only on session-edge prints).
        if ask > 0 and price >= ask:
            side = 1
        elif bid > 0 and price <= bid:
            side = -1
        else:
            side = 0  # mid / ambiguous

        # Push into the SAME ring buffer the L1-inferred path uses. mm_attr
        # joins via lookup_equity_window() automatically pick up Tradier's
        # truer data without any change in the consumer code.
        with _equity_prints_lock:
            buf = _recent_equity_prints.get(symbol)
            if buf is None:
                buf = _deque()
                _recent_equity_prints[symbol] = buf
            buf.append((ts_ms, price, size, mic, side))
            cutoff = ts_ms - int(EQUITY_PRINT_RETENTION_S * 1000)
            while buf and buf[0][0] < cutoff:
                buf.popleft()

        _tradier_equity_timesale_stats['trades'] += 1
        _tradier_equity_timesale_stats[symbol] = _tradier_equity_timesale_stats.get(symbol, 0) + 1

        # Emit equity_tape for UI — normalize to canonical shape used by
        # the LEVELONE_EQUITIES path (schwab_bridge.py:2079) so equity_tape_pane
        # consumes both sources identically. `source` field tags the origin so
        # we can distinguish Tradier-true vs L1-inferred.
        # Canonical fields:  side='b'/'s'/'',  ts=seconds,  exec_mic/bid_mic/ask_mic
        # (Tradier provides only one MIC per print, so we replicate it across all
        #  three slots — the pane only renders exec_mic in the row, the other two
        #  are kept for parity with downstream consumers.)
        if _socketio:
            try:
                side_str = 'b' if side == 1 else ('s' if side == -1 else '')
                _socketio.emit('equity_tape', {
                    'symbol':    symbol,
                    'price':     price,
                    'size':      size,
                    'side':      side_str,
                    'exec_mic':  mic,
                    'bid_mic':   mic,
                    'ask_mic':   mic,
                    'ts':        ts_ms / 1000.0,
                    'bid':       bid,
                    'ask':       ask,
                    'source':    'tradier_ts',
                })
            except Exception:
                pass
    except Exception as _e:
        log.debug(f"[TRADIER-EQ-TS] handler error: {_e}")


def _fetch_single_name_chain(ticker: str) -> tuple:
    """Pull one ticker's 60-day option chain in ONE REST call + populate the
    Greeks cache for every returned contract. Returns (ticker, spot, chain).

    Runs inside a greenlet-backed thread so 8 of these execute in parallel
    under gevent monkeypatch — total wall time ≈ slowest single call (~2-3s),
    not sum of all calls.
    """
    try:
        from server import _schwab_chain_range, _schwab_quote
        from datetime import date, timedelta
    except Exception as e:
        log.warning(f"[SCHWAB-REST] server helpers unavailable: {e}")
        return (ticker, 0.0, [])

    try:
        spot = _schwab_quote(ticker)
        if not spot or spot <= 0:
            return (ticker, 0.0, [])
        today = date.today().isoformat()
        # Full OCC ladder: 3y window saturates at each name's natural ceiling
        # (24-26 expirations depending on ticker, out to the furthest LEAPS).
        # No further contracts exist past this — Schwab/OCC cap it.
        end_dt = (date.today() + timedelta(days=1095)).isoformat()
        chain, underlying_px = _schwab_chain_range(ticker, today, end_dt, strike_count=50)
        if not chain:
            return (ticker, spot, [])

        now_ms = int(time.time() * 1000)
        spot_px = float(underlying_px or spot)

        # Cache Greeks for EVERY returned contract (~1000-1500 per ticker).
        # Memory footprint ≈ 8 × 1200 × 200 bytes = ~2MB total. Fine.
        with _single_name_greeks_lock:
            for opt in chain:
                sym = opt.get("symbol", "") or ""
                if not sym:
                    continue
                _delta = opt.get("delta")
                _gamma = opt.get("gamma")
                _theta = opt.get("theta")
                _iv    = opt.get("volatility")
                try:
                    _iv = float(_iv) if _iv is not None else 0.0
                except Exception:
                    _iv = 0.0
                _single_name_greeks_cache[sym] = {
                    'strike':           float(opt.get("strike", 0) or 0),
                    'contract_type':    'C' if opt.get('option_type') == 'call' else 'P',
                    'delta':            float(_delta) if _delta is not None else 0.0,
                    'gamma':            float(_gamma) if _gamma is not None else 0.0,
                    'theta':            float(_theta) if _theta is not None else 0.0,
                    'iv':               _iv,
                    'oi':               int(opt.get('open_interest') or 0),
                    'vol':              int(opt.get('volume') or 0),
                    'dte':              int(opt.get('dte') or 0),
                    'mark':             float(opt.get('mark') or 0),
                    'mark_change':      float(opt.get('mark_change') or 0),
                    'bid':              float(opt.get('bid') or 0),
                    'ask':              float(opt.get('ask') or 0),
                    'underlying_price': spot_px,
                    'updated_ms':       now_ms,
                }
                # Phase 21: register every Mag-8 chain symbol in the REST
                # routing set so _on_tradier_timesale dispatches to this cache
                # for any Tradier print on a symbol that's not Schwab-streamed.
                _SCHWAB_REST_OSIS.add(sym)
        return (ticker, spot, chain)
    except Exception as e:
        log.debug(f"[SCHWAB-REST] {ticker} chain range fetch failed: {e}")
        return (ticker, 0.0, [])


def _collect_single_name_atm_symbols(max_per_ticker: int = 40) -> list:
    """Collect ATM OCC symbols for Tradier subscription + refresh the Greeks
    cache for all 8 single-names. Parallel across tickers via threads (gevent
    monkeypatch turns them into greenlets, so network calls run concurrently).

    Caches Greeks for the full 60-day chain (~1000 contracts/ticker), but
    only returns the nearest ATM ±5% subset (capped at `max_per_ticker` per
    ticker) for the Tradier WS subscription. This keeps the Tradier frame
    under the 64KB 1009 limit while still giving us Greek lookups for any
    strike that prints.
    """
    from connectors.tradier_streamer import schwab_osi_to_tradier

    # Fire all 8 ticker pulls in parallel. Under gevent monkeypatch, native
    # threads block cooperatively on sockets — actual wall time ≈ slowest
    # single call (2-3s), not sum (~16s).
    results: list = [None] * len(_tradier_single_names)
    threads = []
    for i, ticker in enumerate(_tradier_single_names):
        def _worker(_idx=i, _tk=ticker):
            results[_idx] = _fetch_single_name_chain(_tk)
        t = threading.Thread(target=_worker, daemon=True,
                             name=f"schwab-chain-{ticker}")
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=15)

    out = []
    for result in results:
        if result is None:
            continue
        ticker, spot, chain = result
        if spot <= 0 or not chain:
            continue
        # Group by expiration, then pick nearest-ATM contracts from the
        # nearest 2 expirations for Tradier subscription (tape lives on
        # front-week ATM; deeper strikes are in the cache but not streamed).
        radius = max(2.0, spot * 0.05)
        by_exp: dict = {}
        for opt in chain:
            exp_iso = opt.get('exp_date') or ''
            if not exp_iso:
                continue
            by_exp.setdefault(exp_iso, []).append(opt)
        near_exps = sorted(by_exp.keys())[:2]
        rows: list = []
        for exp_iso in near_exps:
            for opt in by_exp[exp_iso]:
                strike = float(opt.get("strike", 0) or 0)
                sym = opt.get("symbol", "") or ""
                if sym and strike > 0 and abs(strike - spot) <= radius:
                    rows.append((abs(strike - spot), sym))
        rows.sort(key=lambda x: x[0])
        picked = [sym for _, sym in rows[:max_per_ticker]]
        tradier_syms = [schwab_osi_to_tradier(s) for s in picked]
        out.extend(tradier_syms)
        log.info(f"[SCHWAB-REST] {ticker}: picked {len(tradier_syms)} ATM contracts "
                 f"(spot={spot:.2f}, radius=±{radius:.2f}, cached={len(chain)})")

    with _single_name_greeks_lock:
        cache_n = len(_single_name_greeks_cache)
    log.info(f"[SCHWAB-REST] Greeks cache size: {cache_n}")
    return out


def _persist_single_name_cache() -> int:
    """Snapshot _single_name_greeks_cache to disk via atomic rename.

    Returns # entries written. Crash safety: write to .tmp + fsync + os.replace
    so a reader always sees old or new version, never partial.
    """
    t0 = time.time()
    with _single_name_greeks_lock:
        snapshot = dict(_single_name_greeks_cache)
    if not snapshot:
        return 0
    payload = {
        'version':  _SINGLE_NAME_CACHE_VERSION,
        'saved_at': t0,
        'entries':  snapshot,
    }
    try:
        os.makedirs(os.path.dirname(_SINGLE_NAME_CACHE_FILE), exist_ok=True)
        import json as _json
        with open(_SINGLE_NAME_CACHE_TMP, 'w') as f:
            _json.dump(payload, f, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(_SINGLE_NAME_CACHE_TMP, _SINGLE_NAME_CACHE_FILE)
    except Exception as e:
        log.warning(f"[SN-CACHE-PERSIST] save failed: {e}")
        return 0
    return len(snapshot)


def _restore_single_name_cache() -> int:
    """Load _single_name_greeks_cache from disk if a fresh snapshot exists.

    Returns # entries restored. Skips if file is older than the max-age guard
    (Greeks beyond that window risk stale underlying_price corrupting walls).
    """
    if not os.path.exists(_SINGLE_NAME_CACHE_FILE):
        log.info("[SN-CACHE-RESTORE] no state file — fresh start")
        return 0
    age = time.time() - os.path.getmtime(_SINGLE_NAME_CACHE_FILE)
    if age > _SINGLE_NAME_CACHE_RESTORE_MAX_AGE:
        log.info(f"[SN-CACHE-RESTORE] state file age {age:.0f}s > {_SINGLE_NAME_CACHE_RESTORE_MAX_AGE:.0f}s, skipping")
        return 0
    try:
        import json as _json
        with open(_SINGLE_NAME_CACHE_FILE) as f:
            payload = _json.load(f)
    except Exception as e:
        log.warning(f"[SN-CACHE-RESTORE] failed to load: {e}")
        return 0
    if payload.get('version') != _SINGLE_NAME_CACHE_VERSION:
        log.warning(f"[SN-CACHE-RESTORE] schema version mismatch (file={payload.get('version')}, code={_SINGLE_NAME_CACHE_VERSION}), skipping")
        return 0
    entries = payload.get('entries') or {}
    if not isinstance(entries, dict) or not entries:
        return 0
    with _single_name_greeks_lock:
        _single_name_greeks_cache.update(entries)
    saved_age = time.time() - float(payload.get('saved_at') or 0)
    log.info(f"[SN-CACHE-RESTORE] restored {len(entries)} Greeks entries "
             f"(snapshot was {saved_age:.0f}s old, file age {age:.0f}s)")
    return len(entries)


def _single_name_cache_persist_loop() -> None:
    """Daemon thread — snapshot _single_name_greeks_cache every persist interval."""
    while True:
        try:
            time.sleep(_SINGLE_NAME_CACHE_PERSIST_SEC)
            _persist_single_name_cache()
        except Exception as e:
            log.warning(f"[SN-CACHE-PERSIST] loop error: {e}")
            time.sleep(_SINGLE_NAME_CACHE_PERSIST_SEC)


def _start_single_name_cache_persistence() -> bool:
    """Start the cache persistence daemon. Idempotent."""
    global _single_name_cache_persist_started
    if _single_name_cache_persist_started:
        return False
    _single_name_cache_persist_started = True
    t = threading.Thread(target=_single_name_cache_persist_loop, daemon=True,
                         name='sn-cache-persist')
    t.start()
    log.info(f"[SN-CACHE-PERSIST] daemon started ({_SINGLE_NAME_CACHE_PERSIST_SEC:.0f}s cadence)")
    return True


def _compute_single_name_walls() -> list:
    """Compute per-ticker gamma walls + flip from `_single_name_greeks_cache`.

    For each of the 8 single-names, iterate the cache, group by strike, and
    derive:
      - call_wall  : strike with max call OI
      - put_wall   : strike with max put OI
      - gamma_flip : strike where cumulative dealer-net gamma crosses zero
      - total_gex  : total dollar gamma exposure
      - above_flip : bool — is underlying currently above its flip?

    Dealer gamma convention (matches existing QQQ pipeline):
      clients long calls → dealer SHORT call gamma  (negative for dealer)
      clients long puts  → dealer LONG put gamma    (positive for dealer)
      dealer_net(K) = -call_gamma(K) + put_gamma(K)

    Flip = strike below which dealers are net short gamma (amplifying moves)
    and above which they are net long (dampening). The sign-flip point.

    Returns a list of dicts, one per ticker with data.
    """
    # Group cache entries by ticker.
    by_ticker: dict = {}
    with _single_name_greeks_lock:
        for osi, entry in _single_name_greeks_cache.items():
            # OSI format: "NVDA  260425C00200000" — first 6 chars = root (padded).
            root = (osi[:6] or '').strip()
            if not root or root not in _tradier_single_names:
                continue
            by_ticker.setdefault(root, []).append(entry)

    out: list = []
    for ticker, entries in by_ticker.items():
        if len(entries) < 10:
            continue
        # Aggregate per strike (sum across all expirations).
        per_strike: dict = {}
        spot = 0.0
        for e in entries:
            k = float(e.get('strike') or 0)
            if k <= 0:
                continue
            slot = per_strike.setdefault(k, {
                'call_oi': 0, 'put_oi': 0,
                'call_gamma': 0.0, 'put_gamma': 0.0,
            })
            oi = int(e.get('oi') or 0)
            gamma = float(e.get('gamma') or 0)
            if e.get('contract_type') == 'C':
                slot['call_oi'] += oi
                slot['call_gamma'] += gamma * oi
            else:
                slot['put_oi'] += oi
                slot['put_gamma'] += gamma * oi
            if spot == 0.0:
                spot = float(e.get('underlying_price') or 0)
        if not per_strike or spot <= 0:
            continue

        strikes_sorted = sorted(per_strike.keys())
        # Walls: max OI strike.
        call_wall = max(strikes_sorted, key=lambda k: per_strike[k]['call_oi'])
        put_wall  = max(strikes_sorted, key=lambda k: per_strike[k]['put_oi'])

        # Gamma flip: cumulative dealer-net gamma (from lowest strike upward).
        # Flip = first strike where cumulative crosses zero (sign change vs prior).
        cum = 0.0
        flip = 0.0
        prev_cum = 0.0
        for k in strikes_sorted:
            d_gamma = -per_strike[k]['call_gamma'] + per_strike[k]['put_gamma']
            cum += d_gamma
            if prev_cum != 0.0 and (cum * prev_cum) < 0:
                # Linear interp between prev and current strike.
                # k_flip = k_prev + (0 - prev_cum)/(cum - prev_cum) * (k - k_prev)
                # Walk back to find prev_k.
                idx = strikes_sorted.index(k)
                prev_k = strikes_sorted[idx - 1] if idx > 0 else k
                denom = cum - prev_cum
                if denom != 0.0:
                    flip = prev_k + (0 - prev_cum) / denom * (k - prev_k)
                else:
                    flip = k
                break
            prev_cum = cum

        # Total dollar gamma exposure across all strikes.
        # spot² × gamma × OI × 100 = per-strike dollar gamma per 1% underlying move.
        total_gex = 0.0
        for k, s in per_strike.items():
            # Use absolute net (not directional) for "how much gamma is in play".
            call_dg = s['call_gamma'] * 100 * spot * spot / 100
            put_dg  = s['put_gamma']  * 100 * spot * spot / 100
            total_gex += call_dg + put_dg

        out.append({
            'ticker':     ticker,
            'spot':       round(spot, 4),
            'call_wall':  round(float(call_wall), 2),
            'put_wall':   round(float(put_wall), 2),
            'gamma_flip': round(flip, 2) if flip > 0 else 0.0,
            'total_gex':  round(total_gex / 1e6, 3),  # $M per 1% move
            'above_flip': bool(spot > flip) if flip > 0 else None,
            'n_strikes':  len(per_strike),
            'updated_ms': int(time.time() * 1000),
        })
    # Sort by NQ weight (roughly by cap) for stable UI order.
    order = {t: i for i, t in enumerate(_tradier_single_names)}
    out.sort(key=lambda r: order.get(r['ticker'], 99))
    return out


def _compute_ndx_wgc(walls: list) -> dict:
    """NDX-Weighted Gamma Composite — dealer regime aggregated across constituents.

    Combines the per-name gamma-flip state of NDX components (weighted by their
    index weight) into one scalar regime reading. Captures dealer hedging flow
    that QQQ-wrapper GEX can't see: when NVDA single-stock option dealers hedge
    NVDA shares, QQQ GEX doesn't move, but NDX does (NVDA is 8.8% of NDX).

    Output fields:
      wgc_sign       ∈ [-1, +1] : weighted regime vote (sum of w_i × sign_i)
      wgc_sign_norm  ∈ [-1, +1] : normalized by covered weight
      wgc_net_mw     ∈ ℝ        : weighted signed $M gamma
      wgc_gamma_mw   ≥ 0         : weighted absolute $M gamma
      regime         ∈ {DAMP, AMPL, NEUTRAL} : trading interpretation
      ampl_count / damp_count   : names on each side of their flip
      contributions : per-name weight × sign breakdown (for UI)

    DAMP → NDX/NQ moves dampen, fade breakouts, range-bound.
    AMPL → NDX/NQ moves amplify, chase breakouts, trend days.
    """
    total_w = 0.0
    signed_w = 0.0
    net_w = 0.0
    gamma_w = 0.0
    ampl_count = 0
    damp_count = 0
    contribs = []

    for w in walls:
        tk = w.get('ticker') or ''
        weight = NDX_WEIGHTS.get(tk, 0.0)
        if weight <= 0:
            continue
        above = w.get('above_flip')
        if above is None:
            continue
        sign = 1 if above else -1
        gex_m = float(w.get('total_gex') or 0)

        total_w += weight
        signed_w += weight * sign
        net_w += weight * sign * abs(gex_m)
        gamma_w += weight * abs(gex_m)
        if above:
            damp_count += 1
        else:
            ampl_count += 1

        contribs.append({
            'ticker':       tk,
            'weight':       round(weight, 4),
            'sign':         sign,
            'gex_m':        round(gex_m, 2),
            'contribution': round(weight * sign, 4),
        })

    if total_w <= 0:
        return {}

    # Pure sign of the weighted vote — same structural rule as capture-vs-post
    # diff column. NEUTRAL only if signed_w is exactly zero (all flip votes
    # cancel), which is rare but real. No magnitude cutoff.
    if signed_w > 0:
        regime = 'DAMP'
    elif signed_w < 0:
        regime = 'AMPL'
    else:
        regime = 'NEUTRAL'

    return {
        'wgc_sign':       round(signed_w, 4),
        'wgc_sign_norm':  round(signed_w / total_w, 4) if total_w > 0 else 0,
        'wgc_net_mw':     round(net_w, 3),
        'wgc_gamma_mw':   round(gamma_w, 3),
        'covered_weight': round(total_w, 4),
        'ampl_count':     ampl_count,
        'damp_count':     damp_count,
        'regime':         regime,
        'contributions':  contribs,
        'weights_as_of':  NDX_WEIGHTS_REFRESHED,
        'updated_ms':     int(time.time() * 1000),
    }


def _detect_wgc_signals(wgc: dict, walls: list) -> list:
    """Detect two event types from WGC state evolution:
      1. ndx_regime_flip — WGC_sign crosses zero (DAMP↔AMPL boundary)
      2. ampl_cluster    — 3+ constituents flip same direction in 5-min window

    Emits as flow_alert payloads so AIPanel and alert log show them with
    standard bullish/bearish/warning coloring.
    """
    alerts = []
    now_ms = int(time.time() * 1000)
    now_s = now_ms / 1000

    # Track per-ticker above_flip transitions in a rolling window.
    for w in walls:
        tk = w.get('ticker') or ''
        if tk not in NDX_WEIGHTS:
            continue
        above = w.get('above_flip')
        if above is None:
            continue
        prev = _ndx_wgc_prev_above.get(tk)
        if prev is not None and prev != above:
            _ndx_wgc_state['flip_history'].append({
                'ts_ms':  now_ms,
                'ticker': tk,
                'old':    'DAMP' if prev else 'AMPL',
                'new':    'DAMP' if above else 'AMPL',
            })
        _ndx_wgc_prev_above[tk] = above

    # Prune flip history to last 5 minutes.
    cutoff_ms = now_ms - (5 * 60 * 1000)
    _ndx_wgc_state['flip_history'] = [
        f for f in _ndx_wgc_state['flip_history'] if f['ts_ms'] >= cutoff_ms
    ]

    # Event 1: Regime transition between DAMP and AMPL.
    # Fires only on actual regime-label changes, NOT on zero-crosses that land
    # in the NEUTRAL band (otherwise the label would claim "flip → NEUTRAL").
    if wgc:
        prev_sign   = _ndx_wgc_state['prev_sign']
        prev_regime = _ndx_wgc_state['prev_regime']
        curr_sign   = float(wgc.get('wgc_sign') or 0)
        curr_regime = wgc.get('regime')
        is_flip = (
            prev_regime in ('DAMP', 'AMPL') and
            curr_regime in ('DAMP', 'AMPL') and
            prev_regime != curr_regime
        )
        if is_flip and now_s - _ndx_wgc_state['last_cross_alert'] > 60:
            direction = 'bullish' if curr_sign > 0 else 'bearish'
            label = f"NDX regime flip → {curr_regime} ({curr_sign * 100:+.1f}%)"
            alerts.append({
                'type':        'ndx_regime_flip',
                'ticker':      'NDX',
                'direction':   direction,
                'magnitude_m': float(wgc.get('wgc_net_mw') or 0),
                'sigma':       float(curr_sign * 100),
                'ts':          int(now_s),
                'label':       label,
            })
            _ndx_wgc_state['last_cross_alert'] = now_s
        _ndx_wgc_state['prev_sign']   = curr_sign
        _ndx_wgc_state['prev_regime'] = curr_regime

    # Event 2: Cluster — 3+ flips to the SAME side in a 5-min window.
    # Requires one side dominant; a perfectly mixed window (3 AMPL + 3 DAMP)
    # is a noisy regime churn, not a directional signal, so we skip it.
    history = _ndx_wgc_state['flip_history']
    if len(history) >= 3 and now_s - _ndx_wgc_state['last_cluster_alert'] > 300:
        to_ampl = [f for f in history if f['new'] == 'AMPL']
        to_damp = [f for f in history if f['new'] == 'DAMP']
        side = None
        if len(to_ampl) >= 3 and len(to_ampl) > len(to_damp):
            side = ('AMPL', 'bearish', to_ampl)
        elif len(to_damp) >= 3 and len(to_damp) > len(to_ampl):
            side = ('DAMP', 'bullish', to_damp)
        if side is not None:
            new_regime, direction, flips = side
            count   = len(flips)
            tickers = [f['ticker'] for f in flips][:5]
            label = f"NDX cluster flip → {new_regime}: {count} names [{','.join(tickers)}]"
            alerts.append({
                'type':        'ampl_cluster',
                'ticker':      'NDX',
                'direction':   direction,
                'magnitude_m': float(count),
                'sigma':       float(count),
                'ts':          int(now_s),
                'label':       label,
            })
            _ndx_wgc_state['last_cluster_alert'] = now_s

    return alerts


def _fetch_qqq_full_chain() -> None:
    """Refresh full QQQ chain Greeks via Schwab REST. Phase 22 (2026-05-05).

    PROBLEM: Schwab WS only streams ~250 short-DTE QQQ contracts (Phase 21
    tier allocation). For wings (DTE>=8 OR ATM-distant strikes) Tradier WS
    sends print events but no Greeks. Phase 21 routing then falls to BSM —
    which works but introduces ±0.026 delta error vs Schwab's exact value,
    and renders dealer_prints disk log with delta=0 (BSM only fills the
    flow_accumulator path, not the tape payload).

    FIX: pull all 25 QQQ expirations × 200 strikes (ATM ±100) via one
    Schwab REST call every 30s. Cache Greeks in `_single_name_greeks_cache`
    and register OSIs in `_SCHWAB_REST_OSIS` so the existing Phase 21
    routing in `_on_tradier_timesale` picks them up automatically — both
    tape display delta AND flow accumulator delta become Schwab-exact.

    Memory: ~5K contracts × ~200 bytes ≈ 1MB. Bandwidth: ~2MB JSON / 30s.
    Rate budget: 2 calls/30s = 4/min added to existing ~25/min. Well under
    Schwab's 120/min cap.
    """
    try:
        from server import _schwab_chain_range, _schwab_quote
        from datetime import date, timedelta
    except Exception as e:
        log.warning(f"[SCHWAB-REST QQQ] server helpers unavailable: {e}")
        return

    try:
        spot = _schwab_quote('QQQ')
        if not spot or spot <= 0:
            return
        today_d = date.today()
        today = today_d.isoformat()
        # SPLIT into two windows because Schwab API gateway returns
        # "TooBigBody" (502) when the single response exceeds ~10MB:
        #   - 0-90 DTE × strike_count=300 = ~6,000 contracts (covers all
        #     actively-traded wings out to deep OTM ±25%)
        #   - 90-1095 DTE × strike_count=100 = ~3,000 contracts (LEAPS +
        #     mid-DTE; spacing widens at $5/$10 so 100 covers ample range)
        # Together: ~9,000 QQQ contracts, every Tradier-subscribed sym
        # that Schwab knows about gets exact Greeks.
        mid_dt = (today_d + timedelta(days=90)).isoformat()
        end_dt = (today_d + timedelta(days=1095)).isoformat()
        chain_near, underlying_px = _schwab_chain_range('QQQ', today, mid_dt, strike_count=300)
        chain_leaps, _ = _schwab_chain_range('QQQ', mid_dt, end_dt, strike_count=100)
        chain = (chain_near or []) + (chain_leaps or [])
        if not chain:
            return

        now_ms = int(time.time() * 1000)
        spot_px = float(underlying_px or spot)
        added = 0
        refreshed = 0

        # 2026-05-06 PERF FIX: build the new entries OUTSIDE the lock so we
        # only hold it for short bursts. Previously the lock was held for the
        # full 8,400-contract iteration (~22-50s including JSON parsing on
        # the gevent loop), which blocked every options-tick handler that
        # needs Greeks → CPU pinned at 95-98%, WebSocket buffers filled, RSTs.
        # New approach:
        #   1. Iterate chain and build local dict (no lock)
        #   2. Apply to shared cache in 500-key chunks (lock per chunk)
        #   3. time.sleep(0) between chunks to yield to other greenlets
        new_entries: dict = {}
        for opt in chain:
            sym = opt.get("symbol", "") or ""
            if not sym:
                continue
            # Don't clobber Schwab WS-streamed contracts — their Greeks
            # are <100ms-fresh from streaming. REST is 30s-stale fallback.
            if sym in _SCHWAB_WS_OSIS:
                continue
            _delta = opt.get("delta")
            _gamma = opt.get("gamma")
            _theta = opt.get("theta")
            _iv    = opt.get("volatility")
            try:
                _iv = float(_iv) if _iv is not None else 0.0
            except Exception:
                _iv = 0.0
            new_entries[sym] = {
                'strike':           float(opt.get("strike", 0) or 0),
                'contract_type':    'C' if opt.get('option_type') == 'call' else 'P',
                'delta':            float(_delta) if _delta is not None else 0.0,
                'gamma':            float(_gamma) if _gamma is not None else 0.0,
                'theta':            float(_theta) if _theta is not None else 0.0,
                'iv':               _iv,
                'oi':               int(opt.get('open_interest') or 0),
                'vol':              int(opt.get('volume') or 0),
                'dte':              int(opt.get('dte') or 0),
                'mark':             float(opt.get('mark') or 0),
                'mark_change':      float(opt.get('mark_change') or 0),
                'bid':              float(opt.get('bid') or 0),
                'ask':              float(opt.get('ask') or 0),
                'underlying_price': spot_px,
                'updated_ms':       now_ms,
            }

        # Apply to shared state in 500-key chunks, yielding between chunks.
        keys = list(new_entries.keys())
        CHUNK = 500
        for i in range(0, len(keys), CHUNK):
            chunk_keys = keys[i:i+CHUNK]
            with _single_name_greeks_lock:
                for k in chunk_keys:
                    _single_name_greeks_cache[k] = new_entries[k]
                    refreshed += 1
                    if k not in _SCHWAB_REST_OSIS:
                        _SCHWAB_REST_OSIS.add(k)
                        added += 1
            # Yield to gevent — lets WS readers, REST handlers, and tick
            # processors run between chunks. Without this, even with chunked
            # locking the loop is CPU-bound for the duration.
            time.sleep(0)
        log.info(f"[SCHWAB-REST QQQ] refreshed {refreshed} contracts "
                 f"(+{added} new in REST routing set, total REST OSIs: {len(_SCHWAB_REST_OSIS)})")
    except Exception as e:
        log.warning(f"[SCHWAB-REST QQQ] chain fetch failed: {e}")


def _qqq_full_chain_refresh_loop():
    """Background loop: refresh full QQQ chain Greeks every 30s.

    Eliminates BSM fallback for QQQ wing prints by populating Schwab REST
    Greeks cache for every contract not on Schwab WS. See _fetch_qqq_full_chain
    for full rationale.
    """
    log.info("[SCHWAB-REST QQQ] Full-chain Greeks refresher started (30s cadence)")
    # First fire immediately for warmup — without this, 30s of wing prints
    # land before the first cache populates and waste BSM compute.
    try:
        _fetch_qqq_full_chain()
    except Exception:
        pass
    while _bridge_running:
        try:
            time.sleep(30.0)
            if not _bridge_running:
                break
            _fetch_qqq_full_chain()
        except Exception as e:
            log.warning(f"[SCHWAB-REST QQQ] refresh error: {e}")
    log.info("[SCHWAB-REST QQQ] Full-chain Greeks refresher exited")


def _single_name_refresh_loop():
    """Background loop: re-pull Schwab /chains every 6s to keep Greeks fresh.

    Parallel across 8 tickers (one REST call per ticker, 60-day range) via
    threads-as-greenlets. Wall time per refresh ≈ 2-3s. Rate-limit budget:
    8 calls / 6s = 80 req/min, well under Schwab's 120 req/min cap.

    Runs in a dedicated daemon thread so REST timeouts don't stall the bridge.
    """
    log.info(f"[SCHWAB-REST] Single-name Greeks refresher started ({_SINGLE_NAME_REFRESH_SEC:.0f}s cadence — LEAPS/tail backfill since Phase 13 streams ATM)")
    while _bridge_running:
        try:
            time.sleep(_SINGLE_NAME_REFRESH_SEC)
            if not _bridge_running:
                break
            _collect_single_name_atm_symbols(max_per_ticker=40)
            # Compute + emit per-ticker walls right after cache refresh.
            walls = _compute_single_name_walls()
            if walls and _socketio is not None:
                try:
                    _socketio.emit('single_name_walls', {'tickers': walls})
                except Exception as e:
                    log.debug(f"[SCHWAB-REST] single_name_walls emit failed: {e}")
                # WGC composite + regime-flip / cluster alerts.
                try:
                    wgc = _compute_ndx_wgc(walls)
                    if wgc:
                        global _latest_wgc
                        _latest_wgc = wgc
                        _socketio.emit('ndx_wgc', wgc)
                        for alert in _detect_wgc_signals(wgc, walls):
                            _socketio.emit('flow_alert', alert)
                            log.info(f"[WGC] {alert.get('label')}")
                except Exception as e:
                    log.debug(f"[SCHWAB-REST] ndx_wgc emit failed: {e}")
        except Exception as e:
            log.warning(f"[SCHWAB-REST] Single-name refresh error: {e}")
    log.info("[SCHWAB-REST] Single-name Greeks refresher exited")


# ── OPEX wings on Conn-A spare capacity (added 2026-05-01) ──────────
# Conn-A originally only had 195 syms (180 QQQ short-DTE + 15 equity)
# of its empirical ~2,000 per-conn cap. The wing collector hits 1,950
# per-bucket on Conn-D/E iterating in DATE order, leaving high-OI OPEX
# expirations (5/15, 5/22, 5/29, 9/18, 12/18, 1/15/27) UNSUBSCRIBED.
# This module-level set tracks OPEX wing OCC syms so:
#   1. They subscribe to Conn-A (uses spare slots, no flap risk)
#   2. _collect_qqq_chain_wings excludes them (no double-subscribe)
_tradier_a_opex_syms: set = set()

# Target OPEX-class expirations not adequately covered by date-order Phase 15.
# Empirically chosen from per-expiration OI audit (2026-05-01):
#   5/15 (May OPEX, 14 DTE):   1.69M OI  ← biggest miss
#   5/22 (21 DTE):              140K OI
#   5/29 (May-end, 28 DTE):     285K OI
#   9/18 (Sept OPEX, 140 DTE):  967K OI
#   12/18 (Dec OPEX, 231 DTE):  995K OI
#   1/15/27 (Jan OPEX, 259 DTE):  432K OI
# Combined: ~4.5M OI captured by per-print events.
_OPEX_TARGET_EXPS = [
    '2026-05-15',
    '2026-05-22',
    '2026-05-29',
    '2026-09-18',
    '2026-12-18',
    '2027-01-15',
]
# Per-expiration cap — keeps total under Conn-A's free headroom.
# 6 expirations × 250 strikes max = 1,500 contracts (within 1,800 conn-A free).
_OPEX_PER_EXPIRATION_CAP = 250


def _collect_qqq_opex_wings() -> list:
    """Collect QQQ OPEX wing OCC symbols sorted by OI desc per expiration.

    Returns list of Tradier-formatted OCC symbols, capped at
    `_OPEX_PER_EXPIRATION_CAP` per expiration. Idempotent — safe to retry.
    Excludes any sym already on Conn-A (the 180 QQQ short-DTE strikes
    streamed by Schwab WS — caught via _subscribed_option_symbols_by_ticker).
    """
    import urllib.request as _ur
    import json as _json
    from connectors.tradier_streamer import schwab_osi_to_tradier

    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config.json',
    )
    token = ''
    try:
        with open(cfg_path) as _f:
            token = _json.load(_f).get('options_api_key', '') or ''
    except Exception:
        pass
    if not token:
        token = os.getenv('TRADIER_TOKEN', '')
    if not token:
        log.warning("[TRADIER-OPEX] No token — cannot collect OPEX wings")
        return []

    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    def _req(url):
        _r = _ur.Request(url, headers=headers)
        with _ur.urlopen(_r, timeout=15) as _resp:
            return _json.loads(_resp.read())

    # Build dedup set against Conn-A's existing QQQ short-DTE syms.
    already_subscribed = set()
    qqq_streamed = _subscribed_option_symbols_by_ticker.get('QQQ', []) or []
    for sym in qqq_streamed:
        if sym:
            already_subscribed.add(schwab_osi_to_tradier(sym))

    out_syms = []
    per_exp_counts = {}
    for exp in _OPEX_TARGET_EXPS:
        try:
            d = _req(f'https://api.tradier.com/v1/markets/options/chains'
                     f'?symbol=QQQ&expiration={exp}&greeks=false')
            opts = (d.get('options') or {}).get('option') or []
        except Exception as e:
            log.warning(f"[TRADIER-OPEX] {exp} chain fetch failed: {e}")
            continue
        # Sort by OI desc so we pick the highest-OI strikes first
        opts_sorted = sorted(opts, key=lambda o: -int(o.get('open_interest') or 0))
        added = 0
        for opt in opts_sorted:
            sym = opt.get('symbol', '')
            if not sym or sym in already_subscribed:
                continue
            out_syms.append(sym)
            already_subscribed.add(sym)
            added += 1
            if added >= _OPEX_PER_EXPIRATION_CAP:
                break
        per_exp_counts[exp] = added

    log.info(f"[TRADIER-OPEX] Collected {len(out_syms)} OPEX wing syms across "
             f"{len(_OPEX_TARGET_EXPS)} target expirations: {per_exp_counts}")
    return out_syms


def _collect_qqq_chain_wings() -> dict:
    """Phase 15 — collect QQQ contracts NOT already covered by Conn A subscription.
    Returns dict of three DTE-bucketed Tradier OCC lists, each capped at 1,950
    (under the empirical per-WS cap of ~2,000-2,500):
       'leaps':    DTE > 365     (highest signal/print — institutional positioning)
       'mid_dte':  31 < DTE ≤ 365 (institutional weekly/quarterly hedges)
       'near_dte': DTE ≤ 30      (high-volume near-month wings)
    Pulls from Tradier REST /markets/options/chains for ALL 35 expirations.
    Idempotent — safe to call multiple times. Returns empty buckets on error.

    2026-05-01 Path B fix: also excludes OPEX wings now on Conn-A
    (`_tradier_a_opex_syms`) to avoid double-subscribing.
    """
    import urllib.request as _ur
    import json as _json
    from datetime import datetime as _dt, date as _date_cls
    from connectors.tradier_streamer import schwab_osi_to_tradier

    # Read Tradier token (same logic as _start_tradier_streamer)
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config.json',
    )
    token = ''
    try:
        with open(cfg_path) as _f:
            token = _json.load(_f).get('options_api_key', '') or ''
    except Exception:
        pass
    if not token:
        token = os.getenv('TRADIER_TOKEN', '')
    if not token:
        log.warning("[TRADIER-PHASE15] No token — cannot collect wings")
        return {'leaps': [], 'mid_dte': [], 'near_dte': []}

    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    def _req(url):
        _r = _ur.Request(url, headers=headers)
        with _ur.urlopen(_r, timeout=15) as _resp:
            return _json.loads(_resp.read())

    # 1. Get all QQQ expirations
    try:
        d = _req('https://api.tradier.com/v1/markets/options/expirations'
                 '?symbol=QQQ&strikes=false&includeAllRoots=true')
        exps = (d.get('expirations') or {}).get('date') or []
    except Exception as e:
        log.warning(f"[TRADIER-PHASE15] expirations fetch failed: {e}")
        return {'leaps': [], 'mid_dte': [], 'near_dte': []}

    if not exps:
        log.warning("[TRADIER-PHASE15] no expirations returned")
        return {'leaps': [], 'mid_dte': [], 'near_dte': []}

    # 2. Build set of symbols already on Conn A (don't double-subscribe).
    # 2026-05-01 fix: use canonical _subscribed_option_symbols_by_ticker
    # (Phase 21 source). _ndx_option_symbols was legacy and now empty, so
    # without this Phase 15 collected ALL symbols incl. those Conn A already
    # had — defeating the dual-source-dedup intent.
    # ALSO exclude _tradier_a_opex_syms (Path B OPEX wings on Conn-A spare
    # capacity) so Conn-D/E don't double-subscribe to 5/15, 5/22, etc.
    already_subscribed = set()
    qqq_streamed = _subscribed_option_symbols_by_ticker.get('QQQ', []) or []
    for sym in qqq_streamed:
        if sym:
            already_subscribed.add(schwab_osi_to_tradier(sym))
    # Add Conn-A OPEX wings (already in Tradier OCC format)
    already_subscribed.update(_tradier_a_opex_syms)
    log.info(f"[TRADIER-PHASE15] Conn A covers {len(already_subscribed)} QQQ syms "
             f"({len(_tradier_a_opex_syms)} OPEX wings) — "
             f"collecting wings for {len(exps)} expirations")

    # 3. Pull each expiration chain, bucket by DTE.
    # 2026-05-05 BUG FIX: was iterating in DATE-ASC order and concatenating —
    # the 1,950 cap filled up on the FIRST 7-8 expirations, leaving later
    # near-DTE expirations (5/14 Thu, 5/18 Mon, 5/19 Tue — QQQ added these
    # daily expirations recently) entirely SKIPPED. Switched to per-expiration
    # ROUND-ROBIN with intra-expiration OI-desc sort so:
    #   (a) every expiration gets represented (no entire-day omissions)
    #   (b) within each, the highest-OI strikes are picked first
    #   (c) cap-cutoff falls on the LOWEST-OI tail across all expirations
    today = _date_cls.today()
    leaps_buckets   = {}   # exp_str → [syms sorted by OI desc]
    mid_dte_buckets = {}
    near_dte_buckets = {}

    for exp in exps:
        try:
            d = _req(f'https://api.tradier.com/v1/markets/options/chains'
                     f'?symbol=QQQ&expiration={exp}&greeks=false')
            opts = (d.get('options') or {}).get('option') or []
        except Exception as e:
            log.debug(f"[TRADIER-PHASE15] chain {exp} failed: {e}")
            continue

        try:
            dte = (_dt.strptime(exp, "%Y-%m-%d").date() - today).days
        except Exception:
            dte = -1

        # Sort by OI desc (highest-OI strikes get priority within this exp)
        opts_sorted = sorted(opts, key=lambda o: -int(o.get('open_interest') or 0))
        # Filter dedup
        syms_for_exp = []
        for opt in opts_sorted:
            sym = opt.get('symbol', '')
            if not sym or sym in already_subscribed:
                continue
            syms_for_exp.append(sym)

        if dte < 0:
            continue
        elif dte > 365:
            leaps_buckets[exp] = syms_for_exp
        elif dte > 30:
            mid_dte_buckets[exp] = syms_for_exp
        else:
            near_dte_buckets[exp] = syms_for_exp

    def _round_robin_pack(buckets: dict, cap: int) -> list:
        """Pack symbols round-robin across expirations until cap is hit.
        Each pass takes the next-highest-OI sym from each non-exhausted exp."""
        if not buckets: return []
        out = []
        idx = {exp: 0 for exp in buckets}
        # Sort expirations by date (chronological round-robin order)
        exps_sorted = sorted(buckets.keys())
        while len(out) < cap:
            added_this_pass = 0
            for exp in exps_sorted:
                if len(out) >= cap: break
                if idx[exp] < len(buckets[exp]):
                    out.append(buckets[exp][idx[exp]])
                    idx[exp] += 1
                    added_this_pass += 1
            if added_this_pass == 0: break  # all expirations exhausted
        return out

    CAP = 1950
    out = {
        'leaps':    _round_robin_pack(leaps_buckets, CAP),
        'mid_dte':  _round_robin_pack(mid_dte_buckets, CAP),
        'near_dte': _round_robin_pack(near_dte_buckets, CAP),
    }
    # Coverage report — exps fully captured vs trimmed
    def _exp_coverage(buckets, packed):
        packed_set = set(packed)
        covered = 0
        partial = 0
        skipped = 0
        for exp, syms in buckets.items():
            packed_count = sum(1 for s in syms if s in packed_set)
            if packed_count == len(syms): covered += 1
            elif packed_count > 0: partial += 1
            else: skipped += 1
        return covered, partial, skipped
    near_cov = _exp_coverage(near_dte_buckets, out['near_dte'])
    mid_cov  = _exp_coverage(mid_dte_buckets, out['mid_dte'])
    leap_cov = _exp_coverage(leaps_buckets, out['leaps'])
    log.info(f"[TRADIER-PHASE15] round-robin pack:")
    log.info(f"  near_dte: {len(out['near_dte'])} syms across {len(near_dte_buckets)} exps "
             f"(full={near_cov[0]}, partial={near_cov[1]}, skipped={near_cov[2]})")
    log.info(f"  mid_dte:  {len(out['mid_dte'])} syms across {len(mid_dte_buckets)} exps "
             f"(full={mid_cov[0]}, partial={mid_cov[1]}, skipped={mid_cov[2]})")
    log.info(f"  leaps:    {len(out['leaps'])} syms across {len(leaps_buckets)} exps "
             f"(full={leap_cov[0]}, partial={leap_cov[1]}, skipped={leap_cov[2]})")
    return out


def _start_tradier_streamer():
    """Spin up the Tradier streamer and subscribe to the single-name tape universe.

    Tradier's WS frame limit rejects payloads above ~1-2k symbols with close
    code 1009 ("message too big"). Since Schwab already covers QQQ/SPX/SPY
    options via LEVELONE, we use Tradier purely to fill the single-name gap
    (NVDA/AAPL/MSFT/AMZN/META/GOOGL/TSLA/AVGO) — ATM ±5% on next 2 expirations.
    That's ~300 contracts, well inside Tradier's frame budget.

    Idempotent: no-op if a streamer is already running.
    """
    global _tradier_streamer, _tradier_streamer_b
    global _tradier_streamer_c, _tradier_streamer_d, _tradier_streamer_e
    if _tradier_streamer is not None:
        log.info("[TRADIER] Already started")
        return

    # Load token from the same place data_provider.py reads it
    # (config.json → options_api_key, else env TRADIER_TOKEN).
    import json as _json
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config.json',
    )
    token = ''
    try:
        with open(cfg_path) as f:
            token = _json.load(f).get('options_api_key') or ''
    except Exception:
        pass
    if not token:
        token = os.getenv('TRADIER_TOKEN', '')
    if not token:
        log.warning("[TRADIER] No token configured — skipping streamer")
        return

    from connectors.tradier_streamer import TradierStreamer

    # ── Phase 15 (2026-04-30) — 5-WS Tradier deployment ─────────────────
    # Per-WS empirical cap: ~2,000-2,200 symbols (verified live via test —
    # 2,000 ✓, 2,500 ❌). Multi-session verified working at 5 concurrent
    # connections (tradier_5_session_test.py 14:37 ET — all 5 alive +
    # flowing events for 60s+, 26,466 events / 60s).
    #
    # Allocation (target ~1,800-2,000 per conn, well under 2K cap):
    #   Conn A (~1,427): QQQ ATM ±$100 (1,412) + 15 equity tickers
    #   Conn B (~1,366): SPX (800) + VIX (254) + Mag-8 (312)
    #   Conn C (~1,950): QQQ LEAPS (>365 DTE)  ← NEW — institutional positioning
    #   Conn D (~1,950): QQQ 31-365 DTE wings  ← NEW — quarterly/monthly hedges
    #   Conn E (~1,950): QQQ 0-30 DTE wings    ← NEW — high-volume tail wings
    # Combined: ~8,600 symbols covering 65-90% of QQQ chain by count,
    # ~99%+ by volume. Schwab L1 streaming (2,786) unchanged.
    _tradier_streamer   = TradierStreamer(token)    # Conn A
    _tradier_streamer_b = TradierStreamer(token)    # Conn B (Phase 14)
    _tradier_streamer_c = TradierStreamer(token)    # Conn C (Phase 15)
    _tradier_streamer_d = TradierStreamer(token)    # Conn D (Phase 15)
    _tradier_streamer_e = TradierStreamer(token)    # Conn E (Phase 15)

    # Register handlers on ALL 5 — events from any WS route to same handlers
    for streamer in (_tradier_streamer, _tradier_streamer_b,
                     _tradier_streamer_c, _tradier_streamer_d, _tradier_streamer_e):
        streamer.on('timesale', _on_tradier_timesale)         # OCC option prints
        streamer.on('timesale', _on_tradier_equity_timesale)  # equity-ticker prints
        streamer.on('tradex',   _on_tradier_timesale)         # extended-hours options
        streamer.on('tradex',   _on_tradier_equity_timesale)  # extended-hours equity

    _tradier_streamer.start()
    # Stagger conn opens by 4s each — gives Tradier time to register each
    # session before the next OAuth bind. Tested working at 5 concurrent.
    # 2026-05-07: widened spacing 4s → 8s. Each WS init triggers a SUBS burst
    # of 1500-1950 syms; at the original 4s cadence all 5 conns were burst-
    # subscribing within a 16s window during which CPU was already saturated
    # from the parallel Schwab option subscribe + L2 backfill. 8s cadence
    # spreads the Tradier SUBS bursts across 32s instead of 16s.
    import threading as _th
    _th.Timer(8.0,  _tradier_streamer_b.start).start()
    _th.Timer(16.0, _tradier_streamer_c.start).start()
    _th.Timer(24.0, _tradier_streamer_d.start).start()
    _th.Timer(32.0, _tradier_streamer_e.start).start()
    log.info("[TRADIER] ▶️ Phase 15: 5-WS deployment initiated "
             "(A=now, B=+8s, C=+16s, D=+24s, E=+32s; throttled 2026-05-07)")

    # ── Phase 14 (2026-04-30) — FULL alignment via dual WS ──────────────
    # Conn A: QQQ full chain (1,412) + equity tickers (15) = ~1,427 symbols
    # Conn B: SPX (800) + VIX (254) + Mag-8 single-names (312) = ~1,366 symbols
    # Both well under per-conn 2,000-symbol cap. Total 2,793 ≈ Schwab 2,786.
    try:
        from connectors.tradier_streamer import schwab_osi_to_tradier

        # ── CONN A ─────────────────────────────────────────────────────
        # QQQ short-DTE strikes (Phase 21 multi-DTE: 100 0DTE + 80 3-7DTE = 180).
        # 2026-05-01 fix: switched from `_ndx_option_symbols` (empty in
        # Phase 21 — only the legacy `_subscribe_qqq_options` populated it)
        # to `_subscribed_option_symbols_by_ticker['QQQ']` (canonical).
        # Without this, Conn-A silently subscribed 0 QQQ syms — so Phase 15
        # wing dedup couldn't filter against Conn-A coverage and Conn E
        # caught everything (functionally OK but loses dual-source dedup).
        qqq_streamed_syms = _subscribed_option_symbols_by_ticker.get('QQQ', []) or []
        qqq_full_occ = [schwab_osi_to_tradier(s) for s in qqq_streamed_syms if s]
        if qqq_full_occ:
            _tradier_streamer.subscribe(qqq_full_occ)
            log.info(f"[TRADIER-A] ▶️ Subscribed {len(qqq_full_occ)} QQQ OCC symbols "
                     f"(Phase 14 — short-DTE strikes streamed by Schwab WS)")

        # ── Path B (2026-05-01) — OPEX wings on Conn-A spare capacity ──
        # Conn-A has ~1,800 free per-conn slots. Add high-OI OPEX expirations
        # (5/15, 5/22, 5/29, 9/18, 12/18, 1/15/27) that the date-order Phase 15
        # collector misses (it caps at 1,950 per bucket and exhausts budget
        # on early-date expirations like 5/4-5/8 before reaching 5/15 OPEX).
        # Captures ~4.5M OI of dealer hedging that was previously OI-snapshot
        # only (no per-print events). Subscribes synchronously (~6 REST calls,
        # <10s) so wings dedup logic sees the OPEX set when Phase 15 fires +20s.
        try:
            global _tradier_a_opex_syms
            opex_occ = _collect_qqq_opex_wings()
            if opex_occ:
                _tradier_streamer.subscribe(opex_occ)
                _tradier_a_opex_syms.update(opex_occ)
                log.info(f"[TRADIER-A] ▶️ Subscribed {len(opex_occ)} QQQ OPEX wing OCCs "
                         f"(Path B — 5/15+5/22+5/29+9/18+12/18+1/15/27 high-OI strikes)")
        except Exception as _opex_e:
            log.warning(f"[TRADIER-A] OPEX wings subscription failed: {_opex_e}")

        # ── CONN B — single-name OCC (Mag-8) ──────────────────────────
        try:
            single_name_occ = _collect_single_name_atm_symbols(max_per_ticker=40)
        except Exception as e:
            log.warning(f"[SCHWAB-REST] Single-name collection failed: {e}")
            single_name_occ = []

        if single_name_occ:
            _tradier_streamer_b.subscribe(single_name_occ)
            log.info(f"[TRADIER-B] ▶️ Subscribed {len(single_name_occ)} single-name OCC symbols "
                     f"(NVDA/AAPL/MSFT/AMZN/META/GOOGL/TSLA/AVGO)")

        # SPX full streaming set (was 300, now 800)
        spx_syms = _subscribed_option_symbols_by_ticker.get('$SPX') or []
        spx_occ = [schwab_osi_to_tradier(s) for s in spx_syms if s]
        if spx_occ:
            _tradier_streamer_b.subscribe(spx_occ)
            log.info(f"[TRADIER-B] ▶️ Subscribed {len(spx_occ)} SPX OCC symbols "
                     f"(Phase 14 — FULL CHAIN, was 300)")

        # VIX (already 254 = full chain)
        vix_syms = _subscribed_option_symbols_by_ticker.get('$VIX') or []
        vix_occ = [schwab_osi_to_tradier(s) for s in vix_syms if s]
        if vix_occ:
            _tradier_streamer_b.subscribe(vix_occ)
            log.info(f"[TRADIER-B] ▶️ Subscribed {len(vix_occ)} VIX OCC symbols "
                     f"(Phase 14 — full chain, root=VIXW)")
    except Exception as e:
        log.warning(f"[TRADIER] Phase 14 subscription failed: {e}")

    # ── Phase 17B Tradier subscriptions ROLLED BACK 2026-04-30 ──────
    # Adding SPY/IWM/sector ETFs to existing Conn A/B caused flap loop
    # (verified 22:04 — attempt counter climbed to 8 in 2 min, 0.2s uptime
    # per attempt). Likely cause: total per-token symbol count exceeded
    # ~9,000 threshold (Phase 14/15 already at 8,529).
    #
    # Current state for SPY/IWM/sector ETFs after Phase 17B:
    #   ✓ Schwab streaming Greeks (Δ/Γ/IV/OI sub-200ms)
    #   ✗ No per-print events (Schwab gates TIMESALE for ETFs)
    #
    # FLOW chart impact for these tickers:
    #   - Walls/max_pain: WORKING (uses streaming OI)
    #   - Greek surface: WORKING (uses streaming Greeks)
    #   - Per-print FLOW chart line: NOT POPULATED for SPY/IWM/ETFs
    #
    # To fix later: investigate Tradier per-token symbol cap, or add
    # a 6th smaller Tradier connection just for these tickers
    # (untested — 6-conn test failed with synthetic data but with
    # production-sized conns might behave differently).

    # ── Phase 15 (2026-04-30) — Conn C/D/E QQQ wing + LEAPS subscription ──
    # Deferred 20s so:
    #   - Conn C/D/E WS handshakes complete (started at +8/+12/+16s)
    #   - Wing collection takes ~30s (35 REST calls × ~1s each)
    #   - A and B already flowing events → easy to verify by contrast
    def _phase15_subscribe():
        try:
            wings = _collect_qqq_chain_wings()
            leaps_occ = wings.get('leaps', []) or []
            mid_dte_occ = wings.get('mid_dte', []) or []
            near_dte_occ = wings.get('near_dte', []) or []

            if leaps_occ and _tradier_streamer_c is not None:
                _tradier_streamer_c.subscribe(leaps_occ)
                log.info(f"[TRADIER-C] ▶️ Subscribed {len(leaps_occ)} QQQ LEAPS OCC "
                         f"(Phase 15 — DTE>365, institutional positioning)")
            if mid_dte_occ and _tradier_streamer_d is not None:
                _tradier_streamer_d.subscribe(mid_dte_occ)
                log.info(f"[TRADIER-D] ▶️ Subscribed {len(mid_dte_occ)} QQQ 31-365 DTE wings "
                         f"(Phase 15 — quarterly/monthly hedges)")
            if near_dte_occ and _tradier_streamer_e is not None:
                _tradier_streamer_e.subscribe(near_dte_occ)
                log.info(f"[TRADIER-E] ▶️ Subscribed {len(near_dte_occ)} QQQ 0-30 DTE wings "
                         f"(Phase 15 — high-volume tail wings)")
            total_phase15 = len(leaps_occ) + len(mid_dte_occ) + len(near_dte_occ)
            log.info(f"[TRADIER] ▶️ Phase 15 complete: +{total_phase15} QQQ symbols "
                     f"across Conn C/D/E (full chain coverage)")
        except Exception as _e:
            log.warning(f"[TRADIER-PHASE15] subscribe failed: {_e}")

    import threading as _th2
    _th2.Timer(20.0, _phase15_subscribe).start()

    # ── Equity-ticker subscription — fills Schwab TIMESALE_EQUITY code=11 gap ──
    # Adds plain equity tickers to the Tradier WS subscription set. These
    # deliver TRUE per-print equity tape (price, size, bid, ask, exch, ts)
    # vs the L1-inferred synthesis we used to do. Same `_recent_equity_prints`
    # ring buffer feeds mm_attribution joins — downstream auto-upgrades.
    # Started at 2 (QQQ+SPY) on 2026-04-29 morning; expanded to 13 same day
    # (added VIX-ETNs + Mag-8 + leveraged QQQ).
    # ~437 symbol count total on Tradier; still well under their "several
    # hundred" comfort zone. No additional account / WS / pricing tier required.
    try:
        equity_tickers = list(_TRADIER_EQUITY_TICKERS)
        _tradier_streamer.subscribe(equity_tickers)
        log.info(f"[TRADIER] ▶️ Subscribed {len(equity_tickers)} equity tickers "
                 f"({', '.join(equity_tickers)}) — fills Schwab TIMESALE_EQUITY code=11 gap")
    except Exception as e:
        log.warning(f"[TRADIER] Equity-ticker subscription failed: {e}")

    # First walls emit so UI has data before the refresh loop's first tick.
    try:
        walls0 = _compute_single_name_walls()
        if walls0 and _socketio is not None:
            _socketio.emit('single_name_walls', {'tickers': walls0})
            log.info(f"[SCHWAB-REST] Initial walls emit: {len(walls0)} tickers")
            wgc0 = _compute_ndx_wgc(walls0)
            if wgc0:
                global _latest_wgc
                _latest_wgc = wgc0
                _socketio.emit('ndx_wgc', wgc0)
                log.info(f"[WGC] Initial: sign={wgc0['wgc_sign']:+.3f} "
                         f"regime={wgc0['regime']} "
                         f"ampl={wgc0['ampl_count']}/{wgc0['ampl_count']+wgc0['damp_count']}")
    except Exception as e:
        log.debug(f"[SCHWAB-REST] initial walls emit failed: {e}")

    # Spawn Greeks refresher (re-pulls chains every 15s so Tradier prints get
    # populated delta/iv). Idempotent — guarded by module flag.
    global _single_name_refresh_started
    if not _single_name_refresh_started:
        _single_name_refresh_started = True
        t = threading.Thread(target=_single_name_refresh_loop, daemon=True,
                             name="tradier-greeks-refresh")
        t.start()

    # Phase 22 (2026-05-05): spawn full-QQQ-chain Greeks refresher so wing prints
    # (DTE>=8 OR ATM-distant strikes) get Schwab-exact Greeks instead of BSM.
    # Idempotent via module-level flag mirroring the single-name pattern.
    global _qqq_full_chain_refresh_started
    if not _qqq_full_chain_refresh_started:
        _qqq_full_chain_refresh_started = True
        t = threading.Thread(target=_qqq_full_chain_refresh_loop, daemon=True,
                             name="qqq-full-chain-greeks-refresh")
        t.start()


def get_tradier_conn_stats() -> list:
    """Return per-conn Tradier WS health for /api/_debug/capture_rate.

    Each entry has: conn (A/B/C/D/E), connected, symbols, current_uptime_sec,
    cumulative_uptime_sec, total_reconnects, msg_count, last_msg_age_sec,
    last_disconnect_ts, last_disconnect_age_sec, last_disconnect_reason.

    Added 2026-05-05 after live audit caught 3-of-5 conns dropping silently
    at 14:24:42 (uptime ~7min). Without this you only see disconnects by
    grepping the log file.
    """
    out = []
    instances = [
        ('A', _tradier_streamer),
        ('B', _tradier_streamer_b),
        ('C', _tradier_streamer_c),
        ('D', _tradier_streamer_d),
        ('E', _tradier_streamer_e),
    ]
    for label, streamer in instances:
        if streamer is None:
            out.append({'conn': label, 'state': 'not_initialized'})
            continue
        try:
            s = streamer.stats()
            s['conn'] = label
            out.append(s)
        except Exception as e:
            out.append({'conn': label, 'state': 'stats_error', 'error': str(e)[:120]})
    return out


def _is_rth_now() -> bool:
    """True iff current time is inside US RTH (9:30-16:00 ET, Mon-Fri).
    Schwab option chains are static outside RTH so polling is wasted there.
    Used by intel compute loop to gate the cycle (full RTH).

    NOTE: chain rotation uses _is_chain_rotation_active_now() instead, which
    has a 15:00 ET upper bound (Option B, 2026-04-30 — user's trade window).
    """
    try:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            # Fallback: use system TZ (assumes server runs in US/Eastern)
            et = datetime.now()
        if et.weekday() >= 5:
            return False
        minute = et.hour * 60 + et.minute
        return (9 * 60 + 30) <= minute < (16 * 60)
    except Exception:
        return False


def _is_chain_rotation_active_now() -> bool:
    """True iff current time is inside the chain-rotation active window.
    Window: 9:30 → 15:00 ET, Mon-Fri (1 hour shorter than full RTH).

    User confirmed 2026-04-30: trading day ends at 15:00 ET. Layer 2 chain
    rotation provides no actionable signal post-15:00 since (a) UI panels
    aren't being watched and (b) Layer 1 streaming continues for any
    background analytics. Saves ~1,000 reqs/day vs full 6.5h RTH.
    """
    try:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            et = datetime.now()
        if et.weekday() >= 5:
            return False
        minute = et.hour * 60 + et.minute
        return (9 * 60 + 30) <= minute < (15 * 60)
    except Exception:
        return False


def _chain_rotation_interval_now() -> float:
    """Adaptive cycle length based on time-of-day (Option B, 2026-04-30):
      - 9:30 → 12:00 ET:  30s (opening drive — walls migrate fast as 0DTE
                              positions get established)
      - 12:00 → 15:00 ET: 60s (afternoon — slow trends, walls sticky;
                               half the rate, half the budget burn)

    Combined with SPY-dropped tickers (3 instead of 4) and 15:00 cutoff,
    fits under 5,000 daily REST budget with ~4% headroom (4,800 reqs/day
    projected for full trading day).
    """
    try:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            et = datetime.now()
        return 30.0 if et.hour < 12 else 60.0
    except Exception:
        return 60.0  # safe default — afternoon cadence


def _rotate_chain_for(ticker: str) -> int:
    """Pull full chain via REST for `ticker` and merge cap-blocked-tail
    contracts into `_per_ticker_gex` (mirroring the streaming write at
    line ~1687). Returns number of contracts merged this call.

    SAFETY: only writes contracts NOT already populated by streaming —
    streaming has 200ms latency vs our 30s; we never clobber fresher data.

    Raises _ChainRotationRateLimit on HTTP 429 from Schwab.
    """
    global _chain_rotation_lifetime_merged

    import sys, os
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        from server import _schwab_chain_range
    except Exception as _imp_e:
        log.warning(f"[CHAIN-ROTATION] cannot import _schwab_chain_range: {_imp_e}")
        return 0

    # Pull chain via per-ticker date-range chunks. Default = 3 chunks
    # (0-30d, 30-180d, 180-1100d). SPX needs 6 finer chunks because its
    # chain is ~21K contracts and Schwab returns 502 "TooBigBody" on any
    # 30-day SPX window. VIX is small enough for a single 0-1100d chunk.
    # Total: ~13 REST calls per cycle = 26 req/min at 30s = ~22% of cap.
    from datetime import date, timedelta
    today = date.today()

    # Look up per-ticker chunk spec; fall back to default 3-chunk plan.
    chunk_offsets = _CHAIN_ROTATION_CHUNKS.get(ticker, [(0, 30), (31, 180), (181, 1100)])
    chunks = [
        ((today + timedelta(days=fro)).isoformat(),
         (today + timedelta(days=to)).isoformat())
        for fro, to in chunk_offsets
    ]
    # Per-ticker strike_count override (smaller for huge chains like SPX)
    strike_count_for_ticker = _CHAIN_ROTATION_STRIKE_COUNT_BY_TICKER.get(
        ticker, _CHAIN_ROTATION_STRIKE_COUNT
    )

    opts = []
    spot = 0.0
    for from_d, to_d in chunks:
        try:
            chunk_opts, chunk_px = _schwab_chain_range(
                ticker, from_d, to_d,
                strike_count=strike_count_for_ticker,
            )
            opts.extend(chunk_opts)
            if chunk_px and chunk_px > spot:
                spot = float(chunk_px)
        except Exception as _e:
            msg = str(_e).lower()
            if '429' in msg or 'rate' in msg or 'too many' in msg:
                raise _ChainRotationRateLimit(msg)
            # Non-429 (e.g., timeout, network blip) — skip this chunk, keep cycle going
            log.debug(f"[CHAIN-ROTATION] {ticker} {from_d}→{to_d} skip: {_e}")
            continue

    if not opts:
        return 0

    # Normalize ticker to root key matching the streaming logic
    sym_root = ticker.lstrip('$').upper()
    if sym_root == 'SPXW':   sym_root = 'SPX'
    elif sym_root == 'NDXP': sym_root = 'NDX'
    elif sym_root == 'RUTW': sym_root = 'RUT'
    elif sym_root == 'VIXW': sym_root = 'VIX'

    pt = _per_ticker_gex.setdefault(sym_root, {})
    merged = 0
    skipped_streamed = 0

    for o in opts:
        sym_key = (o.get('symbol') or '').strip()
        if not sym_key:
            continue
        # NEVER overwrite a streaming-populated contract — the streaming
        # path has 200ms freshness vs our 30s. _source tag distinguishes
        # the two on subsequent rotations.
        existing = pt.get(sym_key)
        if existing and existing.get('_source') != 'rest_rotation':
            skipped_streamed += 1
            continue

        try:
            strike = float(o.get('strike') or 0)
        except Exception:
            continue
        if strike <= 0:
            continue

        contract_type = (o.get('option_type') or '').lower()
        side = 'call' if contract_type.startswith('c') else 'put' if contract_type.startswith('p') else None
        if not side:
            continue

        gamma = float(o.get('gamma') or 0)
        delta = abs(float(o.get('delta') or 0))
        oi    = int(o.get('open_interest') or 0)

        # Skip dead contracts (no OI, no Greeks) — they contribute nothing
        # to walls/GEX. This trims the noise floor without losing signal.
        if oi == 0 and gamma == 0:
            continue

        # SAME formula as the streaming write at line ~1681
        if spot > 0:
            pt_dollar_gex = gamma * oi * 100 * (spot * spot / 100)
        else:
            pt_dollar_gex = 0

        pt[sym_key] = {
            'strike':        strike,
            'side':          side,
            'gamma_dollars': pt_dollar_gex,
            'oi':            oi,
            'delta':         delta,
            '_source':       'rest_rotation',
            '_ts':           time.time(),
        }
        merged += 1

        # Feed greek_surface for QQQ — same treatment as streaming path
        # (line ~1712). This populates per-strike Vanna/Charm for the
        # cap-blocked tail so hedge_pressure sees full coverage.
        if _greek_surface and sym_root == 'QQQ':
            try:
                _greek_surface.update({
                    'symbol':           sym_key,
                    'strike':           strike,
                    'contract_type':    'C' if side == 'call' else 'P',
                    'gamma':            gamma,
                    'delta':            delta,
                    'theta':            float(o.get('theta') or 0),
                    'vega':             float(o.get('vega') or 0),
                    'open_interest':    oi,
                    'underlying_price': spot,
                    'volatility':       float(o.get('volatility') or 0),
                })
            except Exception:
                pass

    # Update spot cache so downstream reads stay coherent
    if spot > 0:
        _latest_spot_by_ticker[sym_root] = spot

    _chain_rotation_lifetime_merged += merged
    _chain_rotation_last_merge_count[ticker] = merged
    return merged


def _full_chain_rotation_loop():
    """Background loop: STRATEGIC SCHEDULE (Phase 18, 2026-04-30).
    Fires 5x daily at 9:30, 10:30, 12:00, 13:30, 15:00 ET — each fire
    runs one full chain rotation pass through all _CHAIN_ROTATION_TICKERS
    with stagger between them.

    Replaces previous adaptive 30s/60s polling. Rationale:
      - OI is fundamentally EOD-stable (OCC publishes once daily)
      - Polling every 30s for static data wasted ~4,750 reqs/day
      - Greeks/bid/ask now covered by Tradier WS + Phase 17 BSM solver
      - Strategic snapshots capture occasional intraday vendor updates
      - Cost: ~50 reqs/day (was ~4,800)

    All safety guards retained: weekday gate, stagger, 429 backoff,
    auto-disable on streak, daily budget cap (5K — kept as safety).
    """
    global _chain_rotation_request_count, _chain_rotation_429_streak
    global _chain_rotation_last_429_ts, _chain_rotation_enabled
    global _chain_rotation_last_reset_date, _chain_rotation_last_cycle_ts
    global _chain_rotation_fired_today

    fire_times_str = ', '.join(f'{m//60:02d}:{m%60:02d}' for m in _chain_rotation_fire_times_et_min)
    log.info(f"[CHAIN-ROTATION] Started Phase 18 — {len(_CHAIN_ROTATION_TICKERS)} tickers "
             f"({','.join(_CHAIN_ROTATION_TICKERS)}), strategic fires at {fire_times_str} ET, "
             f"stagger {_CHAIN_ROTATION_STAGGER_S}s, daily budget {_CHAIN_ROTATION_DAILY_BUDGET} (safety cap)")

    # ── Persistence (added 2026-05-01) ──────────────────────────────
    # Survives server restarts — without this, every restart resets the
    # in-memory counter to 0 and re-fires all past schedule slots
    # sequentially (~16 min "catch-up" with redundant data). State file
    # is rewritten after every fire and on daily reset.
    from datetime import date as _date
    _FIRE_STATE_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'logs', 'chain_rotation_state.json',
    )

    def _save_fire_state():
        try:
            os.makedirs(os.path.dirname(_FIRE_STATE_FILE), exist_ok=True)
            tmp = _FIRE_STATE_FILE + '.tmp'
            with open(tmp, 'w') as _f:
                import json as _json
                _json.dump({
                    'date': _chain_rotation_last_reset_date or '',
                    'fired_today': sorted(_chain_rotation_fired_today),
                    'request_count': _chain_rotation_request_count,
                    '_saved_at': time.time(),
                }, _f)
            os.replace(tmp, _FIRE_STATE_FILE)
        except Exception as _e:
            log.warning(f"[CHAIN-ROTATION] state save failed: {_e}")

    def _load_fire_state():
        global _chain_rotation_request_count, _chain_rotation_fired_today
        global _chain_rotation_last_reset_date
        try:
            if not os.path.exists(_FIRE_STATE_FILE):
                return False
            import json as _json
            with open(_FIRE_STATE_FILE) as _f:
                state = _json.load(_f)
            saved_date = state.get('date', '')
            today_iso = _date.today().isoformat()
            if saved_date != today_iso:
                log.info(f"[CHAIN-ROTATION] State file is from {saved_date}, "
                         f"today is {today_iso} — treating as fresh day")
                return False
            _chain_rotation_request_count = int(state.get('request_count', 0))
            _chain_rotation_fired_today   = set(state.get('fired_today', []))
            _chain_rotation_last_reset_date = saved_date
            log.info(f"[CHAIN-ROTATION] Restored state from disk: "
                     f"reqs_today={_chain_rotation_request_count}, "
                     f"fires_done={sorted(_chain_rotation_fired_today)}")
            return True
        except Exception as _e:
            log.warning(f"[CHAIN-ROTATION] state load failed: {_e}")
            return False

    # Try restore from disk before entering loop
    _load_fire_state()

    while True:
        try:
            # Daily counter reset (also resets the fired-today set)
            today = _date.today().isoformat()
            if _chain_rotation_last_reset_date != today:
                if _chain_rotation_last_reset_date is not None:
                    log.info(f"[CHAIN-ROTATION] Daily reset — yesterday: {_chain_rotation_request_count} reqs, "
                             f"streak={_chain_rotation_429_streak}, "
                             f"fires_completed={sorted(_chain_rotation_fired_today)}")
                _chain_rotation_request_count = 0
                _chain_rotation_429_streak = 0
                _chain_rotation_enabled = True  # re-arm at start of new day
                _chain_rotation_last_reset_date = today
                _chain_rotation_fired_today = set()  # reset fire tracker
                _save_fire_state()  # persist the daily reset

            if not _chain_rotation_enabled:
                time.sleep(120)
                continue
            if _chain_rotation_request_count >= _CHAIN_ROTATION_DAILY_BUDGET:
                log.warning(f"[CHAIN-ROTATION] Daily budget {_CHAIN_ROTATION_DAILY_BUDGET} hit — pausing 5 min")
                time.sleep(300)
                continue

            # ── Determine current ET time + check if we should fire ───────────
            try:
                from zoneinfo import ZoneInfo
                from datetime import datetime as _dt
                et = _dt.now(ZoneInfo("America/New_York"))
            except Exception:
                from datetime import datetime as _dt
                et = _dt.now()

            # Weekend gate
            if et.weekday() >= 5:
                time.sleep(3600)  # check hourly on weekends
                continue

            current_min = et.hour * 60 + et.minute

            # Find pending fire time (one we haven't fired yet that's due)
            target_fire_time = None
            for fire_min in _chain_rotation_fire_times_et_min:
                if fire_min not in _chain_rotation_fired_today and current_min >= fire_min:
                    target_fire_time = fire_min
                    break

            if target_fire_time is None:
                # No fire time pending. Find next scheduled fire and sleep until it.
                next_fire = next((f for f in _chain_rotation_fire_times_et_min
                                  if f not in _chain_rotation_fired_today and f > current_min), None)
                if next_fire is None:
                    # All fires done for today — sleep until tomorrow's reset
                    time.sleep(3600)  # check hourly
                else:
                    # Sleep until 30s before next fire (gives buffer)
                    sleep_secs = max(10, (next_fire - current_min) * 60 - 30)
                    sleep_secs = min(sleep_secs, 1800)  # cap at 30 min between checks
                    time.sleep(sleep_secs)
                continue

            # ── Fire time arrived — run a full cycle ────────────────────────
            log.info(f"[CHAIN-ROTATION] Scheduled fire at {et.hour:02d}:{et.minute:02d} ET "
                     f"(target slot: {target_fire_time//60:02d}:{target_fire_time%60:02d})")
            cycle_start = time.time()
            _chain_rotation_last_cycle_ts = cycle_start
            cycle_merged = 0
            broke_early = False

            for i, ticker in enumerate(_CHAIN_ROTATION_TICKERS):
                if not _chain_rotation_enabled:
                    broke_early = True
                    break
                # Stagger so calls don't burst together
                target = cycle_start + (i * _CHAIN_ROTATION_STAGGER_S)
                wait = target - time.time()
                if wait > 0:
                    time.sleep(wait)

                try:
                    n = _rotate_chain_for(ticker)
                    _chain_rotation_request_count += len(_CHAIN_ROTATION_CHUNKS.get(ticker, [(0,30),(31,180),(181,1100)]))
                    _chain_rotation_429_streak = 0
                    cycle_merged += n
                except _ChainRotationRateLimit:
                    _chain_rotation_429_streak += 1
                    _chain_rotation_last_429_ts = time.time()
                    backoff = min(60 * (2 ** _chain_rotation_429_streak), 480)
                    log.warning(f"[CHAIN-ROTATION] HTTP 429 on {ticker} — "
                                f"streak={_chain_rotation_429_streak}, backoff {backoff}s")
                    if _chain_rotation_429_streak >= 4:
                        _chain_rotation_enabled = False
                        log.error("[CHAIN-ROTATION] 4-streak of 429s — DISABLED for safety. "
                                  "Resets at next RTH start, or set _chain_rotation_enabled=True manually.")
                    time.sleep(backoff)
                    broke_early = True
                    break
                except Exception as _e:
                    log.warning(f"[CHAIN-ROTATION] {ticker} unexpected error: {_e}")

            if not broke_early:
                # Mark this fire time as completed for today
                _chain_rotation_fired_today.add(target_fire_time)
                log.info(f"[CHAIN-ROTATION] Phase 18 fire complete — merged={cycle_merged} "
                         f"reqs_today={_chain_rotation_request_count} "
                         f"fires_done={sorted(_chain_rotation_fired_today)} "
                         f"per_ticker={_chain_rotation_last_merge_count}")
                _save_fire_state()  # persist after every successful fire

            # Sleep before next check (we don't fire continuously now)
            time.sleep(60)
        except Exception as _outer:
            log.warning(f"[CHAIN-ROTATION] outer loop error: {_outer}")
            time.sleep(30)


def _kv_sample_loop():
    """Phase 19 (Kobayashi 2025) — daily k_v sample collector.

    Once per day at 15:30 ET (end of trade window, IV is stabilizing for the close),
    records (spot, ATM_IV) per tracked ticker into the KvEstimator. Used to fit
    the volatility-delta coefficient k_v = pp of IV per 1% spot move.

    Why 15:30 ET:
      - Late enough that intraday IV has settled
      - Before 0DTE pin distortion (last 30 min)
      - Aligns with our trade-window cutoff
      - Single sample per day = robust against noise

    Tickers sampled: per TICKER_DEFAULT_KV in connectors/kv_estimator.py
    """
    global _kv_last_sample_date

    KV_TICKERS = ['QQQ', 'SPY', 'SPX', 'IWM', 'VIX', 'XLK', 'XLE', 'XLF',
                  'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA']

    log.info(f"[KV-SAMPLER] Started — fires daily at 15:30 ET for {len(KV_TICKERS)} tickers")

    while True:
        try:
            try:
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo
                et = _dt.now(ZoneInfo("America/New_York"))
            except Exception:
                from datetime import datetime as _dt
                et = _dt.now()

            # Only fire on weekdays (Mon-Fri)
            if et.weekday() >= 5:
                time.sleep(3600)
                continue

            today_iso = et.date().isoformat()
            current_min = et.hour * 60 + et.minute

            # Fire once per day at 15:30 ET (window: 15:30-15:45)
            FIRE_WINDOW_START = 15 * 60 + 30
            FIRE_WINDOW_END   = 15 * 60 + 45

            if (FIRE_WINDOW_START <= current_min <= FIRE_WINDOW_END
                    and _kv_last_sample_date != today_iso):
                _collect_kv_samples(KV_TICKERS, today_iso)
                _kv_last_sample_date = today_iso
                # Sleep past the window so we don't double-fire
                time.sleep(900)  # 15 min
                continue

            # Otherwise sleep until next check (60s — checks every minute is fine)
            time.sleep(60)
        except Exception as _e:
            log.warning(f"[KV-SAMPLER] outer loop error: {_e}")
            time.sleep(60)


def _collect_kv_samples(tickers: list, today_iso: str) -> None:
    """Collect (spot, ATM_IV) samples for each ticker and feed to KvEstimator.

    For each ticker:
      1. Get spot from _latest_spot_by_ticker
      2. Find ATM contract in _on_options_quote._sym_cache (closest strike to spot)
      3. Use that contract's IV as the ticker's ATM IV
      4. Check event calendar — flag is_event if announcement today
      5. Add sample to KvEstimator

    Skips ticker if spot or IV unavailable (no streaming data yet).
    """
    try:
        from connectors.kv_estimator import get_kv_estimator
    except Exception as _e:
        log.warning(f"[KV-SAMPLER] cannot import kv_estimator: {_e}")
        return

    # Try to get event calendar (optional — falls back to no events)
    is_event_for_ticker = {}
    try:
        from connectors import event_calendar as _ec
        for tk in tickers:
            is_event_for_ticker[tk] = _ec.is_event_day(tk, today_iso)
    except Exception:
        pass  # event calendar not loaded — assume no events

    est = get_kv_estimator()
    cache = getattr(_on_options_quote, '_sym_cache', {}) or {}
    now_ts = time.time()

    sampled = 0
    for ticker in tickers:
        try:
            spot = float(_latest_spot_by_ticker.get(ticker, 0) or 0)
            if spot <= 0:
                # Try alternate keys (some tickers store under different names)
                for alt_key in (f"${ticker}", f"${ticker}.X"):
                    spot = float(_latest_spot_by_ticker.get(alt_key, 0) or 0)
                    if spot > 0:
                        break
            if spot <= 0:
                continue

            # Find best ATM IV: contract in cache for this ticker, closest to spot,
            # with at least 7 DTE (avoid 0DTE noise) and at most 60 DTE (front month).
            best_iv = 0.0
            best_dist = 1e9
            for sym, rec in cache.items():
                if not isinstance(rec, dict):
                    continue
                rec_root = (rec.get('underlying_symbol') or rec.get('underlying') or '').strip()
                if rec_root != ticker:
                    continue
                strike = float(rec.get('strike', 0) or 0)
                dte = int(rec.get('dte', 0) or 0)
                iv = float(rec.get('implied_vol', 0) or 0)
                if strike <= 0 or iv <= 0 or dte < 7 or dte > 60:
                    continue
                dist = abs(strike - spot)
                if dist < best_dist:
                    best_dist = dist
                    best_iv = iv

            if best_iv <= 0:
                continue  # no valid ATM contract found

            # Schwab streams IV as percent (e.g., 30.0 means 30%)
            est.add_sample(
                ticker=ticker,
                timestamp=now_ts,
                spot=spot,
                iv_atm_pct=best_iv,
                is_event=is_event_for_ticker.get(ticker, False),
            )
            sampled += 1
            log.info(f"[KV-SAMPLER] {ticker} spot={spot:.2f} IV={best_iv:.2f}% "
                     f"k_v={est.get_kv(ticker):.3f} pp/% "
                     f"event={is_event_for_ticker.get(ticker, False)}")
        except Exception as _e:
            log.debug(f"[KV-SAMPLER] {ticker} sample err: {_e}")

    log.info(f"[KV-SAMPLER] Daily fire complete — {sampled}/{len(tickers)} tickers sampled")


def _intel_compute_loop():
    """Background loop for intelligent-panels signal compute (Phase 2 + 3).

    Phase 2 — Pin Convergence:
      - 15s cadence during last hour of RTH (high pin pull amplification)
      - 60s cadence rest of RTH
      - Emits 'intel:pin_update' Socket.IO push after each compute
      - Source: connectors/pin_convergence.compute_pin_state

    Phase 3 — Hedge Forecaster (TBD, wires into same loop later):
      - 5s cadence during RTH
      - Emits 'intel:hedge_forecast' push
      - Source: connectors/hedge_forecaster.compute_forecast

    All intel signals are RTH-gated (skip outside 9:30-16:00 ET) — chains
    don't move overnight, so polling is wasted budget.

    Inner cadence: 5s sleep; per-signal next-fire times tracked separately so
    pin and hedge can run on independent schedules within the same loop.
    """
    global _intel_pin_last_compute_ts, _intel_hedge_last_compute_ts
    global _intel_div_last_compute_ts, _intel_vix_last_compute_ts
    global _intel_wing_last_compute_ts, _intel_skyline_last_compute_ts
    global _intel_warehouse_last_compute_ts, _intel_events_last_compute_ts

    log.info(f"[INTEL] Compute loop started — pin {_INTEL_PIN_LAST_HOUR_INTERVAL_S}s "
             f"(last hour) / {_INTEL_PIN_OFF_INTERVAL_S}s (otherwise) / "
             f"hedge_fc {_INTEL_HEDGE_INTERVAL_S}s / "
             f"spx_qqq_div {_INTEL_DIV_INTERVAL_S}s / "
             f"vix_term {_INTEL_VIX_INTERVAL_S}s / "
             f"wing {_INTEL_WING_INTERVAL_S}s / "
             f"skyline {_INTEL_SKYLINE_INTERVAL_S}s / "
             f"warehouse {_INTEL_WAREHOUSE_INTERVAL_S}s / "
             f"events {int(_INTEL_EVENTS_INTERVAL_S/60)}min")

    # 2026-05-06: per-compute timing to identify which intel signal is the
    # CPU spike source. Logs warning when ANY single compute exceeds 200ms,
    # and a summary if total iteration time exceeds 500ms. Identifies the
    # culprit without needing py-spy/sudo.
    _intel_iter_count = 0

    while True:
        try:
            if not _is_rth_now():
                time.sleep(60)
                continue

            now = time.time()
            _intel_iter_count += 1
            _iter_t0 = time.perf_counter()
            _intel_compute_times: dict = {}

            def _time_intel(label, fn, *args, **kwargs):
                """Run fn, time it, record in _intel_compute_times. Returns fn result or None."""
                _t0 = time.perf_counter()
                try:
                    return fn(*args, **kwargs)
                finally:
                    _dt_ms = (time.perf_counter() - _t0) * 1000.0
                    _intel_compute_times[label] = _dt_ms
                    if _dt_ms > 200.0:
                        log.warning(f"[INTEL-PERF] {label} took {_dt_ms:.0f}ms (>200ms)")

            # ── Pin Convergence (Phase 2) ──────────────────────────────
            try:
                from connectors import pin_convergence as _pc
                tr_sec = _pc.seconds_until_session_close(now)
                interval = (_INTEL_PIN_LAST_HOUR_INTERVAL_S
                            if tr_sec < _pc.TIME_AMP_THRESHOLD_SEC
                            else _INTEL_PIN_OFF_INTERVAL_S)
                if (now - _intel_pin_last_compute_ts) >= interval:
                    _intel_pin_last_compute_ts = now
                    state = _time_intel('pin', _pc.compute_pin_state, 'QQQ')
                    if _socketio is not None and state and state.get('pin_estimate') is not None:
                        try:
                            _socketio.emit('intel:pin_update', state)
                        except Exception as _e:
                            log.debug(f"[INTEL] pin emit err: {_e}")
            except Exception as _pe:
                log.debug(f"[INTEL] pin compute err: {_pe}")

            # ── Hedge Forecaster (Phase 3) ─────────────────────────────
            # 5s cadence — computes Γ-pressure × velocity. Disk ledgers keep
            # all predictive fields for offline research; socket emit ships
            # OBSERVABLE FIELDS ONLY because the directional prediction was
            # empirically demonstrated to have ZERO edge over base rate
            # (audit 2026-05-04: sign_match 53.3% vs majority-class 62.7% =
            # −9.4% deficit. See /tmp/hedge_forecaster_audit.py).
            #
            # Outcome ledgers preserved (compute_forecast still writes full
            # state to logs/hedge_forecast_outcomes_*.jsonl + paired ledger).
            try:
                from connectors import hedge_forecaster as _hf
                if (now - _intel_hedge_last_compute_ts) >= _INTEL_HEDGE_INTERVAL_S:
                    _intel_hedge_last_compute_ts = now
                    fc = _time_intel('hedge_fc', _hf.compute_forecast, 'QQQ')
                    if _socketio is not None and fc and not fc.get('reason'):
                        try:
                            # Strip predictive fields. Keep only observable.
                            descriptive = {
                                'ticker':                fc.get('ticker'),
                                'spot':                  fc.get('spot'),
                                'velocity_per_sec':      fc.get('velocity_per_sec'),
                                'velocity_cv':           fc.get('velocity_cv'),
                                'velocity_stable':       fc.get('velocity_stable'),
                                'distance_to_flip':      fc.get('distance_to_flip'),
                                'gamma_flip':            fc.get('gamma_flip'),
                                'hp_gamma_shares_1pct':  fc.get('hp_gamma_shares_1pct'),
                                'observed_5min_actual':  fc.get('observed_5min_actual'),
                                'observed_5min_count':   fc.get('observed_5min_count'),
                                'data_ts':               fc.get('data_ts'),
                                'server_time':           fc.get('server_time'),
                                'kind':                  'observable_state',
                                # NOTE: forecasts dict deliberately omitted —
                                # 5/15/30 min predicted shares + side + confidence
                                # had zero predictive edge in audit.
                            }
                            _socketio.emit('intel:hedge_forecast', descriptive)
                        except Exception as _e:
                            log.debug(f"[INTEL] hedge_forecast emit err: {_e}")
            except Exception as _he:
                log.debug(f"[INTEL] hedge_forecast compute err: {_he}")

            # ── SPX-vs-QQQ Divergence (Phase 4) ─────────────────────────
            # 10s cadence — cross-asset dealer-regime comparator.
            # Verdict ∈ {ALIGNED_BULL/BEAR, DIVERGENT_REGIME/MAGNITUDE,
            # NEUTRAL, NO_DATA}. Outcome ledger:
            # logs/spx_qqq_divergence_outcomes_YYYYMMDD.jsonl
            try:
                from connectors import spx_qqq_divergence as _sqd
                if (now - _intel_div_last_compute_ts) >= _INTEL_DIV_INTERVAL_S:
                    _intel_div_last_compute_ts = now
                    div_state = _time_intel('spx_qqq_div', _sqd.compute_state)
                    # Always emit so frontend can render NO_DATA placeholder
                    # (gives operator visibility into when SPX feed lags vs QQQ)
                    if _socketio is not None and div_state:
                        try:
                            _socketio.emit('intel:spx_qqq_divergence', div_state)
                        except Exception as _e:
                            log.debug(f"[INTEL] spx_qqq_div emit err: {_e}")
            except Exception as _de:
                log.debug(f"[INTEL] spx_qqq_div compute err: {_de}")

            # ── VIX Regime / Cross-Asset Vol Dashboard (Phase 5) ────────
            # 10s cadence — VIX-family + cross-asset vol regime classifier.
            # Regime ∈ {CALM_CONTANGO, NORMAL, TECH_DIVERGENCE, ELEVATED,
            #           STRESS_CONTANGO, STRESS_BACKWARDATION,
            #           VVIX_DIVERGENCE, NO_DATA}.
            # Outcome ledger: logs/vix_regime_outcomes_YYYYMMDD.jsonl
            try:
                from connectors import vix_term_structure as _vts
                if (now - _intel_vix_last_compute_ts) >= _INTEL_VIX_INTERVAL_S:
                    _intel_vix_last_compute_ts = now
                    vts_state = _time_intel('vix_term', _vts.compute_state)
                    if _socketio is not None and vts_state:
                        try:
                            _socketio.emit('intel:vix_term', vts_state)
                        except Exception as _e:
                            log.debug(f"[INTEL] vix_term emit err: {_e}")
            except Exception as _ve:
                log.debug(f"[INTEL] vix_term compute err: {_ve}")

            # ── 0DTE Wing Tracker (Phase 6) ─────────────────────────────
            # 5s cadence — far-OTM call/put aggressor flow on QQQ 0DTE.
            # Regime ∈ {NORMAL, ACTIVE, EXTREME, NO_DATA}.
            # Wing prints arrive in real-time via _on_tradier_timesale hook;
            # this loop pushes the periodic snapshot with regime classification.
            # Outcome ledger: logs/wing_outcomes_YYYYMMDD.jsonl
            try:
                from connectors import wing_tracker as _wt
                if (now - _intel_wing_last_compute_ts) >= _INTEL_WING_INTERVAL_S:
                    _intel_wing_last_compute_ts = now
                    wt_state = _time_intel('wing', _wt.compute_state)
                    if _socketio is not None and wt_state:
                        try:
                            _socketio.emit('intel:wing_update', wt_state)
                        except Exception as _e:
                            log.debug(f"[INTEL] wing_update emit err: {_e}")
            except Exception as _we:
                log.debug(f"[INTEL] wing compute err: {_we}")

            # ── Gamma Skyline (Phase 7) ─────────────────────────────────
            # 5s cadence — per-strike dealer Γ$ "skyline" with walls overlay.
            # Pure visualization push; reads existing greek_surface +
            # wall_signals data. Outcome ledger: logs/gamma_skyline_outcomes_YYYYMMDD.jsonl
            try:
                from connectors import gamma_skyline as _gs
                if (now - _intel_skyline_last_compute_ts) >= _INTEL_SKYLINE_INTERVAL_S:
                    _intel_skyline_last_compute_ts = now
                    sky_state = _time_intel('gamma_skyline', _gs.compute_state)
                    if _socketio is not None and sky_state:
                        try:
                            _socketio.emit('intel:gamma_skyline', sky_state)
                        except Exception as _e:
                            log.debug(f"[INTEL] gamma_skyline emit err: {_e}")
            except Exception as _ge:
                log.debug(f"[INTEL] gamma_skyline compute err: {_ge}")

            # ── Dealer Warehouse Quality (Phase 8) ──────────────────────
            # 10s cadence — per-strike commitment scorer (COMMITTED / PHANTOM /
            # ACTIVE / INACTIVE). Reads mm_attribution._capture (posted/caught)
            # populated by Schwab OPTIONS_BOOK (120-contract budget) + Tradier
            # prints. Outcome ledger: logs/dealer_warehouse_outcomes_YYYYMMDD.jsonl
            try:
                from connectors import dealer_warehouse as _dw
                if (now - _intel_warehouse_last_compute_ts) >= _INTEL_WAREHOUSE_INTERVAL_S:
                    _intel_warehouse_last_compute_ts = now
                    wh_state = _time_intel('dealer_warehouse', _dw.compute_state)
                    if _socketio is not None and wh_state:
                        try:
                            _socketio.emit('intel:dealer_warehouse', wh_state)
                        except Exception as _e:
                            log.debug(f"[INTEL] dealer_warehouse emit err: {_e}")
            except Exception as _whe:
                log.debug(f"[INTEL] dealer_warehouse compute err: {_whe}")

            # ── Event Calendar (Phase 10B) ──────────────────────────────
            # 60 min cadence — earnings + macro events that drive vol regime.
            # Reads data/event_calendar.json (operator-maintained). Surfaces
            # `vol_warning` flag when high-impact event within 24hr.
            try:
                from connectors import event_calendar as _ec
                if (now - _intel_events_last_compute_ts) >= _INTEL_EVENTS_INTERVAL_S:
                    _intel_events_last_compute_ts = now
                    ev_state = _ec.compute_state()
                    if _socketio is not None and ev_state:
                        try:
                            _socketio.emit('intel:events', ev_state)
                        except Exception as _e:
                            log.debug(f"[INTEL] events emit err: {_e}")
            except Exception as _ee:
                log.debug(f"[INTEL] events compute err: {_ee}")

            # Per-iteration summary: log if total compute time was >500ms
            # OR every 6th iteration (~30s) regardless, so we have a visible
            # baseline. Sorted descending — biggest hog at the front.
            _iter_total_ms = (time.perf_counter() - _iter_t0) * 1000.0
            if _intel_compute_times and (_iter_total_ms > 500.0 or _intel_iter_count % 6 == 0):
                _ranked = sorted(_intel_compute_times.items(), key=lambda x: -x[1])
                _summary = ', '.join(f'{n}={ms:.0f}ms' for n, ms in _ranked)
                _level = 'warning' if _iter_total_ms > 500.0 else 'info'
                if _level == 'warning':
                    log.warning(f"[INTEL-PERF] iter#{_intel_iter_count} total={_iter_total_ms:.0f}ms — {_summary}")
                else:
                    log.info(f"[INTEL-PERF] iter#{_intel_iter_count} total={_iter_total_ms:.0f}ms — {_summary}")

            time.sleep(5)
        except Exception as _outer:
            log.warning(f"[INTEL] outer loop error: {_outer}")
            time.sleep(30)


def _flush_loop():
    """Fixed-cadence flush loop for both mark_batch and trade_batch.

    Runs every 50ms regardless of Schwab's inbound burst rhythm. This is the
    primary latency win — prior code piggy-backed flushes on `_on_options_quote`
    which delivered bursts at ~1Hz, adding ~950ms avg delay on top of Schwab's
    own latency.

    gevent-aware: uses `_socketio.sleep()` so it cooperates with the gevent
    hub rather than blocking the OS thread.
    """
    log.info("[SCHWAB-BRIDGE] Flush loop started (50ms cadence)")
    _last_dpc_flush = 0.0
    while _bridge_running:
        try:
            _flush_option_mark_batch()
            _flush_option_trade_batch()
            # dealer_print_capture forward-sample writer — gated to ~500ms since
            # deadlines are all ≥5s out; no benefit to hammering every 50ms.
            _now = time.time()
            if _now - _last_dpc_flush >= 0.5:
                try:
                    from connectors import dealer_print_capture as _dpc
                    _dpc.flush_pending()
                except Exception:
                    pass
                _last_dpc_flush = _now
            # MM Attribution event drain — every 50ms tick. Batch emit.
            # Plus contract_state push (gated to 250ms internally) for any
            # symbols the pane is watching over the socket.
            try:
                from connectors import mm_attribution as _mma
                _mma.flush_events_to_socket(_socketio)
                _mma.flush_contract_states_to_socket(_socketio)
            except Exception:
                pass
            # Wall signals: continuation + fade scores. Self-gated internally
            # to EMIT_INTERVAL_SEC (1Hz) — the signal changes slowly.
            try:
                from connectors import wall_signals as _ws
                _ws.flush_to_socket(_socketio)
            except Exception:
                pass
            # Signal ledger: finalize outcomes for any entries that have
            # aged past a window (5/10/15min). Cadence is self-gated below.
            try:
                if (_now - getattr(_flush_loop, '_last_slg_tick', 0.0)) >= 30.0:
                    from connectors import signal_ledger as _slg
                    _slg.finalize_outcomes(_now)
                    _flush_loop._last_slg_tick = _now   # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception as e:
            log.debug(f"[SCHWAB-BRIDGE] flush_loop iteration error: {e}")
        if _socketio is not None:
            _socketio.sleep(0.05)
        else:
            time.sleep(0.05)
    log.info("[SCHWAB-BRIDGE] Flush loop exited")


def _compute_walls_for(ticker: str) -> dict:
    """Compute walls for one ticker from _per_ticker_gex. Returns:
        {put_wall, call_wall, flip, gamma_put_wall, gamma_call_wall}
    OI walls = strike with max aggregate open interest across all expirations.
    Gamma walls = strike with max aggregate dollar-gamma concentration.
    Flip = strike where dealer net gamma (-call_gamma + put_gamma) crosses zero.
    Returns {} if too thin.

    _per_ticker_gex[ticker] is keyed by full OCC sym_key, so we sum per-strike.
    """
    per_contract = _per_ticker_gex.get(ticker)
    if not per_contract or len(per_contract) < 3:
        return {}
    # Aggregate contracts into per-strike sums across all expirations
    call_oi    : dict = {}
    put_oi     : dict = {}
    call_gamma : dict = {}
    put_gamma  : dict = {}
    for entry in per_contract.values():
        strike = entry.get('strike')
        if not strike:
            continue
        side = entry.get('side')
        g    = entry.get('gamma_dollars', 0) or 0
        oi   = entry.get('oi',            0) or 0
        if side == 'call':
            call_oi[strike]    = call_oi.get(strike, 0)    + oi
            call_gamma[strike] = call_gamma.get(strike, 0) + g
        elif side == 'put':
            put_oi[strike]    = put_oi.get(strike, 0)    + oi
            put_gamma[strike] = put_gamma.get(strike, 0) + g
    strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
    if len(strikes) < 3:
        return {}
    if not any(call_oi.values()) and not any(put_oi.values()):
        return {}
    call_wall = max(call_oi, key=call_oi.get) if call_oi else 0.0
    put_wall  = max(put_oi,  key=put_oi.get)  if put_oi  else 0.0
    # Gamma walls: strike with max |dollar gamma| concentration per side.
    # This is the "0DTHero-style" wall — where dealer hedging pressure actually
    # lives, not where contracts happen to have been opened historically. The
    # GEX chart in the Feb-3-2025 article showed the QQQ $510 put wall as a
    # gamma concentration, not an OI concentration.
    gamma_call_wall = max(call_gamma, key=call_gamma.get) if any(call_gamma.values()) else 0.0
    gamma_put_wall  = max(put_gamma,  key=put_gamma.get)  if any(put_gamma.values())  else 0.0
    # Signed dealer gamma per strike.
    # Convention (verified against SpotGamma, Tier1Alpha):
    #   Calls: dealers SHORT (sold to retail as income/covered calls) → negative
    #   Puts:  dealers LONG  (bought protection from retail)           → positive
    # dealer_net > 0 at a strike = dealers NET LONG gamma there
    #   → hedge is negative-feedback (sell rallies, buy dips); stabilizing
    # dealer_net < 0 at a strike = dealers NET SHORT gamma there
    #   → hedge is positive-feedback (buy rallies, sell dips); destabilizing
    dealer_net = {
        k: -call_gamma.get(k, 0) + put_gamma.get(k, 0)
        for k in strikes
    }
    flip = 0.0
    for i in range(1, len(strikes)):
        s0, s1 = strikes[i-1], strikes[i]
        g0, g1 = dealer_net[s0], dealer_net[s1]
        if g0 * g1 < 0:
            denom = abs(g0) + abs(g1)
            frac = abs(g0) / denom if denom > 0 else 0.5
            flip = s0 + frac * (s1 - s0)
            break
    # Per-strike dealer_net at each wall's strike — this is what tells us
    # which direction dealers will hedge if spot crosses that wall.
    dealer_net_at_call        = float(dealer_net.get(call_wall,        0) or 0)
    dealer_net_at_put         = float(dealer_net.get(put_wall,         0) or 0)
    dealer_net_at_gamma_call  = float(dealer_net.get(gamma_call_wall,  0) or 0)
    dealer_net_at_gamma_put   = float(dealer_net.get(gamma_put_wall,   0) or 0)
    # Normalization peak (|max| across all strikes) — used by the UI to scale
    # 0-1 intensities. Structural max, not a magnitude threshold.
    dealer_net_peak = max((abs(v) for v in dealer_net.values()), default=0.0)
    return {
        'put_wall':        float(put_wall)        if put_wall        else 0.0,
        'call_wall':       float(call_wall)       if call_wall       else 0.0,
        'flip':            float(flip)            if flip            else 0.0,
        'gamma_put_wall':  float(gamma_put_wall)  if gamma_put_wall  else 0.0,
        'gamma_call_wall': float(gamma_call_wall) if gamma_call_wall else 0.0,
        'dealer_net_at_call':       dealer_net_at_call,
        'dealer_net_at_put':        dealer_net_at_put,
        'dealer_net_at_gamma_call': dealer_net_at_gamma_call,
        'dealer_net_at_gamma_put':  dealer_net_at_gamma_put,
        'dealer_net_peak':          float(dealer_net_peak),
    }


def _push_key_level_walls():
    """Push per-ticker walls to AlertEngine for Key Level detection.
    Runs on the same cadence as _maybe_emit_zones (every 5s when dirty)."""
    global _last_key_level_push
    now = time.time()
    if now - _last_key_level_push < 5.0:
        return
    try:
        from connectors.alert_engine import get_engine
        eng = get_engine()
        if eng is None:
            return
        for ticker in _KEY_LEVEL_INDEX_TICKERS:
            walls = _compute_walls_for(ticker)
            if walls:
                eng.update_walls(ticker, walls)
        _last_key_level_push = now
    except Exception as e:
        log.debug(f"[KEY-LEVEL] push failed: {e}")


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
        # Pick the zero-crossing CLOSEST to spot (not first-from-bottom), because
        # sparse OI can create spurious far-OTM crossings that distort the flip.
        gamma_flip = ndx_spot
        best_dist = float('inf')
        for i in range(1, len(sorted_strikes)):
            s0, s1 = sorted_strikes[i-1], sorted_strikes[i]
            g0 = dealer_net_gex.get(s0, 0)
            g1 = dealer_net_gex.get(s1, 0)
            if g0 * g1 < 0:
                denom = abs(g0) + abs(g1)
                frac = abs(g0) / denom if denom > 0 else 0.5
                candidate = s0 + frac * (s1 - s0)
                d = abs(candidate - ndx_spot)
                if d < best_dist:
                    best_dist = d
                    gamma_flip = candidate

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
        # Max pain — O(N) via prefix/suffix sums (was O(N²)). For each strike K:
        #   pain(K) = Σ_{S<K} put_oi[S]*(K-S) + Σ_{S>K} call_oi[S]*(S-K)
        #          = K*putCumBelow - putSCumBelow + callSCumAbove - K*callCumAbove
        max_pain_strike = ndx_spot
        n = len(sorted_strikes)
        if n > 0:
            put_cum      = [0.0] * (n + 1)   # strict prefix of put_oi
            put_s_cum    = [0.0] * (n + 1)   # strict prefix of put_oi * strike
            call_cum     = [0.0] * (n + 1)   # strict suffix of call_oi
            call_s_cum   = [0.0] * (n + 1)   # strict suffix of call_oi * strike
            for i in range(n):
                s_i = sorted_strikes[i]
                put_cum[i + 1]   = put_cum[i] + total_put_oi.get(s_i, 0)
                put_s_cum[i + 1] = put_s_cum[i] + total_put_oi.get(s_i, 0) * s_i
            for i in range(n - 1, -1, -1):
                s_i = sorted_strikes[i]
                call_cum[i]   = call_cum[i + 1] + total_call_oi.get(s_i, 0)
                call_s_cum[i] = call_s_cum[i + 1] + total_call_oi.get(s_i, 0) * s_i
            min_pain = float("inf")
            for i in range(n):
                K = sorted_strikes[i]
                pain = (K * put_cum[i] - put_s_cum[i]) + (call_s_cum[i + 1] - K * call_cum[i + 1])
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
                    # ── Phase 20B: VIX-term HMM regime in zone_update ──
                    zone_data['vix_hmm_regime'] = vol_state.get('vix_hmm_regime')
                    zone_data['vix_hmm_state'] = vol_state.get('vix_hmm_state')
                    zone_data['vix_hmm_warm'] = vol_state.get('vix_hmm_warm', False)
                    zone_data['vix_hmm_inputs_present'] = vol_state.get(
                        'vix_hmm_inputs_present', False)
                    zone_data['vix_hmm_prob_contango'] = vol_state.get(
                        'vix_hmm_prob_contango', 0)
                    zone_data['vix_hmm_prob_transition'] = vol_state.get(
                        'vix_hmm_prob_transition', 0)
                    zone_data['vix_hmm_prob_backwardation'] = vol_state.get(
                        'vix_hmm_prob_backwardation', 0)
                    # Sample to A/B ledger every ~30s (zone emit cadence is 5s)
                    if not hasattr(_maybe_emit_zones, '_hmm_ab_last_log_ts'):
                        _maybe_emit_zones._hmm_ab_last_log_ts = 0
                    _now_ts = time.time()
                    if _now_ts - _maybe_emit_zones._hmm_ab_last_log_ts >= 30.0:
                        try:
                            _vol_surface.append_hmm_ab_record()
                            _maybe_emit_zones._hmm_ab_last_log_ts = _now_ts
                        except Exception:
                            pass
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

        # Wall-signals: refresh QQQ walls using the PER-TICKER aggregate
        # (`_per_ticker_gex['QQQ']`), NOT the zone_update payload. Reason:
        # `_live_gex` is polluted with SPX / SPY option strikes (the shared
        # `_on_options_quote` handler writes all tickers into one dict), so
        # `zone_data['underlying_*_wall']` is actually max-OI across QQQ∪SPX∪SPY.
        # Observed in live debug — call_wall came back as SPX 7000 / 7200
        # strikes while true QQQ call wall was 660.
        # `_compute_walls_for('QQQ')` buckets by underlying and matches the
        # /api/walls REST endpoint exactly.
        try:
            from connectors import wall_signals as _ws
            _qqq_walls = _compute_walls_for('QQQ')
            # Phase 10D — pass live hp_gamma_shares_1pct for authoritative regime
            _hp_g = 0.0
            try:
                if _greek_surface is not None and _latest_qqq > 0:
                    _hp_state = _greek_surface.export_hedge_pressure(_latest_qqq) or {}
                    _hp_g = float((_hp_state.get('totals') or {}).get('hp_gamma_shares_1pct', 0) or 0)
            except Exception:
                pass
            _ws.update_walls(
                'QQQ',
                put_wall=float(_qqq_walls.get('put_wall') or 0),
                call_wall=float(_qqq_walls.get('call_wall') or 0),
                gamma_flip=float(_qqq_walls.get('flip') or 0),
                # Signed-gamma context (Schwab LEVELONE_OPTIONS greeks + OI,
                # convention: dealers short calls, long puts). Enables regime
                # detection + expected_direction per cross.
                gamma_call_wall=float(_qqq_walls.get('gamma_call_wall') or 0),
                gamma_put_wall=float(_qqq_walls.get('gamma_put_wall') or 0),
                dealer_net_at_call=float(_qqq_walls.get('dealer_net_at_call') or 0),
                dealer_net_at_put=float(_qqq_walls.get('dealer_net_at_put') or 0),
                dealer_net_at_gamma_call=float(_qqq_walls.get('dealer_net_at_gamma_call') or 0),
                dealer_net_at_gamma_put=float(_qqq_walls.get('dealer_net_at_gamma_put') or 0),
                dealer_net_peak=float(_qqq_walls.get('dealer_net_peak') or 0),
                hp_gamma_shares_1pct=_hp_g,
            )
        except Exception:
            pass

        # Backstop: drain option_mark batch in case option stream went quiet
        # after-hours (flush normally rides on _on_options_quote cadence).
        _flush_option_mark_batch()

        # Push per-ticker walls (SPX/SPY/QQQ) into AlertEngine for Key Level
        # detection. Computed from _per_ticker_gex (parallel to _live_gex).
        _push_key_level_walls()

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


# ═══════════════════════════════════════════════════════════════════════════
# OPTIONS_BOOK Phase 1 — raw snapshot logger (no signal derivation yet).
# Writes one JSONL line per book update to logs/options_book_YYYYMMDD.jsonl.
# Purpose: capture empirical distributions of mm_count/size/quote-pull
# magnitudes so downstream detector thresholds can be calibrated from data
# instead of guessed. One hour of captures ≈ 20-100 MB depending on activity.
# ═══════════════════════════════════════════════════════════════════════════
_ob_log_fh = None
_ob_log_date = ''
_ob_logged_sample = False
_ob_log_count = 0


def _ob_log_path() -> str:
    from datetime import datetime as _dt
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'logs',
        f'options_book_{_dt.now().strftime("%Y%m%d")}.jsonl'
    )


def _on_options_book(data):
    """Handle OPTIONS_BOOK (L2 on option contracts) — raw snapshot logger."""
    global _ob_log_fh, _ob_log_date, _ob_logged_sample, _ob_log_count
    symbol = data.get('symbol', '?')
    bids = data.get('bids', []) or []
    asks = data.get('asks', []) or []
    if not bids and not asks:
        return

    # Roll file on date change (UTC is fine — daily granularity).
    from datetime import datetime as _dt
    today = _dt.now().strftime('%Y%m%d')
    if _ob_log_fh is None or today != _ob_log_date:
        try:
            if _ob_log_fh is not None:
                _ob_log_fh.close()
        except Exception:
            pass
        # 2026-05-07 FIX: was buffering=1 (line-buffered = flushes to disk on
        # every newline). At RTH options activity (~67 OPTIONS_BOOK records/sec),
        # that's 67 disk writes/sec blocking the gevent loop and producing
        # sustained 100%+ CPU consumed by syscalls + I/O wait. Switch to 64KB
        # buffer; lose at most 64KB on crash, which is acceptable for this
        # historical-calibration log.
        _ob_log_fh = open(_ob_log_path(), 'a', buffering=65536)
        _ob_log_date = today

    # Compact per-level shape: [price, size, mm_count, [mm_ids]].
    def _pack(levels):
        out = []
        for lvl in levels[:5]:  # top 5 levels is enough for quote-pull analysis
            mms = [m.get('id', '') for m in (lvl.get('market_makers') or [])[:8]]
            out.append([
                lvl.get('price', 0),
                lvl.get('size', 0),
                lvl.get('mm_count', 0),
                mms,
            ])
        return out

    # 2026-05-08 FIX: same stale-ts clamp as the mm_attribution call below.
    # Schwab's data['timestamp'] for OPTIONS_BOOK is the order's post-time,
    # which can be days old for long-resting quotes. Without clamping, the
    # raw options_book disk log lands old-dated rows in today's file (less
    # destructive than mm_events because it's keyed by date in filename via
    # _ob_log_path() at file-rotation time, but still inaccurate for analysis).
    _now_ms = int(time.time() * 1000)
    _ts_ms = data.get('timestamp') or 0
    if not _ts_ms or _ts_ms < _now_ms - 60_000:
        _ts_ms = _now_ms
    rec = {
        'ts': _ts_ms,
        'sym': symbol,
        'b': _pack(bids),
        'a': _pack(asks),
    }
    try:
        _ob_log_fh.write(json.dumps(rec, separators=(',', ':')) + '\n')
        _ob_log_count += 1
    except Exception:
        pass

    # Feed raw snapshot into dealer_print_capture so on_print() can
    # join each timesale against the book that was live at print time.
    try:
        from connectors import dealer_print_capture as _dpc
        _dpc.update_book(symbol, bids, asks)
    except Exception:
        pass

    # MM Attribution — per-exchange structural differ.
    # 2026-05-08 FIX: clamp event ts to "now" when Schwab's reported
    # timestamp (data['timestamp']) is more than 60s in the past. Schwab's
    # OPTIONS_BOOK field 1 is the order's original-post time, not the
    # snapshot delivery time. For long-resting quotes (some posted days
    # ago) this routed mm_events into wrong-date files — discovered when
    # mm_events_20260429.jsonl grew to 6.2GB on 2026-05-08 because new
    # subscriptions surfaced long-resting orders with 04/29 timestamps.
    try:
        from connectors import mm_attribution as _mma
        _now_ms = int(time.time() * 1000)
        _ts_ms = data.get('timestamp') or 0
        # Treat as stale if > 60s in the past (max realistic WS delivery jitter)
        if not _ts_ms or _ts_ms < _now_ms - 60_000:
            _ts_ms = _now_ms
        _mma.on_book_update(
            symbol,
            bids,
            asks,
            _ts_ms / 1000.0,
        )
    except Exception:
        pass

    # One-shot sample to confirm data shape is right.
    if not _ob_logged_sample:
        _ob_logged_sample = True
        log.info(f"[OPTIONS_BOOK] sample ({symbol}) bid_levels={len(bids)} ask_levels={len(asks)}")
        for side, levels in [('BID', bids[:2]), ('ASK', asks[:2])]:
            for lvl in levels:
                mms = [m.get('id', '?') for m in (lvl.get('market_makers') or [])[:5]]
                log.info(f"  {side} {lvl.get('price')}: sz={lvl.get('size')} mm_count={lvl.get('mm_count')} MMs={mms}")

    # Heartbeat every 500 records so we can see ingest rate in logs.
    if _ob_log_count % 500 == 0:
        log.info(f"[OPTIONS_BOOK] wrote {_ob_log_count} snapshots so far")


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

        # Prefer Schwab's REAL delta from the LEVELONE cache if this contract
        # is already subscribed (SPX/SPY/QQQ/NDX/VIX). Only fall back to
        # linear estimation when the contract isn't in our subscription list.
        # Same for real bid/ask — use Schwab's quotes when available.
        #
        # DTE note: Schwab sends dte=null for index options AND for 0DTE on
        # equity options. Symbol-parsed dte (from the OSI YYMMDD slice) is
        # ALWAYS correct, so we always derive dte from the symbol and only
        # prefer Schwab-provided delta/bid/ask.
        real_delta = None
        real_bid = real_ask = None
        try:
            _levelone_cache = getattr(_on_options_quote, '_sym_cache', {})
            cached = _levelone_cache.get(sym)
            if cached:
                _rd = cached.get('delta')
                if _rd is not None and _rd != 0:
                    real_delta = float(_rd)
                _rb = cached.get('bid')
                if _rb is not None and _rb > 0:
                    real_bid = float(_rb)
                _ra = cached.get('ask')
                if _ra is not None and _ra > 0:
                    real_ask = float(_ra)
        except Exception:
            pass

        # Always derive dte from symbol (Schwab's null dte is useless).
        _est_delta, dte = _estimate_delta_and_dte(sym, curr_px, spot)
        if real_delta is not None:
            delta = real_delta
            delta_source = 'levelone_cache'
        else:
            delta = _est_delta
            delta_source = 'estimated'
        if dte != 0:
            continue  # only process 0DTE from screener (non-0DTE we get via LEVELONE)

        prev = _screener_prev.get(sym)
        new_vol = curr_vol - (prev['volume'] if prev else curr_vol)
        px_diff = curr_px - (prev['lastPrice'] if prev else curr_px)
        if new_vol <= 0 or not prev:
            # First-observation: cache + skip (need prior to compute delta)
            _screener_prev[sym] = {'volume': curr_vol, 'lastPrice': curr_px}
            continue

        # SPY OVER-COUNT FIX (2026-05-01):
        # Cap new_vol at 50K contracts. The screener delivers per-symbol
        # cumulative volume; new_vol is the inter-poll delta. When polls have
        # any gap (server lag, screener service pause, network jitter), the
        # delta can balloon to 100K-600K contracts representing a batch of
        # many real trades aggregated together. Treating that as a single
        # "trade" injects a phantom delta-notional spike worth tens of $B.
        # Skip the print and reset prev so we don't keep amplifying the
        # downstream cumulative — the lost flow is reflected in the equity
        # tape and per-strike Greeks anyway.
        if new_vol > 50_000:
            _screener_prev[sym] = {'volume': curr_vol, 'lastPrice': curr_px}
            _scn_drops = getattr(_screener_to_accumulator, '_oversize_drops', 0)
            _screener_to_accumulator._oversize_drops = _scn_drops + 1
            if _scn_drops < 5:
                log.warning(
                    f"[SCREENER-OVERSIZE] DROPPED inter-poll new_vol={new_vol} "
                    f"sym={sym} curr_vol={curr_vol} prev_vol={prev['volume']} "
                    f"— polling gap, not a single trade")
            continue

        # Prefer real bid/ask from LEVELONE cache; fall back to synthesized
        # 1¢ spread ONLY for tickers we don't subscribe to via LEVELONE.
        if real_bid is not None and real_ask is not None and real_bid > 0 and real_ask > 0:
            bid, ask = real_bid, real_ask
        elif px_diff > 0:   # tick up → buyer-initiated
            bid, ask = curr_px - 0.01, curr_px
        elif px_diff < 0: # tick down → seller-initiated
            bid, ask = curr_px, curr_px + 0.01
        else:
            bid, ask = curr_px, curr_px  # ambiguous; accumulator will skip signed

        # Track delta-source ratio (diagnostic). ~90%+ should be 'levelone_cache'
        # for tickers we subscribe to; 'estimated' only for market-wide names.
        _srcdiag = getattr(_screener_to_accumulator, '_src_diag', {'levelone_cache': 0, 'estimated': 0})
        _srcdiag[delta_source] = _srcdiag.get(delta_source, 0) + 1
        _screener_to_accumulator._src_diag = _srcdiag

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

    # Always capture the first 3 full raw items + key-set union for diagnostic
    # so we can empirically see EVERY field Schwab sends (including delta/bid/ask
    # if they exist).
    _rawdiag = getattr(_on_screener_option, '_rawdiag', None)
    if _rawdiag is None:
        _rawdiag = {'samples': [], 'all_keys_seen': set()}
        _on_screener_option._rawdiag = _rawdiag
    for it in data.get('items', []):
        if isinstance(it, dict):
            _rawdiag['all_keys_seen'].update(it.keys())
            if len(_rawdiag['samples']) < 3:
                _rawdiag['samples'].append(dict(it))

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

    # ── REMOVED 2026-05-01: screener→FlowAccumulator bridge ────────────
    # Original rationale (line 5867 of `_screener_to_accumulator`): "Fills
    # the 0DTE gap that LEVELONE silently drops." Obsolete since:
    #
    #   1. Phase 21 (2026-04-29) added explicit Greek source routing.
    #      Every Tradier print is routed schwab_ws / schwab_rest / bsm with
    #      ZERO cache misses verified live. 0DTE prints route correctly.
    #
    #   2. Tradier WS now subscribes 7,148 QQQ OCC symbols across 5 conns
    #      (full chain incl LEAPS). Per-print events flow real-time.
    #
    #   3. The bridge was wired to ALL 5 SCREENER_OPTION subscriptions
    #      (VOLUME_0, VOLUME_5, TRADES_5, PCT_UP_5, PCT_DOWN_5) but read
    #      `item['volume']` blindly without checking sort_field. Each sort
    #      has different volume semantics (cumulative vs 5-min vs trade
    #      count), so `_screener_prev` got stomped between sort firings,
    #      injecting phantom 100K-600K-contract "trades" into the flow
    #      accumulator. Caused $-461B SPY signed_0dte over-count earlier.
    #
    # Screener still feeds the unusual-activity ranker via
    # `screener_option_update` socketio emit above (different code path,
    # alerts-only, no accumulator pollution). That path stays.
    pass


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
            sym = item.get('symbol', '')
            lp  = _safe_num(item.get('lastPrice', 0))
            # Cache spot so SCREENER_OPTION's 0DTE bridge can compute delta
            # for tickers we don't otherwise subscribe to (expands flow beyond
            # our 9 LEVELONE_EQUITIES names toward broader S&P coverage).
            if sym and lp > 0:
                _latest_spot_by_ticker[sym] = lp
            alerts.append({
                'symbol':        sym,
                'description':   item.get('description', ''),
                'volume':        _safe_num(item.get('volume', 0)),
                'trades':        _safe_num(item.get('trades', 0)),
                'lastPrice':     lp,
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


# ═══════════════════════════════════════════════════════════════════════
#  Hedge-Pressure getters (Phase A)
# ═══════════════════════════════════════════════════════════════════════
def get_hedge_pressure_state(ticker: str) -> dict:
    """Expose per-strike hedge-pressure state for the REST layer.

    Thin wrapper over GreekSurface.export_hedge_pressure(). Returns an
    empty dict when the surface has not been populated or when the
    requested ticker is not QQQ (the only ticker the greek surface
    currently tracks in live streams).
    """
    try:
        if _greek_surface is None:
            return {}
        t = (ticker or '').upper()
        if t != 'QQQ':
            return {}
        spot = _tape_spot_for(t) or _latest_qqq
        return _greek_surface.export_hedge_pressure(float(spot or 0))
    except Exception as _e:  # defensive — never crash the REST layer
        log.warning(f"[HP] get_hedge_pressure_state error: {_e}")
        return {}


def get_alignment_for_contract(contract_sym: str) -> dict:
    """Phase C — WITH-MM vs AGAINST-MM alignment for a specific QQQ contract.

    Composes already-live state (wall_signals regime + most-recent cross +
    greek_surface per-strike dn_gamma) into a four-row alignment table +
    one-line recommendation for the given OSI contract. Zero new computation.

    Neutral conditions (structural, not magnitude):
      - regime_sign == 0                         → "regime undefined"
      - expected_underlying_direction is None    → "no recent wall cross"
      - last-cross age > (now - session_open)/10 → "cross stale vs session"
      - dn_gamma at contract's strike == 0       → "no dealer γ at strike"

    Alignment truth table (composed, not guessed):
      side=C · dir=up    → long_call=with, short_call=against
      side=C · dir=down  → long_call=against, short_call=with
      side=P · dir=up    → long_put=against, short_put=with
      side=P · dir=down  → long_put=with, short_put=against
    """
    try:
        from connectors import wall_signals as _ws
        from connectors.wall_signals import _parse_option_sym as _parse

        parsed = _parse(contract_sym or '')
        if not parsed:
            return {'error': 'unparseable contract_sym', 'contract_sym': contract_sym}

        ticker = (parsed.get('ticker') or '').upper()
        strike = float(parsed.get('strike') or 0.0)
        option_side = (parsed.get('side') or '').upper()  # 'C' or 'P'

        # Pull unified wall-signals state (regime + walls + crosses + spot).
        state = _ws.get_state(ticker)
        regime      = state.get('regime') or 'unknown'
        regime_sign = int(state.get('regime_sign') or 0)
        gamma_flip  = float(state.get('gamma_flip') or 0.0)
        spot        = float(state.get('spot') or 0.0)
        spot_ts     = float(state.get('spot_ts') or 0.0)

        # Find the most-recent just_crossed wall across walls[] (newest age wins).
        last_cross = None
        expected_direction = None
        for w in (state.get('walls') or []):
            if w.get('just_crossed'):
                age = w.get('cross_age_sec')
                ed  = w.get('expected_direction')
                if age is None:
                    continue
                if last_cross is None or age < last_cross.get('age_sec', 1e18):
                    last_cross = {
                        'wall':      w.get('name'),
                        'direction': w.get('cross_direction'),
                        'age_sec':   float(age),
                    }
                    if ed:
                        expected_direction = ed

        # Per-strike hedge-pressure snapshot → dn_gamma at our contract's strike.
        hp = _greek_surface.export_hedge_pressure(spot or 0) if _greek_surface is not None else {}
        dn_g_at_strike = 0.0
        dn_g_norm = 0.0
        strikes = hp.get('strikes') or []
        if strikes and strike > 0:
            best = min(strikes, key=lambda r: abs(float(r.get('K', 0)) - strike))
            if abs(float(best.get('K', 0)) - strike) <= 1.0:  # structural match
                dn_g_at_strike = float(best.get('dn_gamma', 0.0) or 0.0)
                # Normalize against the peak |dn_gamma| across strikes.
                peak = 0.0
                for r in strikes:
                    a = abs(float(r.get('dn_gamma', 0.0) or 0.0))
                    if a > peak:
                        peak = a
                if peak > 0:
                    dn_g_norm = max(-1.0, min(1.0, dn_g_at_strike / peak))

        # Staleness check — ratio of (now − session_open) is structural, no magic #.
        from connectors import mm_attribution as _mm
        session_open_ts = float(getattr(_mm, '_session_open_ts', 0.0) or 0.0)
        now = time.time()
        session_elapsed = max(1.0, now - session_open_ts) if session_open_ts > 0 else 0.0
        stale = False
        if last_cross is not None and session_elapsed > 0:
            stale = last_cross['age_sec'] > (session_elapsed / 10.0)

        # Neutral reason (first-matching wins).
        neutral_reason = None
        if regime_sign == 0:
            neutral_reason = 'regime undefined (missing gamma_flip or spot)'
        elif expected_direction is None:
            neutral_reason = 'no recent wall cross'
        elif stale:
            neutral_reason = 'last cross stale relative to session elapsed'
        elif dn_g_at_strike == 0.0:
            neutral_reason = 'no dealer γ exposure at this strike'

        # Alignment truth table.
        if neutral_reason is not None:
            alignment_long_call  = 'neutral'
            alignment_short_call = 'neutral'
            alignment_long_put   = 'neutral'
            alignment_short_put  = 'neutral'
            recommended = 'neutral'
        else:
            # expected_direction is 'up' or 'down'
            if expected_direction == 'up':
                alignment_long_call  = 'with'
                alignment_short_call = 'against'
                alignment_long_put   = 'against'
                alignment_short_put  = 'with'
            else:  # 'down'
                alignment_long_call  = 'against'
                alignment_short_call = 'with'
                alignment_long_put   = 'with'
                alignment_short_put  = 'against'
            # Recommendation for THIS contract (compose option_side).
            if option_side == 'C':
                recommended = 'long' if alignment_long_call == 'with' else 'short'
            elif option_side == 'P':
                recommended = 'long' if alignment_long_put  == 'with' else 'short'
            else:
                recommended = 'neutral'

        return {
            'contract_sym': contract_sym,
            'ticker':       ticker,
            'strike':       strike,
            'option_side':  option_side,
            'regime':       regime,
            'regime_sign':  regime_sign,
            'gamma_flip':   gamma_flip,
            'spot':         spot,
            'spot_ts':      spot_ts,
            'last_cross':   last_cross,
            'expected_underlying_direction': expected_direction,
            'dn_gamma_at_strike':            dn_g_at_strike,
            'dn_gamma_normalized':           dn_g_norm,
            'alignment_long_call':   alignment_long_call,
            'alignment_short_call':  alignment_short_call,
            'alignment_long_put':    alignment_long_put,
            'alignment_short_put':   alignment_short_put,
            'recommended_for_this_contract': recommended,
            'neutral_reason': neutral_reason,
            'ts': now,
        }
    except Exception as _e:
        log.warning(f"[HP] get_alignment_for_contract error: {_e}")
        return {'error': str(_e), 'contract_sym': contract_sym}


def get_hedge_pressure_by_exchange(ticker: str) -> dict:
    """Per-exchange hedge-pressure rollup (Phase B).

    Composes three already-live datasets:
      1. GreekSurface.export_hedge_pressure('QQQ') → per-strike dn_gamma
      2. mm_attribution.watched_symbols()           → OSI contracts currently
                                                      pushing state
      3. mm_attribution.contract_state(sym)         → per-exchange posted_share
                                                      and caught_at_top_pct for
                                                      that contract

    For each contract: weight its dealer-gamma at its strike by each venue's
    posted / caught share, then accumulate per-exchange.

    Returns:
        {
          'exchanges': [
            {'exch', 'hp_gamma_posted', 'hp_gamma_caught', 'diff',
             'contracts_touched'}, ...
          ],
          'spot', 'ts', 'ticker',
        }
    """
    try:
        from connectors import mm_attribution as _mm
        from connectors.wall_signals import _parse_option_sym as _parse

        t = (ticker or '').upper()
        if t != 'QQQ' or _greek_surface is None:
            return {'exchanges': [], 'spot': 0.0, 'ts': time.time(), 'ticker': t}

        spot = _tape_spot_for(t) or _latest_qqq
        hp = _greek_surface.export_hedge_pressure(float(spot or 0))
        strikes = hp.get('strikes') or []
        # Fast lookup: strike → dn_gamma at that strike (nearest match).
        dn_by_strike = {}
        for row in strikes:
            try:
                dn_by_strike[float(row['K'])] = float(row.get('dn_gamma', 0.0) or 0.0)
            except Exception:
                pass

        # Use rank_contracts (all booked-and-tracked contracts) — NOT
        # watched_symbols() which is a socket-room roster (only populated when
        # a browser tab subscribes). rank_contracts filters to booked only.
        try:
            ranked = _mm.rank_contracts(metric='events', limit=500)
            syms = [r.get('sym') for r in ranked if r.get('sym')]
        except Exception:
            syms = _mm.watched_symbols()  # defensive fallback
        # Accumulators per exchange (signed, so a long-γ strike and short-γ
        # strike at different venues can partially cancel — that's structural).
        agg: dict = {}
        touched: dict = {}

        for sym in syms:
            parsed = _parse(sym)
            if not parsed:
                continue
            if (parsed.get('ticker') or '').upper() != t:
                continue
            K = float(parsed.get('strike') or 0.0)
            if K <= 0:
                continue
            # Snap to the closest strike we have dealer-γ for (handles pennies /
            # half-dollar mismatches between OSI strike and surface keys).
            if not dn_by_strike:
                continue
            best_K = min(dn_by_strike.keys(), key=lambda k: abs(k - K))
            if abs(best_K - K) > 1.0:  # structural: ignore if >1$ away (no match)
                continue
            dn_g = dn_by_strike[best_K]
            if dn_g == 0.0:
                continue

            try:
                st = _mm.contract_state(sym)
            except Exception:
                continue

            for r in (st.get('capture') or []):
                exch = (r.get('exch') or '').upper()
                if not exch:
                    continue
                posted_share = float(r.get('posted_share')    or 0.0)
                caught_share = float(r.get('caught_at_top_pct') or 0.0)
                row = agg.setdefault(exch, {
                    'hp_gamma_posted': 0.0,
                    'hp_gamma_caught': 0.0,
                })
                row['hp_gamma_posted'] += posted_share * dn_g
                row['hp_gamma_caught'] += caught_share * dn_g
                touched[exch] = touched.get(exch, 0) + 1

        # Convert raw dealer-γ dollar exposure → rehedge shares per +1% move.
        # Same transform as per-strike HP (greek_surface.export_hedge_pressure):
        #   hp_gamma_shares_1pct = -dn_g / spot
        # Sign convention: +shares = dealers must BUY; -shares = must SELL.
        # Matches wall_signals.expected_direction and per-strike HP for
        # internally-consistent units across the pane.
        _spot_f = float(spot or 0.0)
        _scale  = (-1.0 / _spot_f) if _spot_f > 0 else 0.0

        exchanges = []
        for exch, row in agg.items():
            posted_sh = row['hp_gamma_posted'] * _scale
            caught_sh = row['hp_gamma_caught'] * _scale
            exchanges.append({
                'exch':               exch,
                'hp_gamma_posted':    posted_sh,
                'hp_gamma_caught':    caught_sh,
                'diff':               caught_sh - posted_sh,
                'contracts_touched':  touched.get(exch, 0),
            })
        # Sort by absolute posted magnitude descending — structural, not a cutoff.
        exchanges.sort(key=lambda e: -abs(e['hp_gamma_posted']))

        return {
            'exchanges': exchanges,
            'spot':      float(spot or 0.0),
            'ts':        time.time(),
            'ticker':    t,
        }
    except Exception as _e:
        log.warning(f"[HP] get_hedge_pressure_by_exchange error: {_e}")
        return {'exchanges': [], 'spot': 0.0, 'ts': time.time(), 'ticker': (ticker or '').upper()}

