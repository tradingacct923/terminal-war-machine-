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


SIGMA_THRESHOLD_SPIKE = 2.5         # σ above rolling mean → spike
SIGMA_THRESHOLD_VOLUME = 2.0        # σ above for bullish_volume
DIVERGE_MIN_MAGNITUDE = 5_000_000   # $5M min signed flow to count a diverge
DIVERGE_MIN_SPOT_MOVE_PCT = 0.15    # min 0.15% spot move to count
DIVERGE_WINDOW_SEC = 300            # 5-min look-back for divergence detection
COOLDOWN_SEC = 60                   # min seconds between same-type alerts per ticker


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


class AlertEngine:
    """Per-ticker rolling-window alert detection."""

    def __init__(self, socketio=None):
        self._socketio = socketio
        self._history: dict[str, _TickerHistory] = {}
        # Rolling log of last N alerts (for diagnostic inspection)
        self._alert_log: deque = deque(maxlen=200)
        self._lock = threading.Lock()

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

        # Emit + log
        for a in alerts:
            self._alert_log.append(a)
            self._emit(a)
        return alerts

    def get_log(self, last_n: int = 50) -> list:
        """Return most recent N alerts fired."""
        return list(self._alert_log)[-last_n:]

    def get_sample_count(self, ticker: str) -> int:
        """How many samples has this ticker accumulated?"""
        h = self._history.get(ticker)
        return len(h.samples) if h else 0

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
            # Spike (positive): require both above-σ AND net positive magnitude
            if z > SIGMA_THRESHOLD_SPIKE and recent > 0:
                last_ts = hist.last_alert_ts.get(f'spike_{bucket_name}', 0)
                if ts - last_ts < COOLDOWN_SEC:
                    continue
                hist.last_alert_ts[f'spike_{bucket_name}'] = ts
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
            elif z < -SIGMA_THRESHOLD_SPIKE and recent < 0:
                last_ts = hist.last_alert_ts.get(f'dump_{bucket_name}', 0)
                if ts - last_ts < COOLDOWN_SEC:
                    continue
                hist.last_alert_ts[f'dump_{bucket_name}'] = ts
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

        # Direction check: signed flow over same window must be positive
        flow_all_chg = recent[-1][2] - recent[-30][2]
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

    def _emit(self, alert: dict) -> None:
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
    return _engine
