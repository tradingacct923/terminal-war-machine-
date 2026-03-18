from __future__ import annotations
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

def set_socketio(sio):
    """Called by server.py to inject the SocketIO instance for real-time push."""
    global _socketio
    _socketio = sio
    log.info("Socket.IO reference set for real-time candle push")


# ══════════════════════════════════════════════════════════════════════════════
# ORDERFLOW DETECTION — Iceberg + Sweep engines
# ══════════════════════════════════════════════════════════════════════════════

# ── Iceberg Detection Constants ──
_ICE_REFILL_COUNT     = 3       # min refills at same price to trigger
_ICE_CV_THRESHOLD     = 0.35    # max coefficient of variation (stddev/mean) for clip consistency
_ICE_MIN_CLIP_FLOOR   = 1       # absolute minimum clip size (never go below 1)
_ICE_ZONE_TICKS       = 2       # ±2 ticks = adjacent prices count as same zone
_ICE_ABSORB_MAX_MOVE  = 2       # price must stay within ±2 ticks to be "absorbing"

# Tiered detection windows: (window_seconds, confidence_label)
_ICE_WINDOWS = [
    (5.0,  "high"),    # fast refill = definitely iceberg
    (15.0, "medium"),  # patient algo
    (60.0, "low"),     # very patient, could be coincidence
]

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
_DRIFT_WINDOW_SEC      = 30.0   # look back 30 seconds
_DRIFT_MIN_FILLS       = 5      # need at least 5 same-side fills
_DRIFT_MAX_CV          = 0.40   # clip consistency (looser for drift)
_DRIFT_MIN_PRICE_SPREAD = 3     # must span 3+ distinct prices
# {symbol: {"b": deque, "s": deque}}
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
_SWEEP_MIN_VOLUME     = 30      # total swept volume threshold

# ── Detection State ──
# _ICE_TRACKER: {symbol: {quantized_price_str: [(timestamp, volume, side), ...]}}
_ICE_TRACKER: dict = defaultdict(lambda: defaultdict(list))
# _SWEEP_TRACKER: {symbol: [(timestamp, price, volume, side), ...]}
_SWEEP_TRACKER: dict = defaultdict(list)
# Detected results attached to current candle: {symbol: {tf: {icebergs: {}, sweeps: []}}}
_DETECT_RESULTS: dict = defaultdict(lambda: defaultdict(dict))

# ── Cumulative Delta Divergence Constants ──
_DIV_LOOKBACK_CANDLES = 20       # rolling window to find swing highs/lows
_DIV_MIN_PRICE_MOVE   = 2.0      # minimum price difference for swing
_DIV_MIN_DELTA_GAP    = 50       # minimum delta gap to trigger

# ── Delta Divergence State ──
# {symbol: {tf: [{"t": boundary, "high": h, "low": l, "delta": d}, ...]}}
_DELTA_HISTORY: dict = defaultdict(lambda: defaultdict(list))
# {symbol: {tf: cumulative_delta}}
_CUM_DELTA: dict = defaultdict(lambda: defaultdict(float))

# ── Momentum Ignition Constants ──
_IGN_MIN_TRADES       = 8        # min trades in window
_IGN_WINDOW_SEC       = 2.0      # 2-second window
_IGN_MAX_CLIP_SIZE    = 5        # max individual trade size
_IGN_MAX_TOTAL        = 30       # max total volume (not real conviction)
_IGN_REVERSAL_SEC     = 30.0     # reversal confirmation window

# ── Momentum Ignition State ──
# {symbol: [(timestamp, price, volume, side), ...]}
_IGN_TRACKER: dict = defaultdict(list)
# {symbol: [{"direction": "up"|"down", "prices": [...], "ts": T, "start_price": P}, ...]}
_IGN_ACTIVE: dict = defaultdict(list)

