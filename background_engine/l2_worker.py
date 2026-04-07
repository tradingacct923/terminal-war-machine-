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

from background_engine.topstepx_connector import TopStepXConnector

log = logging.getLogger("l2_worker")

# ── Credentials (from .env) ──────────────────────────────────────────────────
USERNAME = os.getenv("TOPSTEPX_USERNAME", "")
API_KEY  = os.getenv("TOPSTEPX_API_KEY",  "")

# ── Symbols to stream ────────────────────────────────────────────────────────
SYMBOLS = ["NQ", "GC"]

# ── OHLC Candle Engine ────────────────────────────────────────────────────────
# Aggregates tick-by-tick trades into OHLC candles for multiple timeframes.
CANDLE_TIMEFRAMES = {
    "5s": 5, "15s": 15, "30s": 30,
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400,
}
CANDLE_MAX = 2000  # max candles stored per timeframe per symbol (holds full 24h session)

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
_EMIT_MIN_INTERVAL = 0.15   # max ~6.6 emits/sec per symbol/tf

# ── Reconnect gap-fill tracking ──
_LAST_TRADE_TS: dict[str, float] = {}   # {symbol: unix_ts of last trade}
_connector_ref = None                    # set by start() for gap-fill access

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

def set_socketio(sio):
    """Called by server.py to inject the SocketIO instance for real-time push."""
    global _socketio
    _socketio = sio
    log.info("Socket.IO reference set for real-time candle push")

def set_detection_callback(callback):
    """Register a callback for NQ detection events (iceberg, sweep, wall_gone).
    Called by schwab_bridge or server.py to wire EdgeDetector.
    callback(detection_type: str, detection_data: dict, symbol: str)
    """
    global _detection_callback
    _detection_callback = callback
    log.info("Detection callback registered for cross-asset forwarding")

# ── Trade scoring callback (for tape glow via EdgeDetector) ──
_trade_score_callback = None  # callable(symbol, volume, side, price, timestamp) -> dict|None

def set_trade_score_callback(callback):
    """Register EdgeDetector.score_trade() for regime-adaptive tape glow scoring.
    Called by schwab_bridge to wire EdgeDetector.
    callback(symbol: str, volume: int, side: str, price: float, timestamp: float) -> dict|None
    """
    global _trade_score_callback
    _trade_score_callback = callback
    log.info("Trade score callback registered for tape glow")


# ══════════════════════════════════════════════════════════════════════════════
# ORDERFLOW DETECTION — Iceberg + Sweep engines
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# REGIME-ADAPTIVE THRESHOLDS — Options-driven market regime classifier
# ══════════════════════════════════════════════════════════════════════════════
# 5 regimes, each with tuned thresholds for iceberg + drifting detection.
# Updated by server.py whenever options data refreshes (~every 5 min).

_REGIME_THRESHOLDS = {
    # PIN / MEAN-REVERT: long gamma, high GEX, IV cheap, spot between walls
    # Heavy MM activity, walls everywhere → strict filter or drown in signals
    "pin_mean_revert": {
        "ice_cv": 0.25, "ice_refill_count": 4,
        "drift_cv": 0.25, "drift_min_fills": 7, "drift_min_spread": 5,
    },
    # LONG GAMMA / STABLE: long gamma, GEX positive, normal
    # Standard range-bound. Default detection settings
    "long_gamma_stable": {
        "ice_cv": 0.30, "ice_refill_count": 3,
        "drift_cv": 0.30, "drift_min_fills": 5, "drift_min_spread": 4,
    },
    # TRANSITION: spot near gamma flip (±0.5%)
    # Anything goes — regime could shift mid-detection
    "transition": {
        "ice_cv": 0.35, "ice_refill_count": 3,
        "drift_cv": 0.35, "drift_min_fills": 5, "drift_min_spread": 3,
    },
    # SHORT GAMMA / VOLATILE: short gamma, negative GEX
    # Trending. Fewer walls, but ones that show up are significant
    "short_gamma_volatile": {
        "ice_cv": 0.40, "ice_refill_count": 3,
        "drift_cv": 0.45, "drift_min_fills": 4, "drift_min_spread": 3,
    },
    # CRASH / TAIL RISK: short gamma, GEX < -1B, IV rich, spot below put wall
    # Panic. Any wall is a massive signal. Catch everything
    "crash_tail_risk": {
        "ice_cv": 0.45, "ice_refill_count": 3,
        "drift_cv": 0.50, "drift_min_fills": 3, "drift_min_spread": 2,
    },
}

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
    import time as _t

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
        "updated_at": _t.time(),
    }

    if new_regime != old_regime:
        log.info(f"[REGIME] {old_regime} → {new_regime} | "
                 f"spot={spot:.0f} flip={gamma_flip:.0f} "
                 f"gex={total_gex/1e6:.0f}M")


def _get_regime_thresholds() -> dict:
    """Get current adaptive thresholds based on market regime.
    Falls back to hardcoded table during warmup."""
    return _REGIME_THRESHOLDS.get(_CURRENT_REGIME,
                                  _REGIME_THRESHOLDS["transition"])


# ══════════════════════════════════════════════════════════════════════════════
# σ-ADAPTIVE MARKET STATS ENGINE — live threshold computation
# ══════════════════════════════════════════════════════════════════════════════
# Tracks rolling distributions of fill rate, cluster CV, and clip size.
# After warmup (~500 trades), replaces hardcoded thresholds with computed ones.

_ADAPTIVE_WARMUP = 500  # min trades before switching to adaptive mode

_MARKET_STATS: dict = defaultdict(lambda: {
    "fill_timestamps_b": deque(maxlen=500),  # (timestamp,) for buy fills
    "fill_timestamps_s": deque(maxlen=500),  # (timestamp,) for sell fills
    "cluster_cvs": deque(maxlen=200),         # CV of random 5-fill clusters
    "clip_sizes": deque(maxlen=1000),          # all recent trade sizes
    "total_trades": 0,                         # total trades since startup
    # Computed stats (updated every ~1s)
    "fill_rate_b": 0.5,   # buy fills per second (60s window)
    "fill_rate_s": 0.5,   # sell fills per second (60s window)
    "mean_cv": 0.55,      # mean CV of random fill clusters
    "std_cv": 0.15,       # stddev of cluster CVs
    "mean_clip": 2.0,     # mean trade size
    "std_clip": 1.5,      # stddev of trade sizes
    "last_stats_ts": 0,   # throttle: recompute only every 1s
    "last_adaptive_log": 0,  # throttle: log adaptive thresholds every 60s
    "p_coincidence": 0.30,  # fraction of random clusters with CV < threshold (for significance test)
})



def _update_market_stats(symbol: str, volume: int, side: str, timestamp: float):
    """Called on every trade to update rolling market stats."""
    ms = _MARKET_STATS[symbol]
    ms["total_trades"] += 1
    ms["clip_sizes"].append(volume)
    if side == "b":
        ms["fill_timestamps_b"].append(timestamp)
    elif side == "s":
        ms["fill_timestamps_s"].append(timestamp)

    # Build cluster CV samples: every 5th trade, compute CV of last 5 clips
    if ms["total_trades"] % 5 == 0 and len(ms["clip_sizes"]) >= 5:
        last5 = list(ms["clip_sizes"])[-5:]
        m5 = sum(last5) / 5
        if m5 > 0:
            v5 = sum((x - m5) ** 2 for x in last5) / 5
            cv5 = math.sqrt(v5) / m5
            ms["cluster_cvs"].append(cv5)
            # Feed into Kalman filter for state-space CV estimation
            _KALMAN_CV[symbol].update(cv5)

    # Recompute derived stats at most once per second
    if timestamp - ms["last_stats_ts"] < 1.0:
        return
    ms["last_stats_ts"] = timestamp

    # Fill rates (60s window)
    cutoff_60 = timestamp - 60.0
    b_recent = [t for t in ms["fill_timestamps_b"] if t >= cutoff_60]
    s_recent = [t for t in ms["fill_timestamps_s"] if t >= cutoff_60]
    ms["fill_rate_b"] = max(0.1, len(b_recent) / 60.0)
    ms["fill_rate_s"] = max(0.1, len(s_recent) / 60.0)

    # Cluster CV distribution
    cvs = list(ms["cluster_cvs"])
    if len(cvs) >= 10:
        ms["mean_cv"] = sum(cvs) / len(cvs)
        var_cv = sum((c - ms["mean_cv"]) ** 2 for c in cvs) / len(cvs)
        ms["std_cv"] = max(0.01, math.sqrt(var_cv))

        # p_coincidence: what fraction of random clusters accidentally
        # have CV below the adaptive threshold? Used for significance test.
        # We compute threshold here using transition regime (1.5σ) as baseline
        tentative_cv_thresh = max(0.12, ms["mean_cv"] - 1.5 * ms["std_cv"])
        n_below = sum(1 for c in cvs if c < tentative_cv_thresh)
        ms["p_coincidence"] = max(0.05, min(0.70, n_below / len(cvs)))

    # Clip size distribution
    clips = list(ms["clip_sizes"])
    if len(clips) >= 20:
        ms["mean_clip"] = sum(clips) / len(clips)
        var_clip = sum((c - ms["mean_clip"]) ** 2 for c in clips) / len(clips)
        ms["std_clip"] = max(0.01, math.sqrt(var_clip))



# Regime → sigma multiplier (how many σ below mean CV to set threshold)
_REGIME_SIGMA = {
    "pin_mean_revert":      2.0,   # strict: need 2σ below normal CV
    "long_gamma_stable":    1.5,
    "transition":           1.5,
    "short_gamma_volatile": 1.0,
    "crash_tail_risk":      0.75,  # loose: 0.75σ is enough
}

# Regime → target confidence level for fill count significance test
# Lower = more lenient (fewer fills needed), Higher = stricter
_REGIME_CONFIDENCE = {
    "pin_mean_revert":      0.005,  # need P(coincidence) < 0.5% — very strict
    "long_gamma_stable":    0.01,   # need P(coincidence) < 1%
    "transition":           0.01,   # need P(coincidence) < 1%
    "short_gamma_volatile": 0.02,   # need P(coincidence) < 2% — more lenient
    "crash_tail_risk":      0.05,   # need P(coincidence) < 5% — most lenient
}


# Regime → min tick spread for drifting
_REGIME_SPREAD = {
    "pin_mean_revert": 7, "long_gamma_stable": 5,
    "transition": 4, "short_gamma_volatile": 3, "crash_tail_risk": 2,
}


