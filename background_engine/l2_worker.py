from __future__ import annotations
import math

"""
L2 Worker — Background daemon that streams TopStepX Level 2 data
and feeds computed signals into server.py's inference cache.

Run this separately from the Flask server:
    python background_engine/l2_worker.py

Or import and call start_l2_worker() from server.py at startup.
"""

import sys
import os
import time
import logging
import threading
from collections import deque, defaultdict
from dotenv import load_dotenv

# Load .env from project root
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_HERE, ".env"))

# Allow imports from project root
sys.path.insert(0, _HERE)

# ALPHA_ENABLED gates the 8 frameworks/* stat-mech modules. They feed the
# Alpha dashboard (currently hidden). Leave off in prod to save CPU.
_ALPHA_ENABLED = os.getenv("ALPHA_ENABLED", "0") == "1"

import copy
from datetime import datetime, timedelta, timezone
from background_engine.topstepx_connector import TopStepXConnector

import json

log = logging.getLogger("l2_worker")

class _TelemetryLogger:
    def __init__(self, filename="/tmp/altaris_telemetry.jsonl"):
        self.filename = filename
        
    def log_event(self, symbol, event_type, metadata):
        try:
            with open(self.filename, 'a') as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "sym": symbol,
                    "type": event_type,
                    "data": metadata
                }) + "\n")
        except Exception:
            pass

_telemetry = _TelemetryLogger()

# ── Credentials (from .env) ──────────────────────────────────────────────────
USERNAME = os.getenv("TOPSTEPX_USERNAME", "")
API_KEY  = os.getenv("TOPSTEPX_API_KEY",  "")

# ── Symbols to stream ────────────────────────────────────────────────────────
SYMBOLS = ["NQ"]

# ── OHLC Candle Engine ────────────────────────────────────────────────────────
# Aggregates tick-by-tick trades into OHLC candles for multiple timeframes.
CANDLE_TIMEFRAMES = {
    "5s": 5, "15s": 15, "30s": 30,
    "1m": 60, "3m": 180, "200s": 200, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400,
}
CANDLE_MAX = 10080  # max candles stored per TF per symbol (7 days × 1440 min for 1m)

# Per-symbol tick sizes for bubble profile price quantization
TICK_SIZES = {
    "NQ": 0.25,   # NQ tick = $0.25
    "GC": 0.10,   # Gold tick = $0.10
}
DEFAULT_TICK_SIZE = 0.25

# {symbol: {tf: deque([{t,o,h,l,c,v}, ...])}}
_CANDLES: dict[str, dict[str, deque]] = defaultdict(
    lambda: {tf: deque(maxlen=CANDLE_MAX) for tf in CANDLE_TIMEFRAMES}
)
# Current (incomplete) candle being built: {symbol: {tf: {t,o,h,l,c,v}}}
_CURRENT_CANDLE: dict[str, dict[str, dict]] = defaultdict(dict)

# ── Socket.IO reference (set by server.py at startup) ──
_socketio = None
_last_emit_time: dict = {}  # throttle: {"symbol:tf": timestamp}
_EMIT_MIN_INTERVAL = 0.05   # max 20 emits/sec per symbol/tf (was 150ms)
_ACTIVE_TF = "1m"           # legacy singleton — kept for back-compat, do not rely on
# 2026-05-06: User views charts on 30s / 1m / 3m / 200s / 5m. Always enrich
# these so bars BEFORE a chart-tf-switch don't render with empty
# delta_div/depth_deltas/absorption fields. The set is unioned with currently-
# subscribed client TFs by handle_connect/disconnect/subscribe in server.py.
_PREFERRED_ENRICHED_TFS: set = {"30s", "1m", "3m", "200s", "5m"}
_ACTIVE_TFS: set = set(_PREFERRED_ENRICHED_TFS)   # set union of all client-requested TFs (multi-client safe)

# ── Reconnect gap-fill tracking ──
_LAST_TRADE_TS: dict[str, float] = {}   # {symbol: unix_ts of last trade}
_connector_ref = None                    # set by start() for gap-fill access

# ── Refill Speed Tracking (for VP absorption quality) ──
_REFILL_TRACKER: dict = {}    # {symbol: {price_str: {hit_ts, avg_ms, count, classification}}}
_LAST_DOM_DEPTH: dict = {}    # {symbol: {price_str: depth}} — previous DOM snapshot for diff

# ── Max Book Size Seen (BUG FIX 2026-05-03) ──────────────────────────────
# Trade events read DOM AFTER the trade has consumed the inside price level,
# so reading `dom.asks[trade_price]` returns 0 (already eaten). True
# absorption needs to know the WALL DEPTH that was sitting at the price
# BEFORE the trade hit it. We solve this with a per-symbol per-price
# rolling-max tracker, updated on EVERY L2 depth update (at ~10-50Hz).
# At trade time, we read this tracker — it remembers the largest book
# seen at the price within the recent window, regardless of whether the
# current snapshot still shows the wall.
#
# Format: {symbol: {price_str: {'depth': float, 'set_at': epoch_sec}}}
# Decay: entries older than _MAX_BOOK_TTL_S are reset (stale walls cleared)
# Cap:   trimmed to top _MAX_BOOK_CAP entries per symbol on each update
_MAX_BOOK_SEEN: dict = {}
_MAX_BOOK_TTL_S = 60.0          # rolling 60s window — wall must be recent
_MAX_BOOK_CAP   = 1000          # per-symbol cap (NQ touches ~50-200 prices/min)

# ── Bubble Profile Persistence ──
# Saves bp data to disk each time a 1m candle closes, loads on startup.
# Without this, server restarts wipe all bubble profiles and the chart
# shows zero bubbles until new trades accumulate (~20-30 min warmup).
_BP_PERSIST_TF = "1m"
_BP_LOG_DIR = os.path.join(_HERE, "logs")
os.makedirs(_BP_LOG_DIR, exist_ok=True)  # ensure dir exists once at import time

def _bp_persist_path(symbol: str) -> str:
    date_str = time.strftime("%Y%m%d")
    return os.path.join(_BP_LOG_DIR, f"bp_{symbol}_{date_str}.jsonl")