# ── Spoof Detection Constants ──
_SPOOF_MIN_SIZE       = 100      # min order size to track
_SPOOF_MAX_LIFETIME   = 3.0      # max seconds before considered spoof
_SPOOF_MIN_OCCUR      = 2        # min occurrences to trigger

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
    tracker = _DRIFT_TRACKER[symbol][side]
    tracker.append((timestamp, price_f, volume))
    cutoff = timestamp - _DRIFT_WINDOW_SEC
    recent = [(t, p, v) for t, p, v in tracker if t >= cutoff]
    if len(recent) < _DRIFT_MIN_FILLS:
        return None
    prices = set(round(p, 2) for _, p, _ in recent)
    if len(prices) < _DRIFT_MIN_PRICE_SPREAD:
        return None
    vols = [v for _, _, v in recent]
    mean_v = sum(vols) / len(vols)
    if mean_v == 0:
        return None
    var = sum((v - mean_v) ** 2 for v in vols) / len(vols)
    cv = (var ** 0.5) / mean_v
    if cv > _DRIFT_MAX_CV:
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
                                ts: float, size_rank: str, confidence: str):
    """Schedule outcome checks for post-iceberg prediction."""
    _ICE_PENDING[symbol].append({
        "side": side, "price": price, "ts": ts,
        "size_rank": size_rank, "confidence": confidence,
        "check_10s": ts + 10, "check_30s": ts + 30, "check_60s": ts + 60,
        "outcome_10s": None, "outcome_30s": None, "outcome_60s": None,
    })