def _get_adaptive_thresholds(symbol: str, side: str = "b") -> dict:
    """Compute σ-adaptive thresholds from live market data.

    Falls back to hardcoded _REGIME_THRESHOLDS during warmup (<500 trades).
    After warmup, thresholds are derived from rolling stats with regime
    as a strictness multiplier.
    """
    ms = _MARKET_STATS[symbol]
    regime = _CURRENT_REGIME

    # Warmup: not enough data yet, use hardcoded table
    if ms["total_trades"] < _ADAPTIVE_WARMUP:
        return _get_regime_thresholds()

    sigma_mult = _REGIME_SIGMA.get(regime, 1.5)

    # Kalman-filtered CV threshold (replaces laggy rolling mean)
    # Uses Kalman state estimate ± sigma_mult * uncertainty
    kalman = _KALMAN_CV[symbol]
    if kalman._n >= 10:
        # Use Kalman filter: faster regime tracking than rolling mean
        mean_cv = kalman.state
        std_cv = kalman.uncertainty
    else:
        # Fallback to rolling stats during Kalman warmup
        mean_cv = ms["mean_cv"]
        std_cv = ms["std_cv"]
    ice_cv = max(0.12, round(mean_cv - sigma_mult * std_cv, 3))
    drift_cv = max(0.15, round(ice_cv + 0.05, 3))  # drift always slightly looser

    # Statistical significance fill count: N > log(target) / log(p)
    # p = fraction of random clusters that accidentally look consistent
    # target = regime-specific confidence level (e.g., 0.01 = 99% sure)
    fill_rate = ms["fill_rate_b"] if side == "b" else ms["fill_rate_s"]
    p_val = ms["p_coincidence"]
    target = _REGIME_CONFIDENCE.get(regime, 0.01)

    # N > log(target) / log(p)  — how many consistent fills for significance
    if p_val < 0.99:  # avoid log(1) = 0 division
        sig_fills = math.ceil(math.log(target) / math.log(p_val))
    else:
        sig_fills = 15  # if p ≈ 1.0, everything looks consistent, max out

    ice_refill_count = max(3, min(20, sig_fills))
    drift_min_fills = max(3, min(30, sig_fills + 2))  # drift needs slightly more


    # Tick spread for drifting
    drift_min_spread = _REGIME_SPREAD.get(regime, 4)

    # Volume anomaly floor: clip must be above market_avg + 0.5σ
    mean_clip = ms["mean_clip"]
    std_clip = ms["std_clip"]
    min_clip_sigma = max(1, int(mean_clip + 0.5 * std_clip))

    result = {
        "ice_cv": ice_cv,
        "ice_refill_count": ice_refill_count,
        "drift_cv": drift_cv,
        "drift_min_fills": drift_min_fills,
        "drift_min_spread": drift_min_spread,
        "min_clip_sigma": min_clip_sigma,
        "_adaptive": True,  # flag: using computed thresholds
        "_mean_cv": round(mean_cv, 3),
        "_std_cv": round(std_cv, 3),
        "_fill_rate": round(fill_rate, 2),
        "_sigma_mult": sigma_mult,
        "_p_coincidence": round(p_val, 3),
        "_confidence_target": target,

    }

    # Log adaptive thresholds every 60 seconds
    now = ms["last_stats_ts"]
    if now - ms["last_adaptive_log"] >= 60:
        ms["last_adaptive_log"] = now
        hardcoded = _get_regime_thresholds()
        log.info(
            f"[ADAPTIVE] {symbol} regime={regime} | "
            f"cv={ice_cv:.3f} (σ: mean={mean_cv:.3f} std={std_cv:.3f} mult={sigma_mult}) "
            f"fills={ice_refill_count} (p={p_val:.2f} target={target}) "
            f"drift_fills={drift_min_fills} "
            f"min_clip={min_clip_sigma} | "
            f"OLD cv={hardcoded['ice_cv']} fills={hardcoded['ice_refill_count']}"
        )


    return result



# ── Iceberg Detection Constants (defaults, overridden by regime) ──
_ICE_REFILL_COUNT     = 3       # min refills at same price to trigger
_ICE_CV_THRESHOLD     = 0.35    # max coefficient of variation (stddev/mean) for clip consistency
_ICE_MIN_CLIP_FLOOR   = 1       # absolute minimum clip size (never go below 1)
_ICE_ZONE_TICKS       = 2       # ±2 ticks = adjacent prices count as same zone
_ICE_ABSORB_MAX_MOVE  = 2       # price must stay within ±2 ticks to be "absorbing"

# Tiered detection windows: (fallback_seconds, volume_multiplier, confidence_label)
# volume_multiplier × empirical_bucket_size = actual volume window
# Multipliers are structural: 1× = one bucket, 3× = three buckets, 10× = ten buckets
# These are NOT guesses — they represent "how many statistical samples back to look"
_ICE_WINDOWS = [
    (5.0,   1,   "high"),    # 1 bucket  back = fast refill
    (15.0,  3,   "medium"),  # 3 buckets back = patient algo
    (60.0,  10,  "low"),     # 10 buckets back = very patient
]

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

# Per-symbol VPIN engine instances
try:
    from connectors.vpin_engine import VPINEngine
    _VPIN_ENGINES: dict = defaultdict(VPINEngine)
except ImportError:
    _VPIN_ENGINES = {}

# Per-symbol, per-fill volume-clock tagged tracker:
# {symbol: {price_str: deque of (timestamp, volume, side, tau)}}
_ICE_TRACKER_VTAG: dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=200)))

# ── Rolling Trade Size Tracker (for adaptive min clip) ──
# {symbol: deque of recent trade sizes, max 500}
_TRADE_SIZE_HISTORY: dict = defaultdict(lambda: deque(maxlen=500))

# ── Recent Trade Price Tracker (for iceberg absorption detection) ──
# {symbol: deque of (timestamp, price), max 200}
_ICE_PRICE_HISTORY: dict = defaultdict(lambda: deque(maxlen=200))

# ── DOM Cross-Validation State (Elite Feature #1) ──
# {symbol: {price_str: bid_size_int}}  — latest DOM bid sizes
_DOM_BID_SNAP: dict = defaultdict(dict)
# {symbol: {price_str: ask_size_int}}  — latest DOM ask sizes
_DOM_ASK_SNAP: dict = defaultdict(dict)
# {symbol: {price_str: bid_size_int}}  — PREVIOUS DOM bid sizes
_DOM_BID_PREV: dict = defaultdict(dict)
# {symbol: {price_str: ask_size_int}}
_DOM_ASK_PREV: dict = defaultdict(dict)
# Fills between DOM snapshots: {symbol: {price_str: total_vol}}
_DOM_FILLS_PENDING: dict = defaultdict(lambda: defaultdict(int))

# ── Drifting Iceberg State (Elite Feature #4) ──
_DRIFT_WINDOW_SEC      = 30.0   # fallback time-based window
# _DRIFT_WINDOW_VOL: derived as 5× empirical bucket (set dynamically, no hardcode)
_DRIFT_MIN_FILLS       = 5      # need at least 5 same-side fills
_DRIFT_MAX_CV          = 0.40   # clip consistency (looser for drift)
_DRIFT_MIN_PRICE_SPREAD = 3     # must span 3+ distinct prices
# {symbol: {"b": deque, "s": deque}} — now includes tau tag
_DRIFT_TRACKER: dict = defaultdict(lambda: {"b": deque(maxlen=100),
                                             "s": deque(maxlen=100)})

# ── Level Memory State (Elite Feature #5) ──
# {symbol: {price_str: {"count": N, "total_vol": V, "last_side": s, "last_ts": T, "avg_size": f}}}
_ICE_LEVEL_MEMORY: dict = defaultdict(lambda: defaultdict(lambda: {
    "count": 0, "total_vol": 0, "last_side": "", "last_ts": 0, "avg_size": 0.0
}))

# ── Post-Iceberg Prediction State (Elite Feature #6) ──
# {symbol: deque of outcome dicts, maxlen=100}
_ICE_OUTCOMES: dict = defaultdict(lambda: deque(maxlen=100))
# {symbol: list of pending outcome checks}
_ICE_PENDING: dict = defaultdict(list)

# ── Wall Gone Detection State ──
_ICE_GONE_TIMEOUT = 3.0  # seconds without refill = wall gone
# {symbol: {price_str: {"last_refill_ts": T, "was_active": bool, "gone_announced": bool}}}
_ICE_WALL_STATE: dict = defaultdict(lambda: defaultdict(lambda: {
    "last_refill_ts": 0, "was_active": False, "gone_announced": False
}))

# ── DOM Band Depth Tracking (Drifting Layer 2) ──
# {symbol: {"b": deque of (ts, total_depth, fills_since_last), "s": deque}}
_DOM_BAND_DEPTH: dict = defaultdict(lambda: {"b": deque(maxlen=50),
                                              "s": deque(maxlen=50)})

# ── Sweep Detection Constants ──
_SWEEP_MIN_LEVELS     = 3       # min consecutive price levels swept
_SWEEP_WINDOW_SEC     = 0.200   # 200ms window for sweep
_SWEEP_MIN_VOLUME     = 100     # total swept volume threshold (~$2M notional on NQ)

# ── Detection State ──
# _ICE_TRACKER: {symbol: {quantized_price_str: [(timestamp, volume, side), ...]}}
_ICE_TRACKER: dict = defaultdict(lambda: defaultdict(lambda: deque(maxlen=200)))
# _SWEEP_TRACKER: {symbol: [(timestamp, price, volume, side), ...]}
_SWEEP_TRACKER: dict = defaultdict(list)
# Detected results attached to current candle: {symbol: {tf: {icebergs: {}, sweeps: []}}}
_DETECT_RESULTS: dict = defaultdict(lambda: defaultdict(dict))

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
_IGN_TRACKER: dict = defaultdict(list)
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


# ═══════════════════════════════════════════════════════════════════════════════
# ELITE HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _dom_update_snapshots(symbol: str, dom: dict):
    """Called from on_dom_update to store bid/ask snapshots for cross-validation."""
    _DOM_BID_PREV[symbol] = _DOM_BID_SNAP[symbol].copy()
    _DOM_ASK_PREV[symbol] = _DOM_ASK_SNAP[symbol].copy()
    new_bids = {}
    new_asks = {}
    for lvl in dom.get("bids", []):
        if isinstance(lvl, dict):
            p = str(lvl.get("price", ""))
            if p:
                new_bids[p] = lvl.get("size", 0)
    for lvl in dom.get("asks", []):
        if isinstance(lvl, dict):
            p = str(lvl.get("price", ""))
            if p:
                new_asks[p] = lvl.get("size", 0)
    _DOM_BID_SNAP[symbol] = new_bids
    _DOM_ASK_SNAP[symbol] = new_asks
    # Resolve pending fill validations
    _DOM_FILLS_PENDING[symbol].clear()