def _bp_save_candle(symbol: str, frozen_candle: dict) -> None:
    """Append a closed candle's bp to today's persist file (1m only)."""
    bp = frozen_candle.get("bp")
    if not bp:
        return
    try:
        record = {"t": frozen_candle["t"], "bp": bp}
        with open(_bp_persist_path(symbol), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # non-fatal

def _bp_load_today(symbol: str) -> dict:
    """Load today's persisted bp records into {t_int: bp_dict}."""
    bp_map: dict = {}
    try:
        with open(_bp_persist_path(symbol)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    bp_map[int(rec["t"])] = rec["bp"]
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return bp_map

def _bp_restore_candles(symbol: str) -> None:
    """Re-inject persisted bp into already-frozen _CANDLES. Called at startup."""
    bp_map = _bp_load_today(symbol)
    if not bp_map:
        return
    candle_deque = _CANDLES[symbol].get(_BP_PERSIST_TF)
    if not candle_deque:
        return
    restored = 0
    for candle in candle_deque:
        t = int(candle.get("t", 0))
        if t in bp_map and not candle.get("bp"):
            candle["bp"] = bp_map[t]
            restored += 1
    if restored:
        log.info("[BP-RESTORE] %s: restored bp into %d/%d frozen candles from disk",
                 symbol, restored, len(candle_deque))

# ── Detection callback (for EdgeDetector cross-asset forwarding) ──
_detection_callback = None  # callable(detection_type, detection_data, symbol)

# ── Cross-asset context provider (set by EdgeDetector to push GEX/stop cluster data back) ──
_cross_asset_provider = None  # callable(symbol) -> {gex_zone, stop_cluster_near, mm_bias, ...}

def set_cross_asset_provider(provider_fn):
    """Called by EdgeDetector to provide cross-asset context back to l2_worker.
    
    provider_fn(symbol) should return:
        {'gex_zone': str, 'gex_factor': float, 'near_stop_cluster': bool,
         'stop_cluster_price': float, 'stop_cluster_side': str,
         'mm_pull_bias': int}  # -1=SHORT, 0=neutral, +1=LONG
    """
    global _cross_asset_provider
    _cross_asset_provider = provider_fn
    log.info("Cross-asset context provider registered")

# ── Empirical stickiness distribution (replaces hardcoded 0.3 threshold) ──
_STICKINESS_DIST: dict = defaultdict(lambda: deque(maxlen=500))
# {symbol: deque of recent stickiness values}

# ── Empirical absorption-score distribution (Sprint 2) ──
# Replaces hardcoded score >= 2.0 (WALL) / 1.0 (ABS) cutoffs with session
# percentiles. WALL = top 25% of observed score×waves joint signal.
_ABS_SCORE_DIST: dict = defaultdict(lambda: deque(maxlen=500))
_ABS_WAVES_DIST: dict = defaultdict(lambda: deque(maxlen=500))

def set_socketio(sio):
    """Called by server.py to inject the SocketIO instance for real-time push."""
    global _socketio
    _socketio = sio
    log.info("Socket.IO reference set for real-time candle push")

def set_detection_callback(callback):
    """Register a callback for NQ detection events (sweep, absorption).
    Called by schwab_bridge or server.py to wire EdgeDetector.
    callback(detection_type: str, detection_data: dict, symbol: str)
    """
    global _detection_callback
    _detection_callback = callback
    log.info("Detection callback registered for cross-asset forwarding")

# ── Trade scoring callback (for tape glow via EdgeDetector) ──
_trade_score_callback = None  # callable(symbol, volume, side, price, timestamp) -> dict|None

# ── NQ signal check callback (for NQ-native edge signals) ──
_nq_signal_callback = None  # callable(symbol) — calls EdgeDetector.check_nq_signals

def set_nq_signal_callback(callback):
    """Register EdgeDetector.check_nq_signals() for NQ-native signal emission."""
    global _nq_signal_callback
    _nq_signal_callback = callback
    log.info("NQ signal callback registered for TopStepX-native signals")

def set_trade_score_callback(callback):
    """Register EdgeDetector.score_trade() for regime-adaptive tape glow scoring.
    Called by schwab_bridge to wire EdgeDetector.
    callback(symbol: str, volume: int, side: str, price: float, timestamp: float) -> dict|None
    """
    global _trade_score_callback
    _trade_score_callback = callback
    log.info("Trade score callback registered for tape glow")


# ══════════════════════════════════════════════════════════════════════════════
# REGIME CLASSIFIER — Options-driven market regime state
# ══════════════════════════════════════════════════════════════════════════════
# Exposed via _CURRENT_REGIME; consumed by server.py/api_alpha and downstream
# logging. Updated by update_regime() below whenever options data refreshes.

# Current regime state — updated by update_regime() called from server.py
_CURRENT_REGIME = "transition"  # safe default until first options refresh
_REGIME_DATA = {
    "regime": "transition",
    "spot": 0, "gamma_flip": 0, "total_gex": 0,
    "call_wall": 0, "put_wall": 0,
    "flow_ratio": 0.5, "iv_rv_spread": 0,
    "updated_at": 0,
}


def _classify_regime(spot: float, gamma_flip: float, total_gex: float,
                     call_wall: float, put_wall: float,
                     flow_ratio: float = 0.5,
                     iv_rv_spread: float = 0.0) -> str:
    """Classify market regime from options signals.

    Returns one of: pin_mean_revert, long_gamma_stable, transition,
    short_gamma_volatile, crash_tail_risk
    """
    if spot <= 0 or gamma_flip <= 0:
        return "transition"  # no data yet

    dist_to_flip = abs(spot - gamma_flip) / spot
    is_long_gamma = spot > gamma_flip

    # Near the flip → regime uncertain
    if dist_to_flip < 0.005:
        return "transition"

    # Short gamma regimes
    if not is_long_gamma:
        if total_gex < -1e9 and iv_rv_spread > 10 and spot < put_wall:
            return "crash_tail_risk"
        return "short_gamma_volatile"

    # Long gamma regimes
    if total_gex > 0.5e9 and iv_rv_spread < 0 and put_wall < spot < call_wall:
        return "pin_mean_revert"

    return "long_gamma_stable"


def update_regime(spot: float, gamma_flip: float, total_gex: float,
                  call_wall: float, put_wall: float,
                  flow_ratio: float = 0.5,
                  iv_rv_spread: float = 0.0):
    """Called by server.py when options data refreshes.
    Updates the module-level regime state for all detection functions.
    """
    global _CURRENT_REGIME, _REGIME_DATA

    new_regime = _classify_regime(
        spot, gamma_flip, total_gex, call_wall, put_wall,
        flow_ratio, iv_rv_spread
    )

    old_regime = _CURRENT_REGIME
    _CURRENT_REGIME = new_regime
    _REGIME_DATA = {
        "regime": new_regime,
        "spot": spot, "gamma_flip": gamma_flip, "total_gex": total_gex,
        "call_wall": call_wall, "put_wall": put_wall,
        "flow_ratio": flow_ratio, "iv_rv_spread": iv_rv_spread,
        "updated_at": time.time(),
    }

    if new_regime != old_regime:
        log.info(f"[REGIME] {old_regime} → {new_regime} | "
                 f"spot={spot:.0f} flip={gamma_flip:.0f} "
                 f"gex={total_gex/1e6:.0f}M")


# ══════════════════════════════════════════════════════════════════════════════
# σ-ADAPTIVE MARKET STATS ENGINE — live threshold computation
# ══════════════════════════════════════════════════════════════════════════════
# Tracks rolling distributions of trade sizes and per-level absorption samples.
# After warmup (~500 trades), exposes session-rolling percentile cutoffs used
# by the absorption scorer.

_ADAPTIVE_WARMUP = 500  # min trades before switching to adaptive mode

_MARKET_STATS: dict = defaultdict(lambda: {
    "clip_sizes": deque(maxlen=1000),         # all recent trade sizes
    "total_trades": 0,                        # total trades since startup
    # Absorption tier sampling (adaptive tier cutoffs)
    "refill_samples": deque(maxlen=500),      # refill_ratio per level snapshot (t_traded >= 10)
    "traded_samples": deque(maxlen=500),      # t_traded per level snapshot
    "pull_samples":   deque(maxlen=300),      # pull_ratios >= 0.2 (spoof significance)
    "last_stats_ts": 0,                       # throttle: recompute only every 1s
    "last_adaptive_log": 0,                   # throttle: log adaptive thresholds every 60s
})


def _update_market_stats(symbol: str, volume: int, side: str, timestamp: float):
    """Called on every trade. Feeds Kalman CV and the clip-size buffer."""
    ms = _MARKET_STATS[symbol]
    ms["total_trades"] += 1
    ms["clip_sizes"].append(volume)

    # Build CV samples: every 5th trade, compute CV of last 5 clips and push into Kalman
    if ms["total_trades"] % 5 == 0 and len(ms["clip_sizes"]) >= 5:
        last5 = list(ms["clip_sizes"])[-5:]
        m5 = sum(last5) / 5
        if m5 > 0:
            v5 = sum((x - m5) ** 2 for x in last5) / 5
            cv5 = math.sqrt(v5) / m5
            _KALMAN_CV[symbol].update(cv5)

    # Throttle timestamp — used by _get_adaptive_thresholds for log pacing
    if timestamp - ms["last_stats_ts"] < 1.0:
        return
    ms["last_stats_ts"] = timestamp



def _get_adaptive_thresholds(symbol: str, side: str = "b") -> dict:
    """Session-rolling percentile cutoffs for the absorption scorer.

    During warmup (<500 trades) returns an empty dict — callers fall back to
    hardcoded defaults in `.get(...)` calls. After warmup, exposes refill /
    traded / pull percentile cutoffs computed from live per-level samples.
    """
    ms = _MARKET_STATS[symbol]
    if ms["total_trades"] < _ADAPTIVE_WARMUP:
        return {}

    absorb_cutoffs: dict = {}
    if len(ms["refill_samples"]) >= 100:
        rs = sorted(ms["refill_samples"])
        ts = sorted(ms["traded_samples"])
        n_r = len(rs); n_t = len(ts)
        absorb_cutoffs["refill_p90"] = rs[int(n_r * 0.90)]  # FORTRESS refill cutoff
        absorb_cutoffs["refill_p75"] = rs[int(n_r * 0.75)]  # SOLID
        absorb_cutoffs["refill_p50"] = rs[int(n_r * 0.50)]  # HELD
        absorb_cutoffs["traded_p75"] = ts[int(n_t * 0.75)]  # FORTRESS volume
        absorb_cutoffs["traded_p50"] = ts[int(n_t * 0.50)]  # SOLID volume
        absorb_cutoffs["traded_p25"] = ts[int(n_t * 0.25)]  # HELD volume
    if len(ms["pull_samples"]) >= 30:
        ps = sorted(ms["pull_samples"])
        absorb_cutoffs["pull_p90"] = ps[int(len(ps) * 0.90)]  # FAKE spoof cutoff

    now = ms["last_stats_ts"]
    if now - ms["last_adaptive_log"] >= 60 and absorb_cutoffs:
        ms["last_adaptive_log"] = now
        ab_r90 = absorb_cutoffs.get("refill_p90")
        ab_r75 = absorb_cutoffs.get("refill_p75")
        ab_r50 = absorb_cutoffs.get("refill_p50")
        ab_t75 = absorb_cutoffs.get("traded_p75")
        ab_p90 = absorb_cutoffs.get("pull_p90")
        log.info(
            f"[ADAPTIVE] {symbol} regime={_CURRENT_REGIME} | "
            f"absorb[F≥{ab_r90:.2f}/{ab_t75:.0f} S≥{ab_r75:.2f} "
            f"H≥{ab_r50:.2f} FAKE≥{ab_p90:.2f}]"
        )

    return absorb_cutoffs



# ═══════════════════════════════════════════════════════════════════════════════
# VOLUME CLOCK — τ(t) = ⌊(1/V_bucket) × Σv_i⌋
# ═══════════════════════════════════════════════════════════════════════════════
# Replaces chronological time with volume-synchronized time.
# Time only advances when trades happen. Each "volume tick" represents
# V_bucket contracts traded, making all detection windows comparable
# regardless of time-of-day (lunchtime vs market open).
#
# ZERO HARDCODED BUCKET SIZES. The bucket size is the P50 (median) of
# observed 5-second volume windows from the live tape. It self-calibrates
# after ~60 seconds of trading data.

class VolumeClock:
    """Volume-synchronized clock per symbol.

    τ(t) = floor(cumulative_volume / bucket_size)

    Bucket size is EMPIRICAL: the median volume traded per 5-second
    interval, computed from a rolling window of live trade data.
    No hardcoded numbers.
    """

    _CALIBRATION_INTERVAL = 5.0  # compute bucket from 5s volume intervals
    _CALIBRATION_MIN_SAMPLES = 12  # need 12 intervals (~60s of data)

    def __init__(self):
        self._cumulative_volume = 0
        self._tau = 0
        self._trades_in_bucket = 0
        self._total_trades = 0
        self._last_bucket_time = 0
        self._bucket_durations = deque(maxlen=100)

        # Empirical bucket calibration
        self._bucket_size = 0       # 0 = not calibrated yet
        self._calibrated = False
        self._interval_volumes = deque(maxlen=200)  # observed 5s volume totals
        self._current_interval_vol = 0
        self._current_interval_start = 0

    @property
    def tau(self):
        return self._tau

    @property
    def warm(self):
        """True when empirical bucket is calibrated AND we have enough ticks."""
        return self._calibrated and self._tau >= len(self._interval_volumes) // 2

    @property
    def bucket_size(self):
        return self._bucket_size if self._bucket_size > 0 else 1

    def tick(self, volume, timestamp):
        """Process a trade. Self-calibrates bucket size from live data."""
        self._cumulative_volume += volume
        self._total_trades += 1
        self._trades_in_bucket += 1

        # ── Empirical bucket calibration ──
        if self._current_interval_start == 0:
            self._current_interval_start = timestamp

        self._current_interval_vol += volume

        # Close 5-second interval and record volume
        if timestamp - self._current_interval_start >= self._CALIBRATION_INTERVAL:
            self._interval_volumes.append(self._current_interval_vol)
            self._current_interval_vol = 0
            self._current_interval_start = timestamp

            # Recalibrate bucket from P50 of observed intervals
            if len(self._interval_volumes) >= self._CALIBRATION_MIN_SAMPLES:
                sorted_vols = sorted(self._interval_volumes)
                p50_idx = len(sorted_vols) // 2
                new_bucket = max(1, sorted_vols[p50_idx])
                self._bucket_size = new_bucket
                if not self._calibrated:
                    self._calibrated = True
                    import logging
                    logging.getLogger('l2_worker').info(
                        f'[VCLOCK] Calibrated: bucket={new_bucket} '
                        f'(P50 of {len(sorted_vols)} intervals, '
                        f'range={sorted_vols[0]}-{sorted_vols[-1]})'
                    )

        # ── Tick forward ──
        if self._bucket_size > 0:
            new_tau = self._cumulative_volume // self._bucket_size
            if new_tau > self._tau:
                if self._last_bucket_time > 0:
                    duration = timestamp - self._last_bucket_time
                    if duration > 0:
                        self._bucket_durations.append(duration)
                self._last_bucket_time = timestamp
                self._trades_in_bucket = 0
                self._tau = new_tau

        return self._tau

    def get_avg_bucket_duration(self):
        if not self._bucket_durations:
            return 0
        return sum(self._bucket_durations) / len(self._bucket_durations)

    def get_stats(self):
        return {
            'tau': self._tau,
            'cumulative_volume': self._cumulative_volume,
            'bucket_size': self._bucket_size,
            'total_trades': self._total_trades,
            'warm': self.warm,
            'calibrated': self._calibrated,
            'calibration_samples': len(self._interval_volumes),
            'avg_bucket_sec': round(self.get_avg_bucket_duration(), 2),
        }

# Per-symbol Volume Clock instances (self-calibrating, no hardcoded bucket)
_VOLUME_CLOCKS: dict = defaultdict(VolumeClock)

# ══════════════════════════════════════════════════════════════════════════════
# 1D KALMAN FILTER — Order flow variance estimation (from State-Space spec)
# ══════════════════════════════════════════════════════════════════════════════
# Treats true order flow CV as a hidden state. Dynamically weights new
# observations against internal estimate. Reacts instantly to structural
# regime shifts while ignoring noise spikes.
#
# Prediction:  x̂(k|k-1) = x̂(k-1|k-1)       [constant velocity model]
#              P(k|k-1) = P(k-1|k-1) + Q
# Update:      K(k) = P(k|k-1) / (P(k|k-1) + R)
#              x̂(k|k) = x̂(k|k-1) + K(k) * (z(k) - x̂(k|k-1))
#              P(k|k) = (1 - K(k)) * P(k|k-1)

class KalmanCV:
    """1D Kalman Filter for order flow Coefficient of Variation."""

    def __init__(self, Q=0.001, R_init=0.02):
        self.x = 0.55       # initial state estimate (typical random CV)
        self.P = 0.1        # initial uncertainty (wide)
        self.Q = Q           # process noise (regime change rate)
        self.R = R_init      # measurement noise (updated from data)
        self._n = 0
        self._sum_sq_innov = 0.0  # for adaptive R estimation

    def predict(self):
        """A priori step: project state forward."""
        # x̂(k|k-1) = x̂(k-1|k-1)  (constant model, no drift)
        # P(k|k-1) = P(k-1|k-1) + Q
        self.P += self.Q

    def update(self, z):
        """A posteriori step: incorporate new observation z (measured CV).

        Returns the Kalman Gain K for diagnostics.
        """
        self.predict()

        # Innovation (residual)
        innovation = z - self.x

        # Adaptive R: track squared innovations to estimate measurement noise
        self._n += 1
        self._sum_sq_innov += innovation ** 2
        if self._n >= 10:
            # R ≈ variance of innovations (Mehra 1970 approach)
            self.R = max(0.001, self._sum_sq_innov / self._n - self.P)

        # Kalman Gain
        K = self.P / (self.P + self.R) if (self.P + self.R) > 0 else 0.5

        # State update
        self.x += K * innovation

        # Covariance update
        self.P = (1 - K) * self.P

        return K

    @property
    def state(self):
        """Current filtered CV estimate."""
        return self.x

    @property
    def uncertainty(self):
        """Current state uncertainty (sqrt(P))."""
        return math.sqrt(max(0, self.P))

    def get_stats(self):
        return {
            'kalman_cv': round(self.x, 4),
            'kalman_P': round(self.P, 6),
            'kalman_R': round(self.R, 6),
            'kalman_K': round(self.P / (self.P + self.R) if (self.P + self.R) > 0 else 0, 4),
            'kalman_n': self._n,
        }


# Per-symbol Kalman filter instances
_KALMAN_CV: dict = defaultdict(KalmanCV)


# ══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE KALMAN FILTER — Order Flow Imbalance (from combined spec §1A)
# ══════════════════════════════════════════════════════════════════════════════
# Replaces hardcoded σ noise floor with a dynamic state-space filter.
# R_τ is coupled to rolling OFI variance (Welford O(1) online).
# Q_τ is coupled to realized volatility (dual-adaptive per critique).
# When noise rises, Kalman Gain K→0 — system ignores spurious flow.
# When true signal rises (FOMC), gain opens because both Q and R adapt.
#
# Warm-up: outputs `ready: false` for first 30 observations.
# Params are LOCKED in backend — no frontend exposure.


# ── V2 Engine accessor (forwarding shim) ─────────────────────────────────────
# The canonical AdaptiveKalmanOFI + HawkesBranchingRatio implementations are
# defined below near _ensure_v2_engines (they use __slots__ + EWMV and are
# the only definitions in this module).
# _KALMAN_OFI provides O(1) symbol-keyed access for _compute_absorption_scores.
class _KalmanOFIProxy:
    """Proxy so _KALMAN_OFI[sym].theta reads from _V2_KALMAN without requiring
    _V2_KALMAN to be populated yet (lazy init on first trade)."""
    def __getitem__(self, sym):
        return _V2_KALMAN.get(sym)
    def get(self, sym, default=None):
        return _V2_KALMAN.get(sym, default)

_KALMAN_OFI = _KalmanOFIProxy()

# Per-symbol VPIN engine instances
# BUG FIX: Was defaultdict(VPINEngine) — defaultdict creates engine only on
# __getitem__ (e.g. _VPIN_ENGINES[sym]), NOT on __contains__ (sym in _VPIN_ENGINES).
# So the guard `if symbol in _VPIN_ENGINES` was ALWAYS False. Engine never ran.
# Fix: plain dict — lazy init in _ensure_v2_engines() with calibrated bucket sizes.
try:
    from connectors.vpin_engine import VPINEngine as _VPINEngine
    _VPIN_ENGINES: dict = {}   # {symbol: VPINEngine} — populated by _ensure_v2_engines
    _VPIN_AVAILABLE = True
except ImportError:
    _VPIN_ENGINES = {}
    _VPIN_AVAILABLE = False



# ── Rolling Trade Size Tracker (for adaptive min clip) ──
# {symbol: deque of recent trade sizes, max 500}
_TRADE_SIZE_HISTORY: dict = defaultdict(lambda: deque(maxlen=500))


# ── Sweep Detection Constants ──
_SWEEP_MIN_LEVELS     = 3       # min consecutive price levels swept
_SWEEP_WINDOW_SEC     = 0.200   # 200ms window for sweep
_SWEEP_MIN_VOLUME     = 100     # total swept volume threshold (~$2M notional on NQ)

# ── Detection State ──
# _SWEEP_TRACKER: {symbol: [(timestamp, price, volume, side), ...]}
_SWEEP_TRACKER: dict = defaultdict(deque)
# Detected results attached to current candle: {symbol: {tf: {sweeps: []}}}
_DETECT_RESULTS: dict = defaultdict(lambda: defaultdict(dict))

# ── Big Print Detection (signal-only volume bubbles) ──
# Rolling per-symbol deque of (ts, size). 5-min window provides a MEASURED
# baseline; size ≥ P90 of this deque qualifies as "big" — adapts to regime
# (overnight low-activity vs RTH open) within 5 min, no fixed lot threshold.
_PRINT_RING_SEC       = 300.0    # CONFIGURED 5-min rolling baseline window
_PRINT_RING_MIN_N     = 30       # STRUCTURAL — need ≥30 samples to compute P90
_PRINT_RING: dict = defaultdict(deque)
_PRINT_RING_LOCK = threading.Lock()

# ── Cumulative Delta Divergence Constants ──
_DIV_LOOKBACK_CANDLES = 20       # rolling window to find swing highs/lows
_DIV_MIN_PRICE_MOVE   = 5.0      # minimum price difference for swing (20 ticks on NQ)
_DIV_MIN_DELTA_GAP    = 50       # minimum delta gap to trigger

# ── Delta Divergence State ──
# {symbol: {tf: [{"t": boundary, "high": h, "low": l, "delta": d}, ...]}}
_DELTA_HISTORY: dict = defaultdict(lambda: defaultdict(list))
# {symbol: {tf: cumulative_delta}}
_CUM_DELTA: dict = defaultdict(lambda: defaultdict(float))

# ── Momentum Ignition Constants ──
_IGN_MIN_TRADES       = 15       # min trades in window (was 8 — too sensitive for NQ)
_IGN_WINDOW_SEC       = 2.0      # 2-second window
_IGN_MAX_CLIP_SIZE    = 3        # max individual trade size (only flag 1-lot spam)
_IGN_MAX_TOTAL        = 15       # max total volume (micro-probing only)
_IGN_REVERSAL_SEC     = 30.0     # reversal confirmation window
_IGN_MIN_PRICE_SPREAD = 3.0      # min price range in points (12 ticks on NQ)

# ── Momentum Ignition State ──
# {symbol: [(timestamp, price, volume, side), ...]}
_IGN_TRACKER: dict = defaultdict(deque)
# {symbol: [{"direction": "up"|"down", "prices": [...], "ts": T, "start_price": P}, ...]}
_IGN_ACTIVE: dict = defaultdict(list)

# ── Spoof Detection Constants ──
# Regime-adaptive minimum order size to track as potential spoof.
# Volatile markets naturally have large orders appearing/vanishing — higher floor.
_SPOOF_MIN_SIZE_TABLE = {
    "pin_mean_revert":      50,    # tight range, smaller orders matter
    "long_gamma_stable":    75,
    "transition":           100,
    "short_gamma_volatile": 200,   # volatile: large orders flash routinely
    "crash_tail_risk":      300,   # extreme: only flag truly massive spoofs
}
_SPOOF_MAX_LIFETIME   = 1.0      # max seconds before considered spoof (was 3.0 — normal MM refreshes in 1-3s)
_SPOOF_MIN_OCCUR      = 3        # min occurrences to trigger (need pattern, not one-off)

# ── Spoof Detection State ──
# {symbol: {price_str: {"size": V, "first_seen": T, "side": "bid"|"ask"}}}
_DOM_PREV: dict = defaultdict(dict)
# {symbol: {price_str: [{"fake_size": V, "side": s, "ts": T}, ...]}}
_SPOOF_TRACKER: dict = defaultdict(lambda: defaultdict(list))




def _detect_sweep(symbol: str, price: float, volume: int,
                  timestamp: float, side: str):
    """Track consecutive same-side trades across price levels.
    Detect sweep when:
    - 3+ consecutive price levels hit within 200ms
    - All same side (all buys or all sells)
    - Total volume >= 30 contracts
    Returns sweep dict if detected, else None.
    """
    if side == "n":
        return None

    tracker = _SWEEP_TRACKER[symbol]
    tracker.append((timestamp, price, volume, side))

    # Prune entries older than the sweep window
    cutoff = timestamp - _SWEEP_WINDOW_SEC
    while tracker and tracker[0][0] < cutoff:
        tracker.popleft()

    # Need at least N entries
    if len(tracker) < _SWEEP_MIN_LEVELS:
        return None

    # Check: all same side in the window
    sides = [t[3] for t in tracker]
    if len(set(sides)) != 1:
        return None

    # Check: distinct price levels (consecutive level sweep)
    prices = sorted(set(t[1] for t in tracker))
    if len(prices) < _SWEEP_MIN_LEVELS:
        return None

    # Check: total volume threshold
    total_vol = sum(t[2] for t in tracker)
    if total_vol < _SWEEP_MIN_VOLUME:
        return None

    # Sweep detected! Clear tracker to avoid re-firing
    # NQ multiplier = $20/point, GC = $100/oz
    _CONTRACT_MULT = {"NQ": 20.0, "GC": 100.0}
    mid_price = prices[len(prices) // 2]
    mult = _CONTRACT_MULT.get(symbol, 20.0)
    notional = round(total_vol * mid_price * mult, 0)

    sweep_result = {
        "prices": [float(p) for p in prices],
        "vol": total_vol,
        "levels": len(prices),
        "side": sides[0],
        "ts": timestamp,
        "notional": notional,  # dollar value of sweep
    }
    tracker.clear()
    return sweep_result


def _emit_big_print(symbol: str, price: float, volume: int, side: str,
                    timestamp: float, sweep_hit) -> None:
    """Per-print big-order detector + classifier + socket emitter.

    Fires for the top decile of trades over the rolling 5-min window
    (size ≥ P90). Classifies into:
        block      — book_before ≥ 2× size  (institutional absorbed)
        sweep      — sweep_hit fired OR size ≥ 2× book_before  (consumed)
        aggression — neither block nor sweep (top-decile, isolated)

    Emits 'big_print' socket event:
      {symbol, ts (ms), price, size, side, book_before, classification}

    Frontend reads via window.AltarisEvents.on('data:big_print', ...) once
    the index.html fan-out is wired.

    Numerical sanity:
      - Rolling baseline: MEASURED from live print sizes, not magic number
      - P90 cutoff: STRUCTURAL percentile (top decile)
      - 2× book ratio for block: DERIVED (book absorbs ≥2× margin)
      - 2× size ratio for sweep: STRUCTURAL (consumed ≥2× visible book)
    """
    if side not in ('b', 's') or volume <= 0:
        return
    # Update rolling ring + compute P90
    with _PRINT_RING_LOCK:
        ring = _PRINT_RING[symbol]
        ring.append((timestamp, volume))
        cutoff = timestamp - _PRINT_RING_SEC
        while ring and ring[0][0] < cutoff:
            ring.popleft()
        if len(ring) < _PRINT_RING_MIN_N:
            return
        # snapshot for P90 outside lock
        sizes_snapshot = [s for _, s in ring]
    sizes_snapshot.sort()
    n_samples = len(sizes_snapshot)
    p90_idx = int(n_samples * 0.90)
    p99_idx = int(n_samples * 0.99) if n_samples >= 100 else max(0, n_samples - 1)
    p90 = sizes_snapshot[p90_idx]
    p99 = sizes_snapshot[p99_idx]
    if volume < p90:
        return
    # Extreme flag: top 1% of rolling 5-min distribution.
    # STRUCTURAL percentile — adapts to regime (quiet overnight P99 ≈ 5 lots,
    # RTH peak P99 ≈ 200+ lots). Frontend renders extra outer ring when set.
    is_extreme = volume >= p99
    # Look up book_before + cross-stream context at the trade level
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    qp = str(round(round(price / tick_size) * tick_size, 2))
    book_before = 0.0
    abs_tier_label = None       # FORTRESS/SOLID/HELD or None — from absorption v2 engine
    with _L2_LOCK:
        # ── book_before lookup (FIXED 2026-05-04) ──
        # PRIOR BUG: read live DOM at trade time, but the level at the trade
        # price has ALREADY been consumed by this very trade → book_before
        # almost always 0 → block-classification path under-fired.
        # FIX: read _MAX_BOOK_SEEN rolling-max tracker FIRST (same pattern
        # as _feed_candle at L1216-1237). Tracker remembers the LARGEST wall
        # at this price within the rolling 60s window — i.e. the wall that
        # was THERE just before this trade ate it.
        # Fallback to live DOM if tracker is cold for this price.
        mbs = _MAX_BOOK_SEEN.get(symbol, {})
        ent = mbs.get(qp)
        if ent is not None:
            book_before = float(ent.get('depth') or 0)
        if book_before == 0.0:
            dom = L2_STATE["dom"].get(symbol, {})
            if side == 'b':      # buy hit ask side
                book_before = float(dom.get("asks", {}).get(qp, 0))
            elif side == 's':    # sell hit bid side
                book_before = float(dom.get("bids", {}).get(qp, 0))
        # Cross-reference: is THIS price level a defended absorption level?
        # absorption v2 engine populates L2_STATE["absorption"][symbol][qp] with
        # tier/label keys (FORTRESS=3, SOLID=2, HELD=1). FORTRESS/SOLID = level
        # has been REPEATEDLY defended in the session (refill ≥ p75, traded ≥ p50).
        abs_at_level = L2_STATE.get("absorption", {}).get(symbol, {}).get(qp)
        if abs_at_level:
            abs_tier_label = abs_at_level.get('label')
    # Refill class at this level (per-tick, lighter check than full v2)
    refill_class_now = None
    refill_data = _REFILL_TRACKER.get(symbol, {}).get(qp)
    if refill_data:
        refill_class_now = refill_data.get('classification')

    # ── REFINED CLASSIFICATION (multi-leg confirmation) ──
    #
    # SWEEP — only when REAL _detect_sweep fired (multi-tick traversal <200ms).
    #         When a sweep fires, the *trigger* trade may be small (e.g. the
    #         final 1-lot of a 10-tick consume), but the SIGNAL is the whole
    #         sweep. So when sweep_hit is set we override emit_volume +
    #         emit_price with the full sweep totals → ONE bubble per sweep
    #         showing the total swept contracts at the principal price.
    #
    # BLOCK — institutional absorbed. Requires:
    #         (1) book_before ≥ 1.5× size  (passive defender had margin)
    #         AND
    #         (2) refill_class ∈ {instant, fast}  (book RELOADED after eating)
    #             OR abs_tier_label ∈ {FORTRESS, SOLID}  (level historically defended)
    #
    # AGGRESSION — top-decile size, no sweep, no defended level.
    is_at_defended_level = abs_tier_label in ('FORTRESS', 'SOLID')
    is_refill_strong     = refill_class_now in ('instant', 'fast')
    sweep_levels = 0
    emit_volume  = volume
    emit_price   = price
    if sweep_hit:
        cls = 'sweep'
        # Use the full sweep totals as the emit values — the trigger trade
        # is just the final tick. Without this override, sweeps look like
        # 1-lot bubbles instead of representing the multi-tick consume.
        sweep_prices = sweep_hit.get('prices') or [price]
        sweep_levels = len(sweep_prices)
        emit_volume  = int(sweep_hit.get('vol', volume))
        # Principal price = middle of the consumed range (visually centered
        # in the sweep). On a buy sweep this is between bottom and top of
        # the swept range; same for sell sweep.
        try:
            sorted_p = sorted(sweep_prices)
            emit_price = float(sorted_p[len(sorted_p) // 2])
        except Exception:
            emit_price = float(price)
    elif book_before >= 1.5 * volume and (is_refill_strong or is_at_defended_level):
        cls = 'block'
    else:
        cls = 'aggression'

    # ── Aggression noise filter ──
    # Aggression = "top-decile size, no other signal." On its own this is the
    # weakest of our classifications — just "big print, don't know why."
    # Without filtering, every top-decile print on quiet tape (where P90 is
    # near-zero) generates an aggression bubble, producing visual columns.
    #
    # Real footprint chart logic: aggression should only show when the print
    # is GENUINELY extreme (top 1%, not top 10%). Block/sweep/real_abs already
    # have additional signal context (sweep_hit / book + refill / level tier)
    # so they fire at P90. Aggression alone needs P99.
    #
    # STRUCTURAL: percentile gate against same rolling distribution. No
    # absolute lot threshold introduced.
    if cls == 'aggression' and volume < p99:
        return
    # Emit
    if _socketio is not None:
        _payload = {
            'symbol':         symbol,
            'ts':             int(timestamp * 1000),
            'price':          float(emit_price),    # principal price (sweep) or trade price
            'size':           int(emit_volume),     # total swept volume (sweep) or trade size
            'side':           side,
            'book_before':    int(book_before),
            'classification': cls,
            'p90':            int(p90),
            'p99':            int(p99),
            'extreme':        bool(is_extreme),
            'at_level_tier':  abs_tier_label,       # FORTRESS/SOLID/HELD or None
            'refill_class':   refill_class_now,     # instant/fast/slow/gone or None
            'sweep_levels':   int(sweep_levels),    # >0 when this is a real sweep
        }
        try:
            _socketio.emit('big_print', _payload, namespace='/')
        except Exception:
            pass
        # INVESTIGATION LOG — capture every emitted big_print
        try:
            _invest_log("big_prints", {**_payload, "ts_ms": int(time.time() * 1000)})
        except Exception:
            pass


def _detect_delta_divergence(symbol: str, tf: str):
    """Check for cumulative delta divergence on candle close.
    Bearish: price makes new high but delta is lower than at previous high.
    Bullish: price makes new low but delta is higher than at previous low.
    Returns divergence dict if detected, else None.
    """
    history = _DELTA_HISTORY[symbol][tf]
    if len(history) < _DIV_LOOKBACK_CANDLES:
        return None

    recent = history[-_DIV_LOOKBACK_CANDLES:]
    current = recent[-1]

    # Find previous swing high (highest price in lookback excluding last)
    prev_highs = sorted(recent[:-1], key=lambda c: c["high"], reverse=True)
    if prev_highs:
        prev_high = prev_highs[0]
        price_diff = current["high"] - prev_high["high"]
        delta_diff = current["delta"] - prev_high["delta"]
        if (price_diff >= _DIV_MIN_PRICE_MOVE and
                delta_diff <= -_DIV_MIN_DELTA_GAP):
            return {
                "type": "bearish",
                "price_high": current["high"],
                "price_prev": prev_high["high"],
                "delta_current": current["delta"],
                "delta_prev": prev_high["delta"],
                "t_prev": prev_high["t"],
            }

    # Find previous swing low (lowest price in lookback excluding last)
    prev_lows = sorted(recent[:-1], key=lambda c: c["low"])
    if prev_lows:
        prev_low = prev_lows[0]
        price_diff = prev_low["low"] - current["low"]
        delta_diff = current["delta"] - prev_low["delta"]
        if (price_diff >= _DIV_MIN_PRICE_MOVE and
                delta_diff >= _DIV_MIN_DELTA_GAP):
            return {
                "type": "bullish",
                "price_low": current["low"],
                "price_prev": prev_low["low"],
                "delta_current": current["delta"],
                "delta_prev": prev_low["delta"],
                "t_prev": prev_low["t"],
            }

    return None


def _detect_ignition(symbol: str, price: float, volume: int,
                     timestamp: float, side: str):
    """Detect momentum ignition: rapid small orders stepping through levels.
    Signal: 8+ trades within 2s, progressively higher/lower prices,
    small clips (1-5), total < 30. Returns ignition dict if detected.
    """
    if side == "n" or volume > _IGN_MAX_CLIP_SIZE:
        return None

    tracker = _IGN_TRACKER[symbol]
    tracker.append((timestamp, price, volume, side))

    # Prune old entries
    cutoff = timestamp - _IGN_WINDOW_SEC
    while tracker and tracker[0][0] < cutoff:
        tracker.popleft()

    if len(tracker) < _IGN_MIN_TRADES:
        return None

    # Check: all same side
    sides = [t[3] for t in tracker]
    if len(set(sides)) != 1:
        return None

    # Check: total volume is small (probing, not real conviction)
    total_vol = sum(t[2] for t in tracker)
    if total_vol >= _IGN_MAX_TOTAL:
        return None

    # Check: monotonically increasing or decreasing prices
    prices = [t[1] for t in tracker]
    is_up = all(prices[i] >= prices[i - 1] for i in range(1, len(prices)))
    is_down = all(prices[i] <= prices[i - 1] for i in range(1, len(prices)))
    if not is_up and not is_down:
        return None

    direction = "up" if is_up else "down"
    start_price = prices[0]

    # Price spread filter: must span at least _IGN_MIN_PRICE_SPREAD points
    price_spread = max(prices) - min(prices)
    if price_spread < _IGN_MIN_PRICE_SPREAD:
        return None

    # Store as active ignition for reversal tracking
    ign_result = {
        "direction": direction,
        "levels_swept": len(set(prices)),
        "reversed": False,
        "ts": timestamp,
        "price_min": min(prices),
        "price_max": max(prices),
    }
    _IGN_ACTIVE[symbol].append({
        "direction": direction,
        "ts": timestamp,
        "start_price": start_price,
    })
    tracker.clear()
    return ign_result


def _check_ignition_reversals(symbol: str, current_price: float,
                              timestamp: float):
    """Check if any active ignitions have reversed.
    Returns list of confirmed reversals to attach to candle.
    """
    confirmed = []
    remaining = []
    for ign in _IGN_ACTIVE[symbol]:
        age = timestamp - ign["ts"]
        if age > _IGN_REVERSAL_SEC:
            continue  # expired
        # Check reversal: price moved back past start
        if ign["direction"] == "up" and current_price < ign["start_price"]:
            confirmed.append({"direction": "up", "reversed": True, "ts": ign["ts"]})
        elif ign["direction"] == "down" and current_price > ign["start_price"]:
            confirmed.append({"direction": "down", "reversed": True, "ts": ign["ts"]})
        else:
            remaining.append(ign)
    _IGN_ACTIVE[symbol] = remaining
    return confirmed


def _detect_spoof(symbol: str, dom: dict, timestamp: float):
    """Compare current DOM snapshot with previous to detect spoofing.
    Large orders that appear and disappear quickly without being filled.
    Returns list of spoof detections.
    """
    spoofs_found = []
    prev = _DOM_PREV.get(symbol, {})
    current_levels = {}

    # Build current DOM level map from bids and asks
    for side_key, side_label in [("bids", "bid"), ("asks", "ask")]:
        levels = dom.get(side_key, [])
        if isinstance(levels, list):
            for lvl in levels:
                if isinstance(lvl, dict):
                    p = str(lvl.get("price", ""))
                    s = lvl.get("size", 0)
                    _spoof_min = _SPOOF_MIN_SIZE_TABLE.get(_CURRENT_REGIME, 100)
                    if p and s >= _spoof_min:
                        current_levels[p] = {"size": s, "side": side_label}

    # Check for large orders that were in prev but disappeared
    for price_str, info in prev.items():
        if price_str not in current_levels:
            # Large order vanished — possible spoof
            age = timestamp - info.get("first_seen", timestamp)
            if age <= _SPOOF_MAX_LIFETIME and age > 0:
                spoof_tracker = _SPOOF_TRACKER[symbol][price_str]
                spoof_tracker.append({
                    "fake_size": info["size"],
                    "side": info["side"],
                    "ts": timestamp,
                })
                # Prune old spoof events (keep last 30s)
                spoof_tracker[:] = [s for s in spoof_tracker
                                    if timestamp - s["ts"] < 30]
                if len(spoof_tracker) >= _SPOOF_MIN_OCCUR:
                    spoofs_found.append({
                        "price": price_str,
                        "fake_size": info["size"],
                        "side": info["side"],
                        "count": len(spoof_tracker),
                    })

    # Update previous DOM snapshot with timestamps
    new_prev = {}
    for p, info in current_levels.items():
        if p in prev:
            new_prev[p] = prev[p]  # keep original first_seen
        else:
            new_prev[p] = {**info, "first_seen": timestamp}
    _DOM_PREV[symbol] = new_prev

    return spoofs_found if spoofs_found else None


# ── Periodic cleanup to prevent unbounded memory growth ──
_CLEANUP_INTERVAL = 30.0  # run every 30 seconds
_last_cleanup_time = 0.0

def _cleanup_detection_state():
    """Purge stale entries from all detection trackers.
    Called periodically from the heavy-compute loop to prevent memory leaks.
    """
    global _last_cleanup_time
    now = time.time()
    if now - _last_cleanup_time < _CLEANUP_INTERVAL:
        return
    _last_cleanup_time = now

    for sym in list(SYMBOLS):
        # ── SWEEP_TRACKER: hard cap at 100 entries ──
        sweep = _SWEEP_TRACKER.get(sym, [])
        if len(sweep) > 100:
            _SWEEP_TRACKER[sym] = deque(list(sweep)[-50:])

        # ── IGN_TRACKER: hard cap at 200 entries ──
        ign = _IGN_TRACKER.get(sym, [])
        if len(ign) > 200:
            _IGN_TRACKER[sym] = deque(list(ign)[-100:])

        # ── IGN_ACTIVE: expire entries past reversal window ──
        active = _IGN_ACTIVE.get(sym, [])
        _IGN_ACTIVE[sym] = [a for a in active
                            if (now - a["ts"]) <= _IGN_REVERSAL_SEC]

        # ── SPOOF_TRACKER: remove price keys older than 60s ──
        spoof_sym = _SPOOF_TRACKER.get(sym, {})
        stale_spoof = [p for p, entries in spoof_sym.items()
                       if not entries or (now - entries[-1]["ts"]) > 60]
        for p in stale_spoof:
            del spoof_sym[p]

        # ── DELTA_HISTORY: already capped at 50, but double-check ──
        for tf in CANDLE_TIMEFRAMES:
            dh = _DELTA_HISTORY.get(sym, {}).get(tf, [])
            if len(dh) > 50:
                _DELTA_HISTORY[sym][tf] = dh[-50:]


# Dedicated lock for candle data — separate from _L2_LOCK to avoid
# contention with DOM/trade state reads during high-volume periods.
_CANDLE_LOCK = threading.Lock()


def _candle_boundary(timestamp: float, seconds: int) -> float:
    """Return the start timestamp of the candle that `timestamp` belongs to."""
    return (int(timestamp) // seconds) * seconds


_feed_candle_perf_samples: list = []
_feed_candle_perf_last_log: float = 0.0


def _feed_candle(symbol: str, price: float, volume: int, timestamp: float,
                 side: str = "n"):
    """Feed a trade tick into the candle engine for all timeframes.

    Args:
        side: Trade aggression classification.
              'b' = aggressive buy (hit the ask)
              's' = aggressive sell (hit the bid)
              'n' = neutral / passive

    Each candle accumulates a 'bp' (bubble profile) dict:
        {quantized_price_str: [buy_vol, sell_vol]}
    This structure is compact for JSON and lets the frontend render
    volume bubbles at each price level with buy/sell coloring.
    Historical/backfill candles have no 'bp' key — the frontend uses
    this absence to render the 'Live data starts here' seam.

    Thread-safe: acquires _CANDLE_LOCK.
    """
    # 2026-05-06 PERF INSTRUMENTATION
    _perf_t0 = time.perf_counter()
    # Quantize price to symbol-specific tick size for bubble aggregation
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    qp = str(round(round(price / tick_size) * tick_size, 2))

    # ── Book-size snapshot for absorption detection (FIXED 2026-05-04) ──
    # PRIOR BUG: read live DOM at trade time → ask/bid at the trade price
    # had ALREADY been consumed by this trade → _book_sz always 0 → true_abs
    # never fired (verified empirically: 0 of 22,065 levels in Friday's data).
    #
    # FIX: read from _MAX_BOOK_SEEN tracker, which is updated on every L2
    # depth message and remembers the LARGEST wall seen at this price within
    # the rolling 60s window. So if a 50-deep wall sat at $27935 for 30s
    # then a 50-lot trade swept it, we still see depth=50 (the wall that
    # was absorbed), not depth=0 (post-trade live state).
    #
    # Fallback to live DOM if max-seen tracker is empty (cold start).
    _book_sz = 0.0
    with _L2_LOCK:
        # Primary: rolling-max tracker (the wall that was THERE)
        mbs = _MAX_BOOK_SEEN.get(symbol, {})
        ent = mbs.get(qp)
        if ent is not None:
            _book_sz = float(ent.get('depth') or 0)
        # Fallback: live DOM (in case tracker is cold for this price)
        if _book_sz == 0.0:
            _dom = L2_STATE["dom"].get(symbol, {})
            if side == "b":      # aggressive buy hits the ask side
                _book_sz = float(_dom.get("asks", {}).get(qp, 0))
            elif side == "s":    # aggressive sell hits the bid side
                _book_sz = float(_dom.get("bids", {}).get(qp, 0))
        # side == "n": neutral/unknown — _book_sz from tracker (no side gate)

    _pending_emits = []       # collect fast OHLCV payloads (no heavy computation)
    _pending_enriched = []    # collect enriched payloads (bp, depth_deltas, etc.) at 5Hz

    with _CANDLE_LOCK:
        for tf, seconds in CANDLE_TIMEFRAMES.items():
            boundary = _candle_boundary(timestamp, seconds)
            cur = _CURRENT_CANDLE[symbol].get(tf)

            # 2026-05-07 FIX: late-tick guard. TopStepX delivers trades in
            # batches; within a batch ticks can arrive out-of-order. Pre-fix:
            # a tick with boundary < cur["t"] would unconditionally close the
            # current candle and start a fresh one at the older boundary with
            # v=volume. The next forward tick reversed it. Result: ping-pong
            # of tiny-volume bars overwriting correct bars on the chart.
            # Fix: apply late ticks to the matching historical candle in the
            # deque (preserving volume) and skip _CURRENT_CANDLE / emit logic
            # for this TF so no candle_update with tiny volume is broadcast.
            if cur is not None and boundary < cur["t"]:
                hist = _CANDLES[symbol].get(tf)
                if hist and len(hist) > 0 and hist[-1].get("t") == boundary:
                    h = hist[-1]
                    h["h"] = max(h.get("h", price), price)
                    h["l"] = min(h.get("l", price), price)
                    h["c"] = price
                    h["v"] = h.get("v", 0) + volume
                    bp_h = h.get("bp")
                    if bp_h is not None:
                        entry = bp_h.get(qp)
                        if entry:
                            if side == "b":
                                entry[0] += volume
                            elif side == "s":
                                entry[1] += volume
                        else:
                            bp_h[qp] = [volume if side == "b" else 0,
                                        volume if side == "s" else 0,
                                        None, None,
                                        _book_sz]
                # Either applied to history or dropped (too old) — skip the
                # rest of this TF's processing to avoid resetting cur.
                continue

            if cur is None or cur["t"] != boundary:
                # Close previous candle if it exists
                if cur is not None:
                    # 2026-05-06 PERF FIX: gate expensive per-close enrichment
                    # by _ACTIVE_TFS. Frontend only ever requests tf=1m (verified
                    # in /api/l2/candles access log), but pre-fix this loop ran
                    # depth_deltas + divergence + absorption snapshots for ALL
                    # 11 timeframes (5s/15s/30s/200s/5m/10m/15m/30m/1h/4h
                    # included). 5s alone fires 12× per minute. Combined waste
                    # ≈ 20 expensive bar-closes/min vs the 1 we actually need.
                    # Now: only do the heavy work for active TFs. Lightweight
                    # OHLC tracking + cum_delta still happens for all TFs (cheap)
                    # so historical 5m/15m bars remain available via get_candles
                    # if a pane ever subscribes.
                    is_active_tf = tf in _ACTIVE_TFS

                    # ── Cumulative Delta tracking on candle close ──
                    # Lightweight; keep for all TFs.
                    candle_bp = cur.get("bp", {})
                    candle_buy = sum(v[0] for v in candle_bp.values() if isinstance(v, list) and len(v) >= 2)
                    candle_sell = sum(v[1] for v in candle_bp.values() if isinstance(v, list) and len(v) >= 2)
                    candle_delta = candle_buy - candle_sell
                    _CUM_DELTA[symbol][tf] += candle_delta

                    if is_active_tf:
                        # Record delta history for divergence detection (active TFs only)
                        _DELTA_HISTORY[symbol][tf].append({
                            "t": cur["t"],
                            "high": cur["h"],
                            "low": cur["l"],
                            "delta": _CUM_DELTA[symbol][tf],
                        })
                        # Keep only last 50 entries
                        if len(_DELTA_HISTORY[symbol][tf]) > 50:
                            _DELTA_HISTORY[symbol][tf] = _DELTA_HISTORY[symbol][tf][-50:]
                        # Check for divergence (walks history — skip for inactive TFs)
                        div_hit = _detect_delta_divergence(symbol, tf)
                        if div_hit:
                            cur["delta_div"] = div_hit

                        # ── Snapshot absorption + depth_deltas onto closing candle ──
                        # So history bars carry this data for frontend rendering.
                        candle_bp_keys = list(cur.get("bp", {}).keys())
                        if candle_bp_keys:
                            _dd = _compute_depth_deltas(symbol, cur["t"], timestamp, candle_bp_keys)
                            if _dd:
                                cur["depth_deltas"] = _dd
                            # Snapshot absorption scores for prices in this candle's range
                            _abs_global = L2_STATE["absorption"].get(symbol, {})
                            if _abs_global:
                                _abs_snap = {}
                                for pk in candle_bp_keys:
                                    if pk in _abs_global:
                                        _abs_snap[pk] = _abs_global[pk]
                                if _abs_snap:
                                    cur["absorption"] = _abs_snap

                    frozen = _freeze_candle(cur)
                    _CANDLES[symbol][tf].append(frozen)
                    # Persist bubble profile to disk (1m only) so it survives restarts
                    if tf == _BP_PERSIST_TF:
                        _bp_save_candle(symbol, frozen)
                    # Bar-level signal engine — emits absorption/exhaustion/
                    # aggression at most once per bar each (zero spam).
                    if tf == '1m':
                        try:
                            _emit_bar_signals(symbol, tf, frozen, _CANDLES[symbol][tf])
                        except Exception as e:
                            log.debug(f"bar_signal compute err: {e}")
                    # 2026-05-07 FIX: force-emit the CLOSING bar's final OHLCV
                    # even if rate-limited. At RTH open (100+ ticks/sec), the
                    # 50ms rate limit was eating bar-close updates, so the
                    # chart kept the bar frozen at a pre-close volume while
                    # the next bar started showing as a single-tick stub.
                    # This bypasses _EMIT_MIN_INTERVAL for boundary crossings.
                    if _socketio is not None and tf in _ACTIVE_TFS:
                        _pending_emits.append({
                            "symbol": symbol,
                            "tf": tf,
                            "time": cur["t"],
                            "open": cur["o"],
                            "high": cur["h"],
                            "low": cur["l"],
                            "close": cur["c"],
                            "volume": cur["v"],
                            "_emit_ts": time.time(),
                            "_final": True,
                        })
                    # Reset the rate limiter so the NEW bar's first tick can
                    # also emit immediately (no rate-limit lockout from the
                    # bar-close emit we just queued above).
                    _last_emit_time[f"{symbol}:{tf}"] = 0
                # Start new candle with bubble profile
                bp = {}
                bp[qp] = [volume if side == "b" else 0,
                          volume if side == "s" else 0,
                          None, None,   # [2], [3] reserved
                          _book_sz]     # [4]=book_size_at_trade
                _CURRENT_CANDLE[symbol][tf] = {
                    "t": boundary,
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "v": volume,
                    "bp": bp,
                }
            else:
                # Update existing candle
                cur["h"] = max(cur["h"], price)
                cur["l"] = min(cur["l"], price)
                cur["c"] = price
                cur["v"] += volume
                # Accumulate bubble profile
                bp = cur.get("bp")
                if bp is not None:
                    entry = bp.get(qp)
                    if entry:
                        if side == "b":
                            entry[0] += volume
                        elif side == "s":
                            entry[1] += volume
                        # Keep max book size seen at this level (wall size, not cumulative)
                        while len(entry) < 5:
                            entry.append(0)
                        entry[4] = max(entry[4] or 0, _book_sz)
                    else:
                        bp[qp] = [volume if side == "b" else 0,
                                  volume if side == "s" else 0,
                                  None, None,   # [2]=fp_score, [3]=true_abs (set below)
                                  _book_sz]     # [4]=book_size_at_trade
                        entry = bp[qp]
                    # Update fp_score (entry[2]) + true_abs flag (entry[3])
                    _update_bp_signals(entry, symbol, qp)

                # ── bp_large: same as bp but only trades >= threshold ──
                _LARGE_THRESHOLD = {"NQ": 10, "ES": 20}
                _lt = _LARGE_THRESHOLD.get(symbol, 10)
                if volume >= _lt:
                    if "bp_large" not in cur:
                        cur["bp_large"] = {}
                    bpl = cur["bp_large"]
                    if qp in bpl:
                        if side == "b":
                            bpl[qp][0] += volume
                        elif side == "s":
                            bpl[qp][1] += volume
                    else:
                        bpl[qp] = [volume if side == "b" else 0,
                                   volume if side == "s" else 0]

            # ── Emit candle OHLCV fast (20Hz) + enriched slow (5Hz) ──
            # Fast path: lightweight OHLCV only — matches DOM speed
            # Enriched path: bp, signals, depth_deltas — heavy computation at 5Hz
            if _socketio is not None and tf in _ACTIVE_TFS:
                emit_key = f"{symbol}:{tf}"
                enrich_key = f"{symbol}:{tf}:enrich"
                now = time.time()
                last = _last_emit_time.get(emit_key, 0)
                if now - last >= _EMIT_MIN_INTERVAL:
                    _last_emit_time[emit_key] = now
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        # Fast OHLCV — emitted immediately, no heavy computation
                        _pending_emits.append({
                            "symbol": symbol,
                            "tf": tf,
                            "time": cur["t"],
                            "open": cur["o"],
                            "high": cur["h"],
                            "low": cur["l"],
                            "close": cur["c"],
                            "volume": cur["v"],
                            "_emit_ts": now,
                        })
                        # Enriched payload at 5Hz (200ms) — bp, signals, depth
                        last_enrich = _last_emit_time.get(enrich_key, 0)
                        if now - last_enrich >= 0.2:
                            _last_emit_time[enrich_key] = now
                            _pending_enriched.append({
                                "symbol": symbol,
                                "tf": tf,
                                "time": cur["t"],
                                "close": cur["c"],
                                "bp": cur.get("bp"),
                                "sweeps": cur.get("sweeps"),
                                "delta_div": cur.get("delta_div"),
                                "ignition": cur.get("ignition"),
                                "spoofs": cur.get("spoofs"),
                                "micro_ofi": _MICRO_OFI[symbol].ofi if symbol in _MICRO_OFI else None,
                                "hawkes": _V2_HAWKES[symbol].get_state() if symbol in _V2_HAWKES else None,
                                "depth_vel": _DEPTH_VEL_CACHE.get(symbol),
                            })

    # ── Emit fast OHLCV outside lock — no computation, instant ──
    # Double-check _ACTIVE_TF at emit time (may have changed since queue time)
    if _socketio is not None:
        for _emit_candle in _pending_emits:
            if _emit_candle.get("tf") not in _ACTIVE_TFS:
                continue  # stale — no client wants this tf anymore
            try:
                _socketio.emit("candle_update", _emit_candle, namespace="/")
            except Exception:
                pass

    # ── Emit enriched data at 5Hz — heavy computation here ──
    if _socketio is not None and _pending_enriched:
        _pending_enriched = [e for e in _pending_enriched if e.get("tf") in _ACTIVE_TFS]
        for _emit_enrich in _pending_enriched:
            bp_data = _emit_enrich.get("bp")
            if bp_data:
                _emit_enrich["depth_deltas"] = _compute_depth_deltas(
                    symbol, _emit_enrich["time"], time.time(),
                    bp_data.keys()
                )
                with _L2_LOCK:
                    _emit_enrich["book_imbalance"] = _compute_book_imbalance(
                        symbol, bp_data.keys()
                    )
            try:
                _socketio.emit("candle_enriched", _emit_enrich, namespace="/")
            except Exception as e:
                log.warning("candle_enriched emit failed: %s", e)

    # 2026-05-06 PERF INSTRUMENTATION: per-call timing.
    # _feed_candle runs PER NQ TRADE TICK and iterates 11 timeframes.
    # If TopStepX is feeding at 200-1000 trades/sec, this is the dominant
    # CPU consumer. Aggregates dumped every 30s.
    _perf_dt_ms = (time.perf_counter() - _perf_t0) * 1000.0
    global _feed_candle_perf_samples, _feed_candle_perf_last_log
    if _feed_candle_perf_last_log == 0.0:
        _feed_candle_perf_last_log = time.time()
    _feed_candle_perf_samples.append(_perf_dt_ms)
    if (time.time() - _feed_candle_perf_last_log) >= 30.0 and _feed_candle_perf_samples:
        s = sorted(_feed_candle_perf_samples)
        n = len(s)
        p50 = s[n // 2]
        p95 = s[int(n * 0.95)] if n > 1 else s[0]
        p99 = s[int(n * 0.99)] if n > 1 else s[0]
        mx = s[-1]
        avg = sum(s) / n
        wall_dt = max(time.time() - _feed_candle_perf_last_log, 1.0)
        rate = n / wall_dt
        cpu_pct = 100.0 * sum(s) / 1000.0 / wall_dt
        log.info(
            f"[FEED-CANDLE-PERF] n={n} ({rate:.0f}/s) | p50={p50:.3f}ms p95={p95:.3f}ms "
            f"p99={p99:.3f}ms max={mx:.2f}ms | avg={avg:.3f}ms | CPU on this handler: "
            f"{cpu_pct:.1f}% of one core"
        )
        _feed_candle_perf_samples = []
        _feed_candle_perf_last_log = time.time()


# ═══════════════════════════════════════════════════════════════════
# BAR-LEVEL SIGNAL ENGINE — Absorption / Exhaustion / Aggression
# ═══════════════════════════════════════════════════════════════════
#
# Footprint-grade bar-level detection. Runs on candle close.
# Emits ≤1 of each signal type per bar via 'bar_signal' socket event.
# Most bars produce ZERO signals — only fires when criteria converge.
#
# Inputs (all already populated by upstream engine):
#   candle.bp[priceStr]    = [buyVol, sellVol, fp_score, true_abs, max_book_size]
#   candle.h, l, o, c, v   = OHLCV
#   _REFILL_TRACKER[sym]   = per-level refill classification
#   history (deque)        = prior closed bars for swing/divergence comparison

# ── Investigation Logger ──────────────────────────────────────────────────
# OVERHAULED 2026-05-04. Old version had three problems flagged by audit:
#   (1) Comment claimed "auto-stops 09:00 EDT 2026-04-27" — no stop logic existed,
#       so it kept writing past expiry (today is 2026-05-04, ~7 days past expiry).
#   (2) `open(path, "a")` synchronously per call inside _CANDLE_LOCK — measurable
#       hot-path drag during high print rates.
#   (3) `_INVEST_DAY_TAG` set at module import → cross-midnight UTC misroutes all
#       next-day writes to the prior day's file.
#
# New design:
#   - Off by default (`_INVEST_LOG_ENABLED = False`); operator flips on for
#     targeted debugging by setting env var INVEST_LOG=1
#   - When on: buffered writes via in-memory queue, flushed by a background thread
#     at 1 Hz. Hot path's _invest_log() is a non-blocking enqueue.
#   - Day tag computed per-write (not per-import) so cross-midnight writes route
#     correctly.
#   - When env var off, _invest_log() is a no-op (single attribute check).
import os as _os_invest

_INVEST_DIR = "/Users/kaali/Desktop/altaris-dev/investigation"
_INVEST_LOG_LOCK = threading.Lock()  # protects _invest_buffer
_INVEST_BUFFER: list = []            # (category, payload_str) tuples — drained by flusher thread
_INVEST_LOG_ENABLED = bool(_os_invest.environ.get("INVEST_LOG"))
_INVEST_FLUSHER_STARTED = False


def _invest_log(category: str, payload: dict) -> None:
    """Best-effort enqueue for offline analysis. Off by default — opt in via
    `INVEST_LOG=1` env var. Hot-path-safe: non-blocking enqueue, no I/O,
    no JSON serialization on the call site (deferred to flusher).
    """
    if not _INVEST_LOG_ENABLED:
        return
    try:
        line = json.dumps(payload, default=str)
        with _INVEST_LOG_LOCK:
            _INVEST_BUFFER.append((category, line))
    except Exception:
        pass


def _invest_log_flusher():
    """Background thread — drains _INVEST_BUFFER to disk every 1s.
    Files routed by today's UTC date (computed per flush, not import-time).
    """
    while _INVEST_LOG_ENABLED:
        try:
            time.sleep(1.0)
            with _INVEST_LOG_LOCK:
                if not _INVEST_BUFFER:
                    continue
                batch = _INVEST_BUFFER[:]
                _INVEST_BUFFER.clear()
            # Group by category to minimize file open() calls
            by_cat: dict = {}
            for cat, line in batch:
                by_cat.setdefault(cat, []).append(line)
            day_tag = datetime.utcnow().strftime("%Y%m%d")
            for cat, lines in by_cat.items():
                path = f"{_INVEST_DIR}/{cat}/{cat}_{day_tag}.jsonl"
                try:
                    _os_invest.makedirs(_os_invest.path.dirname(path), exist_ok=True)
                    with open(path, "a") as f:
                        for ln in lines:
                            f.write(ln); f.write("\n")
                except Exception:
                    pass
        except Exception:
            time.sleep(2.0)


if _INVEST_LOG_ENABLED and not _INVEST_FLUSHER_STARTED:
    threading.Thread(target=_invest_log_flusher, daemon=True, name="invest-flush").start()
    _INVEST_FLUSHER_STARTED = True
    log.info("[INVEST-LOG] enabled via INVEST_LOG env, background flusher started")


# ── CPU Watchdog (2026-05-06) ─────────────────────────────────────────────────
# Self-monitor: log a warning when our process spends ≥98% CPU for ≥30s.
# Sustained pinned-CPU is the precursor to WebSocket buffer pile-up + RST
# disconnects (proven empirically: pre-active-TF-fix pegged at 100% → 14
# disconnects/30min; post-fix at 31% → 0 disconnects). With user's 5
# enriched TFs raising baseline back toward 95%, we want a tripwire that
# tells us when the system slips back into the danger zone.
_CPU_WATCHDOG_STARTED = False

def _cpu_watchdog_loop():
    """Sample own-process CPU% every 5s. Log warning if 30s rolling avg ≥98%."""
    import os as _os_cpu
    import time as _time_cpu
    samples = deque(maxlen=6)   # 6 × 5s = 30s rolling window
    pid = _os_cpu.getpid()
    last_sample_ts = 0.0
    last_user_t = 0.0
    last_sys_t = 0.0
    while True:
        try:
            _time_cpu.sleep(5.0)
            # Use os.times() for cumulative CPU time of this process+children.
            # Diff between snapshots / wall-clock interval = CPU% on a 1-core basis.
            t = _os_cpu.times()
            now = _time_cpu.time()
            cur_cpu = t.user + t.system
            if last_sample_ts > 0:
                wall_dt = now - last_sample_ts
                cpu_dt = cur_cpu - last_user_t - last_sys_t
                if wall_dt > 0:
                    pct = 100.0 * cpu_dt / wall_dt
                    samples.append(pct)
                    if len(samples) >= 6:
                        avg = sum(samples) / len(samples)
                        if avg >= 98.0:
                            log.warning(
                                f"[CPU-WATCHDOG] sustained {avg:.0f}% CPU over 30s "
                                f"— back-pressure imminent (samples={[round(s) for s in samples]})"
                            )
                        elif avg >= 90.0:
                            log.info(
                                f"[CPU-WATCHDOG] elevated {avg:.0f}% CPU 30s avg "
                                f"(margin tight; {[round(s) for s in samples]})"
                            )
            last_sample_ts = now
            last_user_t = t.user
            last_sys_t = t.system
        except Exception:
            _time_cpu.sleep(5.0)

if not _CPU_WATCHDOG_STARTED:
    threading.Thread(target=_cpu_watchdog_loop, daemon=True, name="cpu-watchdog").start()
    _CPU_WATCHDOG_STARTED = True
    log.info("[CPU-WATCHDOG] started (warns at ≥98% sustained 30s, info at ≥90%)")


# ── Adaptive thresholds: replace static lot floors with session-relative ──
# Rolling per-symbol distributions of (per-level volume, per-bar volume).
# Used to derive regime-aware gates: floor = median × 0.30. Warmup samples
# fall back to constants until N reaches the structural minimum.
_LEVEL_VOL_SAMPLES: dict = defaultdict(lambda: deque(maxlen=500))
_BAR_VOL_SAMPLES:   dict = defaultdict(lambda: deque(maxlen=200))


def _adaptive_level_floor(symbol: str, fallback: float = 1.0) -> float:
    """Return 45 % of session-rolling median per-level volume.

    Tuning history:
      0.30  initial — fire rate 87% of bars (visual noise wall)
      0.50  2026-04-27 tightening — fire rate dropped to 36% of bars (under target)
      0.45  2026-04-28 fine-tune — re-loosened slightly to land in 50-55% target band
            (38h investigation snapshot data showed 36% under-fires the actual
            absorption activity which the snapshots saw at 79.7% of windows)
    """
    samples = _LEVEL_VOL_SAMPLES.get(symbol)
    if not samples or len(samples) < 30:
        return fallback
    sorted_s = sorted(samples)
    median = sorted_s[len(sorted_s) // 2]
    return median * 0.45


def _adaptive_bar_floor(symbol: str, fallback: float = 5.0) -> float:
    """Return 45 % of session-rolling median bar volume.

    Same calibration history as _adaptive_level_floor (0.30 → 0.50 → 0.45).
    Filters out micro-volume bars where any signal is noise.
    """
    samples = _BAR_VOL_SAMPLES.get(symbol)
    if not samples or len(samples) < 10:
        return fallback
    sorted_s = sorted(samples)
    median = sorted_s[len(sorted_s) // 2]
    return median * 0.45


def _wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """95% Wilson-score confidence lower bound on the binomial proportion k/n.
    Used in aggression's stacked-imbalance check to reject side-dominance
    claims that are not statistically defensible (small N, marginal share).
    z = 1.96 is the standard normal critical value for 95% confidence — a
    universal statistical constant, not a magic threshold.
    """
    if n <= 0:
        return 0.0
    p_hat = k / n
    denom = 1.0 + (z * z) / n
    center = p_hat + (z * z) / (2.0 * n)
    margin = z * math.sqrt((p_hat * (1.0 - p_hat) + (z * z) / (4.0 * n)) / n)
    return max(0.0, (center - margin) / denom)


def _bp_level_stats(bp):
    """Convert bp dict to sorted level list with delta + imbalance."""
    out = []
    for ps, e in bp.items():
        if not isinstance(e, list) or len(e) < 2:
            continue
        b = e[0] or 0
        s = e[1] or 0
        if b + s == 0:
            continue
        out.append({
            'price': float(ps),
            'b': b, 's': s,
            'tot': b + s,
            'delta': b - s,
            'imbalance': abs(b - s) / (b + s),  # 0=balanced, 1=pure
        })
    out.sort(key=lambda x: x['price'])
    return out


def _bar_delta(bar):
    """Sum of (buy − sell) across all bp price levels in a bar."""
    bp = bar.get("bp")
    if not bp:
        return 0
    d = 0
    for e in bp.values():
        if isinstance(e, list) and len(e) >= 2:
            d += (e[0] or 0) - (e[1] or 0)
    return d


def _bar_total_volume(bar):
    bp = bar.get("bp")
    if not bp:
        return 0
    t = 0
    for e in bp.values():
        if isinstance(e, list) and len(e) >= 2:
            t += (e[0] or 0) + (e[1] or 0)
    return t


def _detect_bar_absorption(symbol, candle):
    """ABSORPTION: large passive limit orders absorbing aggressive flow with
    minimal price displacement.

    Confirmation cascade (ALL required):
      (1) High volume at level: total ≥ P75 of bar's per-level volumes
      (2) Balanced delta: imbalance < 0.40 (delta NOT expanding despite flow)
      (3) Refill holding: refill_class ∈ {instant, fast, slow} (not 'gone')
      (4) Minimal displacement: level near a bar extreme (price tested but held)

    Returns: list of {price, side, volume, strength, ...} — top 1.
    """
    bp = candle.get("bp")
    if not bp:
        return []
    levels = _bp_level_stats(bp)
    if len(levels) < 3:
        return []

    h = candle.get("h", 0)
    l = candle.get("l", 0)
    bar_range = max(h - l, 0.001)
    bar_total = sum(lv['tot'] for lv in levels)
    # Adaptive bar floor — replaces hard-coded `< 5` with regime-aware gate
    if bar_total < _adaptive_bar_floor(symbol, fallback=5.0):
        return []

    vols = sorted(lv['tot'] for lv in levels)
    p75 = vols[int(len(vols) * 0.75)] if vols else 0
    avg_vol = bar_total / max(len(levels), 1)
    # Adaptive level floor — replaces hard-coded `< 5` per level
    level_floor = _adaptive_level_floor(symbol, fallback=1.0)

    candidates = []
    for lv in levels:
        # (1) High volume gate — must beat both the bar's P75 (relative to
        # this bar's own distribution) AND the session-rolling level floor
        # (relative to recent regime). Both gates required.
        if lv['tot'] < p75 or lv['tot'] < level_floor:
            continue
        # (2) IMBALANCE GATE — INVERTED 2026-05-04 from data audit.
        # Original heuristic was "balanced flow = absorption" but 24,121-bar
        # backtest showed it was empirically wrong: "balanced" levels are
        # transit/VWAP zones, not real defense. REAL absorption is when
        # aggressive flow on ONE side dominates but the level still holds.
        # Ledger numbers (level held ±1tick over 3 bars):
        #   B1+B2(≤0.30)+B4(≥0.50)  CURRENT pre-fix → 16.9% (worse than random)
        #   B1+B2(≥0.50)+B4(≤0.40)  inverted both    → 30.1%
        # Old: if lv['imbalance'] > 0.30: continue   (reject imbalanced)
        if lv['imbalance'] < 0.50:
            continue
        # (3) Refill class gate — unchanged
        # 2026-05-05 BUG FIX: was `f"{lv['price']:.2f}"` which produces
        # "X.00"/"X.50" — but _REFILL_TRACKER stores keys via line 4095's
        # `str(round(float(ps), 2))` which drops trailing zeros and produces
        # "X.0"/"X.5". 50% of NQ tick prices (.0 and .5) silently key-missed,
        # losing refill_class on 184 of 551 fires/day (33%) — those fires
        # got refill_boost=1.0 instead of 1.3-1.5 and could only paint via
        # strength≥3.5 extreme path (92.7% silently dropped from chart).
        # Now uses the same str(round(...)) formatter as the writer.
        ps_str = str(round(float(lv['price']), 2))
        refill = _REFILL_TRACKER.get(symbol, {}).get(ps_str)
        refill_class = refill.get('classification') if refill else None
        if refill_class == 'gone':
            continue  # level wiped out — not absorption
        # (4) PROXIMITY GATE — INVERTED 2026-05-04. Production formula gives
        # ep=1.0 at MID-bar (pos=0.5) and ep=0.0 at extremes (pos=0 or 1) —
        # opposite of the variable name. Old check `if ep < 0.50: continue`
        # rejected near-extreme levels and KEPT mid-bar. Empirically this
        # filtered AWAY the better signals: mid-bar high-vol levels are
        # bar-shape noise; near-extreme levels (where price was tested AND
        # rejected) carry the defense signal.
        # New: keep ep ≤ 0.40 (i.e. position ∈ [0,0.20] ∪ [0.80,1.0]) — the
        # outer 20% of the bar where defense actually shows up.
        position = (lv['price'] - l) / bar_range
        # ep = 1.0 at mid (pos=0.5), 0.0 at h/l (pos=0 or 1)
        extreme_proximity = max(0.0, 1.0 - 2.0 * abs(position - 0.5))
        if extreme_proximity > 0.40:
            continue

        # Strength composition
        vol_score = lv['tot'] / max(avg_vol, 1.0)
        refill_boost = {'instant': 1.5, 'fast': 1.3, 'slow': 1.0}.get(refill_class, 1.0)
        # PROPOSAL C (2026-04-28): replenishment-ratio multiplier on top of
        # the timing-based refill_boost. ratio = avg_refill_size / avg_hit_size.
        # 1.0 = neutral (level fully restored on average), >1 = grew past prior
        # depth (very strong defense), <1 = partial restoration. Bound to
        # [0.7, 1.5] so a single noisy measurement can't dominate the score.
        rep_ratio = (refill or {}).get('replenish_ratio')
        rep_boost = max(0.7, min(1.5, rep_ratio)) if (rep_ratio is not None) else 1.0
        strength = vol_score * refill_boost * rep_boost * (0.5 + 0.5 * extreme_proximity)
        # (5) Strength minimum gate — tuned 2026-04-27.
        # Backtest: strength p25=1.65, p50=2.16, p75=2.71, p95=3.61.
        # Floor at 1.5 cuts ~25% of weakest signals — those that fired
        # because of marginal volume, not real defense.
        if strength < 1.5:
            continue

        # Side: which direction is implied by the absorption
        # Absorption near low = bid defended → bullish (sellers exhausted)
        # Absorption near high = ask defended → bearish (buyers exhausted)
        side = 'bullish' if position < 0.5 else 'bearish'

        candidates.append({
            'price':             lv['price'],
            'side':              side,
            'volume':            lv['tot'],
            'b':                 lv['b'],
            's':                 lv['s'],
            'imbalance':         round(lv['imbalance'], 3),
            'strength':          round(strength, 3),
            'refill_class':      refill_class,
            'extreme_proximity': round(extreme_proximity, 2),
        })

    candidates.sort(key=lambda c: -c['strength'])
    return candidates[:1]  # ≤ 1 absorption signal per bar


def _detect_bar_exhaustion(symbol, tf, candle, history):
    """EXHAUSTION: delta climax with price failure to extend.

    Confirmation:
      Sell exhaustion (at HIGHS):
        - Bar makes new high vs recent swing
        - But bar_delta is significantly LOWER than swing-bar delta
          (buyers spent more energy with less result → exhausted)

      Buy exhaustion (at LOWS):
        - Bar makes new low vs recent swing
        - But bar_delta is significantly HIGHER (less negative) than swing-bar delta
          (sellers exhausted)

    Returns: {side, price, volume, strength, ...} or None.
    """
    if not history or len(history) < 5:
        return None
    bp = candle.get("bp")
    if not bp:
        return None

    bar_total = _bar_total_volume(candle)
    # Adaptive bar floor — was hard-coded `< 10`, now regime-relative
    if bar_total < _adaptive_bar_floor(symbol, fallback=10.0):
        return None

    bar_delta = _bar_delta(candle)
    h = candle.get("h", 0)
    l = candle.get("l", 0)

    # Recent swing window — RELAXED 2026-04-27: 5 → 10 bars.
    # 5-bar window was too narrow — most "new highs/lows" within 5 bars
    # are just minor extension, rarely the true swing top/bottom that
    # exhaustion patterns mark. 10 bars catches genuine turning points.
    recent = list(history)[-10:]
    if len(recent) < 5:
        return None

    swing_high_idx, _ = max(enumerate(recent), key=lambda x: x[1].get("h", 0))
    swing_low_idx,  _ = min(enumerate(recent), key=lambda x: x[1].get("l", 0))
    swing_high_p = recent[swing_high_idx].get("h", 0)
    swing_low_p  = recent[swing_low_idx].get("l", 0)

    # Divergence threshold RELAXED 2026-04-27: 0.7 → 0.55.
    DELTA_DIVERGENCE = 0.55

    # NQ tick = 0.25. Tolerance allows the bar to TEST/REJECT the swing
    # high or low without strictly exceeding it — this is exactly when
    # exhaustion most often prints (rejection candle at the high).
    SWING_TOL = 0.25

    # Minimum prior_delta magnitude — some net direction must exist
    # to "exhaust." Was strictly > 0 / < 0; relaxed to ≥ 5 contracts of
    # net flow so doji-like prior bars don't disqualify the test.
    MIN_PRIOR_DELTA = 5

    # ── PROPOSAL E (2026-04-28): 3-bar cumulative delta decay ───────────────
    # Single-bar comparison can miss exhaustion that builds across multiple
    # bars — buyers spending energy across 3 bars to make a marginal new
    # high while the prior swing was made in 3 bars of much stronger flow.
    # We compute 3-bar cumulative delta on BOTH sides (current trailing 3
    # vs the 3 bars centered on the swing) and accept the OR with the
    # single-bar path. Decay ratio uses the same DELTA_DIVERGENCE so the
    # detector is internally consistent.
    DECAY_3BAR = DELTA_DIVERGENCE  # 0.55 — share with single-bar path
    MIN_3BAR_PRIOR_DELTA = 12      # 3× single-bar floor (5 → 15-ish), keep doji-3bar out

    def _cum3(idx_list):
        """Sum _bar_delta over idx positions in `recent` (clamped)."""
        return sum(_bar_delta(recent[i]) for i in idx_list if 0 <= i < len(recent))

    # Trailing 3 bars (current bar plus the 2 history bars right before it).
    # `candle` itself is the current bar; `recent` is history up through prior.
    cur_trail = bar_delta + _cum3([len(recent) - 1, len(recent) - 2])

    # SELL EXHAUSTION (top): bar tests/exceeds swing high but delta failed to expand.
    # Was `h > swing_high_p` (strict) — many bars REJECT at the high without
    # exceeding by a full tick. Relaxed to within-tick of the swing high.
    if h >= swing_high_p - SWING_TOL:
        prior_swing = recent[swing_high_idx]
        prior_delta = _bar_delta(prior_swing)
        # Buyers exhausted: delta lower than the swing-high bar despite testing/making new high
        if prior_delta >= MIN_PRIOR_DELTA and bar_delta < prior_delta * DELTA_DIVERGENCE:
            strength = (prior_delta - bar_delta) / max(abs(prior_delta), 1)
            return {
                'side':         'sell_exhaustion',
                'price':        h,
                'volume':       bar_total,
                'cur_delta':    bar_delta,
                'prior_delta':  prior_delta,
                'strength':     round(strength, 3),
                'reason':       'new_high_delta_div' if h > swing_high_p else 'rejected_at_high',
            }
        # Proposal E secondary path — 3-bar cumulative delta decay
        prior_trail = _cum3([swing_high_idx - 1, swing_high_idx, swing_high_idx + 1])
        if prior_trail >= MIN_3BAR_PRIOR_DELTA and cur_trail < prior_trail * DECAY_3BAR:
            strength = (prior_trail - cur_trail) / max(abs(prior_trail), 1)
            return {
                'side':         'sell_exhaustion',
                'price':        h,
                'volume':       bar_total,
                'cur_delta':    bar_delta,
                'prior_delta':  prior_delta,
                'cur_3bar_delta':   cur_trail,
                'prior_3bar_delta': prior_trail,
                'strength':     round(strength, 3),
                'reason':       '3bar_decay_at_high',
            }

    # BUY EXHAUSTION (bottom): bar tests/breaks swing low but delta less negative
    if l <= swing_low_p + SWING_TOL:
        prior_swing = recent[swing_low_idx]
        prior_delta = _bar_delta(prior_swing)
        if prior_delta <= -MIN_PRIOR_DELTA and bar_delta > prior_delta * DELTA_DIVERGENCE:
            strength = (bar_delta - prior_delta) / max(abs(prior_delta), 1)
            return {
                'side':         'buy_exhaustion',
                'price':        l,
                'volume':       bar_total,
                'cur_delta':    bar_delta,
                'prior_delta':  prior_delta,
                'strength':     round(strength, 3),
                'reason':       'new_low_delta_div' if l < swing_low_p else 'rejected_at_low',
            }
        # Proposal E secondary path — 3-bar cumulative delta decay (sell side)
        prior_trail = _cum3([swing_low_idx - 1, swing_low_idx, swing_low_idx + 1])
        if prior_trail <= -MIN_3BAR_PRIOR_DELTA and cur_trail > prior_trail * DECAY_3BAR:
            strength = (cur_trail - prior_trail) / max(abs(prior_trail), 1)
            return {
                'side':         'buy_exhaustion',
                'price':        l,
                'volume':       bar_total,
                'cur_delta':    bar_delta,
                'prior_delta':  prior_delta,
                'cur_3bar_delta':   cur_trail,
                'prior_3bar_delta': prior_trail,
                'strength':     round(strength, 3),
                'reason':       '3bar_decay_at_low',
            }

    return None


def _detect_bar_aggression(symbol, tf, candle, history):
    """AGGRESSION: one-sided delta surge with stacked imbalance + follow-through.

    Confirmation cascade (ALL required):
      (1) Delta surge: |bar_delta| ≥ P75 of last 10 bars' |delta|  (DYNAMIC)
      (2) Stacked imbalance: 3+ adjacent prices with same-side dominance ≥ 70%
      (3) Price follow-through: close in trade direction (c>o for buy, c<o for sell)

    Returns: {side, price, levels, range, volume, strength, ...} or None.
    """
    bp = candle.get("bp")
    if not bp:
        return None
    levels = _bp_level_stats(bp)
    if len(levels) < 3:
        return None

    bar_total = sum(lv['tot'] for lv in levels)
    # Adaptive bar floor — regime-relative noise gate
    if bar_total < _adaptive_bar_floor(symbol, fallback=10.0):
        return None

    h = candle.get("h", 0)
    l = candle.get("l", 0)
    o = candle.get("o", 0)
    c = candle.get("c", 0)
    bar_delta = sum(lv['delta'] for lv in levels)

    # (1) Delta surge — dynamic threshold from recent history
    if history and len(history) >= 5:
        recent_abs_deltas = []
        for prior in list(history)[-10:]:
            recent_abs_deltas.append(abs(_bar_delta(prior)))
        if recent_abs_deltas:
            recent_abs_deltas.sort()
            delta_p75 = recent_abs_deltas[int(len(recent_abs_deltas) * 0.75)]
            if abs(bar_delta) < max(delta_p75, 5):
                return None  # not a surge

    # (2) Stacked imbalance — find runs of 3+ same-side dominant levels
    SIDE_THRESHOLD = 0.70
    runs = []
    cur_run = []
    cur_side = None

    def _flush():
        if cur_run and len(cur_run) >= 3:
            runs.append({
                'side':   cur_side,
                'levels': cur_run.copy(),
                'total':  sum(x['tot'] for x in cur_run),
            })

    for lv in levels:  # sorted by price ascending
        # Per-level noise: skip levels below the adaptive level floor
        # (was hard-coded `< 2`). Below this, side dominance is meaningless.
        if lv['tot'] < _adaptive_level_floor(symbol, fallback=2.0):
            _flush()
            cur_run = []
            cur_side = None
            continue
        # Wilson 95% CI lower bound on side share — replaces raw share check.
        # Rejects small-N levels where high raw % is statistical luck.
        # E.g. 5-lot level 4-buy: raw=0.80 but Wilson=0.38 → not aggressive.
        # 200-lot level 160-buy: raw=0.80 and Wilson=0.74 → confirmed aggressive.
        wilson_b = _wilson_lower(lv['b'], lv['tot'])
        wilson_s = _wilson_lower(lv['s'], lv['tot'])
        if wilson_b >= SIDE_THRESHOLD:
            side = 'b'
        elif wilson_s >= SIDE_THRESHOLD:
            side = 's'
        else:
            _flush()
            cur_run = []
            cur_side = None
            continue
        if cur_side is None or side == cur_side:
            cur_side = side
            cur_run.append(lv)
        else:
            _flush()
            cur_run = [lv]
            cur_side = side
    _flush()

    if not runs:
        return None

    best_run = max(runs, key=lambda r: r['total'])

    # (3) Price follow-through
    if best_run['side'] == 'b' and c <= o:
        return None
    if best_run['side'] == 's' and c >= o:
        return None

    sorted_prices = sorted(x['price'] for x in best_run['levels'])
    principal = sorted_prices[len(sorted_prices) // 2]
    n_levels = len(best_run['levels'])

    strength = (best_run['total'] / bar_total) * (n_levels / 3.0)

    return {
        'side':        'buy_aggression' if best_run['side'] == 'b' else 'sell_aggression',
        'price':       principal,
        'price_range': [sorted_prices[0], sorted_prices[-1]],
        'levels':      n_levels,
        'volume':      best_run['total'],
        'bar_delta':   bar_delta,
        'strength':    round(strength, 3),
    }


def _detect_bar_classical_absorption(symbol, tf, candle, history):
    """CLASSICAL ABSORPTION (delta-price divergence) — added 2026-05-06.

    Captures the textbook order-flow absorption pattern:
        STRONG ONE-SIDED FLOW + SMALL PRICE MOVE = someone is providing
        the opposite side. Different from `_detect_bar_absorption` (B2+B4)
        which requires balanced two-sided flow + visible book defense.

    Formula (per rocketmantwentyone's framework):
        A_t = |delta_t| − λ · |ΔP_t|
    where λ normalizes by recent ATR so price moves are scale-comparable.

    Signature:
        Buyers (or sellers) hammered the offer (or bid) but price barely
        moved → SOMEONE absorbed the flow with hidden/iceberg liquidity.
        Trade implication: take the OPPOSITE side (buyers absorbed → SHORT,
        sellers absorbed → LONG).

    Fires once per bar. Strength = absorbed flow / max(price_move_ticks, 0.5)
    """
    bp = candle.get("bp")
    if not bp:
        return None

    bar_delta = _bar_delta(candle)
    bar_total = _bar_total_volume(candle)

    # Floor: bar must have meaningful activity
    if bar_total < _adaptive_bar_floor(symbol, fallback=50.0):
        return None

    # One-sided dominance: delta must be at least 60% of total
    # (i.e., minor side ≤ 40%). Distinguishes climax/iceberg from
    # balanced two-sided trading.
    if bar_total > 0 and abs(bar_delta) / bar_total < 0.60:
        return None

    # Minimum directional flow magnitude — reject noise
    if abs(bar_delta) < 50:
        return None

    # Price move check
    o = candle.get("o", 0); c = candle.get("c", 0)
    h = candle.get("h", 0); l = candle.get("l", 0)
    if o <= 0 or c <= 0:
        return None
    price_move_ticks = abs(c - o) / 0.25   # NQ tick
    bar_range_ticks = (h - l) / 0.25

    # Volatility normalization: use rolling 10-bar median range as λ-proxy
    # so what counts as "small move" adjusts to the current vol regime.
    if history and len(history) >= 5:
        recent = list(history)[-10:]
        recent_ranges = [(b.get("h", 0) - b.get("l", 0)) / 0.25
                         for b in recent if b.get("h", 0) > b.get("l", 0) > 0]
        if recent_ranges:
            recent_ranges.sort()
            atr_proxy = recent_ranges[len(recent_ranges) // 2]   # median range in ticks
        else:
            atr_proxy = 4.0
    else:
        atr_proxy = 4.0
    if atr_proxy < 1.0:
        atr_proxy = 4.0

    # "Small price move" = bar moved less than ~30% of typical recent range
    # AND ≤ 3 ticks absolute. Both gates required.
    if price_move_ticks > 3 or price_move_ticks > 0.3 * atr_proxy:
        return None

    # Optional further constraint: bar_range itself shouldn't be huge
    # (we want price actually STUCK, not just o≈c with intra-bar wild move).
    if bar_range_ticks > 1.5 * atr_proxy:
        return None

    # Score: absorbed flow per tick of price impact (high = strong absorption)
    strength = abs(bar_delta) / max(price_move_ticks, 0.5)

    # Side: which direction was absorbed?
    #   bar_delta > 0 = buyers were absorbed → expect DOWN move (short signal)
    #   bar_delta < 0 = sellers were absorbed → expect UP move (long signal)
    if bar_delta > 0:
        side = 'classical_buy_absorbed'   # buyers hammered, didn't move price
        signal_dir = 'short'              # take the opposite side
        principal_price = c               # closing price as reference
    else:
        side = 'classical_sell_absorbed'  # sellers hammered, didn't move price
        signal_dir = 'long'
        principal_price = c

    return {
        'side':              side,
        'signal_dir':        signal_dir,
        'price':             principal_price,
        'volume':            bar_total,
        'absorbed_delta':    bar_delta,
        'price_move_ticks':  round(price_move_ticks, 2),
        'bar_range_ticks':   round(bar_range_ticks, 2),
        'atr_proxy_ticks':   round(atr_proxy, 2),
        'strength':          round(strength, 1),
        'one_sided_pct':     round(abs(bar_delta) / max(bar_total, 1), 3),
    }


# ═══════════════════════════════════════════════════════════════════
# BATTLE STATE — third bar-signal layer (added 2026-05-07)
# ═══════════════════════════════════════════════════════════════════
# After absorption fires on a bar, observe the next 30s of L2/trade
# activity at the absorbed level. Compute three features (drift, F12
# refill count, F17 price range) and classify into:
#
#   ABSORBER_WINS_HIGH  drift > p75 AND f17 > p75   (~86% hit, n=35/146)
#   ABSORBER_WINS       drift > p75                  (~75% hit, n=47/146)
#   AGGRESSOR_WINS      drift ≤ 0  AND f12 ≤ p25    (~58% aggressor, n=26/146)
#   NO_SIGNAL           else
#
# All thresholds computed as ROLLING PERCENTILES from last 100 events
# per symbol — so the signal auto-calibrates to current vol regime.
# Cold-start uses absolute defaults until 20 events accumulate.
#
# Validated on 64 days NQ MAR26 parquet; permutation p=0.008,
# 5-fold CV mean 78.2%, bootstrap CI [65.5-87.3].
# Strict live-realistic timing audit confirmed no look-ahead.
# ═══════════════════════════════════════════════════════════════════

_BATTLE_PENDING: dict = defaultdict(deque)        # symbol -> [pending events]
_BATTLE_HISTORY: dict = defaultdict(lambda: deque(maxlen=100))  # symbol -> [completed feature samples]
_BATTLE_LOCK = threading.RLock()
_BATTLE_THREAD_STARTED = False

_BATTLE_OBS_WINDOW_S    = 30.0   # observation window after detection
_BATTLE_BAND_TICKS      = 2      # ± ticks for activity tracking
_BATTLE_MIN_HISTORY     = 20     # min events before switching from cold-start to rolling p75/p25
_BATTLE_LOOP_INTERVAL_S = 5.0    # process pending events every 5s

# Cold-start absolute defaults (NQ-tuned; replaced by rolling percentiles after warmup)
_BATTLE_COLD_DRIFT_P75 = 2.0     # ticks
_BATTLE_COLD_F17_P75   = 2.0     # ticks
_BATTLE_COLD_F12_P25   = 1       # refills

_NQ_TICK = 0.25                  # tick size — TODO: read from symbol config


def _enqueue_battle_state(symbol, tf, abs_event, candle):
    """Called from _emit_bar_signals when absorption fires.
    Queues this event for battle-state evaluation in 30s."""
    K = abs_event.get('price')
    if K is None:
        return
    # Map detector side → absorbed-side terminology:
    #   bullish absorption (near low) = bid defended → SELLERS were absorbed
    #   bearish absorption (near high) = ask defended → BUYERS were absorbed
    side = 'SELL_ABSORBED' if abs_event.get('side') == 'bullish' else 'BUY_ABSORBED'
    bar_t = candle.get('t', 0)
    t_now = time.time()
    event = {
        'event_id': f"{symbol}_{int(t_now*1000)}_{int(K*100)}",
        'symbol': symbol,
        'tf': tf,
        'K': K,
        'side': side,
        'absorption_strength': abs_event.get('strength'),
        'absorption_volume':   abs_event.get('volume'),
        'bar_t': bar_t,
        't_detect_end': t_now,
        't_obs_end':    t_now + _BATTLE_OBS_WINDOW_S,
    }
    with _BATTLE_LOCK:
        _BATTLE_PENDING[symbol].append(event)


def _compute_battle_features(symbol, K, side, t_start, t_end):
    """Aggregate trades + book activity in [t_start, t_end) at K ± BAND.
    Returns {drift, f12, f17, n_trades} or None if insufficient data."""
    history = list(_DOM_HISTORY_T0.get(symbol, []))
    relevant = [s for s in history if t_start <= s[0] < t_end]
    if len(relevant) < 3:
        return None

    # Aggregate trades during obs window (drift + range)
    band_lo = K - _BATTLE_BAND_TICKS * _NQ_TICK
    band_hi = K + _BATTLE_BAND_TICKS * _NQ_TICK
    all_prices = []
    for snap in relevant:
        # snap = (now_ts, snap_bids, snap_asks, compact_trades, compact_abs)
        trades = snap[3] if len(snap) >= 4 else []
        for tr in trades:
            tp = tr.get('p')
            if tp is None:
                continue
            try:
                tp_f = float(tp)
            except (TypeError, ValueError):
                continue
            ts_t = tr.get('t', snap[0])
            if ts_t < t_start or ts_t >= t_end:
                continue
            if band_lo <= tp_f <= band_hi:
                all_prices.append(tp_f)

    if len(all_prices) < 2:
        return None

    max_p = max(all_prices)
    min_p = min(all_prices)
    max_up_t = (max_p - K) / _NQ_TICK
    max_dn_t = (K - min_p) / _NQ_TICK
    if side == 'SELL_ABSORBED':
        drift = max_up_t - max_dn_t   # positive = absorbed direction = UP
    else:
        drift = max_dn_t - max_up_t   # positive = absorbed direction = DOWN

    f17 = (max_p - min_p) / _NQ_TICK   # price range during obs (ticks)

    # F12: refills at K — book size jumps on absorbed-side
    K_str = str(round(float(K), 2))
    book_seq = []
    for snap in relevant:
        bids = snap[1] if len(snap) >= 2 else {}
        asks = snap[2] if len(snap) >= 3 else {}
        # SELL_ABSORBED → buyers providing bid liquidity → check bids
        # BUY_ABSORBED  → sellers providing ask liquidity → check asks
        if side == 'SELL_ABSORBED':
            sz = float(bids.get(K_str, 0) or 0)
        else:
            sz = float(asks.get(K_str, 0) or 0)
        book_seq.append(sz)

    f12 = 0
    for i in range(1, len(book_seq)):
        prev_sz = book_seq[i-1]
        cur_sz  = book_seq[i]
        if prev_sz > 0 and cur_sz > prev_sz * 1.2:
            f12 += 1

    return {'drift': drift, 'f12': f12, 'f17': f17, 'n_trades': len(all_prices)}


def _battle_thresholds(symbol):
    """Return current p75/p25 thresholds — rolling if enough history,
    cold-start defaults otherwise."""
    history = list(_BATTLE_HISTORY.get(symbol, []))
    if len(history) >= _BATTLE_MIN_HISTORY:
        drifts = sorted(h['drift'] for h in history)
        f17s   = sorted(h['f17']   for h in history)
        f12s   = sorted(h['f12']   for h in history)
        return {
            'drift_p75': drifts[int(len(drifts) * 0.75)],
            'f17_p75':   f17s[int(len(f17s) * 0.75)],
            'f12_p25':   f12s[int(len(f12s) * 0.25)],
            'history_n': len(history),
            'cold_start': False,
        }
    return {
        'drift_p75': _BATTLE_COLD_DRIFT_P75,
        'f17_p75':   _BATTLE_COLD_F17_P75,
        'f12_p25':   _BATTLE_COLD_F12_P25,
        'history_n': len(history),
        'cold_start': True,
    }


def _classify_battle_tier(drift, f12, f17, thresholds):
    """Apply the validated tier ladder."""
    drift_p75 = thresholds['drift_p75']
    f17_p75   = thresholds['f17_p75']
    f12_p25   = thresholds['f12_p25']
    if drift > drift_p75 and f17 > f17_p75:
        return ('A+', 'ABSORBER_WINS_HIGH')
    if drift > drift_p75:
        return ('A', 'ABSORBER_WINS')
    if drift <= 0 and f12 <= f12_p25:
        return ('B', 'AGGRESSOR_WINS')
    return ('-', 'NO_SIGNAL')


def _compute_and_emit_battle_state(ev):
    """Called by background loop when an event's obs window has elapsed."""
    symbol = ev['symbol']
    feats = _compute_battle_features(symbol, ev['K'], ev['side'],
                                      ev['t_detect_end'], ev['t_obs_end'])
    if feats is None:
        return  # insufficient L2 data — skip silently

    drift = feats['drift']
    f12   = feats['f12']
    f17   = feats['f17']

    thresholds = _battle_thresholds(symbol)
    tier, label = _classify_battle_tier(drift, f12, f17, thresholds)

    # ALWAYS update history — even NO_SIGNAL events feed the percentile distribution
    with _BATTLE_LOCK:
        _BATTLE_HISTORY[symbol].append({'drift': drift, 'f12': f12, 'f17': f17})

    if label == 'NO_SIGNAL':
        return  # don't emit — the level is dead/chaotic

    if _socketio is None:
        return

    payload = {
        'event_id': ev['event_id'],
        'symbol':   symbol,
        'tf':       ev['tf'],
        'bar_t':    ev['bar_t'],
        'K':        ev['K'],
        'side':     ev['side'],
        'label':    label,
        'tier':     tier,
        'drift':    round(drift, 2),
        'f12':      f12,
        'f17':      round(f17, 2),
        'n_trades': feats['n_trades'],
        'thresholds': {
            'drift_p75':  round(thresholds['drift_p75'], 2),
            'f17_p75':    round(thresholds['f17_p75'], 2),
            'f12_p25':    thresholds['f12_p25'],
            'history_n':  thresholds['history_n'],
            'cold_start': thresholds['cold_start'],
        },
        'absorption_strength': ev.get('absorption_strength'),
        'absorption_volume':   ev.get('absorption_volume'),
    }
    try:
        _socketio.emit('battle_state_update', payload, namespace='/')
        log.info(
            f"[BATTLE] {symbol} {ev['tf']} K={ev['K']} {label} (tier {tier}) "
            f"drift={drift:+.1f}t f12={f12} f17={f17:.1f}t  "
            f"thr(d75={thresholds['drift_p75']:.1f} f17_75={thresholds['f17_p75']:.1f} "
            f"f12_25={thresholds['f12_p25']} n={thresholds['history_n']}"
            f"{' COLD' if thresholds['cold_start'] else ''})"
        )
    except Exception as e:
        log.warning(f"[BATTLE] emit err: {e}")


def _process_battle_pending():
    """Process all pending events whose observation window has elapsed."""
    now = time.time()
    ready = []
    with _BATTLE_LOCK:
        for symbol in list(_BATTLE_PENDING.keys()):
            pending = _BATTLE_PENDING[symbol]
            remaining = deque()
            while pending:
                ev = pending.popleft()
                if now >= ev['t_obs_end']:
                    ready.append(ev)
                else:
                    remaining.append(ev)
            # any event not yet ready stays queued
            _BATTLE_PENDING[symbol] = remaining
    # compute outside lock to keep critical section short
    for ev in ready:
        try:
            _compute_and_emit_battle_state(ev)
        except Exception as e:
            log.warning(f"[BATTLE] compute err for {ev.get('event_id')}: {e}")


def _battle_loop():
    """Background thread: poll pending events every 5s."""
    while True:
        try:
            time.sleep(_BATTLE_LOOP_INTERVAL_S)
            _process_battle_pending()
        except Exception as e:
            log.warning(f"[BATTLE] loop err: {e}")
            time.sleep(10.0)


def _start_battle_thread():
    """Idempotent thread starter."""
    global _BATTLE_THREAD_STARTED
    if _BATTLE_THREAD_STARTED:
        return
    _BATTLE_THREAD_STARTED = True
    t = threading.Thread(target=_battle_loop, daemon=True, name='battle_state_loop')
    t.start()
    log.info("[BATTLE] background loop started")


def _emit_bar_signals(symbol, tf, candle, history):
    """Run all 4 detectors on a closed bar; emit a single bar_signal event
    only when at least one signal fires. Most bars produce no event.

    Detectors:
      - absorption          (B2+B4 microstructure: balanced + book + refill)
      - classical_absorption (delta-price divergence: one-sided flow stuck) ← 2026-05-06
      - exhaustion          (delta divergence at swing extremes)
      - aggression          (stacked imbalance + price follow-through)
    """
    if _socketio is None:
        return

    # ── Update rolling distributions for adaptive floors ──
    # Recorded BEFORE detection so the next bar's gate is regime-current.
    bp = candle.get("bp")
    if bp:
        bar_vol = 0
        for entry in bp.values():
            if isinstance(entry, list) and len(entry) >= 2:
                lv = (entry[0] or 0) + (entry[1] or 0)
                if lv > 0:
                    _LEVEL_VOL_SAMPLES[symbol].append(lv)
                    bar_vol += lv
        if bar_vol > 0:
            _BAR_VOL_SAMPLES[symbol].append(bar_vol)

    # ── INVESTIGATION LOG: capture this bar's full context for offline replay ──
    _now_ms = int(time.time() * 1000)
    _bar_t = candle.get("t", 0)
    if bp:
        try:
            _invest_log("candle_bp", {
                "ts_ms":  _now_ms,
                "bar_t":  _bar_t,
                "symbol": symbol,
                "tf":     tf,
                "ohlc":   {
                    "o": candle.get("o"),
                    "h": candle.get("h"),
                    "l": candle.get("l"),
                    "c": candle.get("c"),
                    "v": candle.get("v"),
                },
                "bp":     {k: list(v) if isinstance(v, list) else v for k, v in bp.items()},
                "delta":  _bar_delta(candle),
            })
        except Exception:
            pass
    # Snapshot adaptive floors at this moment (every bar = ~every minute)
    try:
        _level_samples = _LEVEL_VOL_SAMPLES.get(symbol)
        _bar_samples   = _BAR_VOL_SAMPLES.get(symbol)
        _level_med = None
        _bar_med = None
        if _level_samples and len(_level_samples) >= 1:
            _ls = sorted(_level_samples)
            _level_med = _ls[len(_ls) // 2]
        if _bar_samples and len(_bar_samples) >= 1:
            _bs = sorted(_bar_samples)
            _bar_med = _bs[len(_bs) // 2]
        _invest_log("floors_evolution", {
            "ts_ms":           _now_ms,
            "symbol":          symbol,
            "level_floor":     _adaptive_level_floor(symbol),
            "bar_floor":       _adaptive_bar_floor(symbol),
            "level_samples_n": len(_level_samples) if _level_samples else 0,
            "bar_samples_n":   len(_bar_samples) if _bar_samples else 0,
            "level_median":    _level_med,
            "bar_median":      _bar_med,
        })
    except Exception:
        pass

    try:
        abs_events = _detect_bar_absorption(symbol, candle)
    except Exception as e:
        abs_events = []
        log.warning(f"[BAR_SIGNAL] absorption detect err: {e}")
    try:
        exh_event = _detect_bar_exhaustion(symbol, tf, candle, history)
    except Exception as e:
        exh_event = None
        log.warning(f"[BAR_SIGNAL] exhaustion detect err: {e}")
    try:
        agg_event = _detect_bar_aggression(symbol, tf, candle, history)
    except Exception as e:
        agg_event = None
        log.warning(f"[BAR_SIGNAL] aggression detect err: {e}")
    try:
        cabs_event = _detect_bar_classical_absorption(symbol, tf, candle, history)
    except Exception as e:
        cabs_event = None
        log.warning(f"[BAR_SIGNAL] classical_absorption detect err: {e}")

    log.info(
        f"[BAR_SIGNAL] {symbol} {tf} t={candle.get('t')} "
        f"abs={len(abs_events)} exh={'Y' if exh_event else 'N'} "
        f"agg={'Y' if agg_event else 'N'} cabs={'Y' if cabs_event else 'N'} "
        f"bar_vol={_bar_total_volume(candle)} "
        f"levels={len(candle.get('bp', {}))}"
    )

    # INVESTIGATION LOG: record EVERY bar-signal computation outcome,
    # including 'no signal' results, so we can analyze why the system
    # missed events (false negatives) just as much as why it fired.
    try:
        _invest_log("bar_signals", {
            "ts_ms":      _now_ms,
            "bar_t":      _bar_t,
            "symbol":     symbol,
            "tf":         tf,
            "phase":      "fired" if (abs_events or exh_event or agg_event) else "no_signal",
            "absorption":           abs_events,
            "classical_absorption": cabs_event,
            "exhaustion":           exh_event,
            "aggression":           agg_event,
            "bar_total":            _bar_total_volume(candle),
            "level_count":          len(candle.get('bp', {})),
            "thresholds_at_emit": {
                "level_floor": _adaptive_level_floor(symbol),
                "bar_floor":   _adaptive_bar_floor(symbol),
            },
        })
    except Exception:
        pass

    if not abs_events and not exh_event and not agg_event and not cabs_event:
        return

    try:
        _socketio.emit('bar_signal', {
            'symbol':               symbol,
            'tf':                   tf,
            't':                    candle.get("t", 0),
            'absorption':           abs_events,
            'classical_absorption': cabs_event,    # NEW 2026-05-06
            'exhaustion':           exh_event,
            'aggression':           agg_event,
        }, namespace='/')
        log.info(f"[BAR_SIGNAL] EMITTED {symbol} {tf} t={candle.get('t')}")
    except Exception as e:
        log.warning(f"[BAR_SIGNAL] emit err: {e}")

    # ── BATTLE STATE: queue every absorption fire for 30s observation ──
    # The battle classifier runs ASYNC (background thread), so this just
    # registers events and returns immediately. Verdict emits later as
    # 'battle_state_update' Socket.IO event.
    if abs_events:
        try:
            _start_battle_thread()  # idempotent
            for abs_event in abs_events:
                _enqueue_battle_state(symbol, tf, abs_event, candle)
        except Exception as e:
            log.warning(f"[BATTLE] enqueue err: {e}")


# ═══════════════════════════════════════════════════════════════════
# UPGRADE B: Depth Delta Arrows — passive flow direction per candle
# ═══════════════════════════════════════════════════════════════════
# Compares DOM at candle open vs candle close for prices with trades.
# Positive net delta = passive orders loaded (accumulation).
# Negative net delta = passive orders pulled (trap / exhaustion).

def _compute_depth_deltas(symbol, candle_open_ts, candle_close_ts, bp_keys):
    """Compare DOM at candle open vs close for prices with trades.

    Returns: {price_str: net_delta} where net = bid_delta - ask_delta.
    Positive = bid-favored accumulation, negative = ask-favored distribution.
    Only includes levels where |net| >= 10 lots (noise filter).
    """
    history = list(_DOM_HISTORY_T0.get(symbol, []))
    if len(history) < 2:
        return {}

    # Find snapshots nearest to candle boundaries
    # T0 format: (timestamp, snap_bids, snap_asks, compact_trades, compact_abs)
    open_snap = min(history, key=lambda s: abs(s[0] - candle_open_ts))
    close_snap = min(history, key=lambda s: abs(s[0] - candle_close_ts))

    # Require snaps within 2 seconds of boundary
    if abs(open_snap[0] - candle_open_ts) > 2.0:
        return {}
    if abs(close_snap[0] - candle_close_ts) > 2.0:
        return {}

    # Same snapshot → no delta
    if open_snap[0] == close_snap[0]:
        return {}

    deltas = {}
    for qp in bp_keys:
        ob = float(open_snap[1].get(qp, 0))   # open bid size
        oa = float(open_snap[2].get(qp, 0))   # open ask size
        cb = float(close_snap[1].get(qp, 0))  # close bid size
        ca = float(close_snap[2].get(qp, 0))  # close ask size

        bid_delta = cb - ob
        ask_delta = ca - oa
        net = bid_delta - ask_delta   # positive = bid-favored accumulation

        if abs(net) >= 10:   # threshold: 10 lots minimum
            deltas[qp] = round(net, 1)

    return deltas


def _freeze_candle(candle: dict) -> dict:
    """Create a snapshot of a candle for storage. Strips bubble profile
    entries with zero volume to keep memory tight."""
    snap = {
        "t": candle["t"],
        "o": candle["o"],
        "h": candle["h"],
        "l": candle["l"],
        "c": candle["c"],
        "v": candle["v"],
    }
    bp = candle.get("bp")
    if bp:
        # Only keep levels with actual volume (buy or sell > 0)
        clean = {k: v for k, v in bp.items() if v[0] > 0 or v[1] > 0}
        if clean:
            snap["bp"] = clean
    # ── Orderflow detection results ──
    sweeps = candle.get("sweeps")
    if sweeps:
        snap["sweeps"] = sweeps
    delta_div = candle.get("delta_div")
    if delta_div:
        snap["delta_div"] = delta_div
    ignition = candle.get("ignition")
    if ignition:
        snap["ignition"] = ignition
    spoofs = candle.get("spoofs")
    if spoofs:
        snap["spoofs"] = spoofs
    absorption = candle.get("absorption")
    if absorption:
        snap["absorption"] = absorption
    depth_deltas = candle.get("depth_deltas")
    if depth_deltas:
        snap["depth_deltas"] = depth_deltas
    return snap


def get_candles(symbol: str, tf: str) -> list:
    """Return closed candles + current candle for a symbol/timeframe.
    Thread-safe: uses _CANDLE_LOCK (same lock as _feed_candle).

    Each candle dict has keys: t, o, h, l, c, v, and optionally 'bp'.
    Historical (backfill) candles will NOT have a 'bp' key.
    Live candles will have bp = {price_str: [buy_vol, sell_vol], ...}.
    """
    with _CANDLE_LOCK:
        closed = list(_CANDLES.get(symbol, {}).get(tf, []))
        cur = _CURRENT_CANDLE.get(symbol, {}).get(tf)
        if cur:
            closed.append(_freeze_candle(cur))
    # Crucial: the gap-fill fetches in reverse chronological order (Today, Yesterday, etc)
    # Lightweight Charts strictly requires chronological data.
    closed.sort(key=lambda c: c["t"])
    return closed

def get_refill_stats(symbol: str) -> dict:
    """Return refill speed data per price for VP overlay.
    {price_str: {avg_ms, count, classification}}"""
    return dict(_REFILL_TRACKER.get(symbol, {}))


def _update_bp_signals(entry: list, symbol: str, qp: str) -> None:
    """Update entry[2]=fp_score and entry[3]=true_abs flag.

    Called after each bp update in the trade hot path.

    fp_score (entry[2]) — Shannon-derived imbalance asymmetry:
        H(p) = -[p·log2(p) + (1-p)·log2(1-p)] where p = buy / (buy + sell)
        fp_score = 1.0 - H  → 0.0=perfectly balanced, 1.0=pure one-sided

    true_abs (entry[3]) — multi-condition L2-confirmed absorption:
        (1) flow balanced: fp_score ≤ 0.30  (DERIVED: H ≥ 0.85 ↔ p∈[0.28,0.72])
        (2) book absorbed: book_size_at_trade ≥ 2 × total_flow  (STRUCTURAL margin)
        (3) refill confirmed: refill_class ∈ {instant, fast}    (VERIFIED from
            l2_worker refill tracker — independent persistence test)
        (4) min volume floor: total ≥ 5  (STRUCTURAL — excludes 1-tick noise)
    """
    try:
        b = entry[0] or 0
        s = entry[1] or 0
        tot = b + s
        if tot <= 0:
            return
        # fp_score (always populated when there's any flow)
        if 0.001 < b / tot < 0.999:
            p = b / tot
            H = -(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p))
            entry[2] = round(1.0 - H, 4)
        else:
            entry[2] = 1.0
        # true_abs: only set if all 4 conditions pass
        # (don't unset — once a level qualifies in this candle, it stays flagged)
        if entry[3] == 1:
            return
        if tot < 5:
            return
        if entry[2] is None or entry[2] > 0.30:
            return
        book = entry[4] if (len(entry) >= 5 and entry[4] is not None) else 0
        if book < 2 * tot:
            return
        rs = _REFILL_TRACKER.get(symbol, {}).get(qp)
        rc = rs.get('classification') if rs else None
        if rc in ('instant', 'fast'):
            entry[3] = 1
    except Exception:
        pass


# ── Shared signal store (read by server.py /api/l2 endpoint) ─────────────────
# This dict is updated by the worker thread and read by Flask.
L2_STATE = {
    "connected":     False,
    "dom":           {},      # {symbol: dom_snapshot}
    "quotes":        {},      # {symbol: quote_snapshot}
    "imbalance":     {},      # {symbol: float}
    "mid_prices":    {},      # {symbol: float} quick access
    "price_history": {},      # {symbol: [float,...]} rolling 500 ticks
    "trades":        {},      # {symbol: [{price,vol,side,spin,ts},...]}
    "candles":       {},      # populated on-demand via get_candles()
    "absorption":    {},      # {symbol: {price: {score, hits, agg_vol}}}
    "signals": {
        "shannon_entropy":     None,
        "ising_magnetization": None,
        "reynolds_number":     None,
    },
    "last_update": 0,
}
_L2_LOCK = threading.Lock()


def _compute_book_imbalance(symbol, bp_keys, n_ticks=5):
    """Compute bid/ask depth imbalance within ±n_ticks of each bp price.

    Returns: {price_str: imbalance_ratio} where ratio = bid_total / (bid_total + ask_total).
    0.5 = balanced. >0.5 = bid-heavy (gravitational support). <0.5 = ask-heavy (cap).
    Only includes levels with meaningful depth (total > 10 lots).
    """
    dom = L2_STATE["dom"].get(symbol, {})
    bids = dom.get("bids", {})
    asks = dom.get("asks", {})
    if not bids and not asks:
        return {}

    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    imbalance = {}

    for qp in bp_keys:
        price = float(qp)
        bid_total = 0.0
        ask_total = 0.0

        for offset in range(-n_ticks, n_ticks + 1):
            level_px = str(round(price + offset * tick_size, 2))
            bid_total += float(bids.get(level_px, 0))
            ask_total += float(asks.get(level_px, 0))

        total = bid_total + ask_total
        if total > 10:
            imbalance[qp] = round(bid_total / total, 3)

    return imbalance


# ── Absorption Engine v2: Market-Maker Grade ──────────────────────────────────
# Cross-references aggressive trade tape against passive DOM level changes.
# Tracks PER-SIDE volume, flow-through consumption, attack waves, and intensity.
#
# Data model per price level:
#   buy_vol      – aggressive BUY volume that LIFTED this ask level
#   sell_vol     – aggressive SELL volume that HIT this bid level
#   hits         – total trade count at this price
#   buy_hits     – trade count from aggressive buyers
#   sell_hits    – trade count from aggressive sellers
#   first_ts     – timestamp of first trade in tracking window
#   last_ts      – timestamp of most recent trade
#   waves        – distinct attack bursts (gap ≥ WAVE_GAP_SEC between trades)
#   last_wave_ts – timestamp of last wave start (for gap detection)
#   passive_consumed – cumulative shrinkage of passive size (flow-through)
#   peak_passive – largest passive size seen at this level
#   side_flag    – 'bid' or 'ask' (which side of the book this level sits on)

import math as _math


# ═══════════════════════════════════════════════════════════════════
# UPGRADE C: Micro-OFI Engine — per-level Order Flow Imbalance
# ═══════════════════════════════════════════════════════════════════
# Extends BBO-only OFI to top N levels each side.  Shows where passive
# bids are loading (bullish) vs where passive asks are stacking (bearish).
# Updated on every DOM snapshot; latest values emitted with candle_update.

class MicroOFIEngine:
    """Per-level Order Flow Imbalance for top N levels each side."""

    __slots__ = ("n_levels", "prev_bids", "prev_asks", "ofi", "mean_depth")

    def __init__(self, n_levels=5):
        self.n_levels = n_levels
        self.prev_bids = {}   # {price_str: size}
        self.prev_asks = {}
        self.ofi = {}         # {price_str: weighted_ofi}
        self.mean_depth = 100.0  # running estimate of typical level depth (EMA)

    def update(self, dom):
        """Recompute OFI for top N levels each side. Returns ofi dict.

        Score = (delta / max(current_sz, 1)) * min(current_sz / mean_depth, 2)
        First factor: relative change (direction + proportion).
        Second factor: size weight (large levels matter more, capped at 2x).
        This prevents 1-lot noise on thin levels from scoring same as 100-lot loading.
        """
        bids = dom.get("bids", {})
        asks = dom.get("asks", {})

        # Top N levels by price (best first)
        sorted_bids = sorted(bids.items(), key=lambda x: float(x[0]), reverse=True)[:self.n_levels]
        sorted_asks = sorted(asks.items(), key=lambda x: float(x[0]))[:self.n_levels]

        # Update running mean depth (EMA, alpha=0.05)
        all_sizes = [float(s) for _, s in sorted_bids] + [float(s) for _, s in sorted_asks]
        if all_sizes:
            current_mean = sum(all_sizes) / len(all_sizes)
            self.mean_depth = 0.95 * self.mean_depth + 0.05 * current_mean

        ofi = {}

        # Bid-side OFI: positive delta = passive buying loading
        for px, sz in sorted_bids:
            sz = float(sz)
            prev = self.prev_bids.get(px, sz)   # first tick: no delta
            delta = sz - prev
            norm_ofi = delta / max(sz, 1)
            size_weight = min(sz / max(self.mean_depth, 1), 2.0)
            ofi[px] = round(norm_ofi * size_weight, 4)

        # Ask-side OFI: negative convention (ask loading = bearish pressure)
        for px, sz in sorted_asks:
            sz = float(sz)
            prev = self.prev_asks.get(px, sz)
            delta = sz - prev
            norm_ofi = -delta / max(sz, 1)
            size_weight = min(sz / max(self.mean_depth, 1), 2.0)
            ofi[px] = round(norm_ofi * size_weight, 4)

        self.prev_bids = dict(sorted_bids)
        self.prev_asks = dict(sorted_asks)
        self.ofi = ofi
        return ofi


_MICRO_OFI: dict = {}   # {symbol: MicroOFIEngine}
_DEPTH_VEL_CACHE: dict = {}  # {symbol: {priceStr: rate}} — latest depth velocity per symbol (FIX M8)


# ── Price-key normalizer ──────────────────────────────────────────────────
# DOM keys = str(float_from_feed), trade keys = str(round(round(p/tick)*tick, 2)).
# These USUALLY match but can diverge on off-grid prices, string-sourced prices,
# or float repr edge cases. This normalizer guarantees a single canonical form
# so dict lookups never silently miss.

def _norm_pk(pk, tick_size=0.25):
    """Normalize a price key to canonical 2-decimal string format.

    "17850.5" → "17850.50",  "17850" → "17850.00",  "17850.250" → "17850.25"

    This matches JavaScript's price.toFixed(2) exactly, so backend dict keys
    and frontend lookups use the same format — no fallback chains needed.
    """
    try:
        p = float(pk)
        # Snap to tick grid (handles off-grid prices and float drift)
        p = round(round(p / tick_size) * tick_size, 2)
        return f"{p:.2f}"
    except (ValueError, TypeError):
        return str(pk)


# ═══════════════════════════════════════════════════════════════════════════
# ENGINE 1: Queue Dynamics — arrival/cancellation/execution decomposition
# ═══════════════════════════════════════════════════════════════════════════

class QueueDynamicsEngine:
    """Per-level queue decomposition: arrivals vs cancellations vs executions.

    Between consecutive T0 snapshots (~500ms), for top 20 levels per side:
      executed    = trades that consumed passive orders (from compact_trades)
      size_delta  = snap[t+1].size - snap[t].size
      net_passive = size_delta + executed  (add back consumed volume)
      arrived     = max(net_passive, 0)
      cancelled   = max(-net_passive, 0)

    EMA of arrival_rate and cancel_rate per level.
    ratio = arrival / cancel → >1 strengthening, <1 weakening.

    Self-calibrating: EMA smooths noise. No significance threshold.
    """
    __slots__ = ('n_levels', '_prev_bids', '_prev_asks',
                 '_arr_ema', '_can_ema', '_exe_ema',
                 '_alpha', '_decay_factor', '_tick_size')

    def __init__(self, n_levels=20, alpha=0.15, tick_size=0.25):
        self.n_levels = n_levels
        self._prev_bids = {}   # {price_str: size}
        self._prev_asks = {}
        self._arr_ema = {}     # {price_str: float}  arrival EMA
        self._can_ema = {}     # {price_str: float}  cancel EMA
        self._exe_ema = {}     # {price_str: float}  execution EMA
        self._alpha = alpha
        self._decay_factor = 0.97  # shrink stale EMAs each tick
        self._tick_size = tick_size

    def update(self, snap_bids, snap_asks, compact_trades):
        """Decompose DOM changes into arrivals/cancellations/executions.

        Args:
            snap_bids: {price_str: size} current bid snapshot
            snap_asks: {price_str: size} current ask snapshot
            compact_trades: [{"p": price, "v": volume, "s": side}, ...]
        """
        a = self._alpha

        # 1. Build executed_at_price from compact_trades (normalized keys)
        ts = self._tick_size
        exe_at = {}
        for t in compact_trades:
            npk = _norm_pk(t["p"], ts)
            exe_at[npk] = exe_at.get(npk, 0) + t["v"]

        # 2. Top N levels each side (normalize DOM keys to match trade keys)
        top_bids = sorted(snap_bids.items(), key=lambda x: float(x[0]), reverse=True)[:self.n_levels]
        top_asks = sorted(snap_asks.items(), key=lambda x: float(x[0]))[:self.n_levels]

        active_keys = set()

        # 3. Decompose bid side
        for raw_pk, sz in top_bids:
            pk = _norm_pk(raw_pk, ts)
            sz = int(sz)
            active_keys.add(pk)
            prev = self._prev_bids.get(pk, sz)  # first tick: no delta
            exe = exe_at.get(pk, 0)
            size_delta = sz - prev
            net_passive = size_delta + exe  # add back what was consumed
            arrived = max(net_passive, 0)
            cancelled = max(-net_passive, 0)
            self._arr_ema[pk] = a * arrived + (1 - a) * self._arr_ema.get(pk, 0)
            self._can_ema[pk] = a * cancelled + (1 - a) * self._can_ema.get(pk, 0)
            self._exe_ema[pk] = a * exe + (1 - a) * self._exe_ema.get(pk, 0)

        # 4. Decompose ask side
        for raw_pk, sz in top_asks:
            pk = _norm_pk(raw_pk, ts)
            sz = int(sz)
            active_keys.add(pk)
            prev = self._prev_asks.get(pk, sz)
            exe = exe_at.get(pk, 0)
            size_delta = sz - prev
            net_passive = size_delta + exe
            arrived = max(net_passive, 0)
            cancelled = max(-net_passive, 0)
            self._arr_ema[pk] = a * arrived + (1 - a) * self._arr_ema.get(pk, 0)
            self._can_ema[pk] = a * cancelled + (1 - a) * self._can_ema.get(pk, 0)
            self._exe_ema[pk] = a * exe + (1 - a) * self._exe_ema.get(pk, 0)

        # 5. Decay stale keys not in current top N
        stale = [k for k in self._arr_ema if k not in active_keys]
        df = self._decay_factor
        for k in stale:
            self._arr_ema[k] *= df
            self._can_ema[k] *= df
            self._exe_ema[k] *= df
            # Prune near-zero entries to prevent memory leak
            if self._arr_ema[k] < 0.01 and self._can_ema[k] < 0.01 and self._exe_ema.get(k, 0) < 0.01:
                self._arr_ema.pop(k, None)
                self._can_ema.pop(k, None)
                self._exe_ema.pop(k, None)

        # 6. Store current snapshot (normalized keys)
        self._prev_bids = {_norm_pk(k, ts): int(v) for k, v in top_bids}
        self._prev_asks = {_norm_pk(k, ts): int(v) for k, v in top_asks}

    def get_state(self):
        """Return {price_str: {arr, can, exe, ratio}} for active levels."""
        result = {}
        for pk in self._arr_ema:
            arr = self._arr_ema.get(pk, 0)
            can = self._can_ema.get(pk, 0)
            exe = self._exe_ema.get(pk, 0)
            if arr < 0.1 and can < 0.1 and exe < 0.1:
                continue  # skip silent levels
            ratio = round(arr / max(can, 0.01), 2)
            result[pk] = {
                "arr": round(arr, 1),
                "can": round(can, 1),
                "exe": round(exe, 1),
                "ratio": ratio,
            }
        return result

    def reset(self):
        self.__init__(self.n_levels, self._alpha, self._tick_size)


_QUEUE_DYNAMICS: dict = {}  # {symbol: QueueDynamicsEngine}


# ═══════════════════════════════════════════════════════════════════════════
# ENGINE 2: Trade Toxicity — per-level adverse selection measurement
# ═══════════════════════════════════════════════════════════════════════════

class TradeToxicityTracker:
    """Per-level adverse selection: was the trade informed or noise?

    For each trade, record (ts, price, side, level). After 10s, check where
    mid-price went. Permanently moved in trade direction = informed (toxic).
    Reverted = noise (safe).

    toxicity = EMA of normalized realized impact per level.
    1.0 = 100% informed flow (avoid posting here)
    0.0 = 100% noise (safe to post)

    Self-calibrating: prior starts at 0.5 (neutral).
    EMA alpha = 0.1, half-life ~7 observations.
    """
    __slots__ = ('_pending', '_tox_ema', '_alpha', '_tick_size')

    def __init__(self, alpha=0.1, tick_size=0.25):
        self._pending = deque(maxlen=2000)  # (ts, price, side, level_str, resolved_10s)
        self._tox_ema = {}  # {price_str: float}  EMA toxicity [0, 1]
        self._alpha = alpha
        self._tick_size = tick_size

    def record_trade(self, ts, price, side, price_level_str):
        """Called from on_trade() for every classified trade."""
        npk = _norm_pk(price_level_str, self._tick_size)
        self._pending.append([ts, price, side, npk, False])

    def resolve(self, now, current_mid):
        """Called from _record_dom_snapshot() every ~500ms.
        Resolve mature entries and update per-level toxicity EMA.
        """
        if not self._pending or current_mid <= 0:
            return

        a = self._alpha
        ts = self._tick_size

        # Process from oldest to newest
        to_pop = 0
        for i, entry in enumerate(self._pending):
            trade_ts, trade_price, side, level_str, resolved_10s = entry
            age = now - trade_ts

            if age < 10:
                break  # rest are younger, stop scanning

            # 10s resolution
            if not resolved_10s:
                direction = 1.0 if side == 'b' else -1.0
                realized = (current_mid - trade_price) * direction
                # Normalize by tick_size, clamp, map to [0, 1]
                norm_impact = realized / ts
                norm_tox = max(0.0, min(1.0, (norm_impact + 1.0) / 2.0))
                self._tox_ema[level_str] = (
                    a * norm_tox + (1 - a) * self._tox_ema.get(level_str, 0.5)
                )
                entry[4] = True  # mark resolved

            # Pop entries older than 30s (fully resolved, no longer needed)
            if age >= 30:
                to_pop = i + 1

        # Bulk pop resolved entries
        for _ in range(to_pop):
            self._pending.popleft()

        # L1: Prune stale _tox_ema entries near neutral that haven't been updated recently
        # (prevents unbounded growth when levels go out of range)
        if len(self._tox_ema) > 200:
            active_levels = {e[3] for e in self._pending}
            stale_keys = [k for k, v in self._tox_ema.items()
                          if abs(v - 0.5) < 0.02 and k not in active_levels]
            for k in stale_keys:
                del self._tox_ema[k]

    def get_state(self):
        """Return {price_str: {t10: float}} for levels with data."""
        result = {}
        for pk, tox in self._tox_ema.items():
            # Only emit if meaningfully away from neutral
            if abs(tox - 0.5) > 0.03:
                result[pk] = {"t10": round(tox, 3)}
        return result

    def reset(self):
        self.__init__(self._alpha, self._tick_size)


_TRADE_TOXICITY: dict = {}  # {symbol: TradeToxicityTracker}


# ═══════════════════════════════════════════════════════════════════════════
# ENGINE 3: Level Survival — Bayesian P(hold) per price bucket
# ═══════════════════════════════════════════════════════════════════════════

class LevelSurvivalModel:
    """Bayesian survival: P(level holds when tested).

    Per 10-tick price bucket per side, maintains Beta(α, β) posterior.
    α = 1 + times_held  (WALL/ABS from absorption engine)
    β = 1 + times_broke  (CRACK from absorption engine)
    P(hold) = α / (α + β)

    Enriched: base_survival * queue_factor * (1 - toxicity_penalty)

    Self-calibrating: Beta distribution IS the mechanism.
    Prior = Beta(1,1) = uniform. Concentrates with data.
    Slow decay (0.999/tick) adapts to regime changes (~6 min half-life).
    """
    __slots__ = ('_alpha_map', '_beta_map', '_seen_events',
                 '_decay_rate', '_tick_size', '_bucket_size')

    def __init__(self, decay_rate=0.999, tick_size=0.25):
        self._alpha_map = {}   # {(bucket, side): float}
        self._beta_map = {}
        self._seen_events = set()  # deduplicate within same snapshot
        self._decay_rate = decay_rate
        self._tick_size = tick_size
        self._bucket_size = tick_size * 10  # 10-tick buckets

    def _bucket(self, price_str):
        p = float(price_str)
        return round(round(p / self._bucket_size) * self._bucket_size, 2)

    def observe(self, abs_tiers):
        """Update Beta posteriors from absorption tier classifications.

        Args:
            abs_tiers: {price_key: {"tier": int, "label": str, "sd": str, ...}}
        """
        # Apply slow decay to all entries (toward prior)
        dr = self._decay_rate
        for k in list(self._alpha_map.keys()):
            self._alpha_map[k] = max(1.0, self._alpha_map[k] * dr)
            self._beta_map[k] = max(1.0, self._beta_map.get(k, 1.0) * dr)

        # Process new observations
        new_seen = set()
        for pk, td in abs_tiers.items():
            bucket = self._bucket(pk)
            side = td.get("sd", td.get("side", "bid"))
            if isinstance(side, str) and side in ("ask", "bid"):
                pass
            else:
                side = "bid"
            key = (bucket, side)

            # Deduplicate: only count each event once
            new_seen.add(key)  # always remember this key
            if key in self._seen_events:
                continue  # already counted in a previous snapshot

            label = td.get("label", "")
            if label in ("WALL", "ABS", "SUPER_WALL"):
                self._alpha_map[key] = self._alpha_map.get(key, 1.0) + 1.0
                if key not in self._beta_map:
                    self._beta_map[key] = 1.0
            elif label == "CRACK":
                self._beta_map[key] = self._beta_map.get(key, 1.0) + 1.0
                if key not in self._alpha_map:
                    self._alpha_map[key] = 1.0

        self._seen_events = new_seen

    def get_survival(self, snap_bids, snap_asks, mid_price,
                     queue_dynamics=None, toxicity=None):
        """Return {price_str: survival_probability} for visible levels.

        Args:
            snap_bids/snap_asks: current DOM
            mid_price: current mid
            queue_dynamics: output from QueueDynamicsEngine.get_state()
            toxicity: output from TradeToxicityTracker.get_state()
        """
        if not snap_bids and not snap_asks:
            return {}

        n = 20
        top_bids = sorted(snap_bids.items(), key=lambda x: float(x[0]), reverse=True)[:n]
        top_asks = sorted(snap_asks.items(), key=lambda x: float(x[0]))[:n]

        result = {}
        _ts = self._tick_size
        for side_label, levels in (("bid", top_bids), ("ask", top_asks)):
            for raw_pk, _ in levels:
                pk = _norm_pk(raw_pk, _ts)
                bucket = self._bucket(pk)
                key = (bucket, side_label)

                alpha = self._alpha_map.get(key, 1.0)
                beta = self._beta_map.get(key, 1.0)
                base = alpha / (alpha + beta)

                # Enrich with queue dynamics (normalized key lookup)
                if queue_dynamics and pk in queue_dynamics:
                    ratio = queue_dynamics[pk].get("ratio", 1.0)
                    qd_factor = min(ratio, 2.0) / 2.0  # [0, 1]
                    base *= (0.5 + 0.5 * qd_factor)

                # Enrich with toxicity (normalized key lookup)
                if toxicity and pk in toxicity:
                    tox = toxicity[pk].get("t10", 0.5)
                    penalty = max(tox - 0.5, 0) * 0.5  # [0, 0.25]
                    base *= (1.0 - penalty)

                result[pk] = round(max(0.0, min(1.0, base)), 3)

        return result

    def reset(self):
        self.__init__(self._decay_rate, self._tick_size)


_LEVEL_SURVIVAL: dict = {}  # {symbol: LevelSurvivalModel}


_ABSORPTION: dict = defaultdict(dict)  # {sym: {price_key: {...per-level data...}}}
_PREV_DOM_SNAP: dict = {}              # {sym: {bids:{p:sz}, asks:{p:sz}}}
_ABSORPTION_HALF_LIFE = 20.0           # exponential decay half-life in seconds
_WAVE_GAP_SEC = 2.0                    # ≥2s gap = new attack wave

# ═══════════════════════════════════════════════════════════════════
# ABSORPTION ENGINE v2 — consumed/pulled/refilled math
# ═══════════════════════════════════════════════════════════════════
# Between two DOM snapshots at a given price level, three things can happen:
#   depth_drop = max(prev_size - curr_size, 0)     ← depth that disappeared
#   traded     = sum of trades at this price between snapshots
#
#   consumed = min(depth_drop, traded)            ← real absorption by trades
#   pulled   = max(depth_drop - traded, 0)        ← MM yanked orders (spoof)
#   refilled = max(traded - depth_drop, 0)        ← reload → TRUE absorption
#
# Aggregate over time → ratios:
#   refill_ratio = total_refilled / total_traded    (0→1, higher = stronger absorption)
#   pull_ratio   = total_pulled   / (consumed+pulled) (higher = spoofier wall)
#
# Tiering:
#   FORTRESS: refill ≥ 0.7, traded ≥ 100 → immovable wall
#   SOLID:    refill ≥ 0.5, traded ≥ 50  → strong absorption
#   HELD:     refill ≥ 0.3, traded ≥ 30  → moderate absorption
#   FAKE:     pull_ratio ≥ 0.6, pulled ≥ 30 → spoof — fade this


def _track_absorption_trade(symbol, price_key, volume, side, ts):
    """
    Called on every trade. Buffers traded volume per price level for
    reconciliation against DOM delta on next _compute_absorption_scores call.

    side == 'b' → aggressive buyer LIFTED an ask → attack on ASK level
    side == 's' → aggressive seller HIT a bid   → attack on BID level
    """
    tracker = _ABSORPTION[symbol]
    if price_key not in tracker:
        tracker[price_key] = {
            "side": "ask" if side == "b" else "bid" if side == "s" else None,
            "first_ts": ts, "last_ts": ts,
            "hits": 0, "buy_hits": 0, "sell_hits": 0,
            "buy_vol": 0, "sell_vol": 0,
            # Per-window buffer (reset each DOM snap):
            "traded_buffer": 0,
            # Cumulative (over entry's lifetime):
            "total_traded":   0,
            "total_consumed": 0,
            "total_pulled":   0,
            "total_refilled": 0,
            # Book state:
            "peak_passive": 0,
            # Wave tracking:
            "waves": 1, "last_wave_ts": ts,
        }

    e = tracker[price_key]

    # Side determination: latest trade side governs which book side we track.
    # If side flips mid-lifetime, this level has switched from offer→bid or vice versa,
    # which means the absorption regime has changed. We don't reset (keeps history)
    # but the reconciliation will naturally re-align.
    if side == "b":
        e["buy_vol"]  += volume
        e["buy_hits"] += 1
        e["side"] = "ask"
    elif side == "s":
        e["sell_vol"]  += volume
        e["sell_hits"] += 1
        e["side"] = "bid"
    else:
        half = volume // 2 or 1
        e["buy_vol"]  += half
        e["sell_vol"] += half

    # Buffer the trade volume — reconciled against DOM delta next snap
    e["traded_buffer"] += volume
    e["hits"] += 1

    # Wave detection: new burst if ≥ WAVE_GAP_SEC since last trade
    if ts - e["last_wave_ts"] >= _WAVE_GAP_SEC:
        e["waves"] += 1
        e["last_wave_ts"] = ts

    e["last_ts"] = ts


def _compute_absorption_scores(symbol, dom):
    """
    Called on every DOM update. Reconciles buffered trades against
    DOM delta using consumed/pulled/refilled decomposition.

    For each tracked price level between previous DOM snap and now:
        depth_drop = max(prev_size - curr_size, 0)   # depth lost
        traded     = trades buffered since last snap

        consumed = min(depth_drop, traded)           # real absorption
        pulled   = max(depth_drop - traded, 0)       # spoof (MM pulled)
        refilled = max(traded - depth_drop, 0)       # reload (TRUE absorption)

    Aggregate → refill_ratio = refilled/traded, pull_ratio = pulled/(pulled+consumed)

    Tier cutoffs are ADAPTIVE (Sprint 1 — session-rolling percentiles):
        FORTRESS (3): refill ≥ P90(refill_samples) AND total_traded ≥ P75(traded_samples)
        SOLID    (2): refill ≥ P75 AND total_traded ≥ P50
        HELD     (1): refill ≥ P50 AND total_traded ≥ P25
        FAKE     (0, fake=True): pull_ratio ≥ P90(pull_samples) AND total_pulled ≥ 30

    During warmup (<500 trades OR <100 refill samples) falls back to the
    hardcoded 0.7/0.5/0.3 refill + 100/50/30 traded constants for continuity
    with older behavior.
    """
    now = time.time()
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    bids = {_norm_pk(k, tick_size): v for k, v in dom.get("bids", {}).items()}
    asks = {_norm_pk(k, tick_size): v for k, v in dom.get("asks", {}).items()}
    prev = _PREV_DOM_SNAP.get(symbol, {"bids": {}, "asks": {}})
    prev_bids = {_norm_pk(k, tick_size): v for k, v in prev.get("bids", {}).items()}
    prev_asks = {_norm_pk(k, tick_size): v for k, v in prev.get("asks", {}).items()}

    tracker = _ABSORPTION[symbol]
    scores = {}
    purge_keys = []

    # Sprint 1: hoist adaptive-threshold lookup out of the per-level loop.
    # Single call per DOM tick, shared across every price level.
    _adapt = _get_adaptive_thresholds(symbol)
    _f_refill = _adapt.get("refill_p90", 0.7)
    _s_refill = _adapt.get("refill_p75", 0.5)
    _h_refill = _adapt.get("refill_p50", 0.3)
    _f_vol    = max(int(_adapt.get("traded_p75", 100)), 30)
    _s_vol    = max(int(_adapt.get("traded_p50", 50)),  20)
    _h_vol    = max(int(_adapt.get("traded_p25", 30)),  10)
    _fake_pull = _adapt.get("pull_p90", 0.6)

    for price_key, entry in list(tracker.items()):
        age = now - entry["first_ts"]

        # Exponential decay for relevance weighting (does NOT shrink ratios)
        decay_factor = _math.exp(-0.693 * age / _ABSORPTION_HALF_LIFE)
        # Purge levels with no recent activity and low weight
        if decay_factor < 0.05 and (now - entry["last_ts"]) > _ABSORPTION_HALF_LIFE:
            purge_keys.append(price_key)
            continue

        # Need minimum activity to score
        if entry["hits"] < 2:
            continue

        side = entry.get("side")
        if side is None:
            continue

        # O(1) DOM lookup on current and previous snapshots
        if side == "bid":
            curr_size = bids.get(price_key, 0)
            prev_size = prev_bids.get(price_key, 0)
        else:
            curr_size = asks.get(price_key, 0)
            prev_size = prev_asks.get(price_key, 0)

        # ── Core reconciliation ──
        traded     = entry["traded_buffer"]
        depth_drop = max(prev_size - curr_size, 0)

        consumed = min(depth_drop, traded)
        pulled   = max(depth_drop - traded, 0)
        refilled = max(traded - depth_drop, 0)

        # Accumulate cumulative totals
        entry["total_traded"]   += traded
        entry["total_consumed"] += consumed
        entry["total_pulled"]   += pulled
        entry["total_refilled"] += refilled

        # Reset per-window buffer
        entry["traded_buffer"] = 0

        if curr_size > entry["peak_passive"]:
            entry["peak_passive"] = curr_size

        # Per-level aggregate stats
        t_traded   = entry["total_traded"]
        t_consumed = entry["total_consumed"]
        t_pulled   = entry["total_pulled"]
        t_refilled = entry["total_refilled"]

        # Ratios (0→1 scale)
        refill_ratio   = t_refilled / max(t_traded, 1)
        consumed_ratio = t_consumed / max(t_traded, 1)
        pull_base      = t_pulled + t_consumed
        pull_ratio     = t_pulled / max(pull_base, 1)

        # Kalman OFI confluence — price stuck under heavy flow boosts conviction
        kalman = _KALMAN_OFI.get(symbol)
        kalman_confirm = False
        if kalman and kalman.ready and abs(kalman.theta) < 0.2:
            kalman_confirm = True  # flow heavy but price not moving → absorption

        # ── Sprint 1: sample refill/traded/pull into rolling distribution ──
        # Noise floor t_traded >= 10 ignores micro levels dominated by book noise.
        if t_traded >= 10:
            _ms_sample = _MARKET_STATS[symbol]
            _ms_sample["refill_samples"].append(refill_ratio)
            _ms_sample["traded_samples"].append(t_traded)
            if pull_ratio >= 0.2:
                _ms_sample["pull_samples"].append(pull_ratio)

        # ── Tiering (adaptive thresholds — session-rolling percentiles) ──
        # Cutoffs hoisted above the loop for perf.
        tier = 0
        label = ""
        fake = False

        if pull_ratio >= _fake_pull and t_pulled >= 30:
            fake = True
            label = "FAKE"
        elif refill_ratio >= _f_refill and t_traded >= _f_vol:
            tier = 3; label = "FORTRESS"
        elif refill_ratio >= _s_refill and t_traded >= _s_vol:
            tier = 2; label = "SOLID"
        elif refill_ratio >= _h_refill and t_traded >= _h_vol:
            tier = 1; label = "HELD"

        # Kalman confirm boosts HELD → SOLID if refill is strong but volume low
        if kalman_confirm and tier == 1 and refill_ratio >= 0.5:
            tier = 2; label = "SOLID"

        # Intensity: trades per second (burst detection)
        duration = max(age, 0.1)
        side_hits = entry["sell_hits"] if side == "bid" else entry["buy_hits"]
        intensity = side_hits / duration

        # Backward-compatible "score" for frontend sort/filter.
        # 0→10 scale, dominated by refill_ratio, boosted by volume
        vol_boost = min(_math.log10(t_traded + 1) / 2.0, 1.5)  # caps at traded=1000
        legacy_score = refill_ratio * 10.0 * (1.0 + vol_boost * 0.3)

        scores[price_key] = {
            # Primary signal fields
            "tier": tier,
            "label": label,
            "fake": fake,
            "side": side,
            # Ratios (the real signal)
            "refill_ratio":   round(refill_ratio, 3),
            "consumed_ratio": round(consumed_ratio, 3),
            "pull_ratio":     round(pull_ratio, 3),
            # Cumulative totals
            "total_traded":   t_traded,
            "total_consumed": t_consumed,
            "total_pulled":   t_pulled,
            "total_refilled": t_refilled,
            # Book state
            "curr_passive": curr_size,
            "peak_passive": entry["peak_passive"],
            # Tape stats
            "hits": entry["hits"],
            "side_hits": side_hits,
            "buy_vol": entry["buy_vol"],
            "sell_vol": entry["sell_vol"],
            "intensity": round(intensity, 2),
            "waves": entry["waves"],
            "age": round(age, 1),
            "decay": round(decay_factor, 3),
            # Legacy compatibility (frontend sort/filter)
            "score": round(legacy_score, 2),
            "raw_score": round(refill_ratio * 10.0, 2),
            "passive_consumed": t_consumed,
        }

    # Cleanup decayed entries
    for k in purge_keys:
        del tracker[k]

    # Store current DOM as previous for next delta
    _PREV_DOM_SNAP[symbol] = {
        "bids": {str(k): v for k, v in bids.items()},
        "asks": {str(k): v for k, v in asks.items()},
    }

    # ── Cluster adjacent same-side levels (within 2 ticks) ──
    clustered = _cluster_absorption_levels(scores, tick_size)

    # Publish
    with _L2_LOCK:
        L2_STATE["absorption"][symbol] = clustered


def _cluster_absorption_levels(scores: dict, tick_size: float) -> dict:
    """
    Merge adjacent same-side absorption levels within 2 ticks into composite clusters.
    A single wall spanning 26330.00, 26330.25, 26330.50 becomes ONE cluster.

    Anchor = price with highest total_refilled in the cluster.
    Cluster stats = sum of all members.
    Cluster tier = max tier among members (FORTRESS wins over SOLID).
    Cluster fake = True only if ALL members are fake.

    Returns dict keyed by anchor price with 'members' list for transparency.
    """
    if not scores:
        return {}

    # Separate by side so we don't merge bid + ask at same price
    by_side = {"bid": [], "ask": []}
    for pk, data in scores.items():
        side = data.get("side")
        if side in ("bid", "ask"):
            try:
                by_side[side].append((float(pk), pk, data))
            except (ValueError, TypeError):
                continue

    merged = {}
    cluster_radius = tick_size * 2.0  # 2 ticks tolerance

    for side, levels in by_side.items():
        if not levels:
            continue
        # Sort by price ascending
        levels.sort(key=lambda x: x[0])

        cluster = None  # {"members": [(price, pk, data), ...], "min": p, "max": p}
        cluster_list = []

        for price, pk, data in levels:
            if cluster is None:
                cluster = {"members": [(price, pk, data)], "min": price, "max": price}
            elif price - cluster["max"] <= cluster_radius:
                cluster["members"].append((price, pk, data))
                cluster["max"] = price
            else:
                cluster_list.append(cluster)
                cluster = {"members": [(price, pk, data)], "min": price, "max": price}
        if cluster is not None:
            cluster_list.append(cluster)

        # Collapse each cluster into one output entry
        for c in cluster_list:
            members = c["members"]
            if len(members) == 1:
                # Singleton — emit as-is but add cluster metadata
                _, pk, data = members[0]
                data["cluster_size"]   = 1
                data["cluster_span"]   = 0.0
                data["cluster_prices"] = [members[0][0]]
                merged[pk] = data
                continue

            # Composite cluster
            sum_traded   = sum(m[2]["total_traded"]   for m in members)
            sum_consumed = sum(m[2]["total_consumed"] for m in members)
            sum_pulled   = sum(m[2]["total_pulled"]   for m in members)
            sum_refilled = sum(m[2]["total_refilled"] for m in members)

            # Anchor = member with highest refilled
            anchor_member = max(members, key=lambda m: m[2]["total_refilled"])
            anchor_pk = anchor_member[1]
            anchor_data = dict(anchor_member[2])  # copy

            # Recompute ratios from sums
            refill_ratio   = sum_refilled / max(sum_traded, 1)
            consumed_ratio = sum_consumed / max(sum_traded, 1)
            pull_base      = sum_pulled + sum_consumed
            pull_ratio     = sum_pulled / max(pull_base, 1)

            # Max tier across members
            max_tier = max((m[2]["tier"] for m in members), default=0)
            # Fake only if all members fake
            all_fake = all(m[2].get("fake", False) for m in members)

            # Rebuild label from aggregate
            label = ""
            fake = False
            if all_fake and pull_ratio >= 0.6 and sum_pulled >= 30:
                fake = True
                label = "FAKE"
            elif refill_ratio >= 0.7 and sum_traded >= 100:
                label = "FORTRESS"; max_tier = 3
            elif refill_ratio >= 0.5 and sum_traded >= 50:
                label = "SOLID"; max_tier = max(max_tier, 2)
            elif refill_ratio >= 0.3 and sum_traded >= 30:
                label = "HELD"; max_tier = max(max_tier, 1)

            # Sum volumes and hits
            sum_buy_vol  = sum(m[2]["buy_vol"]  for m in members)
            sum_sell_vol = sum(m[2]["sell_vol"] for m in members)
            sum_hits     = sum(m[2]["hits"]     for m in members)
            sum_curr     = sum(m[2]["curr_passive"] for m in members)
            sum_peak     = sum(m[2]["peak_passive"] for m in members)

            # Recompute legacy score
            vol_boost = min(_math.log10(sum_traded + 1) / 2.0, 1.5)
            legacy_score = refill_ratio * 10.0 * (1.0 + vol_boost * 0.3)

            anchor_data.update({
                "tier": max_tier,
                "label": label,
                "fake": fake,
                "refill_ratio":   round(refill_ratio, 3),
                "consumed_ratio": round(consumed_ratio, 3),
                "pull_ratio":     round(pull_ratio, 3),
                "total_traded":   sum_traded,
                "total_consumed": sum_consumed,
                "total_pulled":   sum_pulled,
                "total_refilled": sum_refilled,
                "buy_vol":  sum_buy_vol,
                "sell_vol": sum_sell_vol,
                "hits":     sum_hits,
                "curr_passive": sum_curr,
                "peak_passive": sum_peak,
                "passive_consumed": sum_consumed,
                "score":     round(legacy_score, 2),
                "raw_score": round(refill_ratio * 10.0, 2),
                "cluster_size":   len(members),
                "cluster_span":   round(c["max"] - c["min"], 2),
                "cluster_prices": [m[0] for m in members],
            })
            merged[anchor_pk] = anchor_data

    return merged


# ═══════════════════════════════════════════════════════════════════════════════
# V2 SIGNAL ENGINE: AdaptiveKalmanOFI + HawkesBranchingRatio
# ═══════════════════════════════════════════════════════════════════════════════
# These classes run O(1) per tick in the backend. They emit pre-classified
# signals via the v2_signals Socket.IO channel so the frontend becomes a pure
# renderer with no local re-detection.
# ═══════════════════════════════════════════════════════════════════════════════

class AdaptiveKalmanOFI:
    """
    State-space model for Order Flow Imbalance with dual-adaptive noise.
    
    State:       θ_τ = latent efficient price drift
    Observation: z_τ = raw OFI = (buy_vol - sell_vol) / total_vol
    
    R_τ (measurement noise): Welford's online variance of OFI — O(1)
    Q_τ (process noise):     EWMV of bar returns — scales with volatility
    
    When the tape is dead, R_τ is high → K → 0 → ignores noise.
    When FOMC hits, both R_τ and Q_τ rise, but Q_τ rises faster → K opens.
    """
    __slots__ = ('theta', 'P', 'Q_base', 'alpha', 'ready',
                 '_r_mean', '_r_m2', '_r_n',
                 '_q_ewmv', '_last_K', '_last_snr', '_warmup_target')

    def __init__(self, Q_base=0.001, alpha=0.05, warmup=30):
        self.theta = 0.0       # filtered state (latent OFI)
        self.P = 1.0           # state covariance
        self.Q_base = Q_base   # base process noise (microstructure floor)
        self.alpha = alpha     # EWMV decay for Q_τ
        self.ready = False
        self._warmup_target = warmup

        # Welford's online variance accumulators for R_τ
        self._r_mean = 0.0
        self._r_m2 = 0.0
        self._r_n = 0

        # EWMV for Q_τ (adaptive process noise)
        self._q_ewmv = Q_base

        # Cache last outputs for emit
        self._last_K = 0.0
        self._last_snr = 0.0

    def update(self, ofi_raw, bar_return=0.0):
        """Feed one OFI observation. O(1) time, zero allocations."""
        # ── R_τ: Welford's online variance of OFI ──
        self._r_n += 1
        delta1 = ofi_raw - self._r_mean
        self._r_mean += delta1 / self._r_n
        delta2 = ofi_raw - self._r_mean
        self._r_m2 += delta1 * delta2
        R_tau = max(self._r_m2 / max(self._r_n, 1), 1e-6)

        # ── Q_τ: EWMV of bar returns (scales with volatility) ──
        self._q_ewmv = self.alpha * (bar_return ** 2) + (1.0 - self.alpha) * self._q_ewmv
        Q_tau = max(self._q_ewmv, self.Q_base)

        # ── Kalman predict ──
        P_pred = self.P + Q_tau

        # ── Kalman gain: automatically → 0 in high-noise regimes ──
        K = P_pred / (P_pred + R_tau)

        # ── Kalman update ──
        self.theta += K * (ofi_raw - self.theta)
        self.P = (1.0 - K) * P_pred

        # Cache for emit
        self._last_K = K
        self._last_snr = abs(self.theta) / max(R_tau ** 0.5, 1e-6)

        if self._r_n >= self._warmup_target:
            self.ready = True

    def reset(self):
        """Full reset on symbol switch."""
        self.__init__(self.Q_base, self.alpha, self._warmup_target)


class HawkesBranchingRatio:
    """
    Bivariate Hawkes process for trade event clustering & exhaustion detection.
    
    ρ = spectral_radius(Γ) where Γ is the 2×2 excitation impact matrix.
    
    ρ < 0.8   → subcritical   (mean-reverting, MM edge)
    0.8 ≤ ρ   → near_critical (transition zone)
    ρ ≥ 1.0   → supercritical (momentum ignition, reflexive cascade)
    ρ drops from ≥1.0 to < 1.0-2σ → EXHAUSTION confirmed
    
    Uses moment-matching estimator (not MLE) for numerical stability.
    Minimum 20 events before emitting ρ. Regularized eigenvalues.
    """
    __slots__ = ('decay', 'min_events', 'window_sec',
                 '_events', '_last_rho', '_last_phase', '_last_rho_std',
                 '_prev_rho', '_compute_interval', '_last_compute_ts',
                 '_last_g_diag')  # [G_bb, G_ss] diagonal for directional output

    def __init__(self, decay=0.1, window_sec=30.0, min_events=20):
        self.decay = decay
        self.min_events = min_events
        self.window_sec = window_sec
        self._events = []         # [(t, side_idx, volume)]
        self._last_rho = None
        self._last_phase = "insufficient_data"
        self._last_rho_std = None
        self._prev_rho = None
        self._compute_interval = 0.5  # recompute max every 500ms
        self._last_compute_ts = 0.0
        self._last_g_diag = [0.0, 0.0]  # [G_bb, G_ss]: buy-buy, sell-sell self-excitation

    def add_event(self, t, side, volume):
        """Record a trade event. side='b' or 's'."""
        side_idx = 0 if side == 'b' else 1
        self._events.append((t, side_idx, volume))
        # Trim old events outside window
        cutoff = t - self.window_sec
        if len(self._events) > 2 * self.min_events:
            self._events = [(tt, s, v) for tt, s, v in self._events if tt > cutoff]

    def compute(self, now):
        """Recompute ρ if enough events and enough time has passed."""
        if now - self._last_compute_ts < self._compute_interval:
            return  # throttled
        self._last_compute_ts = now

        # Trim stale events
        cutoff = now - self.window_sec
        self._events = [(t, s, v) for t, s, v in self._events if t > cutoff]

        if len(self._events) < self.min_events:
            self._last_rho = None
            self._last_phase = "insufficient_data"
            self._last_rho_std = None
            return

        # ── Volume-weighted moment-matching for G (Kirchner 2017, modified) ──
        # Weight each triggering event by sqrt(volume / median_volume)
        # so large trades contribute proportionally more excitation.
        n = len(self._events)
        G = [[0.0, 0.0], [0.0, 0.0]]  # [buy→buy, sell→buy; buy→sell, sell→sell]
        weight_sum = [[0.0, 0.0], [0.0, 0.0]]

        # Compute median volume for mark normalization
        vols = [v for _, _, v in self._events]
        vols_sorted = sorted(vols)
        median_vol = vols_sorted[len(vols_sorted) // 2] if vols_sorted else 1.0
        median_vol = max(median_vol, 1.0)

        for i in range(1, n):
            t_i, s_i, v_i = self._events[i]
            for j in range(i - 1, max(i - 50, -1), -1):  # look back up to 50 events
                t_j, s_j, v_j = self._events[j]
                dt = t_i - t_j
                if dt > 5.0:  # beyond 5 seconds, excitation is negligible
                    break
                kernel = _math.exp(-self.decay * dt)
                # Volume mark: f(v) = sqrt(v / median_v) — sub-linear scaling
                mark = _math.sqrt(v_j / median_vol)
                G[s_j][s_i] += kernel * mark
                weight_sum[s_j][s_i] += mark

        # Normalize by total volume-weighted count
        for i2 in range(2):
            for j2 in range(2):
                if weight_sum[i2][j2] > 0:
                    G[i2][j2] /= weight_sum[i2][j2]

        # ── Eigenvalues of 2×2 matrix (closed-form, no numpy needed) ──
        # For [[a, b], [c, d]]: eigenvalues = (trace ± sqrt(trace² - 4det)) / 2
        a, b = G[0][0], G[0][1]
        c, d = G[1][0], G[1][1]
        # Tikhonov regularization for numerical stability
        a += 1e-4
        d += 1e-4
        trace = a + d
        det = a * d - b * c
        discriminant = trace * trace - 4.0 * det
        if discriminant >= 0:
            sqrt_disc = _math.sqrt(discriminant)
            eig1 = (trace + sqrt_disc) / 2.0
            eig2 = (trace - sqrt_disc) / 2.0
            rho = max(abs(eig1), abs(eig2))
        else:
            # Complex eigenvalues: use modulus
            real_part = trace / 2.0
            imag_part = _math.sqrt(-discriminant) / 2.0
            rho = _math.sqrt(real_part ** 2 + imag_part ** 2)

        # ── Store directional diagonals for get_state() output ──
        # G[0][0] = buy→buy kernel (buy-side self-excitation)
        # G[1][1] = sell→sell kernel (sell-side self-excitation)
        self._last_g_diag = [round(G[0][0], 4), round(G[1][1], 4)]

        # ── Standard error via jackknife (fast O(1) approximation) ──
        # For self-exciting process, effective sample size < n due to clustering.
        # Use volume-weighted effective n: n_eff = (Σw)² / Σw² where w=sqrt(v/med)
        marks = [_math.sqrt(v / median_vol) for _, _, v in self._events]
        sum_w = sum(marks)
        sum_w2 = sum(m * m for m in marks)
        n_eff = (sum_w * sum_w) / max(sum_w2, 1.0) if sum_w2 > 0 else n
        rho_std = rho / max(_math.sqrt(n_eff), 1.0)

        self._prev_rho = self._last_rho
        self._last_rho = round(rho, 4)
        self._last_rho_std = round(rho_std, 4)

        # σ-adaptive phase classification: ρ's distance from criticality
        # measured in units of ρ's own sampling std, not a hardcoded 0.8/1.0.
        band = max(2 * rho_std, 0.05)  # floor to avoid zero-std overcommit
        if rho < 1.0 - band:
            self._last_phase = "subcritical"
        elif rho > 1.0 + band:
            self._last_phase = "supercritical"
        else:
            self._last_phase = "near_critical"

    def get_state(self):
        """Return current state for v2_signals emit."""
        g_bb = self._last_g_diag[0]  # buy-side self-excitation
        g_ss = self._last_g_diag[1]  # sell-side self-excitation
        total_g = g_bb + g_ss
        # side_dominance: +1.0 = pure buy clustering, -1.0 = pure sell clustering
        side_dominance = round((g_bb - g_ss) / max(total_g, 1e-6), 4) if total_g > 0 else 0.0
        return {
            "rho":            self._last_rho,
            "phase":          self._last_phase,
            "rho_std":        self._last_rho_std,
            "rho_buy":        g_bb,            # G[buy→buy]: buy aggression self-excitation
            "rho_sell":       g_ss,            # G[sell→sell]: sell aggression self-excitation
            "side_dominance": side_dominance,  # +1 buy-dominant, -1 sell-dominant
        }

    def is_exhaustion(self):
        """True if ρ just dropped from supercritical to subcritical beyond 2σ."""
        if self._prev_rho is None or self._last_rho is None or self._last_rho_std is None:
            return False
        return (self._prev_rho >= 1.0 and
                self._last_rho < 1.0 - 2 * self._last_rho_std)

    def reset(self):
        """Full reset on symbol switch."""
        self._events = []
        self._last_rho = None
        self._last_phase = "insufficient_data"
        self._last_rho_std = None
        self._prev_rho = None
        self._last_compute_ts = 0.0
        self._last_g_diag = [0.0, 0.0]


# ── Per-symbol V2 signal instances ──
_V2_KALMAN: dict = {}   # {symbol: AdaptiveKalmanOFI}
_V2_HAWKES: dict = {}   # {symbol: HawkesBranchingRatio}

# ── Adverse Selection Engine (Kyle lambda, Glosten-Harris, Huang-Stoll) ──
from connectors.adverse_selection import AdverseSelectionEngine
_ADVERSE_SELECTION: dict = {}   # {symbol: AdverseSelectionEngine}

# VPIN bucket size calibration by instrument
# bucket_size = ~1 minute of average RTH volume (contracts/min)
# n_buckets   = 50-bucket rolling window (~50 min of adapted VPIN)
_VPIN_BUCKET_SIZES = {
    'NQ':  50,   # ~50 contracts/min during RTH
    'ES':  200,  # ~200 contracts/min during RTH
    'GC':  10,   # ~10 contracts/min
    'CL':  50,   # ~50 contracts/min
    'MNQ': 100,  # micro NQ (10x smaller, higher frequency)
}
_VPIN_BUCKET_DEFAULT = 50

def _ensure_v2_engines(symbol):
    """Lazily create V2 signal engines for a symbol on first use."""
    if symbol not in _V2_KALMAN:
        _V2_KALMAN[symbol] = AdaptiveKalmanOFI(Q_base=0.001, alpha=0.05, warmup=30)
        log.info(f"[V2] AdaptiveKalmanOFI created for {symbol}")
    if symbol not in _V2_HAWKES:
        _V2_HAWKES[symbol] = HawkesBranchingRatio(decay=0.1, window_sec=30.0, min_events=20)
        log.info(f"[V2] HawkesBranchingRatio created for {symbol}")
    # BUG FIX: VPIN was never initialized because defaultdict(VPINEngine) only
    # creates on __getitem__, not __contains__. Now created explicitly here.
    if _VPIN_AVAILABLE and symbol not in _VPIN_ENGINES:
        bucket = _VPIN_BUCKET_SIZES.get(symbol, _VPIN_BUCKET_DEFAULT)
        _VPIN_ENGINES[symbol] = _VPINEngine(bucket_size=bucket, n_buckets=50, half_life=30)
        log.info(f"[VPIN] Engine initialized for {symbol} (bucket={bucket})")
    tick_sz = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    if symbol not in _QUEUE_DYNAMICS:
        _QUEUE_DYNAMICS[symbol] = QueueDynamicsEngine(n_levels=20, tick_size=tick_sz)
        log.info(f"[V2] QueueDynamicsEngine created for {symbol}")
    if symbol not in _TRADE_TOXICITY:
        _TRADE_TOXICITY[symbol] = TradeToxicityTracker(tick_size=tick_sz)
        log.info(f"[V2] TradeToxicityTracker created for {symbol}")
    if symbol not in _LEVEL_SURVIVAL:
        _LEVEL_SURVIVAL[symbol] = LevelSurvivalModel(tick_size=tick_sz)
        log.info(f"[V2] LevelSurvivalModel created for {symbol}")

def _reset_v2_engines(symbol):
    """Reset V2 engines on symbol switch (prevents stale state bleed)."""
    if symbol in _V2_KALMAN:
        _V2_KALMAN[symbol].reset()
    if symbol in _V2_HAWKES:
        _V2_HAWKES[symbol].reset()
    # VPIN: destroy engine on symbol switch so stale toxicity from NQ
    # doesn't contaminate ES (different bucket size, different flow profile).
    _VPIN_ENGINES.pop(symbol, None)  # plain dict, safe to pop always
    # Clear stale BBO depth state so the new symbol's first OFI computation
    # doesn't compute ΔQ_bid against the old symbol's market depth.
    # Without this, switching NQ → ES produces a false OFI spike on the first trade.
    _PREV_DOM_BEST.pop(symbol, None)
    _QUEUE_DYNAMICS.pop(symbol, None)
    _TRADE_TOXICITY.pop(symbol, None)
    _LEVEL_SURVIVAL.pop(symbol, None)
    _DOM_PREV_SNAP.pop(symbol, None)
    _PREV_DOM_SNAP.pop(symbol, None)
    _ABSORPTION.pop(symbol, None)
    _DEPTH_VEL_CACHE.pop(symbol, None)


# ── 2D DOM Heatmap: Tiered Passive DOM History Store ──────────────────────────
# Stores DOM snapshots across 4 resolution tiers covering the full CME session.
# Each snapshot: (timestamp, {price_str: size, ...}, {price_str: size, ...})
#
# Tier │ Window         │ Interval  │ Max Snaps │ Mem (NQ)
# ─────┼────────────────┼───────────┼───────────┼─────────
# T0   │ Last 5 min     │ ~500ms    │ 600       │ ~1.5 MB
# T1   │ 5–30 min       │ 2 sec     │ 750       │ ~1.9 MB
# T2   │ 30 min – 4 hr  │ 10 sec    │ 1,260     │ ~3.2 MB
# T3   │ 4 hr – 19.5 hr │ 30 sec    │ 1,860     │ ~4.7 MB
#
# Auto-downsample: When T0 fills, oldest entries merge into T1, etc.

from collections import deque as _deque

_DOM_HISTORY_T0: dict = defaultdict(lambda: _deque(maxlen=600))   # live
_DOM_HISTORY_T1: dict = defaultdict(lambda: _deque(maxlen=750))   # recent
_DOM_HISTORY_T2: dict = defaultdict(lambda: _deque(maxlen=1260))  # session
_DOM_HISTORY_T3: dict = defaultdict(lambda: _deque(maxlen=1860))  # deep
_DOM_HIST_LAST_T0: dict = {}    # {sym: last_record_ts}
_DOM_HIST_LAST_T1: dict = {}    # {sym: last_downsample_ts}
_DOM_HIST_LAST_T2: dict = {}
_DOM_HIST_LAST_T3: dict = {}

_T0_INTERVAL = 0.5    # record at most every 500ms
_T1_INTERVAL = 2.0    # downsample to T1 every 2s
_T2_INTERVAL = 10.0   # downsample to T2 every 10s
_T3_INTERVAL = 30.0   # downsample to T3 every 30s

# Trade buffer: collects trades between DOM snapshots, then drained into each snap
_HEATMAP_TRADE_BUF: dict = defaultdict(lambda: deque(maxlen=5000))  # {symbol: [{p,v,s,t}, ...]}

# Previous best-bid and best-ask sizes for OFI depth-change computation.
# OFI = ΔQ_bid - ΔQ_ask (Cont & Kukanov 2013) requires tracking the size
# at the best bid/ask between consecutive DOM updates.
# {symbol: {"bid_size": float, "ask_size": float, "bid_px": float, "ask_px": float}}
_PREV_DOM_BEST: dict = {}

# FIX 7: Previous T0 DOM snapshot for depth velocity computation.
# {symbol: ((snap_bids, snap_asks), timestamp)}
_DOM_PREV_SNAP: dict = {}


def _record_dom_snapshot(symbol, dom):
    """
    Called on every DOM update. Records snapshot into T0 ring buffer
    at max ~500ms resolution, and auto-downsamples into T1/T2/T3.
    """
    now = time.time()

    bids = dom.get("bids", {})
    asks = dom.get("asks", {})

    # ── T0: Live (every ~500ms) — record + compute depth velocity ──
    last_t0 = _DOM_HIST_LAST_T0.get(symbol, 0)
    if now - last_t0 < _T0_INTERVAL:
        return  # throttle: don't record more than 2/sec
    _DOM_HIST_LAST_T0[symbol] = now

    # Ensure engines exist (after throttle gate — no wasted work on skipped ticks)
    _ensure_v2_engines(symbol)

    # Compact snapshot: only store non-zero levels
    snap_bids = {str(k): v for k, v in bids.items() if v > 0}
    snap_asks = {str(k): v for k, v in asks.items() if v > 0}

    # ── FIX 7: DOM depth velocity — drain rate per price level ──
    # Compare current snapshot against the previous T0 snapshot.
    # drain_rate[price] = (prev_size - cur_size) / dt  (positive = shrinking = drain)
    # Emitted in dom_snapshot.depth_vel as {priceStr: drain_rate}.
    # Wall with 500 lots draining at 150/sec is about to break → actionable signal.
    depth_vel = {}
    if symbol in _DOM_PREV_SNAP and _DOM_PREV_SNAP[symbol] is not None:
        prev_snap, prev_ts = _DOM_PREV_SNAP[symbol]
        dt_snap = now - prev_ts
        if dt_snap > 0:
            prev_bids, prev_asks = prev_snap
            for px, cur_sz in snap_bids.items():
                prev_sz = prev_bids.get(px, 0)
                rate = (prev_sz - cur_sz) / dt_snap  # lots/sec drained (positive = losing size)
                if abs(rate) >= 5:  # only emit if moving ≥5 lots/sec
                    depth_vel[px] = round(rate, 1)
            for px, cur_sz in snap_asks.items():
                prev_sz = prev_asks.get(px, 0)
                rate = (prev_sz - cur_sz) / dt_snap
                if abs(rate) >= 5:
                    depth_vel[px] = round(rate, 1)
    _DOM_PREV_SNAP[symbol] = ((snap_bids, snap_asks), now)
    # FIX M8: Cache latest depth_vel for inclusion in candle_update
    if depth_vel:
        _DEPTH_VEL_CACHE[symbol] = depth_vel

    # Drain trade buffer: snapshot + clear (not pop — avoids losing trades between bursts)
    with _L2_LOCK:
        trades = list(_HEATMAP_TRADE_BUF[symbol])
        _HEATMAP_TRADE_BUF[symbol].clear()
    # Compact trades: [{p: price, v: volume, s: 'b'/'s'}, ...]
    compact_trades = []
    for t in trades:
        # Include "t" (Unix seconds timestamp) — always present in the buffer
        # (see line 3323: {"p", "v", "s", "t"}). Was stripped out here by mistake.
        # Frontend TapeEWMA ingests trades by volume ("v"), but time-series ordering
        # and IAT variance testing need the timestamp to be present.
        compact_trades.append({"p": t["p"], "v": t["v"], "s": t["s"], "t": t.get("t", now)})

    # Capture absorption state: compact {price: {s, w, i, h, c, sd}} for active signals
    compact_abs = {}
    abs_tiers = {}
    with _L2_LOCK:
        abs_data = L2_STATE.get("absorption", {}).get(symbol, {})

        # Sprint 2: pre-compute WALL/ABS cutoffs ONCE from rolling score distribution.
        # Hoisted out of the per-level loop — O(N log N) once instead of per level.
        _sd_list = list(_ABS_SCORE_DIST[symbol])
        _wd_list = list(_ABS_WAVES_DIST[symbol])
        if len(_sd_list) >= 50:
            _sd_sorted = sorted(_sd_list)
            _wd_sorted = sorted(_wd_list)
            _wall_score_cut = _sd_sorted[int(len(_sd_sorted) * 0.75)]
            _wall_waves_cut = max(_wd_sorted[int(len(_wd_sorted) * 0.75)], 3)
            _abs_score_cut  = _sd_sorted[int(len(_sd_sorted) * 0.50)]
            _abs_waves_cut  = max(_wd_sorted[int(len(_wd_sorted) * 0.50)], 2)
        else:
            _wall_score_cut, _wall_waves_cut = 2.0, 3
            _abs_score_cut,  _abs_waves_cut  = 1.0, 2

        for pk, av in abs_data.items():
            if isinstance(av, dict) and av.get("hits", 0) >= 2:
                score = av.get("score", 0)
                waves = av.get("waves", 0)
                raw_score = av.get("raw_score", 0)
                shock_count = av.get("side_hits", 0)
                compact_abs[pk] = {
                    "s": score,                           # absorption score
                    "w": waves,                           # wave count
                    "i": av.get("intensity", 0),         # intensity
                    "h": av.get("hits", 0),              # hit count
                    "c": av.get("passive_consumed", 0),  # consumed
                    "sh": shock_count,                    # side_hits
                    "rs": raw_score,                      # raw_score
                    "sd": av.get("side", ""),             # side flag
                }

                # ── P0: Pre-classify absorption tier ──
                # tier -1: CRACK (wall failed under pressure)
                # tier  0: no significant absorption
                # tier  1: ABS (holding under moderate attack)
                # tier  2: WALL (holding under sustained heavy attack)
                #
                # FIX 6: CRACK threshold from _STICKINESS_DIST P10 (not hardcoded 0.3).
                # The P10 of the empirical stickiness distribution = the bottom 10% of
                # wall stickiness values historically seen for THIS symbol. A wall whose
                # raw_score falls below P10 under 3+ shocks has definitively cracked.
                stick_dist = list(_STICKINESS_DIST[symbol])
                if len(stick_dist) >= 20:
                    p10_idx = max(0, len(stick_dist) // 10 - 1)
                    crack_threshold = sorted(stick_dist)[p10_idx]
                else:
                    crack_threshold = 0.3  # fallback only during the first ~20 DOM events

                _abs_side = av.get("side", "")

                # Sprint 2: sample score/waves into rolling deque for percentile cutoffs.
                # Cutoffs themselves are computed once above the loop.
                if score > 0:
                    _ABS_SCORE_DIST[symbol].append(score)
                    _ABS_WAVES_DIST[symbol].append(waves)

                if raw_score < crack_threshold and shock_count >= 3:
                    abs_tiers[pk] = {"tier": -1, "score": round(score, 2), "label": "CRACK", "waves": waves, "sd": _abs_side}
                    _telemetry.log_event(symbol, "ABSORPTION_CRACK", {"price": pk, "score": round(score, 2), "waves": waves, "shock_hits": shock_count})
                elif score >= _wall_score_cut and waves >= _wall_waves_cut:
                    abs_tiers[pk] = {"tier": 2, "score": round(score, 2), "label": "WALL", "waves": waves, "sd": _abs_side}
                    _telemetry.log_event(symbol, "ABSORPTION_WALL", {"price": pk, "score": round(score, 2), "waves": waves})
                elif score >= _abs_score_cut and waves >= _abs_waves_cut:
                    abs_tiers[pk] = {"tier": 1, "score": round(score, 2), "label": "ABS", "waves": waves, "sd": _abs_side}

    # ── Model Engines: Queue Dynamics, Trade Toxicity, Level Survival ──
    qd = _QUEUE_DYNAMICS.get(symbol)
    if qd:
        qd.update(snap_bids, snap_asks, compact_trades)

    tox = _TRADE_TOXICITY.get(symbol)
    if tox:
        with _L2_LOCK:
            _tox_mid = L2_STATE["mid_prices"].get(symbol, 0)
        if _tox_mid > 0:
            tox.resolve(now, _tox_mid)

    surv = _LEVEL_SURVIVAL.get(symbol)
    if surv and abs_tiers:
        surv.observe(abs_tiers)

    snap = (now, snap_bids, snap_asks, compact_trades, compact_abs)
    _DOM_HISTORY_T0[symbol].append(snap)

    # ── Collect V2 signal state (Kalman + Hawkes) ──
    v2_sigs = {}
    kalman_inst = _V2_KALMAN.get(symbol)
    if kalman_inst and kalman_inst.ready:
        v2_sigs["kalman"] = {
            "theta": round(kalman_inst.theta, 6),
            "K": round(kalman_inst._last_K, 4),
            "snr": round(kalman_inst._last_snr, 3),
            "ready": True,
        }
        
        # Telemetry for extreme institutional directional flow
        if kalman_inst._last_snr >= 2.0:
            _telemetry.log_event(symbol, "KALMAN_ANOMALY", v2_sigs["kalman"])
            
    hawkes_inst = _V2_HAWKES.get(symbol)
    if hawkes_inst:
        h_state = hawkes_inst.get_state()
        if h_state["rho"] is not None:
            v2_sigs["hawkes"] = h_state

            # BUG 6 FIX: Emit Exhaustion signal
            if hawkes_inst.is_exhaustion():
                v2_sigs["hawkes"]["exhaustion"] = True

            # Telemetry for exhaustion pulse (rho spikes above 1.0)
            if h_state["rho"] >= 1.0:
                _telemetry.log_event(symbol, "HAWKES_CRITICAL", h_state)

    # ── VPIN Toxicity: live order flow toxicity state ──
    # Now that the VPIN engine is correctly initialized (defaultdict bug fixed),
    # emit its state on every DOM snapshot so the frontend knows:
    #   vpin       : current toxicity [0.0 – 1.0]  (>0.65 = widen spreads, >0.80 = pull quotes)
    #   vpin_regime: 'LOW_TOXICITY' / 'ELEVATED' / 'HIGH' / 'EXTREME'
    #   vpin_pct   : percentile rank in session distribution (0–100)
    vpin_inst = _VPIN_ENGINES.get(symbol)
    if vpin_inst and vpin_inst._buckets_completed >= 5:  # need >=5 buckets before trusting value
        vpin_val = round(vpin_inst.vpin, 4)
        vpin_regime = vpin_inst.get_regime_modifier()
        vpin_pct = round(vpin_inst.get_percentile() * 100, 1) if hasattr(vpin_inst, 'get_percentile') else None
        v2_sigs["vpin"] = {
            "value":  vpin_val,
            "regime": vpin_regime,
            "pct":    vpin_pct,
            "buckets_completed": vpin_inst._buckets_completed,
            # Market-maker thresholds (CME professional standard):
            "alert_widen":      vpin_val >= 0.65,  # widen spreads
            "alert_pull_quotes": vpin_val >= 0.80,  # pull quotes — informed trader active
        }

    # ── tape_floor: authoritative server-side tape size floor ──
    # Frontend TapeEWMA computes its own floor from dom_snapshot trades (client-side).
    # Here we emit the VolumeClock's calibrated bucket size as the ground truth.
    # VolumeClock.bucket_size() is the adaptive mean trade size for the current
    # volume regime, identical in concept to TapeEWMA._mu but computed server-side
    # from the full uncompressed trade stream (not the ~500ms batched snapshot).
    vclock = _VOLUME_CLOCKS.get(symbol)
    if vclock and vclock.warm:
        v2_sigs["tape_floor"] = round(vclock.bucket_size, 1)

    # dom_snapshot + v2_signals removed — all DOM data flows via l2_update push loop

    # ── T1: Downsample (every 2s) ──
    last_t1 = _DOM_HIST_LAST_T1.get(symbol, 0)
    if now - last_t1 >= _T1_INTERVAL:
        _DOM_HIST_LAST_T1[symbol] = now
        # Take the most recent T0 snapshot as the T1 representative
        _DOM_HISTORY_T1[symbol].append(snap)

    # ── T2: Downsample (every 10s) ──
    last_t2 = _DOM_HIST_LAST_T2.get(symbol, 0)
    if now - last_t2 >= _T2_INTERVAL:
        _DOM_HIST_LAST_T2[symbol] = now
        _DOM_HISTORY_T2[symbol].append(snap)

    # ── T3: Downsample (every 30s) ──
    last_t3 = _DOM_HIST_LAST_T3.get(symbol, 0)
    if now - last_t3 >= _T3_INTERVAL:
        _DOM_HIST_LAST_T3[symbol] = now
        _DOM_HISTORY_T3[symbol].append(snap)


def get_dom_history(symbol, since_ts=0, resolution="auto"):
    """
    Returns DOM snapshots for the 2D heatmap.
    Resolution: 'auto' (picks best tier), 't0', 't1', 't2', 't3'.
    Returns list of [timestamp, bids_dict, asks_dict].
    """
    now = time.time()
    age = now - since_ts if since_ts > 0 else 9999999

    # Auto-select tier based on requested time range
    if resolution == "auto":
        if age <= 300:       # ≤ 5 min
            resolution = "t0"
        elif age <= 1800:    # ≤ 30 min
            resolution = "t1"
        elif age <= 14400:   # ≤ 4 hr
            resolution = "t2"
        else:
            resolution = "t3"

    tier_map = {
        "t0": _DOM_HISTORY_T0,
        "t1": _DOM_HISTORY_T1,
        "t2": _DOM_HISTORY_T2,
        "t3": _DOM_HISTORY_T3,
    }
    tier = tier_map.get(resolution, _DOM_HISTORY_T1)
    history = tier.get(symbol)
    if not history:
        return []

    # Filter by since_ts
    result = []
    for snap in history:
        if snap[0] >= since_ts:
            trades = snap[3] if len(snap) > 3 else []
            absorption = snap[4] if len(snap) > 4 else {}
            result.append([snap[0], snap[1], snap[2], trades, absorption])
    return result


def get_l2_state() -> dict:
    """Thread-safe snapshot of L2_STATE — called by server.py /api/l2."""
    import json as _json
    with _L2_LOCK:
        raw = {
            "connected":     bool(L2_STATE["connected"]),
            "dom":           {k: dict(v) for k, v in L2_STATE["dom"].items()},
            "quotes":        {k: dict(v) for k, v in L2_STATE["quotes"].items()},
            "imbalance":     {k: float(v) for k, v in L2_STATE["imbalance"].items()},
            "mid_prices":    {k: float(v) for k, v in L2_STATE["mid_prices"].items()},
            "price_history": {k: list(v)  for k, v in L2_STATE["price_history"].items()},
            "trades":        {k: list(v)[-50:]  for k, v in L2_STATE["trades"].items()},
            "absorption":    {k: dict(v) for k, v in L2_STATE["absorption"].items()},
            "signals":       dict(L2_STATE["signals"]),
            "last_update":   float(L2_STATE["last_update"]),
            "volume_clock":  {sym: vc.get_stats() for sym, vc in _VOLUME_CLOCKS.items()},
        }
    try:
        return _json.loads(_json.dumps(raw, default=str))
    except Exception:
        return raw


# Rolling price history per symbol (for LPPL, PowerLaw, etc.)
_PRICE_HISTORY: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))


# ── Framework engines (lazy imports) ─────────────────────────────────────────
_shannon     = None
_ising       = None
_reynolds    = None
_lppl        = None
_powerlaw    = None
_transfer    = None
_percolation = None
_mutual      = None


def _init_frameworks():
    global _shannon, _ising, _reynolds
    global _lppl, _powerlaw, _transfer, _percolation, _mutual
    from frameworks.shannon_entropy      import ShannonEntropy
    from frameworks.ising_magnetization  import IsingMagnetization
    from frameworks.reynolds_number      import ReynoldsNumber
    from frameworks.lppl_sornette        import LPPLSornette
    from frameworks.powerlaw_tail        import PowerLawTail
    from frameworks.transfer_entropy     import TransferEntropy
    from frameworks.percolation_threshold import PercolationThreshold
    from frameworks.mutual_information   import MutualInformation
    _shannon     = ShannonEntropy(window_size=60)
    _ising       = IsingMagnetization(window_size=60)
    _reynolds    = ReynoldsNumber(window_size=60)
    _lppl        = LPPLSornette()
    _powerlaw    = PowerLawTail()
    _transfer    = TransferEntropy()
    _percolation = PercolationThreshold()
    _mutual      = MutualInformation()
    log.info("L2: all 8 frameworks initialised")


# ── Callbacks ─────────────────────────────────────────────────────────────────

_DIRECT_DOM_LAST = {}  # {symbol: last_emit_time} — throttle direct DOM push

def on_dom_update(symbol: str, dom: dict):
    """Called by connector every time a DOM level changes."""
    global _shannon, _ising, _reynolds

    imb = dom.get("imbalance", 0)
    mid = dom.get("mid_price", 0)
    spr = dom.get("spread", 0)
    tot = dom.get("bid_total", 0) + dom.get("ask_total", 0)

    # Feed Shannon Entropy
    if _shannon and imb != 0:
        _shannon.update(imb)

    # Feed Reynolds Number
    if _reynolds and mid > 0:
        _reynolds.update(price=mid, spread=spr, volume=float(tot))

    with _L2_LOCK:
        # Shallow copy bids/asks dicts (sufficient — values are numbers, not nested)
        _dom_copy = {
            "bids": dict(dom.get("bids", {})),
            "asks": dict(dom.get("asks", {})),
            "best_bid": dom.get("best_bid", 0),
            "best_ask": dom.get("best_ask", 0),
            "mid_price": mid,
            "spread": spr,
            "bid_total": dom.get("bid_total", 0),
            "ask_total": dom.get("ask_total", 0),
            "imbalance": imb,
        }
        L2_STATE["dom"][symbol]        = _dom_copy
        L2_STATE["imbalance"][symbol]  = imb
        L2_STATE["mid_prices"][symbol] = mid
        L2_STATE["last_update"]        = time.time()

        # ── Update _MAX_BOOK_SEEN tracker (book_size capture fix 2026-05-03) ──
        # For every price in current DOM, retain the maximum depth observed
        # over the last _MAX_BOOK_TTL_S window. Trade hot-path reads from
        # this tracker instead of the live (already-consumed) DOM.
        # FIX 2026-05-04: DOM stores prices as FLOATs (e.g. 27889.0) but the
        # trade hot-path queries by STRING (e.g. "27889.00") computed via
        # `str(round(round(price/tick)*tick, 2))`. Quantize and stringify keys
        # at write time so reader and writer share the same key space.
        try:
            tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
            def _qp(p):
                try:
                    return str(round(round(float(p) / tick_size) * tick_size, 2))
                except Exception:
                    return None
            _now_mb = time.time()
            mbs = _MAX_BOOK_SEEN.setdefault(symbol, {})
            # 1. Update with current DOM depths (both bid + ask sides)
            for ps, depth in dom.get("bids", {}).items():
                d = float(depth or 0)
                if d <= 0:
                    continue
                qp_key = _qp(ps)
                if qp_key is None: continue
                ent = mbs.get(qp_key)
                if ent is None or d > ent['depth']:
                    mbs[qp_key] = {'depth': d, 'set_at': _now_mb}
                else:
                    ent['set_at'] = _now_mb  # touch — wall still being seen
            for ps, depth in dom.get("asks", {}).items():
                d = float(depth or 0)
                if d <= 0:
                    continue
                qp_key = _qp(ps)
                if qp_key is None: continue
                ent = mbs.get(qp_key)
                if ent is None or d > ent['depth']:
                    mbs[qp_key] = {'depth': d, 'set_at': _now_mb}
                else:
                    ent['set_at'] = _now_mb
            # 2. Decay stale entries (wall hasn't been seen in TTL seconds)
            stale_keys = [ps for ps, e in mbs.items() if _now_mb - e['set_at'] > _MAX_BOOK_TTL_S]
            for ps in stale_keys:
                del mbs[ps]
            # 3. Cap size — drop oldest entries if over limit
            if len(mbs) > _MAX_BOOK_CAP:
                # Sort by set_at desc, keep newest _MAX_BOOK_CAP
                kept = dict(sorted(mbs.items(), key=lambda kv: -kv[1]['set_at'])[:_MAX_BOOK_CAP])
                _MAX_BOOK_SEEN[symbol] = kept
        except Exception as _mbe:
            log.debug(f"[MAX_BOOK_SEEN] update err: {_mbe}")

        if _shannon:
            L2_STATE["signals"]["shannon_entropy"] = _shannon.get_signal()
        if _reynolds and mid > 0:
            L2_STATE["signals"]["reynolds_number"] = _reynolds.get_signal()

    # Direct DOM push — bypasses the 80ms push loop thread.
    # Throttled to 50ms (20Hz) to prevent flooding Socket.IO.
    if _socketio and mid > 0:
        _now = time.time()
        _last_dom = _DIRECT_DOM_LAST.get(symbol, 0)
        if _now - _last_dom >= 0.05:  # 50ms = 20Hz
            _DIRECT_DOM_LAST[symbol] = _now
            try:
                # Send trimmed DOM directly — 30 levels each side
                _DOM_LVL = 50
                raw_bids = dom.get("bids", {})
                raw_asks = dom.get("asks", {})
                if len(raw_bids) > _DOM_LVL:
                    _tb = sorted(raw_bids.items(), key=lambda x: float(x[0]), reverse=True)[:_DOM_LVL]
                    _bids = dict(_tb)
                else:
                    _bids = raw_bids
                if len(raw_asks) > _DOM_LVL:
                    _ta = sorted(raw_asks.items(), key=lambda x: float(x[0]))[:_DOM_LVL]
                    _asks = dict(_ta)
                else:
                    _asks = raw_asks
                _socketio.emit("l2_update", {
                    "connected": True,
                    "dom": {symbol: {
                        "bids": _bids, "asks": _asks,
                        "best_bid": dom.get("best_bid", 0),
                        "best_ask": dom.get("best_ask", 0),
                        "mid_price": mid, "spread": spr,
                        "bid_total": dom.get("bid_total", 0),
                        "ask_total": dom.get("ask_total", 0),
                        "imbalance": imb,
                    }},
                    "imbalance": {symbol: imb},
                    "mid_prices": {symbol: mid},
                    "last_update": _now,
                    "signals": {},
                }, namespace="/")
            except Exception:
                pass

    # ── 2D DOM Heatmap: record snapshot for historical heatmap ──
    try:
        _record_dom_snapshot(symbol, dom)
    except Exception as e:
        log.debug(f"DOM history record error: {e}")

    # ── Absorption Engine: compute scores from DOM delta vs trade volume ──
    try:
        _compute_absorption_scores(symbol, dom)
    except Exception as e:
        log.debug(f"Absorption compute error: {e}")

    # ── Refill Speed Tracking — measures how fast DOM reloads after hits ──
    try:
        now_rf = time.time()
        _rf_bids = dom.get("bids", {})
        _rf_asks = dom.get("asks", {})
        # Use normalized price keys (round to 2 decimal places)
        cur_depth = {}
        for ps, sz in _rf_bids.items():
            pk = str(round(float(ps), 2))
            cur_depth[pk] = cur_depth.get(pk, 0) + sz
        for ps, sz in _rf_asks.items():
            pk = str(round(float(ps), 2))
            cur_depth[pk] = cur_depth.get(pk, 0) + sz

        prev = _LAST_DOM_DEPTH.get(symbol, {})
        if symbol not in _REFILL_TRACKER:
            _REFILL_TRACKER[symbol] = {}
        rt = _REFILL_TRACKER[symbol]

        for ps, cur_sz in cur_depth.items():
            prev_sz = prev.get(ps, 0)
            # Hit detection: depth decreased by 2+ contracts (not percentage — works on thin books)
            if prev_sz >= 2 and cur_sz < prev_sz - 1:
                # PROPOSAL C (2026-04-28): track *how much* was hit, not just
                # the timing. Used by absorption detector for replenishment-
                # ratio scoring (refill_size / hit_size). Persist running
                # averages across hits at this level so transient noise is
                # smoothed out.
                hit_size_now = prev_sz - cur_sz
                old = rt.get(ps, {})
                old_avg_hit  = old.get("avg_hit_size", 0.0)
                old_avg_rfsz = old.get("avg_refill_size", 0.0)
                rt[ps] = {
                    "hit_ts":          now_rf,
                    "hit_size_last":   hit_size_now,
                    "hit_size_at_hit": prev_sz,    # remember pre-hit depth for ratio @ refill
                    "avg_hit_size":    old_avg_hit,
                    "avg_refill_size": old_avg_rfsz,
                    "avg_ms":          old.get("avg_ms", 0),
                    "count":           old.get("count", 0),
                    "classification":  "gone",
                }
            # Refill detection: was hit, now depth increased
            elif ps in rt and rt[ps].get("hit_ts", 0) > 0 and cur_sz > prev_sz:
                hit_ts = rt[ps]["hit_ts"]
                ms = (now_rf - hit_ts) * 1000
                if ms < 10000:  # only count refills within 10s
                    old_avg = rt[ps].get("avg_ms", ms)
                    old_count = rt[ps].get("count", 0)
                    new_count = old_count + 1
                    new_avg = (old_avg * old_count + ms) / new_count
                    cls = "instant" if new_avg < 150 else "fast" if new_avg < 1000 else "slow"
                    # PROPOSAL C: refill SIZE bookkeeping. Replenishment ratio
                    # = refill_size_so_far / hit_size_at_hit_time. Capped at
                    # 1.0 (full restoration) — values >1 mean the level got
                    # bigger than before, which is even stronger absorption,
                    # so we keep the full ratio (no cap on upside).
                    hit_size_at_hit = rt[ps].get("hit_size_at_hit") or 0
                    refill_size_now = max(cur_sz - 0, 0)  # current depth = total restored
                    # rolling-average update for both hit & refill sizes
                    old_avg_hit  = rt[ps].get("avg_hit_size",    0.0)
                    old_avg_rfsz = rt[ps].get("avg_refill_size", 0.0)
                    new_avg_hit  = (old_avg_hit  * old_count + (rt[ps].get("hit_size_last", 0) or 0)) / new_count
                    new_avg_rfsz = (old_avg_rfsz * old_count + refill_size_now) / new_count
                    replenish_ratio = (new_avg_rfsz / new_avg_hit) if new_avg_hit > 0 else 1.0
                    rt[ps] = {
                        "hit_ts":          0,
                        "avg_ms":          round(new_avg, 0),
                        "count":           new_count,
                        "classification":  cls,
                        "avg_hit_size":    round(new_avg_hit, 1),
                        "avg_refill_size": round(new_avg_rfsz, 1),
                        "replenish_ratio": round(replenish_ratio, 3),
                    }

        # Expire stale entries (>10s since hit with no refill = gone)
        stale = [ps for ps, v in rt.items() if v.get("hit_ts", 0) > 0 and now_rf - v["hit_ts"] > 10]
        for ps in stale:
            rt[ps]["classification"] = "gone"
            rt[ps]["hit_ts"] = 0

        # Cap tracker size (prevent unbounded growth)
        if len(rt) > 500:
            oldest = sorted(rt.items(), key=lambda x: x[1].get("count", 0))[:200]
            for k, _ in oldest:
                del rt[k]

        _LAST_DOM_DEPTH[symbol] = cur_depth
    except Exception:
        pass

    # ── Upgrade C: Micro-OFI — per-level order flow imbalance ──
    try:
        if symbol not in _MICRO_OFI:
            _MICRO_OFI[symbol] = MicroOFIEngine(n_levels=5)
        _MICRO_OFI[symbol].update(dom)
    except Exception as e:
        log.debug(f"Micro-OFI update error: {e}")

    # ── Spoof detection (DOM snapshot diff) ── runs outside lock
    spoof_hits = _detect_spoof(symbol, dom, time.time())
    if spoof_hits:
        with _CANDLE_LOCK:
            for tf in CANDLE_TIMEFRAMES:
                cur = _CURRENT_CANDLE[symbol].get(tf)
                if cur:
                    if "spoofs" not in cur:
                        cur["spoofs"] = []
                    cur["spoofs"].extend(spoof_hits)

    # ── NQ-native signal check (VPIN, Hawkes, Kyle, OFI) — NQ only ──
    if _nq_signal_callback and symbol == 'NQ':
        try:
            _nq_signal_callback(symbol)
        except Exception:
            pass


def on_quote(symbol: str, quote: dict):
    """Called by connector when BBO snapshot arrives.
    The quote feed is lower-latency than DOM snapshots — update BBO immediately
    so on_trade BBO inference doesn't use stale DOM state.
    """
    mid = quote.get("mid_price", 0.0)
    best_bid = quote.get("best_bid", quote.get("bid", 0.0))
    best_ask = quote.get("best_ask", quote.get("ask", 0.0))
    spread    = round(best_ask - best_bid, 4) if best_ask > best_bid > 0 else 0.0

    with _L2_LOCK:
        if mid > 0:
            _PRICE_HISTORY[symbol].append(mid)
        L2_STATE["quotes"][symbol] = quote
        if mid > 0:
            L2_STATE["mid_prices"][symbol] = mid
            L2_STATE["price_history"][symbol] = list(_PRICE_HISTORY[symbol])
        # Update BBO fields in DOM state immediately from quote feed.
        # on_dom_update overwrites these when a full DOM snapshot arrives,
        # but the quote feed fires first — keeping BBO fresh prevents stale
        # inference in on_trade's fallback side-classification.
        if best_bid > 0 or best_ask > 0:
            dom_entry = L2_STATE["dom"].setdefault(symbol, {})
            if best_bid > 0:
                dom_entry["best_bid"] = best_bid
            if best_ask > 0:
                dom_entry["best_ask"] = best_ask
            if spread > 0:
                dom_entry["spread"] = spread
            if mid > 0:
                dom_entry["mid_price"] = mid


def on_trade(symbol: str, trade: dict):
    """Called by connector for every tape print."""
    spin = trade.get("spin", 0)
    if _ising and spin != 0:
        _ising.update_trade(symbol, spin)
        with _L2_LOCK:
            L2_STATE["signals"]["ising_magnetization"] = _ising.get_signal()

    # Feed OHLC candle engine with aggression classification
    price = trade.get("price", 0)
    vol = trade.get("volume", 1)
    ts = trade.get("timestamp", time.time())
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = time.time()
    elif isinstance(ts, (int, float)):
        # If it's a millisecond timestamp (e.g., from TopStepX JS JSON)
        if ts > 20000000000:
            ts = ts / 1000.0
    if price > 0:
        # Track last trade timestamp for reconnect gap-fill
        with _L2_LOCK:
            _LAST_TRADE_TS[symbol] = ts

        # ── Volume Clock: tick on every trade (self-calibrating) ──
        _VOLUME_CLOCKS[symbol].tick(vol, ts)

        # ── Tick classification ──
        # MUST come before VPIN — `side` is defined here.
        # Use the CME native aggressor flag from TopStepX's GatewayTrade event.
        # The exchange knows who initiated the trade — this is 100% accurate.
        # Falls back to BBO comparison only if exchange side is missing.
        trade_side = trade.get("side", "")
        if trade_side == "buy":
            side = "b"   # CME: aggressive buyer (lifted the ask)
        elif trade_side == "sell":
            side = "s"   # CME: aggressive seller (hit the bid)
        else:
            # Fallback: infer from BBO (only for feeds without native aggressor)
            side = "n"
            with _L2_LOCK:
                dom = L2_STATE["dom"].get(symbol)
            if dom:
                best_ask = dom.get("best_ask", 0)
                best_bid = dom.get("best_bid", 0)
                if best_ask > 0 and price >= best_ask:
                    side = "b"
                elif best_bid > 0 and price <= best_bid:
                    side = "s"

        # ── VPIN: feed every trade for toxicity tracking ──
        # _ensure_v2_engines (called 10 lines below) guarantees VPIN exists for this symbol.
        # We call it early here (before ensure_v2_engines) using a direct key check on the
        # plain dict — no defaultdict magic, no NameError risk.
        if _VPIN_AVAILABLE and symbol in _VPIN_ENGINES:
            _VPIN_ENGINES[symbol].on_trade(symbol, vol, side if side in ('b', 's') else 'n', ts)
        elif _VPIN_AVAILABLE and symbol not in _VPIN_ENGINES:
            # Engine not yet created (first trade for this symbol before ensure_v2_engines ran)
            # Initialize it now so we never miss trades.
            bucket = _VPIN_BUCKET_SIZES.get(symbol, _VPIN_BUCKET_DEFAULT)
            _VPIN_ENGINES[symbol] = _VPINEngine(bucket_size=bucket, n_buckets=50, half_life=30)
            _VPIN_ENGINES[symbol].on_trade(symbol, vol, side if side in ('b', 's') else 'n', ts)
            log.info(f"[VPIN] Engine bootstrapped on first trade for {symbol} (bucket={bucket})")

        tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
        qp = str(round(round(price / tick_size) * tick_size, 2))

        # ── Orderflow Detection (runs before candle update) ──

        # ── Absorption Engine: accumulate aggressive volume at this price ──
        try:
            _track_absorption_trade(symbol, qp, vol, side, ts)
        except Exception as e:
            log.debug(f"Absorption trade track error: {e}")

        # ── Trade Toxicity: record every classified trade for 10s outcome check ──
        tox = _TRADE_TOXICITY.get(symbol)
        if tox and side in ('b', 's'):
            tox.record_trade(ts, price, side, qp)

        # ── V2 Signal Engines: feed every trade ──
        # Pre-define BBO variables for use by downstream code (adverse selection)
        best_bid_px = 0.0
        best_ask_px = 0.0
        try:
            _ensure_v2_engines(symbol)

            # ── OFI via depth-change (Cont & Kukanov 2013) ──
            # FIXED: Was ofi_raw = (vol if buy else -vol) / vol = ±1.0 always.
            # That fed the Kalman binary trade direction, not orderflow imbalance.
            # Real OFI = ΔQ_bid - ΔQ_ask at the best price (how much depth
            # was added/removed on each side since the last DOM snapshot).
            #
            # We use the DOM state already stored in L2_STATE by on_dom_update.
            # _prev_dom_best tracks the previous best-bid and best-ask sizes
            # so each trade can compute the depth delta at the BBO.
            ofi_raw = 0.0
            with _L2_LOCK:
                dom_now = L2_STATE["dom"].get(symbol, {})
            best_bid_px = dom_now.get("best_bid", 0.0)
            best_ask_px = dom_now.get("best_ask", 0.0)
            bids_now = dom_now.get("bids", {})
            asks_now = dom_now.get("asks", {})

            if best_bid_px > 0 and best_ask_px > 0:
                bid_key = str(round(best_bid_px, 4))
                ask_key = str(round(best_ask_px, 4))
                bid_size_now = float(bids_now.get(bid_key, 0))
                ask_size_now = float(asks_now.get(ask_key, 0))

                if symbol not in _PREV_DOM_BEST:
                    _PREV_DOM_BEST[symbol] = {"bid_size": bid_size_now, "ask_size": ask_size_now,
                                              "bid_px": best_bid_px, "ask_px": best_ask_px}

                prev = _PREV_DOM_BEST[symbol]
                # Best-bid change: positive = depth added (passive buyers), negative = depth consumed (sellers lifted)
                delta_bid = bid_size_now - prev["bid_size"] if best_bid_px == prev["bid_px"] else 0.0
                # Best-ask change: positive = depth added (passive sellers), negative = consumed (buyers swept)
                delta_ask = ask_size_now - prev["ask_size"] if best_ask_px == prev["ask_px"] else 0.0

                # OFI = ΔQ_bid - ΔQ_ask (normalized by mean trade size for scale-invariance)
                mean_vol = _VOLUME_CLOCKS[symbol].bucket_size if _VOLUME_CLOCKS[symbol].warm else max(vol, 1)
                ofi_raw = (delta_bid - delta_ask) / max(mean_vol, 1)

                # Update prev state
                _PREV_DOM_BEST[symbol] = {"bid_size": bid_size_now, "ask_size": ask_size_now,
                                          "bid_px": best_bid_px, "ask_px": best_ask_px}

            _V2_KALMAN[symbol].update(ofi_raw)

            # Hawkes: record trade event for branching ratio
            _V2_HAWKES[symbol].add_event(ts, side if side in ('b', 's') else 'b', vol)
            _V2_HAWKES[symbol].compute(ts)
        except Exception as e:
            log.debug(f"V2 signal engine error: {e}")

        # ── Adverse Selection: feed every classified trade ──
        if side in ('b', 's'):
            try:
                if symbol not in _ADVERSE_SELECTION:
                    _ADVERSE_SELECTION[symbol] = AdverseSelectionEngine()
                mid = (best_bid_px + best_ask_px) / 2.0 if best_bid_px > 0 and best_ask_px > 0 else price
                sprd = (best_ask_px - best_bid_px) if best_ask_px > best_bid_px else 0.0
                _ADVERSE_SELECTION[symbol].on_trade(mid, price, sprd, vol, side, ts)
            except Exception as e:
                log.debug(f"Adverse selection error: {e}")

        # Sweep detection
        sweep_hit = _detect_sweep(symbol, price, vol, ts, side)
        if sweep_hit:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        if "sweeps" not in cur:
                            cur["sweeps"] = []
                        if len(cur["sweeps"]) < 200:
                            cur["sweeps"].append(sweep_hit)
            # Forward to EdgeDetector
            if _detection_callback:
                try:
                    _detection_callback('sweep', sweep_hit, symbol)
                except Exception:
                    pass

        # Momentum Ignition detection
        ign_hit = _detect_ignition(symbol, price, vol, ts, side)
        if ign_hit:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        if "ignition" not in cur:
                            cur["ignition"] = []
                        cur["ignition"].append(ign_hit)
            # Forward to EdgeDetector
            if _detection_callback:
                try:
                    _detection_callback('ignition', ign_hit, symbol)
                except Exception:
                    pass

        # Ignition reversal checks (runs on every tick)
        reversals = _check_ignition_reversals(symbol, price, ts)
        if reversals:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        if "ignition" not in cur:
                            cur["ignition"] = []
                        cur["ignition"].extend(reversals)

        # ── Periodic cleanup of detection state (runs every 30s) ──
        _cleanup_detection_state()

        _feed_candle(symbol, price, vol, ts, side=side)

        # ── Big-print signal: top-decile rolling 5-min, classified by book ──
        # Fires after _feed_candle so bp is up to date; emits 'big_print'
        # socket event when size ≥ P90(rolling 5-min) for the volume bubbles
        # signal-only renderer (block/sweep/aggression).
        _emit_big_print(symbol, price, vol, side, ts, sweep_hit)

    # Store trade in L2_STATE + buffer for heatmap (single lock acquisition)
    with _L2_LOCK:
        if symbol not in L2_STATE["trades"]:
            L2_STATE["trades"][symbol] = deque(maxlen=500)
        L2_STATE["trades"][symbol].append(trade)
        # Buffer trade for 2D heatmap (will be drained by next DOM snapshot)
        if price > 0:
            _HEATMAP_TRADE_BUF[symbol].append({"p": price, "v": vol, "s": side, "t": ts})

    # ── Emit trade tick via Socket.IO ──
    if _socketio is not None and price > 0:
        try:
            iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            _socketio.emit("trade_tick", {
                "symbol": symbol,
                "price": price,
                "volume": vol,
                "side": side,
                "timestamp": iso_ts,
                "_emit_ts": time.time(),
            }, namespace="/")
        except Exception as e:
            log.debug("trade_tick emit error: %s", e)

    # ── Score trade for tape glow (EdgeDetector regime-adaptive percentile) ──
    if _trade_score_callback is not None and price > 0 and vol > 0:
        try:
            _trade_score_callback(symbol, vol, side, price, ts)
        except Exception:
            pass


# ── Heavy framework pre-compute (runs every 60s in background) ────────────────
def _heavy_compute_loop():
    """Run LPPL, PowerLaw, TransferEntropy, Percolation, MutualInfo every 60s.
    Results written into L2_STATE.signals per-symbol.

    FIX 5: Was hardcoded to NQ price history. Now iterates over ALL active
    symbols so ES/GC signals panels show the correct instrument's data.
    """
    import time as _time
    while True:
        _time.sleep(60)
        try:
            with _L2_LOCK:
                active_symbols = list(_PRICE_HISTORY.keys())

            for symbol in active_symbols:
                with _L2_LOCK:
                    prices = list(_PRICE_HISTORY.get(symbol, []))

                if len(prices) < 30:
                    continue

                results = {}

                if _lppl:
                    try:
                        sig = _lppl.fit(prices)
                        results["lppl_sornette"] = sig
                    except Exception:
                        pass

                if _powerlaw:
                    try:
                        results["powerlaw_tail"] = _powerlaw.compute(prices)
                    except Exception:
                        pass

                if _transfer:
                    try:
                        with _L2_LOCK:
                            imb_vals = list(L2_STATE["imbalance"].values())
                        results["transfer_entropy"] = _transfer.compute(prices, imb_vals)
                    except Exception:
                        pass

                if _percolation:
                    try:
                        with _L2_LOCK:
                            dom_snap = dict(L2_STATE["dom"])
                        results["percolation_threshold"] = _percolation.compute(dom_snap)
                    except Exception:
                        pass

                if _mutual:
                    try:
                        with _L2_LOCK:
                            imb_vals = list(L2_STATE["imbalance"].values())
                        results["mutual_information"] = _mutual.compute(prices, imb_vals)
                    except Exception:
                        pass

                if results:
                    with _L2_LOCK:
                        # BUG 2 FIX: Keep signals dict FLAT using symbol-prefixed keys.
                        # Previously wrote nested L2_STATE["signals"][symbol] = {...}
                        # which corrupted the shape (mix of flat shannon/ising keys and
                        # nested symbol subdicts). Now writes "NQ_lppl_sornette" etc.
                        # so the dict stays consistent with shannon_entropy/ising_magnetization.
                        prefixed = {f"{symbol}_{k}": v for k, v in results.items()}
                        L2_STATE["signals"].update(prefixed)
                        log.debug("Heavy compute updated [%s]: %s", symbol, list(prefixed.keys()))

        except Exception as e:
            log.warning("Heavy compute loop error: %s", e)


# ── L2 State WebSocket Push (replaces frontend REST polling) ────────────────
_L2_PUSH_INTERVAL = 0.08  # 80ms — 12.5Hz L2 state push (was 400ms)

def _l2_push_loop():
    """Push L2 state via Socket.IO every 400ms.
    Only sends data NOT already covered by other WS events:
    - DOM bids/asks → covered by dom_snapshot (don't duplicate)
    - trades → covered by trade_tick (don't duplicate)
    - absorption → covered by dom_snapshot (don't duplicate)
    We DO send: dom metadata (mid/best_bid/ask/imbalance), signals, connected status."""
    import json as _json
    import time as _time
    while True:
        _time.sleep(_L2_PUSH_INTERVAL)
        if _socketio is None:
            continue
        try:
            with _L2_LOCK:
                # Push loop: skip DOM bids/asks entirely — direct emit from
                # on_dom_update handles DOM at 20Hz. This loop only sends signals.
                dom_meta = {}
                # Inject adverse selection state into signals
                for _as_sym, _as_eng in _ADVERSE_SELECTION.items():
                    if _as_eng.warm:
                        _as_state = _as_eng.get_state()
                        for _as_k, _as_v in _as_state.items():
                            L2_STATE["signals"][f"{_as_sym}_as_{_as_k}"] = _as_v

                state = {
                    "connected":     bool(L2_STATE["connected"]),
                    "dom":           dom_meta,
                    "imbalance":     {k: float(v) for k, v in L2_STATE["imbalance"].items()},
                    "mid_prices":    {k: float(v) for k, v in L2_STATE["mid_prices"].items()},
                    "absorption":    {},  # covered by dom_snapshot
                    "signals":       dict(L2_STATE["signals"]),
                    "last_update":   float(L2_STATE["last_update"]),
                }
                # Deep copy inside the lock = thread-safe snapshot
                payload = copy.deepcopy(state)
            _socketio.emit("l2_update", payload, namespace="/")
        except Exception as e:
            log.debug("l2_push_loop emit error: %s", e)


# ── Public API ───────────────────────────────────────────────────────────────

_connector: TopStepXConnector = None


def start_l2_worker() -> TopStepXConnector:
    """
    Initialize and start the L2 background worker.
    Returns the connector instance.
    Call this once at server startup.
    """
    global _connector

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    log.info("=" * 55)
    log.info("  TOPSTEPX L2 WORKER STARTING")
    log.info("  User: %s", USERNAME)
    log.info("=" * 55)

    if _ALPHA_ENABLED:
        _init_frameworks()
    else:
        log.info("L2: frameworks disabled (set ALPHA_ENABLED=1 to enable)")

    _connector = TopStepXConnector(
        username=USERNAME,
        api_key=API_KEY,
        on_dom_update=on_dom_update,
        on_trade=on_trade,
        on_quote=on_quote,
    )

    # Store reference for gap-fill access
    global _connector_ref
    _connector_ref = _connector

    # ── Gap-fill function: called after reconnect to fill missed candles ──
    def _gap_fill_candles(connector, symbols):
        """Fetch missed candles from TopStepX history API and insert into candle engine.
        Called after TopStepX WebSocket reconnects to fill any gap from the disconnect."""
        import traceback
        for sym in symbols:
            last_ts = _LAST_TRADE_TS.get(sym, 0)
            if last_ts == 0:
                continue  # no trades ever recorded, skip
            gap_seconds = time.time() - last_ts
            if gap_seconds < 10:
                continue  # gap too small, nothing to fill
            if gap_seconds > 3600:
                gap_seconds = 3600  # cap at 1 hour to avoid huge requests
            cid = connector._symbol_to_contract.get(sym)
            if not cid:
                log.warning("Gap-fill: no contract ID for %s — skipping", sym)
                continue
            start_iso = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
            log.info("Gap-fill: %s fetching bars from %s (%.0fs gap)...",
                     sym, start_iso, gap_seconds)
            try:
                bars = connector.retrieve_bars(
                    cid, start_time=start_iso,
                    unit=2, unit_number=1, limit=500
                )
                if not bars:
                    log.info("Gap-fill: %s — no bars returned", sym)
                    continue
                inserted = 0
                for bar in bars:
                    ts_str = bar.get("t", "")
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        ts = dt.timestamp()
                    except Exception:
                        continue
                    o = float(bar.get("o", 0))
                    h = float(bar.get("h", 0))
                    l = float(bar.get("l", 0))
                    c = float(bar.get("c", 0))
                    v = int(bar.get("v", 0))
                    if o <= 0:
                        continue
                    # Only insert bars AFTER our last known trade
                    boundary_1m = _candle_boundary(ts, 60)
                    if boundary_1m <= _candle_boundary(last_ts, 60):
                        continue  # we already have this candle
                    with _CANDLE_LOCK:
                        _CANDLES[sym]["1m"].append({
                            "t": boundary_1m,
                            "o": o, "h": h, "l": l, "c": c, "v": v
                        })
                        # Also aggregate into larger timeframes
                        for tf, secs in CANDLE_TIMEFRAMES.items():
                            if tf == "1m" or secs < 60:
                                continue
                            boundary = _candle_boundary(ts, secs)
                            cur = _CURRENT_CANDLE[sym].get(tf)
                            if cur is None or cur["t"] != boundary:
                                if cur is not None:
                                    _CANDLES[sym][tf].append(dict(cur))
                                _CURRENT_CANDLE[sym][tf] = {
                                    "t": boundary, "o": o, "h": h,
                                    "l": l, "c": c, "v": v
                                }
                            else:
                                cur["h"] = max(cur["h"], h)
                                cur["l"] = min(cur["l"], l)
                                cur["c"] = c
                                cur["v"] += v
                    inserted += 1
                    # Update last trade TS so next gap-fill starts from here
                    with _L2_LOCK:
                        _LAST_TRADE_TS[sym] = max(_LAST_TRADE_TS.get(sym, 0), ts)
                log.info("Gap-fill: %s ✓ inserted %d bars (of %d fetched)",
                         sym, inserted, len(bars))
                # Push updated candles to frontend
                if _socketio is not None:
                    try:
                        with _CANDLE_LOCK:
                            candles_1m = list(_CANDLES[sym]["1m"])
                        _socketio.emit("candle_history", {
                            "symbol": sym, "tf": "1m",
                            "candles": candles_1m[-50:]  # send last 50 to refresh view
                        }, namespace="/")
                        log.info("Gap-fill: %s pushed %d candles to frontend",
                                 sym, min(50, len(candles_1m)))
                    except Exception as e:
                        log.warning("Gap-fill: emit failed: %s", e)
            except Exception as e:
                log.warning("Gap-fill: %s FAILED: %s\n%s", sym, e, traceback.format_exc())

    # Register reconnect callback on the connector
    _original_on_open = _connector._on_open
    def _on_reconnect_with_gapfill(contract_ids):
        _original_on_open(contract_ids)
        # Run gap-fill in a separate thread to avoid blocking WebSocket
        def _do_gapfill():
            time.sleep(3)  # wait for connection to stabilize
            _gap_fill_candles(_connector, SYMBOLS)
        threading.Thread(target=_do_gapfill, daemon=True, name="GapFill").start()
    _connector._on_open = _on_reconnect_with_gapfill

    try:
        _connector.start(symbols=SYMBOLS)
        with _L2_LOCK:
            L2_STATE["connected"] = True
        log.info("L2 worker: streaming started for %s", SYMBOLS)

        # ── Restore persisted bubble profiles into historical candles ──
        # After streaming starts, gap-fill may have seeded candles without bp.
        # _bp_restore_candles re-injects today's saved bp data from disk.
        for _sym in SYMBOLS:
            _bp_restore_candles(_sym)

        # Start heavy-framework background loop (daemon — dies with main thread)
        if _ALPHA_ENABLED:
            _heavy_thread = threading.Thread(
                target=_heavy_compute_loop, daemon=True, name="HeavyFrameworks"
            )
            _heavy_thread.start()
            log.info("L2 worker: heavy framework pre-compute loop started (60s interval)")

        # Start L2 state WebSocket push loop (replaces frontend REST polling)
        _l2_push_thread = threading.Thread(
            target=_l2_push_loop, daemon=True, name="L2Push"
        )
        _l2_push_thread.start()
        log.info("L2 worker: WebSocket push loop started (%.0fms interval)", _L2_PUSH_INTERVAL * 1000)

        # ── Tick-level bp backfill: fetch 1s bars, rebuild buy/sell per price ──
        def _backfill_tick_bp(connector, sym, cid, session_open_dt):
            """Fetch 1-second bars in chunks, rebuild bp for each 1m candle.

            Uses Lee-Ready tick rule for buy/sell classification:
            price up from prior bar → buy, price down → sell, unchanged → inherit.
            """
            import time as _bt
            from datetime import timedelta as _td, timezone as _tz

            session_start_utc = session_open_dt.astimezone(_tz.utc)
            now_utc = datetime.now(_tz.utc)
            total_seconds = (now_utc - session_start_utc).total_seconds()

            if total_seconds <= 0:
                log.info("Tick backfill %s: session hasn't started yet", sym)
                return

            # Chunk into 4-hour windows (max ~14,400 1s bars per chunk, under 20k limit)
            CHUNK_HOURS = 4
            chunk_secs = CHUNK_HOURS * 3600
            chunks = []
            cursor = session_start_utc
            while cursor < now_utc:
                chunk_end = min(cursor + _td(seconds=chunk_secs), now_utc)
                chunks.append((cursor, chunk_end))
                cursor = chunk_end

            log.info("Tick backfill %s: %d chunks covering %.1f hours",
                     sym, len(chunks), total_seconds / 3600)

            # Collect all 1s bars
            all_ticks = []
            for i, (c_start, c_end) in enumerate(chunks):
                start_iso = c_start.isoformat()
                ticks = connector.retrieve_bars(
                    cid, start_time=start_iso,
                    unit=1, unit_number=1, limit=20000
                )
                if ticks:
                    # Filter to only ticks within this chunk's window
                    for tk in ticks:
                        try:
                            dt = datetime.fromisoformat(tk["t"].replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=_tz.utc)
                            if dt < c_end:
                                tk["_ts"] = dt.timestamp()
                                all_ticks.append(tk)
                        except Exception:
                            continue

                    truncated = len(ticks) >= 20000
                    log.info("Tick backfill %s chunk %d/%d: %d ticks%s",
                             sym, i + 1, len(chunks), len(ticks),
                             " (TRUNCATED — may have gaps)" if truncated else "")
                else:
                    log.info("Tick backfill %s chunk %d/%d: 0 ticks", sym, i + 1, len(chunks))

                _bt.sleep(0.5)  # Rate limit between API calls

            if not all_ticks:
                log.info("Tick backfill %s: no tick data returned", sym)
                return

            # Sort by timestamp
            all_ticks.sort(key=lambda t: t["_ts"])
            log.info("Tick backfill %s: %d total 1s bars, building bp...", sym, len(all_ticks))

            # ── Lee-Ready tick rule: classify buy/sell ──
            # Group ticks into 1m boundaries, build bp per minute
            bp_per_minute = {}  # {boundary_ts: {price_str: [buy_vol, sell_vol]}}
            prev_close = 0
            prev_side = "buy"

            for tk in all_ticks:
                ts = tk["_ts"]
                price = float(tk.get("c", 0))
                vol = int(tk.get("v", 0))
                if price <= 0 or vol <= 0:
                    continue

                # Lee-Ready: classify based on price movement
                if prev_close > 0:
                    if price > prev_close:
                        side = "buy"
                    elif price < prev_close:
                        side = "sell"
                    else:
                        side = prev_side  # unchanged → inherit
                else:
                    side = "buy"
                prev_close = price
                prev_side = side

                # Also use OHLC within the 1s bar for finer distribution
                # Distribute volume across the high-low range
                o = float(tk.get("o", price))
                h = float(tk.get("h", price))
                l = float(tk.get("l", price))
                tick_size = 0.25

                boundary = _candle_boundary(ts, 60)
                if boundary not in bp_per_minute:
                    bp_per_minute[boundary] = {}
                bp = bp_per_minute[boundary]

                if h == l:
                    # Single price tick
                    pk = f"{price:.2f}"
                    if pk not in bp:
                        bp[pk] = [0, 0]
                    if side == "buy":
                        bp[pk][0] += vol
                    else:
                        bp[pk][1] += vol
                else:
                    # Multi-price 1s bar — distribute volume across range
                    prices_in_bar = []
                    p = l
                    while p <= h + tick_size * 0.01:
                        prices_in_bar.append(round(p, 2))
                        p += tick_size
                    if not prices_in_bar:
                        prices_in_bar = [price]
                    vol_per_level = max(1, vol // len(prices_in_bar))
                    remainder = vol - vol_per_level * len(prices_in_bar)
                    for j, pp in enumerate(prices_in_bar):
                        pk = f"{pp:.2f}"
                        if pk not in bp:
                            bp[pk] = [0, 0]
                        v_alloc = vol_per_level + (1 if j < remainder else 0)
                        if side == "buy":
                            bp[pk][0] += v_alloc
                        else:
                            bp[pk][1] += v_alloc

            # ── Patch existing candles with reconstructed bp ──
            patched = 0
            skipped_live = 0
            with _CANDLE_LOCK:
                candles = _CANDLES.get(sym, {}).get("1m", [])
                for candle in candles:
                    ct = candle.get("t", 0)
                    if "bp" in candle and candle["bp"]:
                        skipped_live += 1
                        continue  # Live candle already has real bp — don't overwrite
                    if ct in bp_per_minute:
                        candle["bp"] = bp_per_minute[ct]
                        patched += 1

            log.info("Tick backfill %s: patched %d candles with real bp (skipped %d live)",
                     sym, patched, skipped_live)

        # Backfill price history + candle chart from retrieveBars API
        def _backfill():
            try:
                import time as _time
                # Wait for contracts to be resolved by the connector
                log.info("L2 backfill: waiting for contracts (up to 30s)...")
                for i in range(30):
                    if _connector._symbol_to_contract:
                        log.info("L2 backfill: contracts resolved after %ds: %s", i, list(_connector._symbol_to_contract.keys()))
                        break
                    _time.sleep(1)
                else:
                    log.warning("L2 backfill: no contracts after 30s — aborting backfill")
                    log.warning("L2 backfill: _symbol_to_contract=%s, _contract_to_symbol=%s",
                                dict(_connector._symbol_to_contract),
                                dict(_connector._contract_to_symbol))
                    return
                _time.sleep(3)  # Extra buffer for connection stability
                try:
                    from zoneinfo import ZoneInfo
                    ny_tz = ZoneInfo('America/New_York')
                except ImportError:
                    import pytz
                    ny_tz = pytz.timezone('America/New_York')
                now_ny = datetime.now(ny_tz)
                if now_ny.hour < 18:
                    session_open = now_ny.replace(hour=18, minute=0, second=0, microsecond=0) - timedelta(days=1)
                else:
                    session_open = now_ny.replace(hour=18, minute=0, second=0, microsecond=0)

                # ── Weekend adjustment: skip phantom Sat/Sun sessions ──
                # Futures sessions run Sun 6pm → Fri 5pm ET.
                # If session_open lands on Saturday or Sunday, rewind to Friday 6pm.
                dow = session_open.weekday()  # 0=Mon .. 6=Sun
                if dow == 5:      # Saturday → rewind 1 day to Friday
                    session_open -= timedelta(days=1)
                    log.info("L2 backfill: weekend adj: Saturday → rewound to Friday session")
                elif dow == 6:    # Sunday → rewind 2 days to Friday
                    session_open -= timedelta(days=2)
                    log.info("L2 backfill: weekend adj: Sunday → rewound to Friday session")

                log.info("L2 backfill: now_ny=%s (%s), session_open=%s (%s)",
                         now_ny.strftime('%Y-%m-%d %H:%M %Z'),
                         now_ny.strftime('%A'),
                         session_open.strftime('%Y-%m-%d %H:%M %Z'),
                         session_open.strftime('%A'))

                # Pull N days of history so VP prior_day / weekly / 2day modes
                # have real data. 7 days × 1440 min = 10,080 bars (< 20k API cap).
                # Configurable via BACKFILL_DAYS env var; defaults to 7.
                try:
                    _backfill_days = int(os.environ.get("BACKFILL_DAYS", "7"))
                except Exception:
                    _backfill_days = 7
                _backfill_days = max(1, min(_backfill_days, 13))  # 20k cap ≈ 13.8 days

                for sym in SYMBOLS:
                    cid = _connector._symbol_to_contract.get(sym)
                    if not cid:
                        log.warning("L2 backfill: no contract ID for %s — skipping", sym)
                        continue

                    # Single call from session_open - N days → now covers weekly
                    start_dt = session_open - timedelta(days=_backfill_days)
                    start_utc = start_dt.astimezone(timezone.utc).isoformat()
                    log.info("L2 backfill: %s fetching %d days from %s NY → %s UTC",
                             sym, _backfill_days,
                             start_dt.strftime('%Y-%m-%d %H:%M'),
                             start_utc)
                    bars = _connector.retrieve_bars(
                        cid, start_time=start_utc,
                        unit=2, unit_number=1, limit=20000
                    )
                    if bars:
                        log.info("L2 backfill: %s ✓ got %d bars spanning %d days",
                                 sym, len(bars), _backfill_days)
                    else:
                        log.warning("L2 backfill: no bars for %s — chart will be empty", sym)
                        continue

                    # Seed price history — build list outside lock, then publish
                    _backfill_prices = [float(bar.get("c", 0)) for bar in bars]
                    _backfill_prices = [p for p in _backfill_prices if p > 0]
                    with _L2_LOCK:
                        _PRICE_HISTORY[sym].extend(_backfill_prices)
                        L2_STATE["price_history"][sym] = list(_PRICE_HISTORY[sym])

                    # Seed candle engine — insert bars as 1m candles
                    for bar in bars:
                        ts_str = bar.get("t", "")
                        try:
                            dt_obj = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if dt_obj.tzinfo is None:
                                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                            ts = dt_obj.timestamp()
                        except Exception as e:
                            continue
                        o = float(bar.get("o", 0))
                        h = float(bar.get("h", 0))
                        l = float(bar.get("l", 0))
                        c = float(bar.get("c", 0))
                        v = int(bar.get("v", 0))
                        if o <= 0:
                            continue

                        # Insert into 1m candle deque directly
                        with _CANDLE_LOCK:
                            _CANDLES[sym]["1m"].append({
                                "t": _candle_boundary(ts, 60),
                                "o": o, "h": h, "l": l, "c": c, "v": v
                            })

                        # Also aggregate into larger timeframes
                        # Uses _CANDLE_LOCK to protect _CURRENT_CANDLE + _CANDLES
                        with _CANDLE_LOCK:
                            for tf, secs in CANDLE_TIMEFRAMES.items():
                                if tf == "1m":
                                    continue  # already done
                                if secs < 60:
                                    continue  # can't build sub-minute from 1m bars
                                boundary = _candle_boundary(ts, secs)
                                cur = _CURRENT_CANDLE[sym].get(tf)
                                if cur is None or cur["t"] != boundary:
                                    if cur is not None:
                                        _CANDLES[sym][tf].append(dict(cur))
                                    _CURRENT_CANDLE[sym][tf] = {
                                        "t": boundary, "o": o, "h": h, "l": l, "c": c, "v": v
                                    }
                                else:
                                    cur["h"] = max(cur["h"], h)
                                    cur["l"] = min(cur["l"], l)
                                    cur["c"] = c
                                    cur["v"] += v

                    # Flush remaining current candles to deques
                    with _CANDLE_LOCK:
                        for tf in CANDLE_TIMEFRAMES:
                            cur = _CURRENT_CANDLE[sym].get(tf)
                            if cur is not None:
                                _CANDLES[sym][tf].append(dict(cur))
                                # Only reset if live trading hasn't already advanced past backfill
                                _raw_t = bars[-1].get("t", "") if bars else ""
                                try:
                                    backfill_last_t = datetime.fromisoformat(_raw_t.replace("Z", "+00:00")).timestamp() if _raw_t else 0
                                except Exception:
                                    backfill_last_t = 0
                                if cur["t"] <= backfill_last_t:
                                    _CURRENT_CANDLE[sym][tf] = None

                    candle_count = sum(len(_CANDLES[sym][tf]) for tf in CANDLE_TIMEFRAMES)
                    log.info("L2 backfill: %s seeded %d bars → %d total candles across all TFs",
                             sym, len(bars), candle_count)

                log.info("L2 backfill: 1m bars complete — starting tick bp backfill...")

                # ── Phase 2: Tick-level bp backfill ──
                # Fetch 1-second bars in chunks, rebuild buy/sell per price (bp)
                # for each 1m candle. This gives the VP real volume-at-price
                # instead of the estimated 60/40 split.
                for sym in SYMBOLS:
                    cid = _connector._symbol_to_contract.get(sym)
                    if not cid:
                        continue
                    try:
                        _backfill_tick_bp(_connector, sym, cid, session_open)
                    except Exception as tick_err:
                        log.warning("L2 tick backfill failed for %s: %s", sym, tick_err)

                log.info("L2 backfill: complete for all symbols (1m + tick bp)")
            except Exception as e:
                import traceback
                log.warning("L2 backfill FAILED: %s\n%s", e, traceback.format_exc())
        threading.Thread(target=_backfill, daemon=True, name="L2Backfill").start()

    except Exception as e:
        log.error("L2 worker: failed to start — %s", e)
        with _L2_LOCK:
            L2_STATE["connected"] = False

    return _connector



def get_connector() -> TopStepXConnector:
    return _connector


def get_adverse_selection(symbol: str):
    """Return the AdverseSelectionEngine for a symbol, or None if not yet created."""
    return _ADVERSE_SELECTION.get(symbol)


# ── Standalone execution ──────────────────────────────────────────────────────
if __name__ == "__main__":
    conn = start_l2_worker()
    print("\nLevel 2 streaming active. Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(5)
            state = get_l2_state()
            print(f"[L2] connected={state['connected']}  "
                  f"mid_prices={state['mid_prices']}  "
                  f"imbalance={state['imbalance']}")
    except KeyboardInterrupt:
        print("\nStopping L2 worker...")
        if conn:
            conn.stop()
