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

# Regime-adaptive est_duration: shorter in crashes (fast fills),
# longer in pin markets (slow drip). Affects est_hidden accuracy.
_EST_DURATION_BY_REGIME = {
    "crash_tail_risk":      30.0,
    "short_gamma_volatile": 45.0,
    "transition":           60.0,
    "long_gamma_stable":    90.0,
    "pin_mean_revert":     120.0,
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

    # Fill rates (60s window) — reverse-scan with early break (deques are time-ordered)
    cutoff_60 = timestamp - 60.0
    b_recent = []
    for t in reversed(ms["fill_timestamps_b"]):
        if t < cutoff_60:
            break
        b_recent.append(t)
    s_recent = []
    for t in reversed(ms["fill_timestamps_s"]):
        if t < cutoff_60:
            break
        s_recent.append(t)
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

# BUG A FIX: Cooldown guard — prevents the same drifting iceberg from firing
# 9+ times from identical ring buffer data on consecutive trades.
# One real drifting iceberg event = one signal per side per 30 seconds.
_DRIFT_LAST_EMIT: dict = {}  # {(symbol, side): last_emit_timestamp}
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

# ── Seller Exhaustion Memory ──
# When a SHORT iceberg (side=b, ask wall) resolves with NEGATIVE outcome
# (sellers tried to drive price down but it DIDN'T go down), that is a
# seller exhaustion event. Store it so Long entries can require confirmation.
# Data proof: Long WITH prior failed short = 69% WR vs 37% WR without (+32pp).
# Structure: {symbol: deque of {ts, price} dicts, maxlen=20}
_SELLER_EXHAUSTION: dict = defaultdict(lambda: deque(maxlen=20))

# ── Buyer Exhaustion Memory ──
# When a LONG iceberg (side=s, bid wall) resolves with NEGATIVE outcome
# (buyers tried to hold a bid wall, price broke through them), that is a
# buyer exhaustion event. Store it so Short entries can require confirmation.
# Data proof: Short WITH prior failed long = 73% WR vs 51% WR without (+22pp).
_BUYER_EXHAUSTION: dict = defaultdict(lambda: deque(maxlen=20))

# ── Volatile CV Tier constants ──
# Sweep of 1,957 exhaustion events: inflection at CV=0.12.
# Below 0.12 = calm tape, WR=70%, mean=+4pt (solid baseline).
# 0.12-0.25  = volatile tape, WR=80%, mean=+8pt (target tier).
# Above 0.25 = shock events, WR=82% but only 90 trades in 5 days — too sparse.
# Use volatile tier as quality filter on exhaustion events.
_EX_CV_VOLATILE_LO: float = 0.12
_EX_CV_VOLATILE_HI: float = 0.25

# ── Level Failure Memory (Double-Rejection) ──
# Tracks how many times the OPPOSING side has failed at a price level recently.
# Data proof (4,161 outcome events, deduped, sequential):
#   No prior fail             = 39% WR, mean -2.21 (base noise)
#   1 prior fail within 3min  = 55% WR, mean +1.53 (+16pp lift)
#   2+ prior fails within 3min= 62% WR, mean +3.02 (+23pp lift)
# Combined with volatile CV: 67% WR, mean +2.61 on 451 clean trades.
# Structure: {(symbol, price_bucket, side): deque of failure timestamps}
# price_bucket = round(price/2.5)*2.5 (10-tick grid for NQ, ~2.5pt for ES)
_LEVEL_FAIL_MEMORY: dict = defaultdict(lambda: deque(maxlen=20))
_LEVEL_FAIL_WINDOW: float = 180.0  # 3-minute failure window

# ── Cross-Symbol Co-Exhaustion (Feature 11) ──
# When NQ AND ES fail the same directional iceberg within 30s,
# the signal spans both contracts — highest institutional conviction.
# Key: side ('b' or 's'); value: deque of {ts, symbol, price}
_CROSS_SYM_EXHAUSTION: dict = defaultdict(lambda: deque(maxlen=50))
_CROSS_SYM_WINDOW: float = 30.0
_CROSS_SYM_SYMBOLS: set = {'NQ', 'ES', 'GC'}

# ── Live Ψ Pre-Detection State (Feature 12) ──
# Stores the most recently computed absorption coefficient per symbol.
# Used by the pre-detection Ψ filter in _record_iceberg_completion.
# Updated every time an iceberg fires: {symbol: {ts, psi, side}}
_LIVE_PSI: dict = {}

# ── Footprint / Cumulative Delta Engine ──
# Tracks cumulative buy/sell volume at each exact price level within the
# current 1-minute candle. This is the institutional absorption fingerprint.
#
# True Absorption fingerprint:
#   sell_vol >> buy_vol at a level (net sellers)  -> delta is VERY negative
#   BUT the bid price stays anchored at that level -> hidden buyer eating every seller
#   Result: price reverses 40+ points
#
# False Absorption / Falling Knife:
#   sell_vol >> buy_vol at a level (net sellers)
#   AND the bid moves DOWN after each fill -> no buyer, thin book
#   Result: price collapses through the level
#
# Structure: {symbol: {
#     'candle_ts': int,          # current 1-min candle start timestamp
#     'levels': {price: {
#         'buy_vol': int,         # aggressive buy volume at this price
#         'sell_vol': int,        # aggressive sell volume at this price
#         'bid_anchored': int,    # times bid STAYED at this price after a sell hit
#         'bid_dropped': int,     # times bid DROPPED after a sell hit (falling knife)
#         'n_trades': int,
#     }}
# }}
_FOOTPRINT: dict = defaultdict(lambda: {
    'candle_ts': 0,
    'levels': defaultdict(lambda: {
        'buy_vol':      0,
        'sell_vol':     0,
        'bid_anchored': 0,  # sell hit bid, bid stayed (buyer wall holding)
        'bid_dropped':  0,  # sell hit bid, bid moved away (falling knife)
        'ask_anchored': 0,  # buy lifted ask, ask stayed (seller wall holding)
        'ask_dropped':  0,  # buy lifted ask, ask moved away (exhausted ask wall)
        'n_trades':     0,
    })
})
_FOOTPRINT_CANDLE_SEC: int = 60  # 1-minute candle footprint
_FOOTPRINT_ZONE_TICKS: int = 4   # check ±4 ticks around iceberg price for absorption zone
# {symbol: list of pending outcome checks}
_ICE_PENDING: dict = defaultdict(list)

# BUG D FIX: Dedup guard for multi-timeframe candle loop.
# _record_iceberg_completion is called once per candle TF (30s/60s/300s/etc)
# for the same ice_hit — creating 3x duplicate pending outcomes at same price/ts.
_ICE_DEDUP_SEEN: dict = {}   # {(symbol, round(price,2), side, round(ts,1)): emit_ts}
_ICE_DEDUP_TTL  = 60.0         # purge keys older than 60s

# BUG B FIX: Kill combo filter. Was declared as a local variable inside
# _record_iceberg_completion() and _detect_iceberg(), resetting to empty set on
# every call — making the filter permanently dead. Now module-level so it persists.
# Re-populate after 10+ sessions of corrected-direction OOS data.
_KILL_COMBOS: set = set()

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
_SWEEP_TRACKER: dict = defaultdict(deque)
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
    """Inter-fill timing analysis (IAT Variance Test).
    Tests whether a sequence of fills exhibits the statistical
    signature of algorithmic execution:
    1. Low/Moderate CV on inter-arrival times (pacing, not uniform).
    2. Autocorrelation near zero (not a simple timer loop).
    Returns (iat_confidence_float, timing_label_str).
    """
    if len(fills_in_window) < 3:
        return (0.0, "insufficient")
    timestamps = sorted([f[0] for f in fills_in_window])
    gaps = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    gaps = [g for g in gaps if g > 0]
    
    if not gaps:
        return (0.0, "instant")
        
    n_gaps = len(gaps)
    mean_gap = sum(gaps) / n_gaps
    if mean_gap == 0:
        return (0.0, "instant")
        
    variance = sum((g - mean_gap) ** 2 for g in gaps) / n_gaps
    gap_cv = (variance ** 0.5) / mean_gap
    
    # Lag-1 Autocorrelation (do consecutive gaps correlate?)
    autocorr = 0.0
    if n_gaps >= 3 and variance > 0:
        num = sum((gaps[i] - mean_gap) * (gaps[i+1] - mean_gap) for i in range(n_gaps - 1))
        # strictly should trace variances but this is a fast approximation
        autocorr = num / (variance * (n_gaps - 1))
        
    # Classification:
    # Naive algo (loop): CV near 0, autocorr near 0/undefined
    # Modern algo (TWAP randomized): CV 0.3 - 0.8, autocorr near 0 (independent draws)
    # Retail / Panic: CV > 1.0 (bursty), autocorr positive (clustering)
    abs_ac = abs(autocorr)
    
    if gap_cv < 0.2:
        return (0.4, "naive_loop")  # Too perfect, easily gamable bot
    elif 0.2 <= gap_cv <= 0.8 and abs_ac < 0.4:
        return (0.9, "algo_confirmed")  # Randomized pacing, independent
    elif gap_cv < 1.2:
        return (0.6, "algo_likely")
    else:
        return (0.2, "random")


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
    iat_confidence, timing = _analyze_fill_timing([(t, 0, "") for t, _, _ in recent])

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

    # BUG A FIX: Cooldown guard — one drifting iceberg = one signal per 30s per (symbol, side).
    # Without this, every consecutive trade re-checks the same ring buffer data and
    # re-fires the detection (confirmed: 9+ identical vol=103 signals in server.log).
    _cooldown_sec = 30.0
    _emit_key = (symbol, side)
    _last_emit = _DRIFT_LAST_EMIT.get(_emit_key, 0)
    if timestamp - _last_emit < _cooldown_sec:
        return None  # suppress duplicate — same ring buffer, same signal
    _DRIFT_LAST_EMIT[_emit_key] = timestamp

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
        "iat_confidence": iat_confidence,
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
    # BUG D FIX: Dedup guard — multi-TF candle loop calls this once per timeframe
    # for the SAME detection, producing 3 identical pending outcomes.
    _now = time.time()
    _dedup_key = (symbol, round(price, 2), side, round(ts, 1))
    if _dedup_key in _ICE_DEDUP_SEEN:
        return  # already registered this detection
    _ICE_DEDUP_SEEN[_dedup_key] = _now
    # ── Phase 6 Alpha Filters (universal choke point) ──
    # NOTE: Kill combos were reset after discovering direction inversion bug.
    # Old combos were calibrated on inverted PnL — they were killing WINNING signals.
    # With correct direction semantics:
    #   side="s" = bid iceberg → LONG  (buyer wall absorbing sellers)
    #   side="b" = ask iceberg → SHORT (seller wall absorbing buyers)
    # Fresh kill combos will be re-derived from corrected OOS data.
    # BUG B FIX: _KILL_COMBOS is now module-level (not re-declared here as local).
    # Previous local `_KILL_COMBOS: set = set()` reset to empty on every call.
    current_regime = _CURRENT_REGIME
    if state_vector:
        current_regime = state_vector.get('regime', _CURRENT_REGIME)
    if (current_regime, side) in _KILL_COMBOS:
        return  # suppress toxic combo entirely

    # ── Seller Exhaustion Gate for LONG signals ──
    # Data: Long WITH prior failed short nearby = 69% WR vs 37% WR without (+32pp).
    # Require at least one seller exhaustion event in the past 120s within 5pts
    # of the current entry price before allowing a LONG to fire.
    # This filters out "catching falling knives" — only Longs after sellers FAIL.
    #
    # Exception: skip this gate if we have no exhaustion history yet (< 10 resolved
    # outcomes in memory) to avoid blocking valid signals during session warmup.
    _LONG_ALLOWED_REGIMES  = {"short_gamma_volatile", "crash_tail_risk", "transition"}
    _SHORT_ALLOWED_REGIMES = {"long_gamma_stable", "pin_mean_revert", "transition"}
    _exhaustion_window     = 120.0  # seconds
    _exhaustion_band       = 5.0    # pts price proximity
    _warmed                = len(_ICE_OUTCOMES.get(symbol, [])) >= 10

    def _check_exhaustion_gate(direction: str) -> bool:
        """Return False (block) if exhaustion gate not satisfied. direction='long' or 'short'."""
        is_long       = direction == "long"
        allowed       = _LONG_ALLOWED_REGIMES if is_long else _SHORT_ALLOWED_REGIMES
        label         = "LONG" if is_long else "SHORT"
        ex_dict       = _SELLER_EXHAUSTION if is_long else _BUYER_EXHAUSTION
        co_side_key   = "b" if is_long else "s"
        opp_label     = "seller" if is_long else "buyer"

        if current_regime not in allowed:
            log.debug(f"[REGIME-GATE] BLOCKED {label} in regime={current_regime}")
            return False
        if not _warmed:
            return True
        recent = ex_dict.get(symbol)
        if not recent:
            log.debug(f"[EXHAUSTION-GATE] BLOCKED {label} @ {price:.2f} — no {opp_label} exhaustion memory")
            return False
        volatile = any(
            ts - e["ts"] <= _exhaustion_window
            and abs(price - e["price"]) <= _exhaustion_band
            and _EX_CV_VOLATILE_LO <= e.get("cv", 0) <= _EX_CV_VOLATILE_HI
            for e in recent
        )
        any_ex = volatile or any(
            ts - e["ts"] <= _exhaustion_window
            and abs(price - e["price"]) <= _exhaustion_band
            for e in recent
        )
        if not any_ex:
            log.debug(f"[EXHAUSTION-GATE] BLOCKED {label} @ {price:.2f} — no {opp_label} exhaustion")
            return False
        if not volatile:
            log.debug(f"[CV-PREFER] {label} @ {price:.2f} — calm-tape exhaustion (weaker)")
        # Cross-symbol co-exhaustion boost (Feature 11 — log only, no hard gate yet)
        co = [e for e in _CROSS_SYM_EXHAUSTION.get(co_side_key, [])
              if e["symbol"] != symbol and ts - e["ts"] <= _CROSS_SYM_WINDOW]
        if co:
            log.info(f"[CO-EXHAUST] {label} confirmed by {co[-1]['symbol']} {opp_label} exhaustion")
        return True

    if side == "s" and not _check_exhaustion_gate("long"):
        return
    elif side == "b" and not _check_exhaustion_gate("short"):
        return

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
                # Note: safe to mutate — caller creates a fresh dict each time,
                # and pending.update(dict(state_vector)) copies at line 1478.
                if state_vector is not None:
                    state_vector['mm_bias_at_detect'] = mm_bias
                    state_vector['gex_zone_at_detect'] = gex_zone
                    state_vector['gex_factor_at_detect'] = gex_factor
        except Exception as e:
            log.debug(f"[DIRECTIONAL-GATE] Cross-asset check error: {e}")
            # Don't block if cross-asset data is unavailable

    # FIX 9: Capture mid-price at detection time for realized spread computation.
    # realized_spread = |outcome_price - mid_at_detect| = actual MM edge realized
    # BUG 4 FIX: Was reading L2_STATE["dom"][symbol] without _L2_LOCK (race condition).
    # mid_prices is a scalar float updated atomically under the lock — safe to read directly.
    _mid_detect = L2_STATE["mid_prices"].get(symbol, price)

    # ── Double-Rejection: count prior failures of opposing side at this level ──
    # Opposing side key: the side that WOULD FAIL to produce this signal
    #   LONG signal (side=s) → prior seller failures (side=b) at same level
    #   SHORT signal (side=b) → prior buyer failures (side=s) at same level
    _opp_side      = "b" if side == "s" else "s"
    _px_b          = round(price / 2.5) * 2.5
    _fail_key_opp  = (symbol, _px_b, _opp_side)
    _prior_fails   = [t for t in _LEVEL_FAIL_MEMORY.get(_fail_key_opp, [])
                      if ts - t <= _LEVEL_FAIL_WINDOW]
    _n_prior_fails = len(_prior_fails)

    # Determine signal quality tier
    _cur_cv = _KALMAN_CV[symbol].state if symbol in _KALMAN_CV else 0.0
    _is_volatile_cv = _EX_CV_VOLATILE_LO <= _cur_cv <= _EX_CV_VOLATILE_HI
    _true_abs = state_vector.get("true_absorption", False) if state_vector else False
    if _true_abs:
        # True absorption confirmed by footprint: bid held under massive sell pressure.
        # This is the strongest structural signal we can produce. Always HQ.
        _sig_tier = "HQ"   # Footprint-confirmed: bid held against negative delta
    elif _n_prior_fails >= 2 and _is_volatile_cv:
        _sig_tier = "HQ"   # 67% WR, +2.61 mean — double rejection in volatile tape
    elif _n_prior_fails >= 1 or _is_volatile_cv:
        _sig_tier = "MQ"   # 55-62% WR — one condition met
    else:
        _sig_tier = "LQ"   # 39-50% WR — baseline, no prior failure context

    pending = {
        "side": side, "price": price, "ts": ts,
        "size_rank": size_rank, "confidence": confidence,
        "n_prior_fails": _n_prior_fails,   # opposing-side failure count at this level
        "signal_tier": _sig_tier,          # HQ / MQ / LQ
        "check_10s": ts + 10, "check_30s": ts + 30, "check_60s": ts + 60,
        "outcome_10s": None, "outcome_30s": None, "outcome_60s": None,
        # Intra-window MAE/MFE tracking
        "mfe": 0.0, "mae": 0.0,
        "mfe_10s": None, "mae_10s": None,
        "mfe_30s": None, "mae_30s": None,
        "mfe_60s": None, "mae_60s": None,
        # Realized spread
        "mid_at_detect": round(_mid_detect, 4),
        "realized_spread_10s": None,
        "realized_spread_30s": None,
        "realized_spread_60s": None,
    }
    if state_vector:
        pending.update(dict(state_vector))  # copy to prevent mutation leak
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

        # ── Dynamic SL: cv-adaptive, floored at 2pts ──
        # Computed ONCE at entry. cv*100 = Kalman volatility estimate in pts.
        # floor at 2.0 so fast scalp markets don't get stopped out by noise.
        if "dynamic_sl" not in p:
            cv = _KALMAN_CV[symbol].state if symbol in _KALMAN_CV else 0.05
            p["dynamic_sl"] = round(max(2.0, cv * 100), 2)
            p["dynamic_sl_hit"] = False
            p["dynamic_sl_hit_ts"] = None
            p["dynamic_sl_pnl"] = None

        if not p["dynamic_sl_hit"] and pnl <= -p["dynamic_sl"]:
            p["dynamic_sl_hit"] = True
            p["dynamic_sl_hit_ts"] = current_ts
            p["dynamic_sl_pnl"] = round(-p["dynamic_sl"], 2)

        # ── BUG C FIX: If SL was hit, cap realized PnL at stop level ──
        # Previously dynamic_sl_hit was RECORDED but never applied — pnl kept
        # bleeding to -10pts over 60s even after stop was triggered.
        # Data shows 80% of LONG losers hit -2pts MAE; mean loser bleeds to -7.88.
        # Cap at the stop so outcome windows reflect actual realized loss.
        if p["dynamic_sl_hit"]:
            pnl = min(pnl, p["dynamic_sl_pnl"])  # can't be worse than stop
        # Emergency floor: if dynamic_sl somehow missed initialization, -4pt hard stop
        pnl = max(pnl, -4.0)

        if p["outcome_10s"] is None and current_ts >= p["check_10s"]:
            p["outcome_10s"] = pnl
            p["mfe_10s"] = p["mfe"]
            p["mae_10s"] = p["mae"]
            if p.get("mid_at_detect") is not None:
                p["realized_spread_10s"] = round(abs(current_price - p["mid_at_detect"]), 4)

            # ── Exhaustion memory: write at 10s resolution, not 60s ──
            # OOS backtest (deduped, no look-ahead, 30% holdout):
            #   60s exhaustion → only 27 allowed trades, barely enough signal
            #   10s exhaustion → 368 allowed trades, 62% WR, +713 pts on OOS set
            # Threshold -0.5pt at 10s is sufficient — if price didn't move half a pt
            # in the direction the opposing iceberg predicted within 10 seconds,
            # those traders are already exhausted. No need to wait 60s.
            _EX_THRESH = -0.5
            _ex_cv = _KALMAN_CV[symbol].state if symbol in _KALMAN_CV else 0.0
            if p["side"] == "b" and pnl < _EX_THRESH:
                _SELLER_EXHAUSTION[symbol].append({"ts": current_ts, "price": p["price"], "cv": _ex_cv})
                log.debug(f"[EXHAUSTION-10s] Seller @ {p['price']:.2f} cv={_ex_cv:.3f}")
                # FEATURE 11: Cross-symbol co-exhaustion write
                if symbol in _CROSS_SYM_SYMBOLS:
                    _CROSS_SYM_EXHAUSTION["b"].append({"ts": current_ts, "symbol": symbol, "price": p["price"]})
            elif p["side"] == "s" and pnl < _EX_THRESH:
                _BUYER_EXHAUSTION[symbol].append({"ts": current_ts, "price": p["price"], "cv": _ex_cv})
                log.debug(f"[EXHAUSTION-10s] Buyer  @ {p['price']:.2f} cv={_ex_cv:.3f}")
                # FEATURE 11: Cross-symbol co-exhaustion write
                if symbol in _CROSS_SYM_SYMBOLS:
                    _CROSS_SYM_EXHAUSTION["s"].append({"ts": current_ts, "symbol": symbol, "price": p["price"]})

            # ── Level Failure Memory: track failures for double-rejection signal ──
            if pnl < _EX_THRESH:
                _px_bucket = round(p["price"] / 2.5) * 2.5
                _fail_key  = (symbol, _px_bucket, p["side"])
                _LEVEL_FAIL_MEMORY[_fail_key].append(current_ts)
                # Purge entries older than the window
                while _LEVEL_FAIL_MEMORY[_fail_key] and \
                      current_ts - _LEVEL_FAIL_MEMORY[_fail_key][0] > _LEVEL_FAIL_WINDOW:
                    _LEVEL_FAIL_MEMORY[_fail_key].popleft()

        if p["outcome_30s"] is None and current_ts >= p["check_30s"]:
            p["outcome_30s"] = pnl
            p["mfe_30s"] = p["mfe"]
            p["mae_30s"] = p["mae"]
            if p.get("mid_at_detect") is not None:
                p["realized_spread_30s"] = round(abs(current_price - p["mid_at_detect"]), 4)

        if p["outcome_60s"] is None and current_ts >= p["check_60s"]:
            p["outcome_60s"] = pnl
            p["mfe_60s"] = p["mfe"]
            p["mae_60s"] = p["mae"]
            if p.get("mid_at_detect") is not None:
                p["realized_spread_60s"] = round(abs(current_price - p["mid_at_detect"]), 4)
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
            # Footprint / True Absorption (Feature #2 from MM gap analysis)
            "fp_delta":            outcome.get("fp_delta", 0),
            "fp_total":            outcome.get("fp_total", 0),
            "fp_absorption_score": outcome.get("fp_absorption_score", 0.0),
            "true_absorption":     outcome.get("true_absorption", False),
            "fp_anchor_ratio":     outcome.get("fp_anchor_ratio", 0.0),
            # Signal quality tier (HQ/MQ/LQ) and double-rejection count
            "signal_tier":   outcome.get("signal_tier", "LQ"),
            "n_prior_fails": outcome.get("n_prior_fails", 0),
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
        window_cutoff = timestamp - window_sec  # always defined (used by absorption/psi below)
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
        # Regime-adaptive est_duration uses module-level _EST_DURATION_BY_REGIME
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
            # BUG #5 FIX: single definition for 'absorbing' — price range only.
            # Previously a broken stickiness calculation could override this,
            # marking falling knives as 'absorbing'. Now only the price-range
            # criterion determines whether price is holding (absorbing) or not.
            if price_range / tick_size <= _ICE_ABSORB_MAX_MOVE:
                absorbing = True

            # BUG #2 FIX: Stickiness denominator — count actual CONTRACTS, not price ticks.
            # Old code: sum(1 for t,_ in _ICE_PRICE_HISTORY) → counts price events, not vol.
            # Fix: sum all fills from _ICE_TRACKER across all prices in window.
            total_window_vol = sum(
                v for _, v, _ in (
                    f for f in _ICE_TRACKER[symbol].get(price_str, [])
                    if f[0] >= window_cutoff
                )
            )
            # Total market volume in window = sum of ALL fills across ALL prices
            total_mkt_vol = sum(
                _mkt_v
                for _px_fills in _ICE_TRACKER[symbol].values()
                for _mkt_ts, _mkt_v, _ in _px_fills
                if _mkt_ts >= window_cutoff
            )
            # Stickiness = fraction of candle volume at THIS level (0.0 → 1.0)
            if total_mkt_vol > 0:
                stickiness = round(visible_total / max(total_mkt_vol, 1), 3)
            # Record distribution for empirical P75 reporting (log only, not signal)
            _STICKINESS_DIST[symbol].append(stickiness)
            stick_vals = list(_STICKINESS_DIST[symbol])
            if len(stick_vals) >= 30:
                stick_vals_sorted = sorted(stick_vals)
                p75_idx = int(len(stick_vals_sorted) * 0.75)
                stickiness_threshold = stick_vals_sorted[p75_idx]
            else:
                stickiness_threshold = 0.3
            # Note: stickiness is now purely diagnostic — it does NOT override
            # 'absorbing'. Use it for correlation analysis vs 40pt reversal targets.


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

        # ── FOOTPRINT: True Absorption vs Falling Knife ──
        # Scan ±_FOOTPRINT_ZONE_TICKS around the iceberg price for:
        #
        # BID iceberg (side='s' — sellers hitting, buyers holding):
        #   True Absorption: heavy sell vol + bid held (bid_anchored >> bid_dropped)
        #   Falling Knife:   heavy sell vol + bid moving away (bid_dropped >> bid_anchored)
        #
        # ASK iceberg (side='b' — buyers lifting, sellers holding):
        #   True Absorption: heavy buy vol + ask held (ask_anchored >> ask_dropped)
        #   Exhausted Wall:  heavy buy vol + ask moving up (ask_dropped >> ask_anchored)
        fp_zone_buy   = 0
        fp_zone_sell  = 0
        fp_bid_anchor = 0
        fp_bid_drop   = 0
        fp_ask_anchor = 0
        fp_ask_drop   = 0
        _fp = _FOOTPRINT.get(symbol)
        if _fp and _fp['levels']:
            for _t in range(-_FOOTPRINT_ZONE_TICKS, _FOOTPRINT_ZONE_TICKS + 1):
                _scan_px = round(price_f + _t * tick_size, 2)
                _lvl = _fp['levels'].get(_scan_px)
                if _lvl:
                    fp_zone_buy   += _lvl['buy_vol']
                    fp_zone_sell  += _lvl['sell_vol']
                    fp_bid_anchor += _lvl['bid_anchored']
                    fp_bid_drop   += _lvl['bid_dropped']
                    fp_ask_anchor += _lvl['ask_anchored']
                    fp_ask_drop   += _lvl['ask_dropped']

        fp_delta = fp_zone_buy - fp_zone_sell  # negative = net sellers
        fp_total = fp_zone_buy + fp_zone_sell

        # Anchor ratios: separate bid vs ask anchoring (correct per side)
        fp_bid_anchor_ratio = fp_bid_anchor / max(fp_bid_anchor + fp_bid_drop, 1)
        fp_ask_anchor_ratio = fp_ask_anchor / max(fp_ask_anchor + fp_ask_drop, 1)
        fp_sell_dominance   = fp_zone_sell / max(fp_total, 1)
        fp_buy_dominance    = fp_zone_buy  / max(fp_total, 1)

        # Canonical anchor_ratio for log/display (side-appropriate)
        if fill_sides[0] == 'b':  # Ask iceberg: ask anchoring matters
            fp_anchor_ratio = fp_ask_anchor_ratio
        else:                     # Bid iceberg: bid anchoring matters
            fp_anchor_ratio = fp_bid_anchor_ratio

        if fill_sides[0] == 'b':  # Ask iceberg — buyers lifting, ask wall holds
            # True absorption: buyers aggressively lifting, but ask stays anchored.
            # Score = (fraction of vol that is buys) × (fraction of times ask held)
            fp_absorption_score = fp_buy_dominance * fp_ask_anchor_ratio
        else:  # Bid iceberg (side='s') — sellers hitting, bid wall holds
            # True absorption: sellers hitting bid, but bid stays anchored.
            fp_absorption_score = fp_sell_dominance * fp_bid_anchor_ratio

        fp_absorption_score = round(fp_absorption_score, 3)

        # Hard threshold for 'true absorption' classification:
        # - BID iceberg (side='s'): sellers hitting bid, bid holds → long setup
        # - ASK iceberg (side='b'): buyers lifting ask, ask holds → short setup
        # Each uses its own side-appropriate anchor ratio (not crossed data).
        true_absorption = (
            fp_total >= 20
            and fp_sell_dominance >= 0.6      # sellers clearly dominating
            and fp_bid_anchor_ratio >= 0.7    # bid holds 70%+ of the time
            and fill_sides[0] == 's'          # bid iceberg → we look for buyer wall
        ) or (
            fp_total >= 20
            and fp_buy_dominance >= 0.6       # buyers clearly dominating
            and fp_ask_anchor_ratio >= 0.7    # ask holds 70%+ of the time
            and fill_sides[0] == 'b'          # ask iceberg → we look for seller wall
        )

        # ── BUG E FIX: Use session baseline for trade sizes ──
        # Previously used `trade_hist` which only contained the 5-15 fills from THIS
        # single iceberg detection. sigma was always ~0, making everything "retail".
        # Now uses the rolling 100 recent clip sizes from _DRIFT_TRACKER for baseline.
        recent_fills = [v for _, _, v in _DRIFT_TRACKER[symbol][side]]
        size_rank = "retail"
        if len(recent_fills) > 20:
            th_mean = sum(recent_fills) / len(recent_fills)
            th_var = sum((v - th_mean) ** 2 for v in recent_fills) / len(recent_fills)
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
        iat_confidence, timing_conf = _analyze_fill_timing(fills_in_window)

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
            "iat_confidence": iat_confidence,
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
            # Footprint / True Absorption
            "fp_delta":           fp_delta,            # cumulative delta in zone (neg=net sellers)
            "fp_total":           fp_total,            # total volume in zone this candle
            "fp_absorption_score": fp_absorption_score, # 0.0-1.0 (1.0 = perfect absorption)
            "true_absorption":    true_absorption,     # bool: structural absorption confirmed
            "fp_anchor_ratio":    round(fp_anchor_ratio, 3),   # fraction of bids that held
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
                        signal_is_long = (side == "s")  # side="s"→LONG (bid iceberg), side="b"→SHORT
                        counter_trend = (trending_up and not signal_is_long) or \
                                        (not trending_up and signal_is_long)

            if not counter_trend:
                # ── Phase 6 Alpha Filters ──
                # Kill Filter: reset — previous combos calibrated on inverted direction.
                # With corrected direction (side="s"→LONG, side="b"→SHORT), all prior
                # combo PnL figures are sign-flipped. Re-derive from corrected OOS data.
                # BUG B FIX: _KILL_COMBOS is now module-level (not re-declared here as local).
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

                # ── FEATURE 10: Clip Size Decay Detector ──
                # Linear regression slope already computed above (slope, decay).
                # slope < -0.25 = active ammo depletion while iceberg is STILL LIVE.
                clip_decay_active = (decay == "exhausting" and slope < -0.25)
                if clip_decay_active:
                    log.debug(f"[CLIP-DECAY] {side} @ {price_f:.2f} slope={slope:.3f}")

                # ── FEATURE 12: Pre-detection Ψ filter ──
                # SHORT (side=b) into Ψ > 1.5 = buyers absorbing = risky short
                # LONG  (side=s) into Ψ < 0.5 = sellers absorbing = risky long
                _lp           = _LIVE_PSI.get(symbol, {})
                _live_psi_val = _lp.get("psi", 1.0)
                _psi_stale    = (timestamp - _lp.get("ts", 0)) > 30.0
                _psi_warn     = False
                if not _psi_stale:
                    if side == "b" and _live_psi_val > 1.5:
                        log.debug(f"[PSI-GATE] SHORT into Ψ={_live_psi_val:.2f}")
                        _psi_warn = True
                    elif side == "s" and _live_psi_val < 0.5:
                        log.debug(f"[PSI-GATE] LONG into Ψ={_live_psi_val:.2f}")
                        _psi_warn = True
                _LIVE_PSI[symbol] = {"ts": timestamp, "psi": psi, "side": side}

                # Snapshot state vector at detection time for JSONL persistence
                # CRITICAL: captured NOW to avoid T+60s lookahead bias
                sv = {
                    "psi":              result.get("psi", 0),
                    "stickiness":       result.get("stickiness", 0),
                    "absorption_ratio": result.get("absorption_ratio", 0),
                    "urgency":          result.get("urgency", 0),
                    "regime":           result.get("regime", _CURRENT_REGIME),
                    "kalman_cv_at_detect":     round(_KALMAN_CV[symbol].state, 4),
                    "kalman_P_at_detect":      round(_KALMAN_CV[symbol].P, 6),
                    "vclock_bucket_at_detect": _VOLUME_CLOCKS[symbol].bucket_size,
                    # Feature 10: clip size decay
                    "clip_slope":        slope,
                    "clip_decay":        decay,
                    "clip_decay_active": clip_decay_active,
                    # Feature 12: pre-detection psi
                    "psi_warn":  _psi_warn,
                    "live_psi":  round(_live_psi_val, 3),
                    # Footprint: true absorption fingerprint at detection time
                    "fp_delta":            result.get("fp_delta", 0),
                    "fp_total":            result.get("fp_total", 0),
                    "fp_absorption_score": result.get("fp_absorption_score", 0.0),
                    "true_absorption":     result.get("true_absorption", False),
                    "fp_anchor_ratio":     result.get("fp_anchor_ratio", 0.0),
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
        # ── ICE_TRACKER: remove price keys with no recent fills ──
        ice_sym = _ICE_TRACKER.get(sym, {})
        stale_prices = [p for p, fills in ice_sym.items()
                        if not fills or (now - fills[-1][0]) > _ICE_WINDOWS[-1][0] * 3]
        for p in stale_prices:
            del ice_sym[p]

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

    # ── ICE_DEDUP_SEEN: purge stale dedup keys ──
    stale_dedup = [k for k, t in _ICE_DEDUP_SEEN.items() if now - t > _ICE_DEDUP_TTL]
    for k in stale_dedup:
        del _ICE_DEDUP_SEEN[k]

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

    # ── Upgrade A: snapshot book size under L2_LOCK before entering CANDLE_LOCK ──
    # Reading DOM inside CANDLE_LOCK without L2_LOCK is a race condition.
    _book_sz = 0.0
    with _L2_LOCK:
        _dom = L2_STATE["dom"].get(symbol, {})
        if side == "b":      # aggressive buy hits the ask side
            _book_sz = float(_dom.get("asks", {}).get(qp, 0))
        elif side == "s":    # aggressive sell hits the bid side
            _book_sz = float(_dom.get("bids", {}).get(qp, 0))
        # side == "n": neutral/unknown — leave _book_sz = 0 (no meaningful book context)

    _pending_emits = []   # collect candle_update payloads to emit outside lock

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
                    frozen = _freeze_candle(cur)
                    _CANDLES[symbol][tf].append(frozen)
                    # Persist bubble profile to disk (1m only) so it survives restarts
                    if tf == _BP_PERSIST_TF:
                        _bp_save_candle(symbol, frozen)
                # Start new candle with bubble profile
                bp = {}
                bp[qp] = [volume if side == "b" else 0,
                          volume if side == "s" else 0,
                          None, None,   # [2]=fp_score, [3]=true_abs (set by iceberg detector)
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
                                  None, None,   # [2]=fp_score, [3]=true_abs
                                  _book_sz]     # [4]=book_size_at_trade

            # ── Emit candle update via Socket.IO (throttled) ──
            # Snapshot candle data inside lock; depth_deltas computed outside lock
            # to avoid holding _CANDLE_LOCK during the expensive T0 history search.
            if _socketio is not None:
                emit_key = f"{symbol}:{tf}"
                now = time.time()
                last = _last_emit_time.get(emit_key, 0)
                if now - last >= _EMIT_MIN_INTERVAL:
                    _last_emit_time[emit_key] = now
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        _pending_emits.append({
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
                            "micro_ofi": _MICRO_OFI[symbol].ofi if symbol in _MICRO_OFI else None,
                            # FIX F2: Pass backend Hawkes (Kirchner 2017 bivariate)
                            # to frontend instead of frontend recomputing with broken heuristic
                            "hawkes": _V2_HAWKES[symbol].get_state() if symbol in _V2_HAWKES else None,
                            # FIX M8: depth velocity at price levels (lots/sec drain rate)
                            "depth_vel": _DEPTH_VEL_CACHE.get(symbol),
                            # book_imbalance + depth_deltas computed outside lock below
                        })

    # ── Emit outside _CANDLE_LOCK to avoid blocking trades ──
    for _emit_candle in _pending_emits:
        bp_data = _emit_candle.get("bp")
        if bp_data:
            _emit_candle["depth_deltas"] = _compute_depth_deltas(
                symbol, _emit_candle["time"], time.time(),
                bp_data.keys()
            )
            # Book imbalance: compute under L2_LOCK (reads DOM)
            with _L2_LOCK:
                _emit_candle["book_imbalance"] = _compute_book_imbalance(
                    symbol, bp_data.keys()
                )
        try:
            _socketio.emit("candle_update", _emit_candle, namespace="/")
        except Exception as e:
            log.warning("candle_update emit failed: %s", e)


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
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    bids = {_norm_pk(k, tick_size): v for k, v in dom.get("bids", {}).items()}
    asks = {_norm_pk(k, tick_size): v for k, v in dom.get("asks", {}).items()}
    prev = _PREV_DOM_SNAP.get(symbol, {"bids": {}, "asks": {}})
    prev_bids = {_norm_pk(k, tick_size): v for k, v in prev.get("bids", {}).items()}
    prev_asks = {_norm_pk(k, tick_size): v for k, v in prev.get("asks", {}).items()}

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

        # Keys are already normalized via _norm_pk — O(1) lookup is sufficient.

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

        # ── Pre-Classification (Tiering) ──
        # Integrates Kalman OFI divergence if available:
        div_multiplier = 1.0
        kalman = _KALMAN_OFI.get(symbol)
        if kalman and kalman.ready:
            if abs(kalman.theta) < 0.2:
                div_multiplier = 1.25  # Absorption confirmed (flow is heavy, price is stuck)
            elif abs(kalman.theta) > 0.6:
                div_multiplier = 0.7   # Price is moving with flow (not true absorption)

        adj_score = effective_score * div_multiplier
        tier = 0
        label = ""
        if adj_score >= 15.0:
            tier = 3; label = "SUPER_WALL"
        elif adj_score >= 6.0:
            tier = 2; label = "WALL"
        elif adj_score >= 2.0:
            tier = 1; label = "ABS"

        scores[price_key] = {
            "score": round(effective_score, 2),
            "tier": tier,
            "label": label,
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

        # ── Moment-matching estimator for G (Kirchner 2017) ──
        # Cross-excitation: count how often a buy follows a sell within decay window
        # and vice versa. This is cheaper and more stable than full MLE.
        n = len(self._events)
        G = [[0.0, 0.0], [0.0, 0.0]]  # [buy→buy, sell→buy; buy→sell, sell→sell]
        count = [[0, 0], [0, 0]]

        for i in range(1, n):
            t_i, s_i, _ = self._events[i]
            for j in range(i - 1, max(i - 30, -1), -1):  # look back max 30 events
                t_j, s_j, _ = self._events[j]
                dt = t_i - t_j
                if dt > 5.0:  # beyond 5 seconds, excitation is negligible
                    break
                kernel = _math.exp(-self.decay * dt)
                G[s_j][s_i] += kernel
                count[s_j][s_i] += 1

        # Normalize by count to get average excitation
        for i2 in range(2):
            for j2 in range(2):
                if count[i2][j2] > 0:
                    G[i2][j2] /= count[i2][j2]

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

        # ── Bootstrap standard error (fast approximation) ──
        # Poisson approximation: std ≈ rho / sqrt(n_events)
        rho_std = rho / max(_math.sqrt(n), 1.0)

        self._prev_rho = self._last_rho
        self._last_rho = round(rho, 4)
        self._last_rho_std = round(rho_std, 4)

        if rho < 0.8:
            self._last_phase = "subcritical"
        elif rho < 1.0:
            self._last_phase = "near_critical"
        else:
            self._last_phase = "supercritical"

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

    # Drain trade buffer: capture all trades since last snapshot
    with _L2_LOCK:
        trades = _HEATMAP_TRADE_BUF.pop(symbol, [])
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
                if raw_score < crack_threshold and shock_count >= 3:
                    abs_tiers[pk] = {"tier": -1, "score": round(score, 2), "label": "CRACK", "waves": waves, "sd": _abs_side}
                    _telemetry.log_event(symbol, "ABSORPTION_CRACK", {"price": pk, "score": round(score, 2), "waves": waves, "shock_hits": shock_count})
                elif score >= 2.0 and waves >= 3:
                    abs_tiers[pk] = {"tier": 2, "score": round(score, 2), "label": "WALL", "waves": waves, "sd": _abs_side}
                    _telemetry.log_event(symbol, "ABSORPTION_WALL", {"price": pk, "score": round(score, 2), "waves": waves})
                elif score >= 1.0 and waves >= 2:
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

    # ── Push to frontend via WebSocket ──
    if _socketio is not None:
        try:
            # Compute live spread from snapshot.
            # Spread = best_ask - best_bid in tick units.
            # Critical market-maker signal: widening spread = thin book = don't quote.
            _snap_best_bid = max((float(k) for k in snap_bids), default=0.0)
            _snap_best_ask = min((float(k) for k in snap_asks), default=0.0)
            _snap_spread   = round(_snap_best_ask - _snap_best_bid, 4) if _snap_best_ask > _snap_best_bid else 0.0

            payload = {
                "sym":      symbol,
                "ts":       now,
                "bids":     snap_bids,
                "asks":     snap_asks,
                "trades":   compact_trades,
                "abs":      compact_abs,
                "best_bid": _snap_best_bid,
                "best_ask": _snap_best_ask,
                "spread":   _snap_spread,
            }
            # Only include depth_vel if non-empty (avoid payload bloat)
            if depth_vel:
                payload["depth_vel"] = depth_vel
            # Only include abs_tiers if non-empty (avoid payload bloat)
            if abs_tiers:
                payload["abs_tiers"] = abs_tiers
            # ── Model engine outputs ──
            qd = _QUEUE_DYNAMICS.get(symbol)
            if qd:
                qd_state = qd.get_state()
                if qd_state:
                    payload["queue_dynamics"] = qd_state
            tox = _TRADE_TOXICITY.get(symbol)
            if tox:
                tox_state = tox.get_state()
                if tox_state:
                    payload["trade_toxicity"] = tox_state
            surv = _LEVEL_SURVIVAL.get(symbol)
            if surv:
                _mid = (_snap_best_bid + _snap_best_ask) / 2 if _snap_best_bid > 0 and _snap_best_ask > 0 else 0
                surv_state = surv.get_survival(
                    snap_bids, snap_asks, _mid,
                    payload.get("queue_dynamics"),
                    payload.get("trade_toxicity"),
                )
                if surv_state:
                    payload["level_survival"] = surv_state
            _socketio.emit("dom_snapshot", payload, namespace="/")
        except Exception as e:
            log.debug("dom_snapshot emit error: %s", e)

        # ── V2 Signals: separate channel at ~2Hz (same cadence as dom_snapshot) ──
        if v2_sigs:
            try:
                v2_sigs["sym"] = symbol
                v2_sigs["ts"] = now
                _socketio.emit("v2_signals", v2_sigs, namespace="/")
            except Exception:
                pass

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
        L2_STATE["dom"][symbol]        = copy.deepcopy(dom)
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

        # ── Footprint Engine: cumulative delta per price level ──
        # Feed every trade into the candle footprint so iceberg detection
        # can compute true absorption vs falling knife at detection time.
        tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
        qp = str(round(round(price / tick_size) * tick_size, 2))
        if side in ('b', 's'):
            _candle_ts = int(ts / _FOOTPRINT_CANDLE_SEC) * _FOOTPRINT_CANDLE_SEC
            _fp = _FOOTPRINT[symbol]
            if _candle_ts != _fp['candle_ts']:
                # New candle: clear the footprint (keep only current candle's data)
                _fp['candle_ts'] = _candle_ts
                _fp['levels'].clear()
            _fp_level = _fp['levels'][round(price, 2)]
            _fp_level['n_trades'] += 1
            if side == 'b':
                _fp_level['buy_vol'] += vol
                # Ask-hold check: did the ask stay at this price after the buy lift?
                # If ask stayed → seller wall absorbing buyers (short setup)
                with _L2_LOCK:
                    _cur_ask = L2_STATE['dom'].get(symbol, {}).get('best_ask', 0.0)
                if _cur_ask > 0:
                    if abs(_cur_ask - price) <= tick_size:  # ask still AT this level
                        _fp_level['ask_anchored'] += vol
                    else:
                        _fp_level['ask_dropped'] += vol    # ask moved away
            else:
                _fp_level['sell_vol'] += vol
                # Bid-hold check: did the bid stay at this price after the sell?
                # If bid stayed → buyer wall absorbing sellers (long setup)
                with _L2_LOCK:
                    _cur_bid = L2_STATE['dom'].get(symbol, {}).get('best_bid', 0.0)
                if _cur_bid > 0:
                    if abs(_cur_bid - price) <= tick_size:  # bid still AT this level
                        _fp_level['bid_anchored'] += vol
                    else:
                        _fp_level['bid_dropped'] += vol    # bid moved away

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
                        # Stamp the bp entry with footprint data so the bubble
                        # renderer can visually distinguish true absorption.
                        # bp[qp] = [buyVol, sellVol, fp_score, true_abs, book_size_at_trade]
                        _bp = cur.get("bp", {})
                        if qp in _bp:
                            entry = _bp[qp]
                            # Extend bp entry with footprint metadata at index 2, 3
                            while len(entry) < 4:
                                entry.append(None)
                            _fp_score = ice_hit.get("fp_absorption_score", 0.0)
                            _t_abs    = 1 if ice_hit.get("true_absorption", False) else 0
                            entry[2]  = round(_fp_score, 3)
                            entry[3]  = _t_abs
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
                        if len(cur["wall_gone"]) < 200:
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
            iso_ts = datetime.utcfromtimestamp(ts).isoformat() + "Z"
            _socketio.emit("trade_tick", {
                "symbol": symbol,
                "price": price,
                "volume": vol,
                "side": side,
                "timestamp": iso_ts,
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