def _dom_cross_validate(symbol: str, price_str: str,
                        volume: int, side: str):
    """DOM cross-validation: check if order book refilled after a fill.
    Returns (dom_confidence, refill_amount)."""
    _DOM_FILLS_PENDING[symbol][price_str] += volume
    if side == "b":
        # Buyer lifted the ask → check ask side
        dom_before = _DOM_ASK_PREV[symbol].get(price_str, 0)
        dom_after = _DOM_ASK_SNAP[symbol].get(price_str, 0)
    else:
        # Seller hit the bid → check bid side
        dom_before = _DOM_BID_PREV[symbol].get(price_str, 0)
        dom_after = _DOM_BID_SNAP[symbol].get(price_str, 0)
    if dom_before == 0:
        return ("unconfirmed", 0)
    expected = max(0, dom_before - volume)
    refill = dom_after - expected
    if refill <= 0:
        return ("unconfirmed", 0)
    ratio = refill / max(volume, 1)
    if ratio > 0.8:
        return ("confirmed", refill)
    elif ratio > 0.3:
        return ("likely", refill)
    elif ratio > 0.0:
        return ("possible", refill)
    return ("unconfirmed", 0)


def _analyze_fill_timing(fills_in_window):
    """Inter-fill timing analysis: algo vs random.
    Returns (gap_cv, timing_confidence)."""
    if len(fills_in_window) < 3:
        return (None, "insufficient")
    timestamps = sorted([f[0] for f in fills_in_window])
    gaps = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return (0.0, "instant")
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap == 0:
        return (0.0, "instant")
    variance = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
    gap_cv = (variance ** 0.5) / mean_gap
    if gap_cv < 0.3:
        return (round(gap_cv, 3), "algo_confirmed")
    elif gap_cv < 0.6:
        return (round(gap_cv, 3), "algo_likely")
    elif gap_cv < 1.0:
        return (round(gap_cv, 3), "mixed")
    return (round(gap_cv, 3), "random")


def _detect_drifting_iceberg(symbol: str, price_f: float, volume: int,
                              timestamp: float, side: str):
    """Drifting iceberg detection — 3-layer multi-level detection.
    Layer 1: Behavioral fingerprint (clip CV across all prices)
    Layer 2: DOM total depth anomaly (depth_leak_ratio)
    Layer 3: Timing regularity (gap CV)
    """
    if side == "n":
        return None
    # ── σ-adaptive thresholds (falls back to hardcoded during warmup) ──
    _rt = _get_adaptive_thresholds(symbol, side)
    drift_min_fills = _rt["drift_min_fills"]
    drift_min_spread = _rt["drift_min_spread"]
    drift_cv_max = _rt["drift_cv"]

    tracker = _DRIFT_TRACKER[symbol][side]
    tracker.append((timestamp, price_f, volume))

    # ── Volume Clock windowing (primary) vs time-based (fallback) ──
    vclock = _VOLUME_CLOCKS[symbol]
    if vclock.warm:
        # Drift uses 5× bucket multiplier (between iceberg 3× and 10×)
        drift_ticks_back = 5
        avg_bucket_sec = vclock.get_avg_bucket_duration()
        if avg_bucket_sec > 0:
            approx_sec = drift_ticks_back * avg_bucket_sec
            cutoff = timestamp - approx_sec
        else:
            cutoff = timestamp - _DRIFT_WINDOW_SEC
    else:
        cutoff = timestamp - _DRIFT_WINDOW_SEC

    recent = [(t, p, v) for t, p, v in tracker if t >= cutoff]
    if len(recent) < drift_min_fills:
        return None
    prices = set(round(p, 2) for _, p, _ in recent)
    # Price RANGE check in ticks (not just count of distinct prices)
    # Requires the drift to span a meaningful range, not just walk the book
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    price_range_ticks = (max(prices) - min(prices)) / tick_size
    if price_range_ticks < drift_min_spread:
        return None

    vols = [v for _, _, v in recent]
    mean_v = sum(vols) / len(vols)
    if mean_v == 0:
        return None
    var = sum((v - mean_v) ** 2 for v in vols) / len(vols)
    cv = (var ** 0.5) / mean_v
    if cv > drift_cv_max:
        return None

    # ── Layer 2: DOM Total Depth Anomaly ──
    # Check if total bid/ask depth in the band barely dropped despite fills
    band_low = min(p for _, p, _ in recent)
    band_high = max(p for _, p, _ in recent)
    dom_leak = None
    dom_snap = _DOM_BID_SNAP[symbol] if side == "s" else _DOM_ASK_SNAP[symbol]
    if dom_snap:
        # Total current depth across the band
        actual_depth = 0
        for ps, sz in dom_snap.items():
            try:
                pf = float(ps)
                if band_low <= pf <= band_high:
                    actual_depth += sz
            except (ValueError, TypeError):
                pass
        total_fills = sum(vols)
        # Check depth history for this side
        depth_history = _DOM_BAND_DEPTH[symbol][side]
        if depth_history:
            # Use the oldest depth snapshot in our window
            oldest_depth = depth_history[0][1]
            expected_depth = max(0, oldest_depth - total_fills)
            if total_fills > 0:
                dom_leak = round((actual_depth - expected_depth) / total_fills, 2)
        # Record current depth for future comparisons
        depth_history.append((timestamp, actual_depth, sum(vols)))

    # ── Layer 3: Timing analysis ──
    gap_cv_val, timing = _analyze_fill_timing([(t, 0, "") for t, _, _ in recent])

    # ── Composite confidence (all 3 layers) ──
    score = 0
    # Layer 1: Clip consistency
    if cv < 0.25:
        score += 2
    elif cv < 0.40:
        score += 1
    # Layer 2: DOM depth anomaly
    if dom_leak is not None and dom_leak > 0.5:
        score += 2  # hidden liquidity confirmed
    elif dom_leak is not None and dom_leak > 0.2:
        score += 1  # some hidden liquidity
    # Layer 3: Timing regularity
    if timing in ("algo_confirmed",):
        score += 2
    elif timing in ("algo_likely",):
        score += 1

    drift_conf = "confirmed" if score >= 4 else "likely" if score >= 2 else "possible"
    return {
        "type": "drifting",
        "fills": len(recent),
        "prices_hit": len(prices),
        "band_low": round(band_low, 2),
        "band_high": round(band_high, 2),
        "band_range": round(band_high - band_low, 2),
        "total_vol": sum(vols),
        "avg_clip": round(mean_v, 1),
        "cv": round(cv, 3),
        "side": side,
        "gap_cv": gap_cv_val,
        "timing": timing,
        "dom_leak": dom_leak,
        "drift_confidence": drift_conf,
    }


def _update_level_memory(symbol: str, price_str: str, iceberg_result: dict):
    """Record iceberg detection at this level for historical tracking."""
    mem = _ICE_LEVEL_MEMORY[symbol][price_str]
    mem["count"] += 1
    mem["total_vol"] += iceberg_result.get("est_hidden", 0)
    mem["last_side"] = iceberg_result.get("side", "")
    mem["last_ts"] = time.time()
    mem["avg_size"] = mem["total_vol"] / max(mem["count"], 1)


def _record_iceberg_completion(symbol: str, price: float, side: str,
                                ts: float, size_rank: str, confidence: str,
                                state_vector: dict = None):
    """Schedule outcome checks for post-iceberg prediction.
    state_vector: snapshot of detection-time features for JSONL logging.
    """
    # ── Phase 6 Alpha Filters (universal choke point) ──
    # NOTE: Kill combos were reset after discovering direction inversion bug.
    # Old combos were calibrated on inverted PnL — they were killing WINNING signals.
    # With correct direction semantics:
    #   side="s" = bid iceberg → LONG  (buyer wall absorbing sellers)
    #   side="b" = ask iceberg → SHORT (seller wall absorbing buyers)
    # Fresh kill combos will be re-derived from corrected OOS data.
    _KILL_COMBOS: set = set()  # cleared — re-calibrate after 10+ sessions with correct direction
    current_regime = _CURRENT_REGIME
    if state_vector:
        current_regime = state_vector.get('regime', _CURRENT_REGIME)
    if (current_regime, side) in _KILL_COMBOS:
        return  # suppress toxic combo entirely

    # CV Gate: low Kalman CV = noise, not signal
    # Re-evaluate threshold after collecting corrected-direction OOS data.
    kalman = _KALMAN_CV.get(symbol)
    if kalman and kalman._n > 250 and kalman.state < 0.04:
        return  # suppress noise-regime signals

    # ── Directional Gate: Cross-Asset Consensus ──
    # Iceberg gives +6.6 pts entry alpha but no directional edge.
    # GEX/MM/0DTE from EdgeDetector provides the directional bias.
    # BLOCK when cross-asset signals contradict iceberg direction.
    #
    # Direction semantics (corrected):
    #   side="s" (sell aggressor hitting bid) → bid iceberg → buyer wall → LONG
    #   side="b" (buy aggressor lifting ask) → ask iceberg → seller wall → SHORT
    if _cross_asset_provider:
        try:
            ctx = _cross_asset_provider(symbol)
            if ctx:
                iceberg_is_long = (side != 'b')  # side="s" → LONG, side="b" → SHORT

                # MM withdrawal direction: -1=bearish (pulled bids), +1=bullish, 0=neutral
                mm_bias = ctx.get('mm_pull_bias', 0)
                gex_zone = ctx.get('gex_zone', 'NO_DATA')
                gex_factor = ctx.get('gex_factor', 1.0)

                # HARD BLOCK: MM consensus directly contradicts iceberg direction
                # AND GEX structure confirms the contra-move
                #   LONG iceberg + MM bearish + below gamma flip = likely steamrolled
                #   SHORT iceberg + MM bullish + above gamma flip = likely short-squeezed
                if iceberg_is_long and mm_bias == -1:
                    # MMs pulling bids → bearish. Is GEX also against us?
                    gex_contra = gex_zone in (
                        'AT_PUT_WALL_BREAKOUT',  # near put wall breaking down
                        'SHORT_GAMMA_ZONE',       # below flip → amplified downside
                        'AT_GAMMA_FLIP',          # at flip → volatile, direction unclear
                    )
                    if gex_contra or gex_factor < 0.8:
                        log.info(
                            f"[DIRECTIONAL-GATE] BLOCKED LONG iceberg @ {price}: "
                            f"MM={mm_bias} GEX={gex_zone}({gex_factor:.2f})"
                        )
                        return

                elif not iceberg_is_long and mm_bias == 1:
                    # MMs pulling asks → bullish. Is GEX also against us?
                    gex_contra = gex_zone in (
                        'AT_CALL_WALL_BREAKOUT',  # near call wall breaking up
                        'LONG_GAMMA_ZONE',         # above flip → dampened downside
                    )
                    if gex_contra or gex_factor < 0.8:
                        log.info(
                            f"[DIRECTIONAL-GATE] BLOCKED SHORT iceberg @ {price}: "
                            f"MM={mm_bias} GEX={gex_zone}({gex_factor:.2f})"
                        )
                        return

                # Log cross-asset context for all passed trades (for future analysis)
                if state_vector is not None:
                    state_vector['mm_bias_at_detect'] = mm_bias
                    state_vector['gex_zone_at_detect'] = gex_zone
                    state_vector['gex_factor_at_detect'] = gex_factor
        except Exception as e:
            log.debug(f"[DIRECTIONAL-GATE] Cross-asset check error: {e}")
            # Don't block if cross-asset data is unavailable

    pending = {
        "side": side, "price": price, "ts": ts,
        "size_rank": size_rank, "confidence": confidence,
        "check_10s": ts + 10, "check_30s": ts + 30, "check_60s": ts + 60,
        "outcome_10s": None, "outcome_30s": None, "outcome_60s": None,
        # Intra-window MAE/MFE tracking
        "mfe": 0.0, "mae": 0.0,     # Current running MFE/MAE
        "mfe_10s": None, "mae_10s": None,
        "mfe_30s": None, "mae_30s": None,
        "mfe_60s": None, "mae_60s": None,
    }
    if state_vector:
        pending.update(state_vector)
    _ICE_PENDING[symbol].append(pending)


