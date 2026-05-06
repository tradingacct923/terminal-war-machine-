"""
Alert Engine — 0DT-Hero-style signal detection on FlowAccumulator output.

Consumes 1Hz snapshots of per-ticker state and fires 6 alert types:

  flow_cross    — 0DTE curve crosses all-exp curve (bullish: up; bearish: down)
  flow_divergence — spot direction and signed flow direction disagree over N-min window
  flow_convergence — previously diverged curves re-align
  spike         — signed Δ notional rate exceeds +Nσ threshold
  dump          — signed Δ notional rate exceeds −Nσ threshold
  bullish_volume — unsigned volume Nσ above rolling mean AND net signed positive

All thresholds are σ-adaptive (rolling 10-min window). Alerts are emitted as
'flow_alert' socket events with schema:
    {type, ticker, direction, magnitude_m, bucket, ts, confidence}
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class _TickerHistory:
    """Rolling window of snapshots for one ticker."""
    # Each entry: (ts, signed_0dte, signed_all, unsigned_0dte, unsigned_all, spot)
    samples: deque = field(default_factory=lambda: deque(maxlen=600))  # 10 min @ 1Hz
    last_cross_side: int = 0           # +1 bullish, -1 bearish, 0 none
    last_diverge_side: int = 0         # current divergence direction (0 = aligned)
    last_diverge_start_ts: float = 0
    last_alert_ts: dict = field(default_factory=dict)   # {alert_type: ts} for cooldown
    # Directional state for the AI Panel matrix (readable via get_state_matrix).
    # last_key_level_side tracks the sign of the most recent wall/flip cross.
    # last_spike_side: +1 on bullish spike, -1 on bearish dump.
    last_key_level_side: int = 0
    last_spike_side: int = 0
    # Per-ticker key levels fed by schwab_bridge's per-ticker GEX pipeline.
    # Shape: {'put_wall': float, 'call_wall': float, 'flip': float}
    last_walls: dict = field(default_factory=dict)
    last_walls_update_ts: float = 0


# ────────────────────────────────────────────────────────────────────────────
# ABSOLUTE-DOLLAR THRESHOLDS (0DTHero-style).
# Calibrated from observed 0DTHero alert magnitudes:
#   SPY dump -102.17M, -115.28M  → dump_floor must be ≥$50M
#   QQQ dump -120.80M, QQQ spike +140.34M / +147.28M / +171.78M
#   (their log never shows sub-$50M events)
# Absolute floors mean a $0.05M retail-sized AMZN trade never trips a spike.
# σ thresholds stayed in place as a SECONDARY filter — a move must be BOTH
# big in dollars AND unusual vs the rolling window. (Before this change the
# σ test alone could fire on $50k moves during a quiet window.)
# ────────────────────────────────────────────────────────────────────────────
SPIKE_DUMP_MIN_MAGNITUDE   = 50_000_000    # $50M floor for spike/dump [all exp]
SPIKE_DUMP_MIN_MAGNITUDE_0DTE = 25_000_000 # $25M floor for 0DTE spike/dump (tighter window → smaller notional)
BULLISH_VOLUME_MIN_MAGNITUDE  = 25_000_000 # $25M floor for bullish_volume
FLOW_CROSS_MIN_MAGNITUDE      = 5_000_000  # $5M floor for flow cross (0dte−all_exp delta)
SIGMA_THRESHOLD_SPIKE   = 2.5              # σ above rolling mean → spike (secondary filter)
SIGMA_THRESHOLD_VOLUME  = 2.0              # σ above for bullish_volume (secondary filter)
DIVERGE_MIN_MAGNITUDE   = 25_000_000       # $25M min signed flow to count a divergence
DIVERGE_MIN_SPOT_MOVE_PCT = 0.15           # min 0.15% spot move to count
DIVERGE_WINDOW_SEC   = 300                 # 5-min look-back for divergence detection
COOLDOWN_SEC         = 60                  # min seconds between same-type alerts per ticker
MATRIX_TTL_SEC       = 300                 # State matrix cell decays to 'none' after 5 min idle

# ── Outcome tracking ────────────────────────────────────────────────────────
# For every directional alert we log spot(t+N) at N=5min/15min/30min so we can
# compute realized hit rates empirically. Hit = sign(spot_move) matches the
# alert's direction. Aggregated via get_hit_rates() and /api/alerts/outcomes.
_OUTCOME_HORIZONS_SEC = (300, 900, 1800)
_OUTCOME_GC_MAX_AGE_SEC = 3600             # drop pending outcomes older than 1hr


def _stats(vals):
    """Return (mean, stddev) of a sequence."""
    n = len(vals)
    if n == 0:
        return (0.0, 0.0)
    m = sum(vals) / n
    if n < 2:
        return (m, 0.0)
    var = sum((v - m) ** 2 for v in vals) / n
    return (m, var ** 0.5)


import json
import os

# Daily JSONL log — persists fired alerts across restarts so the UI can
# replay today's flow after a reconnect. File name: logs/alerts_YYYYMMDD.jsonl
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs')


def _daily_alert_log_path(when: float = 0) -> str:
    from datetime import date as _d, datetime as _dt
    dt = _dt.fromtimestamp(when) if when else _dt.now()
    return os.path.join(_LOG_DIR, f'alerts_{dt.strftime("%Y%m%d")}.jsonl')


_OUTCOMES_LOG_PATH = os.path.join(_LOG_DIR, 'alert_outcomes.jsonl')

# ── Per-ticker rolling history disk persistence (added 2026-05-06) ──
# `_history[ticker].samples` holds the 10-min rolling window (~600 entries)
# that detection methods compare against. After a process restart it's
# empty and detectors warm-up-gate (`if len(samples) < 30`) blocks all
# alerts for ~15s while fresh samples accumulate. Persisting `_history`
# eliminates that warmup gap, and also restores `last_walls`,
# `last_alert_ts` (cooldown), and the directional `last_*_side` fields so
# the AI panel matrix paints immediately instead of cold-starting.
_HISTORY_STATE_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   '..', 'state')
_HISTORY_STATE_FILE = os.path.join(_HISTORY_STATE_DIR, 'alert_engine_history.json')
_HISTORY_STATE_TMP  = _HISTORY_STATE_FILE + '.tmp'
_HISTORY_PERSIST_SEC      = 30.0
_HISTORY_RESTORE_MAX_AGE  = 600.0   # 10 min — older windows don't reflect current rolling stats
_HISTORY_STATE_VERSION    = 1


class AlertEngine:
    """Per-ticker rolling-window alert detection."""

    def __init__(self, socketio=None):
        self._socketio = socketio
        self._history: dict[str, _TickerHistory] = {}
        # Rolling log of last N alerts (for diagnostic inspection)
        self._alert_log: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        # Outcome queue: each entry is {alert, spot0, horizons_remaining, filled}
        # Spot at each horizon is stamped as the ticker's 1Hz observe() tick
        # crosses the horizon. Completed outcomes flushed to JSONL.
        self._pending_outcomes: list = []

    def observe(self, ticker: str, ts: float, s0: float, sa: float,
                u0: float, ua: float, spot: float = 0.0) -> list[dict]:
        """
        Ingest one 1Hz snapshot for a ticker. Returns list of alerts fired.

        s0 = cum_signed_0dte, sa = cum_signed_all
        u0 = cum_unsigned_0dte, ua = cum_unsigned_all
        spot = current underlying price (for divergence detection)
        """
        alerts = []
        with self._lock:
            hist = self._history.setdefault(ticker, _TickerHistory())
            hist.samples.append((ts, s0, sa, u0, ua, spot))
            if len(hist.samples) < 30:  # warmup
                return alerts

            # ── 1. FLOW CROSS (0DTE vs all-exp) ────────────────────────────
            alerts.extend(self._detect_cross(ticker, ts, hist))

            # ── 2. SPIKE / DUMP (signed flow rate) ─────────────────────────
            alerts.extend(self._detect_spike_dump(ticker, ts, hist))

            # ── 3. FLOW DIVERGENCE / CONVERGENCE (spot vs flow) ────────────
            alerts.extend(self._detect_diverge_converge(ticker, ts, hist))

            # ── 4. BULLISH VOLUME ──────────────────────────────────────────
            alerts.extend(self._detect_bullish_volume(ticker, ts, hist))

            # ── 5. KEY LEVEL (price breaks wall/flip) ─────────────────────
            alerts.extend(self._detect_key_level(ticker, ts, hist))

            # ── 5b. WALL PROXIMITY (spot within 0.3% of put/call wall) ─────
            alerts.extend(self._detect_wall_proximity(ticker, ts, hist, spot))

            # ── Outcome tracking: stamp spot at each horizon for prior alerts,
            # flush completed ones, register new alerts for future stamping.
            # Spot is ticker-specific, so process only this ticker's queue.
            if spot > 0:
                self._process_pending_outcomes(ticker, ts, spot)
                for a in alerts:
                    self._register_outcome(a, spot)

        # Emit + log
        for a in alerts:
            self._alert_log.append(a)
            self._emit(a)
        return alerts

    def get_log(self, last_n: int = 50) -> list:
        """Return most recent N alerts fired."""
        return list(self._alert_log)[-last_n:]

    def load_from_disk(self, date_str: str = None) -> int:
        """Load alerts from the daily JSONL file into the in-memory log.
        Used on startup to restore today's alerts across restarts.
        date_str format: 'YYYYMMDD' (defaults to today)."""
        from datetime import date as _d
        if date_str is None:
            date_str = _d.today().strftime('%Y%m%d')
        path = os.path.join(_LOG_DIR, f'alerts_{date_str}.jsonl')
        if not os.path.exists(path):
            return 0
        loaded = 0
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        a = json.loads(line)
                        self._alert_log.append(a)
                        loaded += 1
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"[ALERT] load_from_disk failed: {e}")
        return loaded

    def get_history(self, date_str: str, last_n: int = 500) -> list:
        """Read a specific day's alerts off disk (for date-picker replay).
        date_str format: 'YYYYMMDD'."""
        path = os.path.join(_LOG_DIR, f'alerts_{date_str}.jsonl')
        if not os.path.exists(path):
            return []
        out = []
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
        except Exception as e:
            log.warning(f"[ALERT] get_history failed: {e}")
        return out[-last_n:]

    def get_sample_count(self, ticker: str) -> int:
        """How many samples has this ticker accumulated?"""
        h = self._history.get(ticker)
        return len(h.samples) if h else 0

    def save_history_state(self) -> int:
        """Atomic JSON snapshot of `_history` to disk.

        Persists the 10-min rolling window per ticker plus directional state
        fields so a process restart can resume detection without the 15-second
        warmup gate or losing cooldown timestamps. Returns # tickers written.
        """
        with self._lock:
            payload_tickers = {}
            for tk, hist in self._history.items():
                if not hist.samples:
                    continue
                payload_tickers[tk] = {
                    'samples': list(hist.samples),
                    'last_cross_side':        hist.last_cross_side,
                    'last_diverge_side':      hist.last_diverge_side,
                    'last_diverge_start_ts':  hist.last_diverge_start_ts,
                    'last_alert_ts':          dict(hist.last_alert_ts),
                    'last_key_level_side':    hist.last_key_level_side,
                    'last_spike_side':        hist.last_spike_side,
                    'last_walls':             dict(hist.last_walls),
                    'last_walls_update_ts':   hist.last_walls_update_ts,
                }
        if not payload_tickers:
            return 0
        payload = {
            'version':  _HISTORY_STATE_VERSION,
            'saved_at': time.time(),
            'tickers':  payload_tickers,
        }
        try:
            os.makedirs(_HISTORY_STATE_DIR, exist_ok=True)
            with open(_HISTORY_STATE_TMP, 'w') as f:
                json.dump(payload, f, separators=(',', ':'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(_HISTORY_STATE_TMP, _HISTORY_STATE_FILE)
        except Exception as e:
            log.warning(f"[ALERT-STATE] save failed: {e}")
            return 0
        return len(payload_tickers)

    def load_history_state(self) -> int:
        """Load `_history` from disk if a fresh snapshot exists.

        Skips if file is older than `_HISTORY_RESTORE_MAX_AGE` (the rolling
        stats wouldn't reflect current market conditions). Returns # tickers
        restored.
        """
        if not os.path.exists(_HISTORY_STATE_FILE):
            return 0
        age = time.time() - os.path.getmtime(_HISTORY_STATE_FILE)
        if age > _HISTORY_RESTORE_MAX_AGE:
            log.info(f"[ALERT-STATE] file age {age:.0f}s > {_HISTORY_RESTORE_MAX_AGE:.0f}s, skipping")
            return 0
        try:
            with open(_HISTORY_STATE_FILE) as f:
                payload = json.load(f)
        except Exception as e:
            log.warning(f"[ALERT-STATE] load failed: {e}")
            return 0
        if payload.get('version') != _HISTORY_STATE_VERSION:
            log.warning(f"[ALERT-STATE] version mismatch (file={payload.get('version')}, code={_HISTORY_STATE_VERSION}), skipping")
            return 0
        tickers_data = payload.get('tickers') or {}
        if not isinstance(tickers_data, dict) or not tickers_data:
            return 0
        restored = 0
        with self._lock:
            for tk, snap in tickers_data.items():
                if not isinstance(snap, dict):
                    continue
                hist = self._history.setdefault(tk, _TickerHistory())
                samples = snap.get('samples') or []
                hist.samples.clear()
                for s in samples:
                    if isinstance(s, (list, tuple)) and len(s) >= 6:
                        hist.samples.append(tuple(s[:6]))
                hist.last_cross_side       = int(snap.get('last_cross_side') or 0)
                hist.last_diverge_side     = int(snap.get('last_diverge_side') or 0)
                hist.last_diverge_start_ts = float(snap.get('last_diverge_start_ts') or 0)
                hist.last_alert_ts         = dict(snap.get('last_alert_ts') or {})
                hist.last_key_level_side   = int(snap.get('last_key_level_side') or 0)
                hist.last_spike_side       = int(snap.get('last_spike_side') or 0)
                hist.last_walls            = dict(snap.get('last_walls') or {})
                hist.last_walls_update_ts  = float(snap.get('last_walls_update_ts') or 0)
                restored += 1
        saved_age = time.time() - float(payload.get('saved_at') or 0)
        log.info(f"[ALERT-STATE] restored {restored} tickers' history "
                 f"(snapshot was {saved_age:.0f}s old, file age {age:.0f}s)")
        return restored

    def _detect_cross(self, ticker, ts, hist) -> list[dict]:
        """0DTE curve crosses all-exp curve."""
        out = []
        if len(hist.samples) < 5:
            return out
        prev = hist.samples[-2]
        curr = hist.samples[-1]
        # prev_diff = prev.s0 - prev.sa, curr_diff = curr.s0 - curr.sa
        prev_diff = prev[1] - prev[2]
        curr_diff = curr[1] - curr[2]
        crossed_up = prev_diff <= 0 < curr_diff
        crossed_down = prev_diff >= 0 > curr_diff

        if not (crossed_up or crossed_down):
            return out
        # Absolute-dollar floor: don't alert on crosses where neither leg has
        # meaningful notional. Retail $0.01M crosses between 0DTE and all-exp
        # are not institutional signals — 0DTHero's log never shows them.
        if abs(curr_diff) < FLOW_CROSS_MIN_MAGNITUDE:
            return out
        # cooldown
        last_ts = hist.last_alert_ts.get('flow_cross', 0)
        if ts - last_ts < COOLDOWN_SEC:
            return out

        side = 1 if crossed_up else -1
        if side == hist.last_cross_side:
            return out  # same-direction cross chain, ignore
        hist.last_cross_side = side
        hist.last_alert_ts['flow_cross'] = ts

        return [{
            'type': 'flow_cross',
            'ticker': ticker,
            'direction': 'bullish' if crossed_up else 'bearish',
            'ts': ts,
            'magnitude_m': round(abs(curr_diff) / 1e6, 2),
            'label': f"{ticker} {'bullish' if crossed_up else 'bearish'} flow cross",
        }]

    def _detect_spike_dump(self, ticker, ts, hist) -> list[dict]:
        """Signed flow rate exceeds ±Nσ rolling threshold."""
        out = []
        if len(hist.samples) < 61:
            return out
        # Take last 61 samples → compute 60 per-second deltas
        recent = list(hist.samples)[-61:]
        rates_all = [recent[i][2] - recent[i - 1][2] for i in range(1, 61)]
        rates_0dte = [recent[i][1] - recent[i - 1][1] for i in range(1, 61)]

        # Current rate (last sample delta)
        curr_rate_all = rates_all[-1] if rates_all else 0
        curr_rate_0dte = rates_0dte[-1] if rates_0dte else 0

        # Rolling σ from earlier part of window
        mean_all, std_all = _stats(rates_all[:-5])   # exclude last 5s (current event)
        mean_0dte, std_0dte = _stats(rates_0dte[:-5])

        # Aggregate over the last 30s window for magnitude
        recent_30s_all = sum(rates_all[-30:])
        recent_30s_0dte = sum(rates_0dte[-30:])

        for bucket_name, recent, std, mean in [
            ('all exp', recent_30s_all, std_all * (30 ** 0.5), mean_all),
            ('0dte', recent_30s_0dte, std_0dte * (30 ** 0.5), mean_0dte),
        ]:
            if std < 1000:  # insufficient variance
                continue
            z = (recent - mean * 30) / std if std > 0 else 0
            # Absolute-dollar floor: 0DTHero's log shows spike/dump events ≥$50M
            # for all-exp and ≥$25M for 0DTE. Anything smaller is retail noise
            # that their platform never surfaces as an alert.
            abs_floor = (SPIKE_DUMP_MIN_MAGNITUDE_0DTE if bucket_name == '0dte'
                         else SPIKE_DUMP_MIN_MAGNITUDE)
            # Spike (positive): require both above-σ AND net positive magnitude
            if z > SIGMA_THRESHOLD_SPIKE and recent > 0 and recent >= abs_floor:
                last_ts = hist.last_alert_ts.get(f'spike_{bucket_name}', 0)
                if ts - last_ts < COOLDOWN_SEC:
                    continue
                hist.last_alert_ts[f'spike_{bucket_name}'] = ts
                hist.last_spike_side = +1
                out.append({
                    'type': 'spike',
                    'ticker': ticker,
                    'direction': 'bullish',
                    'ts': ts,
                    'magnitude_m': round(recent / 1e6, 2),
                    'bucket': bucket_name,
                    'sigma': round(z, 1),
                    'label': f"{ticker} spike +{recent / 1e6:.2f}M [{bucket_name}]",
                })
            # Dump (negative): require both below-σ AND net negative magnitude
            elif z < -SIGMA_THRESHOLD_SPIKE and recent < 0 and abs(recent) >= abs_floor:
                last_ts = hist.last_alert_ts.get(f'dump_{bucket_name}', 0)
                if ts - last_ts < COOLDOWN_SEC:
                    continue
                hist.last_alert_ts[f'dump_{bucket_name}'] = ts
                hist.last_spike_side = -1
                out.append({
                    'type': 'dump',
                    'ticker': ticker,
                    'direction': 'bearish',
                    'ts': ts,
                    'magnitude_m': round(recent / 1e6, 2),
                    'bucket': bucket_name,
                    'sigma': round(z, 1),
                    'label': f"{ticker} dump {recent / 1e6:.2f}M [{bucket_name}]",
                })
        return out

    def _detect_diverge_converge(self, ticker, ts, hist) -> list[dict]:
        """Spot direction vs signed flow direction over 5-min window."""
        out = []
        if len(hist.samples) < DIVERGE_WINDOW_SEC:
            return out
        first = hist.samples[-DIVERGE_WINDOW_SEC]
        curr = hist.samples[-1]
        # first = (ts, s0, sa, u0, ua, spot)
        spot_chg = curr[5] - first[5]
        flow_chg = curr[2] - first[2]

        if first[5] == 0:
            return out
        spot_pct = (spot_chg / first[5]) * 100

        diverging = False
        diverge_side = 0  # +1 bullish divergence (spot down, flow up), -1 bearish
        if abs(flow_chg) >= DIVERGE_MIN_MAGNITUDE and abs(spot_pct) >= DIVERGE_MIN_SPOT_MOVE_PCT:
            if spot_pct < 0 and flow_chg > 0:
                diverging = True; diverge_side = +1
            elif spot_pct > 0 and flow_chg < 0:
                diverging = True; diverge_side = -1

        # FLOW DIVERGENCE — new diverge state
        if diverging and hist.last_diverge_side != diverge_side:
            last_ts = hist.last_alert_ts.get('flow_divergence', 0)
            if ts - last_ts >= COOLDOWN_SEC:
                hist.last_diverge_side = diverge_side
                hist.last_diverge_start_ts = ts
                hist.last_alert_ts['flow_divergence'] = ts
                out.append({
                    'type': 'flow_divergence',
                    'ticker': ticker,
                    'direction': 'bullish' if diverge_side > 0 else 'bearish',
                    'ts': ts,
                    'magnitude_m': round(flow_chg / 1e6, 2),
                    'label': f"{ticker} {'bullish' if diverge_side > 0 else 'bearish'} flow divergence",
                })
        # FLOW CONVERGENCE — diverge state ends (spot and flow re-align)
        elif not diverging and hist.last_diverge_side != 0:
            last_ts = hist.last_alert_ts.get('flow_convergence', 0)
            if ts - last_ts >= COOLDOWN_SEC:
                prior_side = hist.last_diverge_side
                hist.last_diverge_side = 0
                hist.last_alert_ts['flow_convergence'] = ts
                out.append({
                    'type': 'flow_convergence',
                    'ticker': ticker,
                    'direction': 'bullish' if prior_side > 0 else 'bearish',
                    'ts': ts,
                    'duration_s': int(ts - hist.last_diverge_start_ts),
                    'label': f"{ticker} {'bullish' if prior_side > 0 else 'bearish'} flow convergence",
                })
        return out

    def _detect_bullish_volume(self, ticker, ts, hist) -> list[dict]:
        """Unsigned volume spike with positive signed flow."""
        out = []
        if len(hist.samples) < 61:
            return out
        recent = list(hist.samples)[-61:]
        vols = [recent[i][4] - recent[i - 1][4] for i in range(1, 61)]
        recent_30s = sum(vols[-30:])
        mean_v, std_v = _stats(vols[:-5])
        if std_v < 1000:
            return out
        z = (recent_30s - mean_v * 30) / (std_v * (30 ** 0.5))
        if z < SIGMA_THRESHOLD_VOLUME:
            return out
        # Absolute floor: only surface bullish-volume alerts at institutional
        # scale. Below $25M / 30s it's retail activity, not size.
        if recent_30s < BULLISH_VOLUME_MIN_MAGNITUDE:
            return out

        # Direction check: signed flow over same window must be positive.
        # Volume window is recent[30]..recent[60] (30 intervals via vols[-30:]),
        # so flow must span the SAME 30 intervals: recent[-31] to recent[-1].
        flow_all_chg = recent[-1][2] - recent[-31][2]
        if flow_all_chg <= 0:
            return out

        last_ts = hist.last_alert_ts.get('bullish_volume', 0)
        if ts - last_ts < COOLDOWN_SEC:
            return out
        hist.last_alert_ts['bullish_volume'] = ts
        return [{
            'type': 'bullish_volume',
            'ticker': ticker,
            'direction': 'bullish',
            'ts': ts,
            'magnitude_m': round(recent_30s / 1e6, 2),
            'label': f"{ticker} bullish volume",
        }]

    def _detect_key_level(self, ticker, ts, hist) -> list[dict]:
        """Spot crosses a wall/flip level with follow-through.

        Walls are fed in from schwab_bridge via update_walls(). Fires when the
        spot from ~5s ago was on one side of a level and the current spot is
        on the other side, with >=0.02% sustained move over the window
        (filters 1-tick jitter). Cooldown (60s per level) prevents oscillation
        fire-chains when spot hovers at a level.
        """
        out = []
        walls = hist.last_walls or {}
        if not walls or len(hist.samples) < 10:
            return out
        # 5-sample lookback (~5s) vs current. Wider than adjacent samples so
        # slow drifts into a level still register as a cross.
        prev = hist.samples[-6]
        curr = hist.samples[-1]
        prev_spot, curr_spot = prev[5], curr[5]
        if prev_spot <= 0 or curr_spot <= 0:
            return out
        spot_pct_move = abs(curr_spot - prev_spot) / prev_spot
        if spot_pct_move < 0.0002:  # 0.02% sustained over 5s
            return out

        for level_name, level in walls.items():
            if not level or level <= 0:
                continue
            crossed_up   = prev_spot <= level < curr_spot
            crossed_down = prev_spot >= level > curr_spot
            if not (crossed_up or crossed_down):
                continue

            # Cooldown per level (not per ticker) — so flipping between put and
            # call wall in the same minute can fire twice, but same level can't.
            cooldown_key = f'key_level_{level_name}'
            last_ts = hist.last_alert_ts.get(cooldown_key, 0)
            if ts - last_ts < COOLDOWN_SEC:
                continue

            # Direction semantics:
            #   call_wall crossed up   → bullish (breakout)
            #   call_wall crossed down → bearish (rejection)
            #   put_wall  crossed down → bearish (breakdown)
            #   put_wall  crossed up   → bullish (reclaim)
            #   flip      crossed up   → bullish (gamma regime flip positive)
            #   flip      crossed down → bearish (gamma regime flip negative)
            if level_name == 'call_wall':
                side = +1 if crossed_up else -1
                wall_label = 'call wall'
            elif level_name == 'put_wall':
                side = -1 if crossed_down else +1
                wall_label = 'put wall'
            else:
                side = +1 if crossed_up else -1
                wall_label = 'gamma flip'

            hist.last_alert_ts[cooldown_key] = ts
            hist.last_key_level_side = side
            direction = 'bullish' if side > 0 else 'bearish'
            out.append({
                'type': 'key_level',
                'ticker': ticker,
                'direction': direction,
                'ts': ts,
                'level': round(level, 2),
                'level_name': level_name,
                'label': f"{ticker} {direction} key level break @ {level:.2f} {wall_label}",
            })
        return out

    def _detect_wall_proximity(self, ticker, ts, hist, spot) -> list[dict]:
        """Spot within 0.3% of put/call wall — 'confluence trigger' signal.

        Companion to _detect_key_level: cross fires only on the break, this
        fires while price is *approaching* and sitting near a wall. 0DTHero's
        content repeatedly emphasizes "flow signal + wall proximity = trade",
        so this cell should flash BEFORE the break so a user can set up.

        Direction:
          near put_wall  → bullish  (floor defending)
          near call_wall → bearish  (ceiling capping)

        2026-05-04 GAMMA-REGIME GATE added:
          Empirical hit rate over 7d / 861 alerts = 11.5% (88.5% inverted!).
          Theory: walls function as S/R only in LONG-gamma regime (spot above
          gamma_flip) where dealers DAMPEN moves toward the wall. In SHORT-gamma
          (below flip), dealers AMPLIFY moves THROUGH walls, so the textbook
          "near wall = reversal" inverts.
          Fix: suppress wall_proximity when spot ≤ gamma_flip. Will re-measure
          hit rate over the next 7d to confirm. If still <50%, then invert
          direction outright.

        Cooldown: 120s per wall (passive-state signal, not a trigger).
        Inhibited if a cross just fired for the same wall within 60s.
        """
        out = []
        if spot <= 0:
            return out
        walls = hist.last_walls or {}
        if not walls:
            return out

        # ── GAMMA-REGIME GATE (added 2026-05-04) ──────────────────────────
        # In short-gamma regime (spot ≤ gamma_flip), walls fail as S/R because
        # dealer hedging amplifies trends through them. Suppress all
        # wall_proximity alerts in that regime.
        gamma_flip = walls.get('flip') or walls.get('gamma_flip') or 0
        if gamma_flip > 0 and spot <= gamma_flip:
            return out  # short-gamma — walls don't behave as S/R here

        PROXIMITY_PCT  = 0.003   # 0.3% band around the wall
        PROX_COOLDOWN  = 120     # one proximity alert per wall per 2 min

        for level_name in ('put_wall', 'call_wall'):
            level = walls.get(level_name) or 0
            if level <= 0:
                continue
            dist_pct = abs(spot - level) / level
            if dist_pct > PROXIMITY_PCT:
                continue

            # Geometry gate: fire only when the wall is still functioning as the
            # support/resistance it's named. If price has already broken through,
            # the wall is the OPPOSITE side (put_wall below spot → resistance)
            # and the bullish/bearish label would be inverted. Measured in the
            # legacy 375-outcome sample: 63/68 "bullish put_wall" fires had spot
            # BELOW the put wall — 3.2% hit rate, confirming geometry bug.
            if level_name == 'put_wall' and spot < level:
                continue  # broken below put → wall is now overhead resistance
            if level_name == 'call_wall' and spot > level:
                continue  # broken above call → wall is now underfoot support

            # Suppress proximity if the cross detector just fired for this wall.
            cross_key = f'key_level_{level_name}'
            cross_ts  = hist.last_alert_ts.get(cross_key, 0)
            if ts - cross_ts < 60:
                continue

            prox_key = f'wall_proximity_{level_name}'
            last_ts  = hist.last_alert_ts.get(prox_key, 0)
            if ts - last_ts < PROX_COOLDOWN:
                continue

            side = +1 if level_name == 'put_wall' else -1
            direction = 'bullish' if side > 0 else 'bearish'
            wall_label = 'put wall' if level_name == 'put_wall' else 'call wall'

            hist.last_alert_ts[prox_key] = ts
            hist.last_key_level_side = side
            out.append({
                'type':        'wall_proximity',
                'ticker':      ticker,
                'direction':   direction,
                'ts':          ts,
                'level':       round(level, 2),
                'level_name':  level_name,
                'magnitude_m': round(dist_pct * 100, 3),   # distance in %
                'sigma':       round(dist_pct * 100, 3),
                'label':       f"{ticker} near {wall_label} @ {level:.2f} ({dist_pct * 100:.2f}%)",
            })
        return out

    def update_walls(self, ticker: str, walls: dict) -> None:
        """Called by schwab_bridge whenever per-ticker zone_update is recomputed.

        Keeps a cached {put_wall, call_wall, flip} per ticker for the Key Level
        detector. Rejects stale/tiny updates to avoid wall-jitter alert storms
        (walls that move <0.3% within 60s are ignored).
        """
        if not walls or not ticker:
            return
        with self._lock:
            hist = self._history.setdefault(ticker, _TickerHistory())
            now = time.time()
            prev = hist.last_walls or {}
            # Jitter guard: reject micro-updates if we just updated recently.
            if prev and (now - hist.last_walls_update_ts) < 60:
                stable = True
                for k in ('put_wall', 'call_wall', 'flip'):
                    pv, nv = prev.get(k, 0) or 0, walls.get(k, 0) or 0
                    if pv and nv and abs(nv - pv) / pv > 0.003:
                        stable = False
                        break
                if stable:
                    return
            hist.last_walls = {
                'put_wall':        walls.get('put_wall')        or 0.0,
                'call_wall':       walls.get('call_wall')       or 0.0,
                'flip':            walls.get('flip')            or 0.0,
                'gamma_put_wall':  walls.get('gamma_put_wall')  or 0.0,
                'gamma_call_wall': walls.get('gamma_call_wall') or 0.0,
            }
            hist.last_walls_update_ts = now

    def get_state_matrix(self) -> dict:
        """Snapshot per-ticker last-known direction for each alert row.
        Used by /api/alerts/state to power the AI Panel 4×3 matrix UI.

        Cells decay to 'none' after MATRIX_TTL_SEC of silence for that
        (ticker, type) pair — prevents a 10am signal from misleading a
        noon trader who assumes the matrix shows current activity.
        """
        def label(side: int, alert_type_key: str, hist) -> str:
            last_ts = hist.last_alert_ts.get(alert_type_key, 0)
            # Special case: spike/dump have two keys (spike_<bucket>, dump_<bucket>)
            if alert_type_key == 'spike_dump':
                last_ts = max(
                    hist.last_alert_ts.get('spike_all exp', 0),
                    hist.last_alert_ts.get('spike_0dte', 0),
                    hist.last_alert_ts.get('dump_all exp', 0),
                    hist.last_alert_ts.get('dump_0dte', 0),
                )
            elif alert_type_key == 'key_level':
                last_ts = max(
                    hist.last_alert_ts.get('key_level_put_wall', 0),
                    hist.last_alert_ts.get('key_level_call_wall', 0),
                    hist.last_alert_ts.get('key_level_flip', 0),
                )
            if last_ts and (time.time() - last_ts) > MATRIX_TTL_SEC:
                return 'none'
            if side > 0: return 'bullish'
            if side < 0: return 'bearish'
            return 'none'
        with self._lock:
            return {
                t: {
                    'flow_cross':      label(h.last_cross_side,       'flow_cross',       h),
                    'flow_divergence': label(h.last_diverge_side,     'flow_divergence',  h),
                    'key_level':       label(h.last_key_level_side,   'key_level',        h),
                    'spike_dump':      label(h.last_spike_side,       'spike_dump',       h),
                }
                for t, h in self._history.items()
            }

    def _register_outcome(self, alert: dict, spot0: float) -> None:
        """Queue an alert for future spot-move stamping. Only directional
        alerts (bullish/bearish) are tracked."""
        direction = alert.get('direction')
        if direction not in ('bullish', 'bearish'):
            return
        if not spot0 or spot0 <= 0:
            return
        # Capture wall-distance at alert time so we can slice hit rates by
        # "at-wall vs not-at-wall" later. Two wall definitions tagged:
        #   OI wall    = strike with max open interest (historical positioning)
        #   gamma wall = strike with max dollar-gamma (live dealer hedging load)
        # The 0DTHero Feb-3-2025 chart defined the wall via gamma, not OI.
        # Raw distances only — threshold choice is an analysis decision.
        ticker = alert.get('ticker')
        hist = self._history.get(ticker)
        wall_tags = {
            'nearest_oi_wall_pct':     None,
            'nearest_oi_wall_name':    None,
            'nearest_gamma_wall_pct':  None,
            'nearest_gamma_wall_name': None,
        }
        if hist and hist.last_walls:
            def _nearest(names):
                best = None
                for name in names:
                    w = hist.last_walls.get(name) or 0.0
                    if w <= 0:
                        continue
                    dist_pct = abs(spot0 - w) / spot0 * 100.0
                    if best is None or dist_pct < best[0]:
                        best = (dist_pct, name)
                return best
            oi_best    = _nearest(('put_wall', 'call_wall', 'flip'))
            gamma_best = _nearest(('gamma_put_wall', 'gamma_call_wall'))
            if oi_best is not None:
                wall_tags['nearest_oi_wall_pct']  = round(oi_best[0], 4)
                wall_tags['nearest_oi_wall_name'] = oi_best[1]
            if gamma_best is not None:
                wall_tags['nearest_gamma_wall_pct']  = round(gamma_best[0], 4)
                wall_tags['nearest_gamma_wall_name'] = gamma_best[1]
        self._pending_outcomes.append({
            'alert': alert,
            'spot0': spot0,
            **wall_tags,
            'horizons': {h: None for h in _OUTCOME_HORIZONS_SEC},
        })

    def _process_pending_outcomes(self, ticker: str, now: float, spot: float) -> None:
        """For each pending outcome on this ticker, fill any horizon whose
        elapsed time has been crossed. Flush fully filled outcomes to JSONL
        and GC anything older than _OUTCOME_GC_MAX_AGE_SEC."""
        if not self._pending_outcomes:
            return
        keep = []
        for p in self._pending_outcomes:
            a = p['alert']
            if a.get('ticker') != ticker:
                keep.append(p)
                continue
            age = now - a.get('ts', now)
            if age > _OUTCOME_GC_MAX_AGE_SEC:
                # Stale — drop even if not all horizons filled
                continue
            for h in _OUTCOME_HORIZONS_SEC:
                if p['horizons'][h] is None and age >= h:
                    p['horizons'][h] = spot
            if all(v is not None for v in p['horizons'].values()):
                self._write_outcome(p)
            else:
                keep.append(p)
        self._pending_outcomes = keep

    def _write_outcome(self, p: dict) -> None:
        """Serialize a completed outcome to alert_outcomes.jsonl."""
        a = p['alert']
        spot0 = p['spot0']
        direction = a.get('direction')
        dir_sign = 1 if direction == 'bullish' else -1
        row = {
            'ts': a.get('ts'),
            'ticker': a.get('ticker'),
            'type': a.get('type'),
            'direction': direction,
            'magnitude_m': a.get('magnitude_m'),
            'bucket': a.get('bucket'),
            'sigma': a.get('sigma'),
            'spot0': spot0,
            'nearest_oi_wall_pct':     p.get('nearest_oi_wall_pct'),
            'nearest_oi_wall_name':    p.get('nearest_oi_wall_name'),
            'nearest_gamma_wall_pct':  p.get('nearest_gamma_wall_pct'),
            'nearest_gamma_wall_name': p.get('nearest_gamma_wall_name'),
        }
        for h, spot_h in p['horizons'].items():
            delta_pct = ((spot_h - spot0) / spot0) * 100 if spot0 else 0.0
            row[f'spot_{h}s'] = spot_h
            row[f'delta_{h}s_pct'] = round(delta_pct, 4)
            row[f'hit_{h}s'] = int(dir_sign * delta_pct > 0)
        try:
            os.makedirs(_LOG_DIR, exist_ok=True)
            with open(_OUTCOMES_LOG_PATH, 'a') as f:
                f.write(json.dumps(row) + '\n')
        except Exception as e:
            log.debug(f"[OUTCOME] persist failed: {e}")

    def get_hit_rates(self, last_n_days: int = 7) -> dict:
        """Aggregate JSONL outcomes into per-(ticker, type, bucket) hit rates
        per horizon. Returns shape:
            {ticker: {type: {bucket: {horizon_sec: {n, hit_rate, avg_move_pct, expectancy}}}}}
        """
        if not os.path.exists(_OUTCOMES_LOG_PATH):
            return {}
        cutoff = time.time() - last_n_days * 86400
        agg: dict = {}
        try:
            with open(_OUTCOMES_LOG_PATH, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if row.get('ts', 0) < cutoff:
                        continue
                    t = row.get('ticker') or '?'
                    ty = row.get('type') or '?'
                    bk = row.get('bucket') or '-'
                    node = agg.setdefault(t, {}).setdefault(ty, {}).setdefault(bk, {})
                    for h in _OUTCOME_HORIZONS_SEC:
                        hit_key = f'hit_{h}s'
                        delta_key = f'delta_{h}s_pct'
                        if hit_key not in row:
                            continue
                        b = node.setdefault(h, {'n': 0, 'hits': 0, 'sum_move': 0.0, 'sum_signed': 0.0})
                        b['n'] += 1
                        b['hits'] += int(row[hit_key])
                        delta = float(row.get(delta_key, 0.0))
                        b['sum_move'] += abs(delta)
                        dir_sign = 1 if row.get('direction') == 'bullish' else -1
                        b['sum_signed'] += dir_sign * delta
        except Exception as e:
            log.debug(f"[OUTCOME] read failed: {e}")
            return {}
        # Finalize: compute rates from accumulators
        for t, tymap in agg.items():
            for ty, bkmap in tymap.items():
                for bk, hmap in bkmap.items():
                    for h, b in hmap.items():
                        n = b['n']
                        b['hit_rate'] = round(b['hits'] / n, 3) if n else 0.0
                        b['avg_move_pct'] = round(b['sum_move'] / n, 3) if n else 0.0
                        b['expectancy_pct'] = round(b['sum_signed'] / n, 3) if n else 0.0
                        del b['sum_move']
                        del b['sum_signed']
        return agg

    def _emit(self, alert: dict) -> None:
        # Persist to daily JSONL file before emitting, so alerts survive a
        # server restart and the UI can replay today's flow on reconnect.
        try:
            os.makedirs(_LOG_DIR, exist_ok=True)
            with open(_daily_alert_log_path(alert.get('ts', 0)), 'a') as f:
                f.write(json.dumps(alert) + '\n')
        except Exception as e:
            log.debug(f"[ALERT] persist failed: {e}")
        if not self._socketio:
            return
        try:
            self._socketio.emit('flow_alert', alert)
        except Exception as e:
            log.debug(f"[ALERT] emit failed: {e}")


_engine: Optional[AlertEngine] = None


def get_engine() -> Optional[AlertEngine]:
    return _engine


def init_engine(socketio=None) -> AlertEngine:
    global _engine
    if _engine is None:
        _engine = AlertEngine(socketio=socketio)
        # Restore today's persisted alerts so the UI doesn't lose its history
        # across a server restart.
        try:
            n = _engine.load_from_disk()
            if n > 0:
                log.info(f"[ALERT] Restored {n} alerts from today's disk log")
        except Exception as e:
            log.debug(f"[ALERT] load_from_disk failed on init: {e}")
        # Restore rolling-window history so detectors skip the 15s warmup gate
        # and `last_alert_ts` cooldowns survive restarts. Then start the
        # background persist daemon (idempotent).
        try:
            n = _engine.load_history_state()
            if n > 0:
                log.info(f"[ALERT-STATE] Seeded {n} tickers' rolling history from disk")
        except Exception as e:
            log.warning(f"[ALERT-STATE] load on init failed: {e}")
        _start_history_persistence()
    return _engine


# ── History-state persistence daemon ────────────────────────────────
_history_persist_started = False


def _history_persist_loop() -> None:
    while True:
        try:
            time.sleep(_HISTORY_PERSIST_SEC)
            eng = _engine
            if eng is not None:
                eng.save_history_state()
        except Exception as e:
            log.warning(f"[ALERT-STATE] persist loop error: {e}")
            time.sleep(_HISTORY_PERSIST_SEC)


def _start_history_persistence() -> bool:
    """Idempotent — spawn the daemon at most once."""
    global _history_persist_started
    if _history_persist_started:
        return False
    _history_persist_started = True
    t = threading.Thread(target=_history_persist_loop, daemon=True,
                         name='alert-state-persist')
    t.start()
    log.info(f"[ALERT-STATE] persist daemon started ({_HISTORY_PERSIST_SEC:.0f}s cadence)")
    return True
