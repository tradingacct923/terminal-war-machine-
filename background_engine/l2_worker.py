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
_ICE_WINDOW_SEC       = 5.0     # time window for refill detection
_ICE_SIZE_TOLERANCE   = 0.30    # clip sizes must be within ±30% of each other
_ICE_MIN_CLIP         = 3       # minimum individual clip size to consider

# ── Sweep Detection Constants ──
_SWEEP_MIN_LEVELS     = 3       # min consecutive price levels swept
_SWEEP_WINDOW_SEC     = 0.200   # 200ms window for sweep
_SWEEP_MIN_VOLUME     = 30      # total swept volume threshold

# ── Detection State ──
# _ICE_TRACKER: {symbol: {quantized_price_str: [(timestamp, volume), ...]}}
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


def _detect_iceberg(symbol: str, price_str: str, volume: int,
                    timestamp: float, side: str):
    """Track fills at each price level. Detect iceberg when:
    - 3+ fills at the same price within 5 seconds
    - Each fill is similar size (within ±30% of each other)
    - All fills are the same side (all buys or all sells)
    Returns iceberg dict if detected, else None.
    """
    if volume < _ICE_MIN_CLIP or side == "n":
        return None

    tracker = _ICE_TRACKER[symbol][price_str]
    tracker.append((timestamp, volume, side))

    # Prune old fills outside the window
    cutoff = timestamp - _ICE_WINDOW_SEC
    while tracker and tracker[0][0] < cutoff:
        tracker.pop(0)

    # Need at least N fills
    if len(tracker) < _ICE_REFILL_COUNT:
        return None

    # Check: all same side
    sides = [f[2] for f in tracker]
    if len(set(sides)) != 1:
        return None

    # Check: similar clip sizes (each within ±tolerance of the median)
    vols = [f[1] for f in tracker]
    median_vol = sorted(vols)[len(vols) // 2]
    if median_vol == 0:
        return None
    for v in vols:
        if abs(v - median_vol) / median_vol > _ICE_SIZE_TOLERANCE:
            return None

    # Iceberg detected!
    est_total = sum(vols)
    return {
        "clips": len(tracker),
        "est_total": est_total,
        "side": sides[0],
    }


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