def _check_pending_outcomes(symbol: str, current_price: float, current_ts: float):
    """Resolve pending post-iceberg outcome checks and track intra-window MFE/MAE.
    Phase 7: Also tracks dynamic SL = max(3.0, CV*100) exit simulation.

    DIRECTION SEMANTICS (critical):
      p["side"] is the AGGRESSOR side — who is hitting the iceberg.
      side="s" (sell aggressor repeatedly hitting bid) → BID ICEBERG → buyer's wall → LONG
      side="b" (buy aggressor repeatedly lifting ask) → ASK ICEBERG → seller's wall → SHORT
      Therefore: direction = -1 if side=="b" else 1
      (Trade WITH the iceberg, AGAINST the aggressor.)
    """
    still_pending = []
    for p in _ICE_PENDING[symbol]:
        direction = -1 if p["side"] == "b" else 1   # FIXED: was 1 if "b" else -1 (inverted)
        pnl = round((current_price - p["price"]) * direction, 2)

        # Continually update MFE/MAE while trade is active
        p["mfe"] = max(p["mfe"], pnl)
        p["mae"] = min(p["mae"], pnl)

        # Phase 7: Track dynamic SL hit (computed once at entry)
        if "dynamic_sl" not in p:
            cv = _KALMAN_CV[symbol].state if symbol in _KALMAN_CV else 0.05
            p["dynamic_sl"] = round(max(3.0, cv * 100), 2)
            p["dynamic_sl_hit"] = False
            p["dynamic_sl_hit_ts"] = None
            p["dynamic_sl_pnl"] = None

        if not p["dynamic_sl_hit"] and pnl <= -p["dynamic_sl"]:
            p["dynamic_sl_hit"] = True
            p["dynamic_sl_hit_ts"] = current_ts
            p["dynamic_sl_pnl"] = round(-p["dynamic_sl"], 2)

        if p["outcome_10s"] is None and current_ts >= p["check_10s"]:
            p["outcome_10s"] = pnl
            p["mfe_10s"] = p["mfe"]
            p["mae_10s"] = p["mae"]
            
        if p["outcome_30s"] is None and current_ts >= p["check_30s"]:
            p["outcome_30s"] = pnl
            p["mfe_30s"] = p["mfe"]
            p["mae_30s"] = p["mae"]
            
        if p["outcome_60s"] is None and current_ts >= p["check_60s"]:
            p["outcome_60s"] = pnl
            p["mfe_60s"] = p["mfe"]
            p["mae_60s"] = p["mae"]
            _ICE_OUTCOMES[symbol].append(p)
            # ── Persist to JSONL ──
            _persist_iceberg_outcome(symbol, p)
            continue
            
        still_pending.append(p)
    _ICE_PENDING[symbol] = still_pending