def _check_pending_outcomes(symbol: str, current_price: float, current_ts: float):
    """Resolve pending post-iceberg outcome checks."""
    still_pending = []
    for p in _ICE_PENDING[symbol]:
        direction = 1 if p["side"] == "b" else -1
        if p["outcome_10s"] is None and current_ts >= p["check_10s"]:
            p["outcome_10s"] = round((current_price - p["price"]) * direction, 2)
        if p["outcome_30s"] is None and current_ts >= p["check_30s"]:
            p["outcome_30s"] = round((current_price - p["price"]) * direction, 2)
        if p["outcome_60s"] is None and current_ts >= p["check_60s"]:
            p["outcome_60s"] = round((current_price - p["price"]) * direction, 2)
            _ICE_OUTCOMES[symbol].append(p)
            continue
        still_pending.append(p)
    _ICE_PENDING[symbol] = still_pending


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

    # Check pending prediction outcomes on every trade
    _check_pending_outcomes(symbol, price_f, timestamp)

    # Adaptive min clip: 50% of rolling average trade size
    trade_hist = _TRADE_SIZE_HISTORY[symbol]
    if len(trade_hist) > 10:
        avg_trade = sum(trade_hist) / len(trade_hist)
        min_clip = max(_ICE_MIN_CLIP_FLOOR, int(avg_trade * 0.5))
    else:
        avg_trade = float(volume)
        min_clip = _ICE_MIN_CLIP_FLOOR

    if volume < min_clip:
        return None

    # Track this fill at this price
    tracker = _ICE_TRACKER[symbol][price_str]
    tracker.append((timestamp, volume, side))

    # Prune oldest fills beyond widest window
    max_window = _ICE_WINDOWS[-1][0]  # 60s
    cutoff_prune = timestamp - max_window
    while tracker and tracker[0][0] < cutoff_prune:
        tracker.pop(0)

    # ── ZONE DETECTION: collect fills from adjacent ±N ticks ──
    tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
    zone_fills_all = []
    zone_levels_hit = set()

    for offset in range(-_ICE_ZONE_TICKS, _ICE_ZONE_TICKS + 1):
        adj_price = round(price_f + offset * tick_size, 2)
        adj_key = str(adj_price)
        adj_fills = _ICE_TRACKER[symbol].get(adj_key, [])
        if adj_fills:
            zone_levels_hit.add(adj_key)
            zone_fills_all.extend(adj_fills)

    is_zone = len(zone_levels_hit) > 1

    # Try each window tier from tightest to widest
    for window_sec, confidence in _ICE_WINDOWS:
        window_cutoff = timestamp - window_sec

        raw_fills = zone_fills_all if is_zone else list(tracker)
        fills_in_window = [f for f in raw_fills if f[0] >= window_cutoff]

        if len(fills_in_window) < _ICE_REFILL_COUNT:
            continue

        fill_sides = [f[2] for f in fills_in_window]
        if len(set(fill_sides)) != 1:
            continue

        vols = [f[1] for f in fills_in_window]
        mean_vol = sum(vols) / len(vols)
        if mean_vol == 0:
            continue
        variance = sum((v - mean_vol) ** 2 for v in vols) / len(vols)
        stddev_vol = variance ** 0.5
        cv = stddev_vol / mean_vol

        if cv > _ICE_CV_THRESHOLD:
            continue

        # ═══════════════ ICEBERG DETECTED — COMPUTE ALL INTELLIGENCE ═══════════

        visible_total = sum(vols)
        n_fills = len(fills_in_window)
        time_elapsed = max(fills_in_window[-1][0] - fills_in_window[0][0], 0.01)
        fill_rate = n_fills / time_elapsed
        avg_clip = mean_vol
        est_duration = 90.0
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

        # ── Absorption context ──
        absorbing = False
        prices_during = [p for t, p in _ICE_PRICE_HISTORY[symbol] if t >= window_cutoff]
        if len(prices_during) >= 2:
            price_range = max(prices_during) - min(prices_during)
            if price_range / tick_size <= _ICE_ABSORB_MAX_MOVE:
                absorbing = True

        # ── Opposition volume & absorption ratio ──
        opp_side = "s" if fill_sides[0] == "b" else "b"
        opposition_vol = 0
        for offset in range(-_ICE_ZONE_TICKS, _ICE_ZONE_TICKS + 1):
            adj_price = round(price_f + offset * tick_size, 2)
            adj_key = str(adj_price)
            for ts_o, vol_o, s_o in _ICE_TRACKER[symbol].get(adj_key, []):
                if s_o == opp_side and ts_o >= window_cutoff:
                    opposition_vol += vol_o
        absorption_ratio = round(opposition_vol / max(visible_total, 1), 2)

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

        # ── Urgency score (0-1 composite) ──
        time_factor = min(fill_rate / 2.0, 1.0)
        size_factor = min(avg_clip / max(avg_trade, 1), 1.0)
        remaining_factor = 1.0 - fill_pct
        urgency = round(time_factor * 0.4 + size_factor * 0.3 + remaining_factor * 0.3, 3)

        # ── Pressure signal (decision tree) ──
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

        # ── Prediction (Elite #6) ──
        prediction = _get_prediction(symbol, fill_sides[0])

        # ── Update wall state for gone detection ──
        wall_st = _ICE_WALL_STATE[symbol][price_str]
        wall_st["last_refill_ts"] = timestamp
        wall_st["was_active"] = True
        wall_st["gone_announced"] = False
        wall_st["side"] = fill_sides[0]

        # ── Update level memory ──
        result = {
            # Core detection
            "clips": n_fills,
            "est_total": visible_total,
            "est_hidden": est_hidden,
            "avg_clip": round(avg_clip, 1),
            "cv": round(cv, 3),
            "confidence": final_confidence,
            "side": fill_sides[0],
            # Zone
            "zone": is_zone,
            "zone_levels": len(zone_levels_hit) if is_zone else 1,
            # Exhaustion
            "decay": decay,
            "slope": slope,
            # Absorption
            "absorbing": absorbing,
            "opposition_vol": opposition_vol,
            "absorption_ratio": absorption_ratio,
            # Size & urgency
            "size_rank": size_rank,
            "fill_pct": round(fill_pct, 3),
            "urgency": urgency,
            "pressure": pressure,
            "est_remaining_sec": depletes_in,
            # DOM cross-validation (Elite #1)
            "dom_confirmed": dom_conf,
            "dom_refill": dom_refill,
            # Inter-fill timing (Elite #2)
            "gap_cv": gap_cv_val,
            "timing": timing_conf,
            # Countdown (Elite #3)
            "state": ice_state,
            "depletes_in_sec": depletes_in,
            # Level memory (Elite #5)
            "level_ice_count": level_ice_count,
            "level_avg_size": level_avg_size,
            # Prediction (Elite #6)
            "prediction": prediction,
        }

        _update_level_memory(symbol, price_str, result)

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
    sweep_result = {
        "prices": [float(p) for p in prices],
        "vol": total_vol,
        "side": sides[0],
        "ts": timestamp,
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
                    if p and s >= _SPOOF_MIN_SIZE:
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
                        }
                        try:
                            _socketio.emit("candle_update", candle_data, namespace="/")
                        except Exception:
                            pass  # don't let emit errors break the candle engine


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
    "signals": {
        "shannon_entropy":     None,
        "ising_magnetization": None,
        "reynolds_number":     None,
    },
    "last_update": 0,
}
_L2_LOCK = threading.Lock()


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
            "signals":       dict(L2_STATE["signals"]),
            "last_update":   float(L2_STATE["last_update"]),
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
        L2_STATE["dom"][symbol]        = dom
        L2_STATE["imbalance"][symbol]  = imb
        L2_STATE["mid_prices"][symbol] = mid
        L2_STATE["last_update"]        = time.time()

        if _shannon:
            L2_STATE["signals"]["shannon_entropy"] = _shannon.get_signal()
        if _reynolds and mid > 0:
            L2_STATE["signals"]["reynolds_number"] = _reynolds.get_signal()

    # ── DOM snapshot for iceberg cross-validation (Elite #1) ──
    _dom_update_snapshots(symbol, dom)

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
    if price > 0:
        # ── Tick classification ──
        # Classify trade aggression by comparing price to current BBO.
        # Reading best_bid/best_ask from L2_STATE["dom"] without _L2_LOCK
        # is safe here: float reads are atomic in CPython, and we only need
        # an approximate snapshot (off-by-one-tick is acceptable for bubbles).
        side = "n"  # default: neutral/passive
        dom = L2_STATE["dom"].get(symbol)
        if dom:
            best_ask = dom.get("best_ask", 0)
            best_bid = dom.get("best_bid", 0)
            if best_ask > 0 and price >= best_ask:
                side = "b"  # aggressive buy (lifted the ask)
            elif best_bid > 0 and price <= best_bid:
                side = "s"  # aggressive sell (hit the bid)

        # ── Orderflow Detection (runs before candle update) ──
        tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
        qp = str(round(round(price / tick_size) * tick_size, 2))

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

        # Drifting iceberg detection (Elite #4)
        drift_hit = _detect_drifting_iceberg(symbol, price, vol, ts, side)
        if drift_hit:
            with _CANDLE_LOCK:
                for tf in CANDLE_TIMEFRAMES:
                    cur = _CURRENT_CANDLE[symbol].get(tf)
                    if cur:
                        cur["drifting_iceberg"] = drift_hit

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

    # ── Emit trade tick via Socket.IO ──
    if _socketio is not None and price > 0:
        try:
            _socketio.emit("trade_tick", {
                "symbol": symbol,
                "price": price,
                "volume": vol,
                "side": side,
                "timestamp": ts,
            }, namespace="/")
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

        # Backfill price history + candle chart from retrieveBars API
        def _backfill():
            try:
                import time as _time
                # Wait for contracts to be resolved by the connector
                for i in range(30):
                    if _connector._symbol_to_contract:
                        log.info("L2 backfill: contracts resolved after %ds: %s", i, list(_connector._symbol_to_contract.keys()))
                        break
                    _time.sleep(1)
                else:
                    log.warning("L2 backfill: no contracts after 30s — aborting")
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
                session_start_utc = session_open.astimezone(timezone.utc).isoformat()
                log.info("L2 backfill: session open = %s NY → %s UTC", session_open.strftime('%Y-%m-%d %H:%M'), session_start_utc)

                for sym in SYMBOLS:
                    cid = _connector._symbol_to_contract.get(sym)
                    if not cid:
                        continue

                    # Fetch 1-minute bars from session open
                    bars = _connector.retrieve_bars(
                        cid, start_time=session_start_utc,
                        unit=2, unit_number=1, limit=20000
                    )
                    if not bars:
                        log.warning("L2 backfill: no bars for %s", sym)
                        continue
                    log.info("L2 backfill: %s got %d bars from API", sym, len(bars))

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
                            ts = dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                        except Exception:
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

            except Exception as e:
                import traceback
                log.warning("L2 backfill failed: %s\n%s", e, traceback.format_exc())
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