def _persist_iceberg_outcome(symbol, outcome):
    """Append completed iceberg outcome to JSONL log file."""
    try:
        import os, json
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "iceberg_outcomes.jsonl")

        # State vector for future RL training
        kalman = _KALMAN_CV[symbol]
        vclock = _VOLUME_CLOCKS[symbol]
        bucket_ma = sum(vclock._interval_volumes) / max(len(vclock._interval_volumes), 1) \
                    if vclock._interval_volumes else vclock.bucket_size
        bucket_ratio = round(vclock.bucket_size / max(bucket_ma, 1), 3)

        record = {
            "symbol": symbol,
            "side": outcome["side"],
            "price": outcome["price"],
            "ts": outcome["ts"],
            "ts_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(outcome["ts"])),
            "size_rank": outcome.get("size_rank", ""),
            "confidence": outcome.get("confidence", ""),
            "outcome_10s": outcome["outcome_10s"],
            "outcome_30s": outcome["outcome_30s"],
            "outcome_60s": outcome["outcome_60s"],
            "win_10s": outcome["outcome_10s"] is not None and outcome["outcome_10s"] > 0,
            "win_30s": outcome["outcome_30s"] is not None and outcome["outcome_30s"] > 0,
            "win_60s": outcome["outcome_60s"] is not None and outcome["outcome_60s"] > 0,
            # Sub-window Excursion Tracking (MAE/MFE)
            "mfe_10s": outcome.get("mfe_10s", 0),
            "mae_10s": outcome.get("mae_10s", 0),
            "mfe_30s": outcome.get("mfe_30s", 0),
            "mae_30s": outcome.get("mae_30s", 0),
            "mfe_60s": outcome.get("mfe_60s", 0),
            "mae_60s": outcome.get("mae_60s", 0),
            # State-Space Engine: state vector (s_t for SAC training)
            # Use detection-time snapshot to avoid T+60s lookahead bias
            "kalman_cv": outcome.get("kalman_cv_at_detect", round(kalman.state, 4)),
            "kalman_P": outcome.get("kalman_P_at_detect", round(kalman.P, 6)),
            "psi": outcome.get("psi", 0),
            "vclock_bucket": vclock.bucket_size,
            "bucket_ratio": bucket_ratio,
            "regime": outcome.get("regime", _CURRENT_REGIME),
            "stickiness": outcome.get("stickiness", 0),
            "absorption_ratio": outcome.get("absorption_ratio", 0),
            "urgency": outcome.get("urgency", 0),
            # VPIN Toxicity
            "vpin": round(_VPIN_ENGINES[symbol].vpin, 4) if _VPIN_ENGINES and symbol in _VPIN_ENGINES else 0,
            "vpin_regime": _VPIN_ENGINES[symbol].get_regime_modifier() if _VPIN_ENGINES and symbol in _VPIN_ENGINES else 'NEUTRAL',
            # Phase 7: Dynamic Exit Simulation
            "dynamic_sl": outcome.get("dynamic_sl", 0),
            "dynamic_sl_hit": outcome.get("dynamic_sl_hit", False),
            "dynamic_sl_pnl": outcome.get("dynamic_sl_pnl"),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[ICE] Outcome persist error: {e}")


def _get_prediction(symbol: str, side: str):
    """Get statistical prediction from historical iceberg outcomes."""
    outcomes = [o for o in _ICE_OUTCOMES[symbol] if o["side"] == side]
    if len(outcomes) < 5:
        return None
    moves = [o["outcome_30s"] for o in outcomes if o["outcome_30s"] is not None]
    if not moves:
        return None
    avg_move = sum(moves) / len(moves)
    wins = sum(1 for m in moves if m > 0)
    n = len(moves)
    win_rate = wins / n
    pred_conf = "high" if n >= 20 else "medium" if n >= 10 else "low"
    return {
        "avg_move_30s": round(avg_move, 2),
        "win_rate": round(win_rate * 100, 1),
        "sample_size": n,
        "pred_confidence": pred_conf,
    }


def _check_wall_gone(symbol: str, current_ts: float):
    """Check if any active icebergs stopped refilling → wall gone."""
    alerts = []
    for price_str, state in list(_ICE_WALL_STATE[symbol].items()):
        if not state["was_active"]:
            continue
        if current_ts - state["last_refill_ts"] >= _ICE_GONE_TIMEOUT:
            if not state["gone_announced"]:
                state["gone_announced"] = True
                state["was_active"] = False
                alerts.append({
                    "type": "wall_gone", "price": price_str, "ts": current_ts,
                })
                # Record completion for prediction tracking
                _record_iceberg_completion(
                    symbol, float(price_str), state.get("side", "b"),
                    current_ts, "unknown", "unknown"
                )
    return alerts


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ICEBERG DETECTION — v4 FULL INTELLIGENCE
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_iceberg(symbol: str, price_str: str, volume: int,
                    timestamp: float, side: str):
    """Iceberg detection v4 — full elite trading intelligence.

    Returns enriched iceberg dict with ~30 fields if detected, else None.
    Includes: zone, decay, absorption, size rank, urgency, pressure,
    DOM cross-validation, inter-fill timing, countdown, level memory,
    prediction, and drifting detection.
    """
    if side == "n":
        return None

    price_f = float(price_str)

    # Track trade size + price for adaptive thresholds and absorption
    _TRADE_SIZE_HISTORY[symbol].append(volume)
    _ICE_PRICE_HISTORY[symbol].append((timestamp, price_f))

    # Update σ-adaptive market stats on every trade
    _update_market_stats(symbol, volume, side, timestamp)

    # Check pending prediction outcomes on every trade
    _check_pending_outcomes(symbol, price_f, timestamp)

    # Adaptive min clip: use σ-based floor if available, else 50% of average
    trade_hist = _TRADE_SIZE_HISTORY[symbol]
    if len(trade_hist) > 10:
        avg_trade = sum(trade_hist) / len(trade_hist)
        min_clip = max(_ICE_MIN_CLIP_FLOOR, int(avg_trade * 0.5))
    else:
        avg_trade = float(volume)
        min_clip = _ICE_MIN_CLIP_FLOOR

    if volume < min_clip:
        return None


    # Track this fill at this price (with volume-clock tag)
    vclock = _VOLUME_CLOCKS[symbol]
    current_tau = vclock.tau
    tracker = _ICE_TRACKER[symbol][price_str]
    tracker.append((timestamp, volume, side))

    # Also store in volume-tagged tracker
    vtag_tracker = _ICE_TRACKER_VTAG[symbol][price_str]
    vtag_tracker.append((timestamp, volume, side, current_tau))

    # Prune oldest fills beyond widest window (both time and volume)
    max_window_sec = _ICE_WINDOWS[-1][0]  # 60s
    cutoff_prune = timestamp - max_window_sec
    while tracker and tracker[0][0] < cutoff_prune:
        tracker.popleft()
    # Volume-based prune: keep fills within widest volume window (multiplier + 2 margin)
    max_window_mult = _ICE_WINDOWS[-1][1]  # widest multiplier (10)
    vol_cutoff_tau = max(0, current_tau - max_window_mult - 2)
    while vtag_tracker and vtag_tracker[0][3] < vol_cutoff_tau:
        vtag_tracker.popleft()

    # ── ZONE DETECTION: collect fills from adjacent ±N ticks ──
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    zone_fills_all = []
    zone_fills_vtag = []  # volume-tagged zone fills
    zone_levels_hit = set()

    for offset in range(-_ICE_ZONE_TICKS, _ICE_ZONE_TICKS + 1):
        adj_price = round(price_f + offset * tick_size, 2)
        adj_key = str(adj_price)
        adj_fills = _ICE_TRACKER[symbol].get(adj_key, [])
        adj_vtag = _ICE_TRACKER_VTAG[symbol].get(adj_key, [])
        if adj_fills:
            zone_levels_hit.add(adj_key)
            zone_fills_all.extend(adj_fills)
            zone_fills_vtag.extend(adj_vtag)

    is_zone = len(zone_levels_hit) > 1

    # Try each window tier from tightest to widest
    for window_sec, window_vol, confidence in _ICE_WINDOWS:

        # ── Volume Clock windowing (primary) vs time-based (fallback) ──
        if vclock.warm:
            # window_vol is the multiplier (1, 3, 10): look back that many buckets
            tau_cutoff = max(0, current_tau - window_vol)
            raw_vtag = zone_fills_vtag if is_zone else list(vtag_tracker)
            fills_in_window = [(t, v, s) for t, v, s, tau in raw_vtag if tau >= tau_cutoff]
        else:
            # Time-based fallback
            window_cutoff = timestamp - window_sec
            raw_fills = zone_fills_all if is_zone else list(tracker)
            fills_in_window = [f for f in raw_fills if f[0] >= window_cutoff]

        # ── σ-adaptive thresholds (falls back to hardcoded during warmup) ──
        fill_sides = [f[2] for f in fills_in_window]
        dominant_side = fill_sides[0] if fill_sides else "b"
        _rt = _get_adaptive_thresholds(symbol, dominant_side)
        if len(fills_in_window) < _rt["ice_refill_count"]:
            continue

        if len(set(fill_sides)) != 1:
            continue

        vols = [f[1] for f in fills_in_window]
        mean_vol = sum(vols) / len(vols)
        if mean_vol == 0:
            continue
        variance = sum((v - mean_vol) ** 2 for v in vols) / len(vols)
        stddev_vol = variance ** 0.5
        cv = stddev_vol / mean_vol

        if cv > _rt["ice_cv"]:
            continue

        # ── Volume anomaly gate: clip size must be above market mean + 0.5σ ──
        min_clip_sigma = _rt.get("min_clip_sigma", 1)
        if mean_vol < min_clip_sigma:
            continue


        # ═══════════════ ICEBERG DETECTED — COMPUTE ALL INTELLIGENCE ═══════════

        visible_total = sum(vols)
        n_fills = len(fills_in_window)
        time_elapsed = max(fills_in_window[-1][0] - fills_in_window[0][0], 0.01)
        fill_rate = n_fills / time_elapsed
        avg_clip = mean_vol
        # Regime-adaptive est_duration: shorter in crashes (fast fills),
        # longer in pin markets (slow drip). Affects est_hidden accuracy.
        _EST_DURATION_BY_REGIME = {
            "crash_tail_risk":      30.0,
            "short_gamma_volatile": 45.0,
            "transition":           60.0,
            "long_gamma_stable":    90.0,
            "pin_mean_revert":     120.0,
        }
        est_duration = _EST_DURATION_BY_REGIME.get(_CURRENT_REGIME, 60.0)
        est_remaining_fills = max(0.0, fill_rate * (est_duration - max(time_elapsed, 0.1)))
        est_hidden = int(visible_total + est_remaining_fills * avg_clip)
        fill_pct = min(1.0, visible_total / max(est_hidden, 1))
        est_remaining_sec = max(0.0, est_duration - time_elapsed)

        # ── Fill exhaustion (linear regression slope) ──
        decay = "holding"
        slope = 0.0
        if n_fills >= 3 and time_elapsed > 0.1:
            t_start = fills_in_window[0][0]
            xs = [(f[0] - t_start) / time_elapsed for f in fills_in_window]
            ys = vols
            x_mean = sum(xs) / len(xs)
            y_mean = mean_vol
            num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
            den = sum((x - x_mean) ** 2 for x in xs)
            if den > 0 and y_mean > 0:
                raw_slope = num / den
                slope = round(raw_slope / y_mean, 3)
                if slope < -0.15:
                    decay = "exhausting"
                elif slope > 0.15:
                    decay = "strengthening"

        # ── Absorption context + price stickiness score ──
        absorbing = False
        stickiness = 0.0
        prices_during = [p for t, p in _ICE_PRICE_HISTORY[symbol] if t >= window_cutoff]
        if len(prices_during) >= 2:
            price_range = max(prices_during) - min(prices_during)
            if price_range / tick_size <= _ICE_ABSORB_MAX_MOVE:
                absorbing = True
            # Stickiness: what fraction of all volume during this window
            # happened at this iceberg's price zone?
            total_window_vol = sum(
                v for _, v, _ in (
                    f for f in _ICE_TRACKER[symbol].get(price_str, [])
                    if f[0] >= window_cutoff
                )
            )
            # Approximate total market volume in window from trade history
            total_mkt_vol = sum(
                1 for t, _ in _ICE_PRICE_HISTORY[symbol] if t >= window_cutoff
            )
            if total_mkt_vol > 0:
                stickiness = round(visible_total / max(total_mkt_vol, 1), 3)
            # Empirical stickiness: track distribution and flag above P75
            _STICKINESS_DIST[symbol].append(stickiness)
            stick_vals = list(_STICKINESS_DIST[symbol])
            if len(stick_vals) >= 30:
                stick_vals_sorted = sorted(stick_vals)
                p75_idx = int(len(stick_vals_sorted) * 0.75)
                stickiness_threshold = stick_vals_sorted[p75_idx]
            else:
                stickiness_threshold = 0.3  # fallback during warmup
            # Override absorbing from stickiness if empirically extreme
            if stickiness > stickiness_threshold:
                absorbing = True


        # ── Opposition volume & absorption ratio ──
        opp_side = "s" if fill_sides[0] == "b" else "b"
        opposition_vol = 0
        # Split into bid-side and ask-side absorption for Ψ coefficient
        bid_absorbed = 0  # Volume consumed at bid with no price decline
        ask_absorbed = 0  # Volume consumed at ask with no price advance
        for offset in range(-_ICE_ZONE_TICKS, _ICE_ZONE_TICKS + 1):
            adj_price = round(price_f + offset * tick_size, 2)
            adj_key = str(adj_price)
            for ts_o, vol_o, s_o in _ICE_TRACKER[symbol].get(adj_key, []):
                if s_o == opp_side and ts_o >= window_cutoff:
                    opposition_vol += vol_o
                # Ψ calculation: classify absorbed volume by side
                if ts_o >= window_cutoff:
                    # Check if price moved after this fill
                    future_prices = [p for t, p in _ICE_PRICE_HISTORY[symbol]
                                     if ts_o < t <= ts_o + 2.0]
                    if future_prices:
                        price_after = future_prices[-1]
                        if s_o == "b" and price_after <= adj_price:
                            bid_absorbed += vol_o  # Buy absorbed, no advance
                        elif s_o == "s" and price_after >= adj_price:
                            ask_absorbed += vol_o  # Sell absorbed, no decline
        absorption_ratio = round(opposition_vol / max(visible_total, 1), 2)

        # ── Dark Pool Absorption Coefficient (Ψ) — from State-Space spec ──
        # Ψ = Σ V_bid(no decline) / Σ V_ask(no advance)
        # Ψ > 1.0 → buy-side absorption dominance (passive institutional buying)
        # Ψ < 1.0 → sell-side absorption dominance (passive institutional selling)
        # Ψ ≈ 1.0 → balanced flow (no conviction)
        psi = round(bid_absorbed / max(ask_absorbed, 1), 3)

        # ── Size rank (σ distance) ──
        size_rank = "retail"
        if len(trade_hist) > 20:
            th_mean = sum(trade_hist) / len(trade_hist)
            th_var = sum((v - th_mean) ** 2 for v in trade_hist) / len(trade_hist)
            th_std = max(th_var ** 0.5, 0.01)
            sigma = (avg_clip - th_mean) / th_std
            if sigma >= 3.0:
                size_rank = "whale"
            elif sigma >= 2.0:
                size_rank = "institutional"
            elif sigma >= 1.0:
                size_rank = "professional"

        # ── Prediction (Elite #6 — feeds into urgency) ──
        prediction = _get_prediction(symbol, fill_sides[0])

        # ── Urgency score (0-1 composite, outcome-weighted) ──
        time_factor = min(fill_rate / 2.0, 1.0)
        size_factor = min(avg_clip / max(avg_trade, 1), 1.0)
        remaining_factor = 1.0 - fill_pct
        base_urgency = time_factor * 0.4 + size_factor * 0.3 + remaining_factor * 0.3

        # Outcome-weighted adjustment: if historical win rate is known,
        # boost urgency for high-accuracy sides, reduce for low-accuracy
        outcome_modifier = 1.0
        if prediction and prediction.get('sample_size', 0) >= 10:
            wr = prediction.get('win_rate', 50.0)
            if wr >= 65.0:
                outcome_modifier = 1.15  # historically profitable side
            elif wr <= 35.0:
                outcome_modifier = 0.80  # historically losing side

        # Cross-asset modifier: boost if GEX/MM/stop cluster aligns
        cross_asset_modifier = 1.0
        cross_context = None
        if _cross_asset_provider:
            try:
                cross_context = _cross_asset_provider(symbol)
                if cross_context:
                    gex_factor = cross_context.get('gex_factor', 1.0)
                    cross_asset_modifier *= gex_factor
                    # If near a stop cluster on our side, boost urgency
                    if cross_context.get('near_stop_cluster'):
                        cluster_side = cross_context.get('stop_cluster_side', '')
                        if (cluster_side == 'SELL' and fill_sides[0] == 's') or \
                           (cluster_side == 'BUY' and fill_sides[0] == 'b'):
                            cross_asset_modifier *= 1.2  # iceberg near relevant stop cluster
                    # If MM withdrawal agrees with our direction, boost
                    mm_bias = cross_context.get('mm_pull_bias', 0)
                    if (mm_bias < 0 and fill_sides[0] == 's') or \
                       (mm_bias > 0 and fill_sides[0] == 'b'):
                        cross_asset_modifier *= 1.1  # MM consensus agrees
            except Exception:
                pass

        urgency = round(min(1.0, base_urgency * outcome_modifier * cross_asset_modifier), 3)

        # ── Pressure signal (decision tree + outcome-informed) ──
        if decay == "exhausting" and fill_pct > 0.5:
            pressure = "wall_exhausted"
        elif decay == "exhausting" and not absorbing:
            pressure = "wall_breaking"
        elif absorbing and fill_pct < 0.3:
            pressure = "bullish_wall" if fill_sides[0] == "b" else "bearish_wall"
        elif absorbing:
            pressure = "bullish_wall" if fill_sides[0] == "b" else "bearish_wall"
        elif fill_pct < 0.2:
            pressure = "wall_fresh"
        else:
            pressure = "wall_active"

        # Outcome-informed pressure override: if this side has <35% win rate,
        # downgrade to "low_edge" regardless of other signals
        if prediction and prediction.get('sample_size', 0) >= 15:
            wr = prediction.get('win_rate', 50.0)
            if wr <= 35.0 and pressure not in ('wall_exhausted', 'wall_breaking'):
                pressure = 'low_edge_' + pressure

        # ── DOM cross-validation (Elite #1) ──
        dom_conf, dom_refill = _dom_cross_validate(symbol, price_str, volume, side)

        # ── Inter-fill timing (Elite #2) ──
        gap_cv_val, timing_conf = _analyze_fill_timing(fills_in_window)

        # Upgrade/downgrade confidence based on timing
        final_confidence = confidence
        if timing_conf == "algo_confirmed" and confidence != "high":
            final_confidence = "high" if confidence == "medium" else "medium"
        elif timing_conf == "random" and confidence == "high":
            final_confidence = "medium"

        # ── Completion countdown state (Elite #3) ──
        if fill_pct < 0.15:
            ice_state = "fresh"
        elif fill_pct < 0.50:
            ice_state = "active"
        elif fill_pct < 0.85:
            ice_state = "depleting"
        else:
            ice_state = "critical"
        depletes_in = round(est_remaining_sec, 1)

        # ── Level memory (Elite #5) ──
        level_mem = _ICE_LEVEL_MEMORY[symbol].get(price_str, {})
        level_ice_count = level_mem.get("count", 0)
        level_avg_size = int(level_mem.get("avg_size", 0))

        # ── Update wall state for gone detection ──
        wall_st = _ICE_WALL_STATE[symbol][price_str]
        wall_st["last_refill_ts"] = timestamp
        wall_st["was_active"] = True
        wall_st["gone_announced"] = False
        wall_st["side"] = fill_sides[0]

        # ── Update level memory ──
        result = {
            # Core detection — all from real L2 data
            "clips": n_fills,
            "est_total": visible_total,
            "avg_clip": round(avg_clip, 1),
            "cv": round(cv, 3),
            "confidence": final_confidence,
            "side": fill_sides[0],
            "size_rank": size_rank,
            # Notional dollar value (real: clips × avg_clip × price × $20 NQ multiplier)
            "notional": round(visible_total * price_f * 20, 0),
            # Zone
            "zone": is_zone,
            "zone_levels": len(zone_levels_hit) if is_zone else 1,
            # Exhaustion
            "decay": decay,
            "slope": slope,
            # Absorption
            "absorbing": absorbing,
            "stickiness": stickiness,
            "opposition_vol": opposition_vol,
            "absorption_ratio": absorption_ratio,
            "psi": psi,  # Dark Pool Absorption Coefficient (Ψ)
            # DOM cross-validation (Elite #1)
            "dom_confirmed": dom_conf,
            "dom_refill": dom_refill,
            # Inter-fill timing (Elite #2)
            "gap_cv": gap_cv_val,
            "timing": timing_conf,
            # Urgency + Pressure (now outcome-weighted)
            "urgency": urgency,
            "pressure": pressure,
            "outcome_modifier": round(outcome_modifier, 2),
            "cross_asset_modifier": round(cross_asset_modifier, 2),
            # Level memory (Elite #5)
            "level_ice_count": level_ice_count,
            "level_avg_size": level_avg_size,
            # Prediction (Elite #6 — now feeds back into urgency)
            "prediction": prediction,
            # Cross-asset context (from EdgeDetector)
            "cross_context": cross_context,
            # Regime + adaptive thresholds
            "regime": _CURRENT_REGIME,
            "adaptive": _rt.get("_adaptive", False),
            "adaptive_cv_threshold": _rt.get("ice_cv"),
            "adaptive_fill_threshold": _rt.get("ice_refill_count"),
            # State-Space Engine: Kalman + Volume Clock
            "kalman_cv": round(_KALMAN_CV[symbol].state, 4),
            "kalman_P": round(_KALMAN_CV[symbol].P, 6),
            "vclock_bucket": _VOLUME_CLOCKS[symbol].bucket_size,
            "vclock_tau": _VOLUME_CLOCKS[symbol].tau,
            # VPIN Toxicity
            "vpin": round(_VPIN_ENGINES[symbol].vpin, 4) if _VPIN_ENGINES and symbol in _VPIN_ENGINES else 0,
            "vpin_regime": _VPIN_ENGINES[symbol].get_regime_modifier() if _VPIN_ENGINES and symbol in _VPIN_ENGINES else 'NEUTRAL',
        }
        _update_level_memory(symbol, price_str, result)

        # Record outcome at detection time — HIGH confidence only (audit: med/unknown = negative EV)
        # + Dynamic directional filter: Volume Clock-scaled (replaces static 5-pt threshold)
        if final_confidence == "high":
            recent_prices = _ICE_PRICE_HISTORY[symbol]
            counter_trend = False
            if len(recent_prices) >= 10:
                prices_30s = [(ts, px) for ts, px in recent_prices if timestamp - ts <= 30]
                if len(prices_30s) >= 5:
                    trend_move = prices_30s[-1][1] - prices_30s[0][1]

                    # Dynamic threshold: scale with Volume Clock stress
                    # When volume spikes (bucket_ratio > 1), threshold TIGHTENS
                    # (less price movement needed to confirm trend)
                    vclock = _VOLUME_CLOCKS[symbol]
                    bucket_ma = sum(vclock._interval_volumes) / max(len(vclock._interval_volumes), 1) \
                                if vclock._interval_volumes else vclock.bucket_size
                    bucket_ratio = vclock.bucket_size / max(bucket_ma, 1)
                    dynamic_threshold = 5.0 / max(bucket_ratio, 0.5)  # 5pt base, tighter when hot

                    if abs(trend_move) > dynamic_threshold:
                        trending_up = trend_move > 0
                        signal_is_long = (side == "b")
                        counter_trend = (trending_up and not signal_is_long) or \
                                        (not trending_up and signal_is_long)

            if not counter_trend:
                # ── Phase 6 Alpha Filters ──
                # Kill Filter: reset — previous combos calibrated on inverted direction.
                # With corrected direction (side="s"→LONG, side="b"→SHORT), all prior
                # combo PnL figures are sign-flipped. Re-derive from corrected OOS data.
                _KILL_COMBOS: set = set()  # cleared pending recalibration
                current_regime = result.get("regime", _CURRENT_REGIME)
                if (current_regime, side) in _KILL_COMBOS:
                    log.debug(f"[ALPHA-FILTER] KILLED {current_regime}+{side} "
                              f"(negative EV combo)")
                    return result

                # CV Gate: low Kalman CV = noise — re-evaluate threshold with corrected data.
                kalman_cv = _KALMAN_CV[symbol].state
                if kalman_cv < 0.04 and _KALMAN_CV[symbol]._n > 250:
                    log.debug(f"[ALPHA-FILTER] CV_GATE blocked (cv={kalman_cv:.4f} < 0.04)")
                    return result

                # Snapshot state vector at detection time for JSONL persistence
                # CRITICAL: These must be captured NOW, not at persist time (T+60s)
                sv = {
                    "psi": result.get("psi", 0),
                    "stickiness": result.get("stickiness", 0),
                    "absorption_ratio": result.get("absorption_ratio", 0),
                    "urgency": result.get("urgency", 0),
                    "regime": result.get("regime", _CURRENT_REGIME),
                    # Snapshot at detection time to avoid T+60s lookahead bias
                    "kalman_cv_at_detect": round(_KALMAN_CV[symbol].state, 4),
                    "kalman_P_at_detect": round(_KALMAN_CV[symbol].P, 6),
                    "vclock_bucket_at_detect": _VOLUME_CLOCKS[symbol].bucket_size,
                }
                _record_iceberg_completion(
                    symbol, price_f, side, timestamp,
                    size_rank,
                    final_confidence,
                    state_vector=sv,
                )

        return result

    return None


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
        tracker.pop(0)

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
        tracker.pop(0)

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
        # ── ICE_TRACKER: remove price keys with no recent fills ──
        ice_sym = _ICE_TRACKER.get(sym, {})
        stale_prices = [p for p, fills in ice_sym.items()
                        if not fills or (now - fills[-1][0]) > _ICE_WINDOWS[-1][0] * 3]
        for p in stale_prices:
            del ice_sym[p]

        # ── SWEEP_TRACKER: hard cap at 100 entries ──
        sweep = _SWEEP_TRACKER.get(sym, [])
        if len(sweep) > 100:
            _SWEEP_TRACKER[sym] = sweep[-50:]

        # ── IGN_TRACKER: hard cap at 200 entries ──
        ign = _IGN_TRACKER.get(sym, [])
        if len(ign) > 200:
            _IGN_TRACKER[sym] = ign[-100:]

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

    # ── DOM_PREV: naturally bounded (replaced each DOM update) ──
    # No cleanup needed.

    # ── DRIFT_TRACKER: prune old fills ──
    for sym in list(_DRIFT_TRACKER.keys()):
        for s in ["b", "s"]:
            tracker = _DRIFT_TRACKER[sym][s]
            while tracker and tracker[0][0] < now - _DRIFT_WINDOW_SEC:
                tracker.popleft()

    # ── WALL_STATE: prune old walls ──
    for sym in list(_ICE_WALL_STATE.keys()):
        stale = [p for p, st in _ICE_WALL_STATE[sym].items()
                 if now - st["last_refill_ts"] > 300]
        for p in stale:
            del _ICE_WALL_STATE[sym][p]

    # ── LEVEL_MEMORY: prune levels older than 4 hours ──
    for sym in list(_ICE_LEVEL_MEMORY.keys()):
        stale = [p for p, m in _ICE_LEVEL_MEMORY[sym].items()
                 if now - m["last_ts"] > 14400]
        for p in stale:
            del _ICE_LEVEL_MEMORY[sym][p]

    # ── PENDING OUTCOMES: prune stale ──
    for sym in list(_ICE_PENDING.keys()):
        _ICE_PENDING[sym] = [p for p in _ICE_PENDING[sym]
                             if now - p["ts"] < 120]

    # ── DOM_BAND_DEPTH: prune old entries ──
    for sym in list(_DOM_BAND_DEPTH.keys()):
        for s in ["b", "s"]:
            dq = _DOM_BAND_DEPTH[sym][s]
            while dq and dq[0][0] < now - _DRIFT_WINDOW_SEC * 2:
                dq.popleft()


# Dedicated lock for candle data — separate from _L2_LOCK to avoid
# contention with DOM/trade state reads during high-volume periods.
_CANDLE_LOCK = threading.Lock()


def _candle_boundary(timestamp: float, seconds: int) -> float:
    """Return the start timestamp of the candle that `timestamp` belongs to."""
    return (int(timestamp) // seconds) * seconds


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
    # Quantize price to symbol-specific tick size for bubble aggregation
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    qp = str(round(round(price / tick_size) * tick_size, 2))

    with _CANDLE_LOCK:
        for tf, seconds in CANDLE_TIMEFRAMES.items():
            boundary = _candle_boundary(timestamp, seconds)
            cur = _CURRENT_CANDLE[symbol].get(tf)

            if cur is None or cur["t"] != boundary:
                # Close previous candle if it exists
                if cur is not None:
                    # ── Cumulative Delta tracking on candle close ──
                    candle_bp = cur.get("bp", {})
                    candle_buy = sum(v[0] for v in candle_bp.values() if isinstance(v, list) and len(v) >= 2)
                    candle_sell = sum(v[1] for v in candle_bp.values() if isinstance(v, list) and len(v) >= 2)
                    candle_delta = candle_buy - candle_sell
                    _CUM_DELTA[symbol][tf] += candle_delta
                    # Record delta history for divergence detection
                    _DELTA_HISTORY[symbol][tf].append({
                        "t": cur["t"],
                        "high": cur["h"],
                        "low": cur["l"],
                        "delta": _CUM_DELTA[symbol][tf],
                    })
                    # Keep only last 50 entries
                    if len(_DELTA_HISTORY[symbol][tf]) > 50:
                        _DELTA_HISTORY[symbol][tf] = _DELTA_HISTORY[symbol][tf][-50:]
                    # Check for divergence
                    div_hit = _detect_delta_divergence(symbol, tf)
                    if div_hit:
                        cur["delta_div"] = div_hit
                    _CANDLES[symbol][tf].append(_freeze_candle(cur))
                # Start new candle with bubble profile
                bp = {}
                bp[qp] = [volume if side == "b" else 0,
                          volume if side == "s" else 0]
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
                    else:
                        bp[qp] = [volume if side == "b" else 0,
                                  volume if side == "s" else 0]

            # ── Emit candle update via Socket.IO (throttled) ──
            if _socketio is not None:
                emit_key = f"{symbol}:{tf}"
                now = time.time()
                last = _last_emit_time.get(emit_key, 0)
                if now - last >= _EMIT_MIN_INTERVAL:
                    _last_emit_time[emit_key] = now
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        candle_data = {
                            "symbol": symbol,
                            "tf": tf,
                            "time": cur["t"],
                            "open": cur["o"],
                            "high": cur["h"],
                            "low": cur["l"],
                            "close": cur["c"],
                            "volume": cur["v"],
                            "bp": cur.get("bp"),
                            "icebergs": cur.get("icebergs"),
                            "sweeps": cur.get("sweeps"),
                            "delta_div": cur.get("delta_div"),
                            "ignition": cur.get("ignition"),
                            "spoofs": cur.get("spoofs"),
                            "drifting_iceberg": cur.get("drifting_iceberg"),
                            "wall_gone": cur.get("wall_gone"),
                            "absorption": cur.get("absorption"),
                        }
                        try:
                            _socketio.emit("candle_update", candle_data, namespace="/")
                        except Exception as e:
                            logger.warning("candle_update emit failed: %s", e)


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
    icebergs = candle.get("icebergs")
    if icebergs:
        snap["icebergs"] = icebergs
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
    drifting_iceberg = candle.get("drifting_iceberg")
    if drifting_iceberg:
        snap["drifting_iceberg"] = drifting_iceberg
    wall_gone = candle.get("wall_gone")
    if wall_gone:
        snap["wall_gone"] = wall_gone
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

_ABSORPTION: dict = defaultdict(dict)  # {sym: {price_key: {...per-level data...}}}
_PREV_DOM_SNAP: dict = {}              # {sym: {bids:{p:sz}, asks:{p:sz}}}
_ABSORPTION_HALF_LIFE = 20.0           # exponential decay half-life in seconds
_WAVE_GAP_SEC = 2.0                    # ≥2s gap = new attack wave


def _track_absorption_trade(symbol, price_key, volume, side, ts):
    """
    Called on every trade. Accumulate side-aware aggressive volume.

    side == 'b' → aggressive buyer LIFTED an ask → attack on ASK wall
    side == 's' → aggressive seller HIT a bid   → attack on BID wall
    """
    tracker = _ABSORPTION[symbol]
    if price_key not in tracker:
        tracker[price_key] = {
            "buy_vol": 0, "sell_vol": 0,
            "hits": 0, "buy_hits": 0, "sell_hits": 0,
            "first_ts": ts, "last_ts": ts,
            "waves": 1, "last_wave_ts": ts,
            "passive_consumed": 0, "peak_passive": 0,
            "side_flag": "ask" if side == "b" else "bid",
        }

    e = tracker[price_key]

    # ── Side-aware volume accumulation ──
    if side == "b":
        e["buy_vol"] += volume
        e["buy_hits"] += 1
        e["side_flag"] = "ask"  # buyers attack ask walls
    elif side == "s":
        e["sell_vol"] += volume
        e["sell_hits"] += 1
        e["side_flag"] = "bid"  # sellers attack bid walls
    else:
        # Neutral / unknown side: split evenly (conservative)
        half = volume // 2 or 1
        e["buy_vol"] += half
        e["sell_vol"] += half

    e["hits"] += 1

    # ── Wave detection: new burst if gap ≥ WAVE_GAP_SEC ──
    if ts - e["last_wave_ts"] >= _WAVE_GAP_SEC:
        e["waves"] += 1
        e["last_wave_ts"] = ts

    e["last_ts"] = ts


def _compute_absorption_scores(symbol, dom):
    """
    Called on every DOM update. Cross-references DOM delta against
    accumulated aggressive volume using 6 institutional metrics:

    1. Side-aware: only counts aggression on the relevant book side
    2. O(1) lookup: direct dict.get() instead of linear scan
    3. Flow-through: tracks total passive consumed, not just net delta
    4. Intensity: hits_per_second for burst detection
    5. Exponential decay: score fades with half-life, not hard cutoff
    6. Wave count: distinct attack bursts = conviction
    """
    now = time.time()
    bids = dom.get("bids", {})
    asks = dom.get("asks", {})
    prev = _PREV_DOM_SNAP.get(symbol, {"bids": {}, "asks": {}})
    prev_bids = prev.get("bids", {})
    prev_asks = prev.get("asks", {})

    tracker = _ABSORPTION[symbol]
    scores = {}
    purge_keys = []

    for price_key, entry in list(tracker.items()):
        age = now - entry["first_ts"]

        # ── Exponential decay: purge if effective weight < 5% ──
        decay_factor = _math.exp(-0.693 * age / _ABSORPTION_HALF_LIFE)  # ln(2)≈0.693
        if decay_factor < 0.05:
            purge_keys.append(price_key)
            continue

        # Need minimum activity to score
        if entry["hits"] < 2:
            continue

        # ── O(1) DOM lookup: direct key match ──
        side_flag = entry["side_flag"]
        if side_flag == "bid":
            curr_size = bids.get(price_key, 0)
            prev_size = prev_bids.get(price_key, 0)
        else:
            curr_size = asks.get(price_key, 0)
            prev_size = prev_asks.get(price_key, 0)

        # Try numeric conversion for keys that might differ in format
        if curr_size == 0 and prev_size == 0:
            # Fallback: try without string mismatch
            pk_float = float(price_key)
            source = bids if side_flag == "bid" else asks
            prev_source = prev_bids if side_flag == "bid" else prev_asks
            for k, v in source.items():
                if abs(float(k) - pk_float) < 0.01:
                    curr_size = v
                    break
            for k, v in prev_source.items():
                if abs(float(k) - pk_float) < 0.01:
                    prev_size = v
                    break

        # ── Flow-through: track CUMULATIVE passive consumption ──
        shrinkage = max(prev_size - curr_size, 0)  # only count shrinkage, not growth
        entry["passive_consumed"] += shrinkage
        if curr_size > entry["peak_passive"]:
            entry["peak_passive"] = curr_size

        # ── Side-aware aggression volume ──
        if side_flag == "bid":
            agg_vol = entry["sell_vol"]  # sellers attacking bid wall
        else:
            agg_vol = entry["buy_vol"]   # buyers attacking ask wall

        # ── Core Score: agg_vol / flow-through consumption ──
        consumed = max(entry["passive_consumed"], 1)
        raw_score = agg_vol / consumed

        # ── Apply exponential decay ──
        effective_score = raw_score * decay_factor

        # ── Intensity: hits per second (burst detection) ──
        duration = max(age, 0.1)
        side_hits = entry["sell_hits"] if side_flag == "bid" else entry["buy_hits"]
        intensity = side_hits / duration

        scores[price_key] = {
            "score": round(effective_score, 2),
            "raw_score": round(raw_score, 2),
            "buy_vol": entry["buy_vol"],
            "sell_vol": entry["sell_vol"],
            "hits": entry["hits"],
            "side_hits": side_hits,
            "passive_consumed": entry["passive_consumed"],
            "peak_passive": entry["peak_passive"],
            "curr_passive": curr_size,
            "intensity": round(intensity, 2),
            "waves": entry["waves"],
            "age": round(age, 1),
            "decay": round(decay_factor, 3),
            "side": side_flag,
        }

    # Cleanup decayed entries
    for k in purge_keys:
        del tracker[k]

    # Store current DOM as previous for next delta
    _PREV_DOM_SNAP[symbol] = {
        "bids": {str(k): v for k, v in bids.items()},
        "asks": {str(k): v for k, v in asks.items()},
    }

    # Publish
    with _L2_LOCK:
        L2_STATE["absorption"][symbol] = scores


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
_HEATMAP_TRADE_BUF: dict = defaultdict(list)  # {symbol: [{p,v,s,t}, ...]}


def _record_dom_snapshot(symbol, dom):
    """
    Called on every DOM update. Records snapshot into T0 ring buffer
    at max ~500ms resolution, and auto-downsamples into T1/T2/T3.
    """
    now = time.time()
    bids = dom.get("bids", {})
    asks = dom.get("asks", {})

    # ── T0: Live (every ~500ms) ──
    last_t0 = _DOM_HIST_LAST_T0.get(symbol, 0)
    if now - last_t0 < _T0_INTERVAL:
        return  # throttle: don't record more than 2/sec
    _DOM_HIST_LAST_T0[symbol] = now

    # Compact snapshot: only store non-zero levels
    snap_bids = {str(k): v for k, v in bids.items() if v > 0}
    snap_asks = {str(k): v for k, v in asks.items() if v > 0}

    # Drain trade buffer: capture all trades since last snapshot
    trades = _HEATMAP_TRADE_BUF.pop(symbol, [])
    # Compact trades: [{p: price, v: volume, s: 'b'/'s'}, ...]
    compact_trades = []
    for t in trades:
        compact_trades.append({"p": t["p"], "v": t["v"], "s": t["s"]})

    # Capture absorption state: compact {price: {s, w, i, h, c, sd}} for active signals
    compact_abs = {}
    with _L2_LOCK:
        abs_data = L2_STATE.get("absorption", {}).get(symbol, {})
        for pk, av in abs_data.items():
            if isinstance(av, dict) and av.get("hits", 0) >= 2:
                compact_abs[pk] = {
                    "s": av.get("score", 0),       # absorption score
                    "w": av.get("waves", 0),        # wave count
                    "i": av.get("intensity", 0),    # intensity
                    "h": av.get("hits", 0),         # hit count
                    "c": av.get("passive_consumed", 0),  # consumed
                    "sh": av.get("side_hits", 0),   # side_hits
                    "rs": av.get("raw_score", 0),   # raw_score
                    "sd": av.get("side", ""),        # side flag
                }

    snap = (now, snap_bids, snap_asks, compact_trades, compact_abs)
    _DOM_HISTORY_T0[symbol].append(snap)

    # ── Push to frontend via WebSocket ──
    if _socketio is not None:
        try:
            _socketio.emit("dom_snapshot", {
                "sym": symbol,
                "ts": now,
                "bids": snap_bids,
                "asks": snap_asks,
                "trades": compact_trades,
                "abs": compact_abs,
            }, namespace="/")
        except Exception:
            pass  # never let emit errors break the DOM engine

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
        # Deep copy to prevent connector thread from mutating nested bids/asks
        import copy as _copy
        L2_STATE["dom"][symbol]        = _copy.deepcopy(dom)
        L2_STATE["imbalance"][symbol]  = imb
        L2_STATE["mid_prices"][symbol] = mid
        L2_STATE["last_update"]        = time.time()

        if _shannon:
            L2_STATE["signals"]["shannon_entropy"] = _shannon.get_signal()
        if _reynolds and mid > 0:
            L2_STATE["signals"]["reynolds_number"] = _reynolds.get_signal()

    # ── DOM snapshot for iceberg cross-validation (Elite #1) ──
    _dom_update_snapshots(symbol, dom)

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


def on_quote(symbol: str, quote: dict):
    """Called by connector when BBO snapshot arrives."""
    mid = quote.get("mid_price", 0.0)
    if mid > 0:
        _PRICE_HISTORY[symbol].append(mid)
    with _L2_LOCK:
        L2_STATE["quotes"][symbol] = quote
        if mid > 0:
            L2_STATE["mid_prices"][symbol] = mid
            L2_STATE["price_history"][symbol] = list(_PRICE_HISTORY[symbol])


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
            from datetime import datetime
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = time.time()
    elif isinstance(ts, (int, float)):
        # If it's a millisecond timestamp (e.g., from TopStepX JS JSON)
        if ts > 20000000000:
            ts = ts / 1000.0
    if price > 0:
        # Track last trade timestamp for reconnect gap-fill
        _LAST_TRADE_TS[symbol] = ts

        # ── Volume Clock: tick on every trade (self-calibrating) ──
        _VOLUME_CLOCKS[symbol].tick(vol, ts)

        # ── VPIN: feed every trade for toxicity tracking ──
        if _VPIN_ENGINES and symbol in ('NQ', '/NQ'):
            _VPIN_ENGINES[symbol].on_trade(symbol, vol, side if side in ('b', 's') else 'n', ts)
        # ── Tick classification ──
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
            dom = L2_STATE["dom"].get(symbol)
            if dom:
                best_ask = dom.get("best_ask", 0)
                best_bid = dom.get("best_bid", 0)
                if best_ask > 0 and price >= best_ask:
                    side = "b"
                elif best_bid > 0 and price <= best_bid:
                    side = "s"

        # ── Orderflow Detection (runs before candle update) ──
        tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
        qp = str(round(round(price / tick_size) * tick_size, 2))

        # ── Absorption Engine: accumulate aggressive volume at this price ──
        try:
            _track_absorption_trade(symbol, qp, vol, side, ts)
        except Exception as e:
            log.debug(f"Absorption trade track error: {e}")

        # Iceberg detection
        ice_hit = _detect_iceberg(symbol, qp, vol, ts, side)
        if ice_hit:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        if "icebergs" not in cur:
                            cur["icebergs"] = {}
                        cur["icebergs"][qp] = ice_hit
            # Forward to EdgeDetector
            if _detection_callback:
                try:
                    _detection_callback('iceberg', ice_hit, symbol)
                except Exception:
                    pass

        # Drifting iceberg detection (Elite #4)
        drift_hit = _detect_drifting_iceberg(symbol, price, vol, ts, side)
        if drift_hit:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        cur["drifting_iceberg"] = drift_hit
            # Forward to EdgeDetector
            if _detection_callback:
                try:
                    _detection_callback('drifting_iceberg', drift_hit, symbol)
                except Exception:
                    pass

        # Wall Gone detection (Elite #3)
        wall_gone_alerts = _check_wall_gone(symbol, ts)
        if wall_gone_alerts:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        if "wall_gone" not in cur:
                            cur["wall_gone"] = []
                        cur["wall_gone"].extend(wall_gone_alerts)
            # Forward each wall_gone alert to EdgeDetector
            if _detection_callback:
                for wg in wall_gone_alerts:
                    try:
                        _detection_callback('wall_gone', wg, symbol)
                    except Exception:
                        pass

        # Sweep detection
        sweep_hit = _detect_sweep(symbol, price, vol, ts, side)
        if sweep_hit:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        if "sweeps" not in cur:
                            cur["sweeps"] = []
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

    # Store trade in L2_STATE
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
            from datetime import datetime
            iso_ts = datetime.utcfromtimestamp(ts).isoformat() + "Z"
            _socketio.emit("trade_tick", {
                "symbol": symbol,
                "price": price,
                "volume": vol,
                "side": side,
                "timestamp": iso_ts,
            }, namespace="/")
        except Exception:
            pass

    # ── Score trade for tape glow (EdgeDetector regime-adaptive percentile) ──
    if _trade_score_callback is not None and price > 0 and vol > 0:
        try:
            _trade_score_callback(symbol, vol, side, price, ts)
        except Exception:
            pass


# ── Heavy framework pre-compute (runs every 60s in background) ────────────────
def _heavy_compute_loop():
    """Run LPPL, PowerLaw, TransferEntropy, Percolation, MutualInfo every 60s.
    Results written into L2_STATE.signals — Flask endpoints just read from there."""
    import time as _time
    while True:
        _time.sleep(60)
        try:
            with _L2_LOCK:
                # Use NQ price history (longest series)
                prices = list(_PRICE_HISTORY.get("NQ", []))

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
                    L2_STATE["signals"].update(results)
                    log.debug("Heavy compute updated: %s", list(results.keys()))

        except Exception as e:
            log.warning("Heavy compute loop error: %s", e)


# ── L2 State WebSocket Push (replaces frontend REST polling) ────────────────
_L2_PUSH_INTERVAL = 0.4  # 400ms — faster than old 500ms REST poll

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
                # Build a LEAN state — only what other events don't cover
                dom_meta = {}
                for k, v in L2_STATE["dom"].items():
                    if isinstance(v, dict):
                        dom_meta[k] = {
                            "bids": dict(v.get("bids", {})) if isinstance(v.get("bids"), dict) else {},
                            "asks": dict(v.get("asks", {})) if isinstance(v.get("asks"), dict) else {},
                            "best_bid": v.get("best_bid", 0),
                            "best_ask": v.get("best_ask", 0),
                            "mid_price": v.get("mid_price", 0),
                            "spread": v.get("spread", 0),
                            "bid_total": v.get("bid_total", 0),
                            "ask_total": v.get("ask_total", 0),
                            "imbalance": v.get("imbalance", 0),
                        }
                state = {
                    "connected":     bool(L2_STATE["connected"]),
                    "dom":           dom_meta,
                    "imbalance":     {k: float(v) for k, v in L2_STATE["imbalance"].items()},
                    "mid_prices":    {k: float(v) for k, v in L2_STATE["mid_prices"].items()},
                    "absorption":    {},  # covered by dom_snapshot
                    "signals":       dict(L2_STATE["signals"]),
                    "last_update":   float(L2_STATE["last_update"]),
                }
                # JSON round-trip inside the lock = thread-safe deep copy
                payload = _json.loads(_json.dumps(state, default=str))
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

    _init_frameworks()

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
        from datetime import datetime, timezone
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
                            from datetime import timezone
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
                    _LAST_TRADE_TS[sym] = max(_LAST_TRADE_TS.get(sym, 0), ts)
                log.info("Gap-fill: %s ✓ inserted %d bars (of %d fetched)",
                         sym, inserted, len(bars))
                # Push updated candles to frontend
                if _socketio is not None:
                    try:
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

        # Start heavy-framework background loop (daemon — dies with main thread)
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
                from datetime import datetime, timedelta, timezone
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

                for sym in SYMBOLS:
                    cid = _connector._symbol_to_contract.get(sym)
                    if not cid:
                        log.warning("L2 backfill: no contract ID for %s — skipping", sym)
                        continue

                    # Try current session first, then go back up to 5 days
                    # (handles holidays where retrieve_bars returns empty)
                    bars = []
                    for day_offset in range(6):  # 0..5 days back
                        try_open = session_open - timedelta(days=day_offset)
                        # Skip weekends in the retry loop too
                        if try_open.weekday() in (5, 6):  # Sat or Sun
                            log.debug("L2 backfill: %s skipping %s (%s) — weekend",
                                      sym, try_open.strftime('%Y-%m-%d'), try_open.strftime('%A'))
                            continue
                        try_utc = try_open.astimezone(timezone.utc).isoformat()
                        log.info("L2 backfill: %s trying session %s NY (%s) → %s UTC (offset=%d)",
                                 sym, try_open.strftime('%Y-%m-%d %H:%M'),
                                 try_open.strftime('%A'), try_utc, day_offset)
                        bars = _connector.retrieve_bars(
                            cid, start_time=try_utc,
                            unit=2, unit_number=1, limit=20000
                        )
                        if bars:
                            log.info("L2 backfill: %s ✓ got %d bars from %s (offset=%d)",
                                     sym, len(bars), try_open.strftime('%Y-%m-%d'), day_offset)
                            break
                        else:
                            log.info("L2 backfill: %s ✗ 0 bars from %s (offset=%d)",
                                     sym, try_open.strftime('%Y-%m-%d'), day_offset)
                    if not bars:
                        log.warning("L2 backfill: no bars for %s after trying 6 sessions — chart will be empty", sym)
                        continue

                    # Seed price history
                    for bar in bars:
                        close = float(bar.get("c", 0))
                        if close > 0:
                            _PRICE_HISTORY[sym].append(close)
                    with _L2_LOCK:
                        L2_STATE["price_history"][sym] = list(_PRICE_HISTORY[sym])

                    # Seed candle engine — insert bars as 1m candles
                    from datetime import datetime as dt
                    for bar in bars:
                        ts_str = bar.get("t", "")
                        try:
                            dt_obj = dt.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if dt_obj.tzinfo is None:
                                from datetime import timezone
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
                                _CURRENT_CANDLE[sym][tf] = None

                    candle_count = sum(len(_CANDLES[sym][tf]) for tf in CANDLE_TIMEFRAMES)
                    log.info("L2 backfill: %s seeded %d bars → %d total candles across all TFs",
                             sym, len(bars), candle_count)

                log.info("L2 backfill: complete for all symbols")
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
