"""
Hedge Forecaster — projected dealer hedge flow over 5/15/30 min windows.

Output: signed predicted equity-side hedge flow (shares to BUY or SELL) at three
forecast horizons, computed as:

    hp_gamma_shares_1pct × (ΔS_pct / 0.01)
    where ΔS_pct = (velocity × T) / spot
          velocity = (spot[T_now] − spot[T_now − 60s]) / 60s
          T = 5/15/30 min

Sign convention (matches greek_surface.export_hedge_pressure):
  hp_gamma_shares_1pct > 0 → dealers BUY on price rise (short-γ regime)
  hp_gamma_shares_1pct < 0 → dealers SELL on price rise (long-γ regime)

Confidence components:
  velocity_stability:   sigmoid of (1 / (1 + CV)) where CV = std/|mean| over 5min
  distance_to_flip:     1 − exp(−|spot − gamma_flip| / 20.0) — closer to flip = lower conf
  combined_confidence:  geometric mean of the two

Validation field:
  observed_5min_actual: sum(signed_shares) in _recent_equity_prints over last 5 min
  Compared against forecasts.5min.shares offline to compute calibration ratio.

Outcome ledger:
  logs/hedge_forecast_outcomes_YYYYMMDD.jsonl — per-cycle records
  {ts, spot, velocity_per_sec, forecast_5min_shares, observed_5min_actual,
   forecast_15min_shares, forecast_30min_shares, confidence}
  Reconcile observed_5min_actual vs forecast_5min_shares to derive calibration ratio.

Source modules (all existing — no re-streaming):
  - schwab_bridge._latest_qqq                live spot
  - schwab_bridge._greek_surface             per-strike Γ → totals
  - schwab_bridge._recent_equity_prints      observed equity flow validation
  - wall_signals._walls['QQQ']               gamma_flip
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ── CONFIGURED constants (categorized in MEASURED_VALUES.md) ────────────────
VELOCITY_WINDOW_SEC          = 60        # CONFIGURED — last 60s for velocity calc
VELOCITY_HISTORY_SEC         = 300       # CONFIGURED — last 5min for stability/CV
SPOT_HISTORY_CAP             = 240       # CONFIGURED — 5s × 240 = 20 min ring
FORECAST_WINDOWS_SEC         = (300, 900, 1800)  # CONFIGURED — 5/15/30 min windows
VELOCITY_STABLE_CV_CUTOFF    = 0.50      # CONFIGURED — CV < this = "stable" boolean flag
DISTANCE_FLIP_HALFLIFE_USD   = 20.0      # CONFIGURED — confidence half-life vs |spot − flip|
OBSERVATION_WINDOW_SEC       = 300       # CONFIGURED — 5min observed-actual lookback

# ── Module state ────────────────────────────────────────────────────────────
# Per-ticker spot history: deque of (ts, spot)
_spot_history: dict = defaultdict(lambda: deque(maxlen=SPOT_HISTORY_CAP))
_state_cache: dict = {}
_state_lock = threading.RLock()

# Outcome ledger
_ledger_fh = None
_ledger_date: str = ''
_ledger_lock = threading.Lock()

# ── Paired-validation queue (added 2026-05-04) ──
# The legacy `_write_ledger` records forecast(T) AND observed_5min_actual(T-300:T)
# at the same row — wrong window. To validate, we need forecast(T) paired with
# observed actual flow over [T, T+300]. This deque holds in-flight forecasts;
# on each compute we mature any whose window has elapsed and write a paired
# record to logs/hedge_forecast_paired_*.jsonl.
_pending_pairs: dict = defaultdict(deque)   # ticker → deque[(ts, fc_5m, fc_15m, fc_30m, spot)]
_paired_ledger_fh = None
_paired_ledger_date: str = ''
_paired_ledger_lock = threading.Lock()
PAIR_QUEUE_MAX = 500   # ~42 min at 5s cadence (covers 30-min window with margin)


def _empty_state(ticker: str, reason: str = '') -> dict:
    return {
        'ticker':                 ticker,
        'spot':                   0.0,
        'velocity_per_sec':       0.0,
        'velocity_stable':        False,
        'velocity_cv':            None,
        'distance_to_flip':       None,
        'gamma_flip':             None,
        'hp_gamma_shares_1pct':   0.0,
        'forecasts': {
            '5min':  {'shares': None, 'side': None, 'confidence': 0.0, 'window_sec': 300},
            '15min': {'shares': None, 'side': None, 'confidence': 0.0, 'window_sec': 900},
            '30min': {'shares': None, 'side': None, 'confidence': 0.0, 'window_sec': 1800},
        },
        'observed_5min_actual':   None,
        'observed_5min_count':    0,
        'data_ts':                0.0,
        'server_time':            time.time(),
        'reason':                 reason,
    }


def _compute_velocity(ticker: str, now_ts: float) -> tuple:
    """Return (velocity_per_sec, cv, num_samples_in_window).

    Velocity = (spot_now - spot_60s_ago) / 60.
    CV = std/|mean| of spot deltas over last 5 min (stability metric).
    """
    history = _spot_history.get(ticker)
    if not history or len(history) < 2:
        return (0.0, None, 0)

    # Filter to window
    cutoff = now_ts - VELOCITY_HISTORY_SEC
    window = [(ts, sp) for ts, sp in history if ts >= cutoff and sp > 0]
    if len(window) < 2:
        return (0.0, None, len(window))

    # Velocity from first sample within VELOCITY_WINDOW_SEC vs latest
    vel_cutoff = now_ts - VELOCITY_WINDOW_SEC
    older = [(ts, sp) for ts, sp in window if ts <= vel_cutoff]
    if older and len(window) >= 2:
        # Most recent older sample, paired with newest
        oldest_in_vel = older[-1]
        newest = window[-1]
        dt = newest[0] - oldest_in_vel[0]
        if dt > 0:
            velocity_per_sec = (newest[1] - oldest_in_vel[1]) / dt
        else:
            velocity_per_sec = 0.0
    else:
        # Fallback — use first vs last in whatever window we have
        dt = window[-1][0] - window[0][0]
        velocity_per_sec = ((window[-1][1] - window[0][1]) / dt) if dt > 0 else 0.0

    # CV: stdev of consecutive-delta velocities over the 5-min window
    cv = None
    if len(window) >= 4:
        deltas = []
        for i in range(1, len(window)):
            dt = window[i][0] - window[i-1][0]
            if dt > 0:
                deltas.append((window[i][1] - window[i-1][1]) / dt)
        if len(deltas) >= 3:
            try:
                mean = statistics.mean(deltas)
                std = statistics.stdev(deltas)
                if abs(mean) > 1e-9:
                    cv = std / abs(mean)
                else:
                    # near-zero mean: report std relative to spot (alt CV)
                    spot_now = window[-1][1] if window else 1.0
                    cv = std / max(spot_now, 1.0)
            except Exception:
                cv = None

    return (velocity_per_sec, cv, len(window))


def _observed_actual_5min(ticker: str, now_ts: float) -> tuple:
    """Sum signed shares from _recent_equity_prints over last OBSERVATION_WINDOW_SEC.

    Returns (signed_shares, num_prints).
    """
    try:
        from background_engine import schwab_bridge as _sb
        if not hasattr(_sb, 'lookup_equity_window'):
            return (None, 0)
        ts_end_ms = int(now_ts * 1000)
        ts_start_ms = ts_end_ms - OBSERVATION_WINDOW_SEC * 1000
        prints = _sb.lookup_equity_window(ticker, ts_start_ms, ts_end_ms)
        if not prints:
            return (0, 0)
        # Each tuple: (ts_ms, price, size, exec_mic, side_sign)
        total = 0
        for p in prints:
            if len(p) >= 5:
                total += int(p[4]) * int(p[2])  # side_sign × size
        return (total, len(prints))
    except Exception:
        return (None, 0)


def compute_forecast(ticker: str) -> dict:
    """Compute hedge flow forecast for `ticker`. Cached in `_state_cache`.

    Called from schwab_bridge._intel_compute_loop every 5s during RTH.
    Also pushes via Socket.IO 'intel:hedge_forecast'.
    """
    ticker = (ticker or '').upper()
    if not ticker:
        return _empty_state(ticker, reason='no_ticker')

    try:
        from background_engine import schwab_bridge as _sb
    except Exception as e:
        return _empty_state(ticker, reason=f'sb_import_err:{e}')

    # 1) Live spot
    spot = 0.0
    try:
        if ticker == 'QQQ':
            spot = float(getattr(_sb, '_latest_qqq', 0.0) or 0.0)
        else:
            spots = getattr(_sb, '_latest_spot_by_ticker', {}) or {}
            spot = float(spots.get(ticker, 0.0) or 0.0)
    except Exception:
        spot = 0.0
    if spot <= 0:
        return _empty_state(ticker, reason='no_spot')

    now_ts = time.time()

    # 2) Append to spot history (for velocity)
    with _state_lock:
        _spot_history[ticker].append((now_ts, spot))

    # 3) Compute velocity + stability
    velocity_per_sec, cv, num_samples = _compute_velocity(ticker, now_ts)
    velocity_stable = (cv is not None) and (cv < VELOCITY_STABLE_CV_CUTOFF)

    # 4) Get hp_gamma_shares_1pct from greek_surface (existing pipeline)
    gs = getattr(_sb, '_greek_surface', None)
    hp_gamma_shares_1pct = 0.0
    data_ts = 0.0
    if gs is not None and ticker == 'QQQ':
        try:
            hp = gs.export_hedge_pressure(spot)
            totals = (hp or {}).get('totals') or {}
            hp_gamma_shares_1pct = float(totals.get('hp_gamma_shares_1pct', 0) or 0)
            data_ts = float((hp or {}).get('ts', 0) or 0)
        except Exception:
            pass

    # 5) Get gamma_flip from wall_signals
    gamma_flip = None
    try:
        from connectors import wall_signals as _ws
        w = (getattr(_ws, '_walls', {}) or {}).get(ticker) or {}
        gf = w.get('gamma_flip')
        if isinstance(gf, (int, float)) and gf > 0:
            gamma_flip = float(gf)
    except Exception:
        pass

    distance_to_flip = abs(spot - gamma_flip) if (gamma_flip is not None and gamma_flip > 0) else None

    # 6) Per-window forecast
    forecasts: dict = {}
    for window_sec in FORECAST_WINDOWS_SEC:
        delta_s = velocity_per_sec * window_sec
        delta_s_pct = delta_s / spot if spot > 0 else 0.0
        # forecast_shares = hp_gamma_shares_1pct × (delta_s_pct / 0.01)
        forecast_shares = hp_gamma_shares_1pct * (delta_s_pct / 0.01) if abs(delta_s_pct) > 0 else 0.0

        # Side
        if abs(forecast_shares) < 1.0:
            side = 'FLAT'
        elif forecast_shares > 0:
            side = 'BUY'
        else:
            side = 'SELL'

        # Confidence components
        # Velocity stability — sigmoid: cv=0 → 1.0, cv=1 → 0.5, cv=∞ → 0
        if cv is not None:
            cv_score = 1.0 / (1.0 + cv)
        else:
            cv_score = 0.0

        # Distance-to-flip score — closer to flip = lower confidence (regime can switch)
        if distance_to_flip is not None:
            distance_score = 1.0 - math.exp(-distance_to_flip / DISTANCE_FLIP_HALFLIFE_USD)
        else:
            distance_score = 0.5  # unknown flip = mid confidence

        # Combined confidence (geometric mean) — degrades with longer horizon
        # 5min: full conf, 15min: 0.85x, 30min: 0.70x (CONFIGURED time-decay penalty)
        horizon_factor = {300: 1.00, 900: 0.85, 1800: 0.70}.get(window_sec, 0.50)
        combined = math.sqrt(max(0.0, cv_score) * max(0.0, distance_score)) * horizon_factor

        key = f'{int(window_sec/60)}min'
        forecasts[key] = {
            'shares':       round(forecast_shares, 1),
            'side':         side,
            'confidence':   round(combined, 4),
            'window_sec':   window_sec,
            'predicted_delta_s_pct': round(delta_s_pct * 100, 4),  # in percent
            'predicted_delta_s_usd': round(delta_s, 4),
        }

    # 7) Observed actual (last 5 min equity flow)
    observed_signed_shares, observed_count = _observed_actual_5min(ticker, now_ts)

    # 8) Build state + cache
    state = {
        'ticker':                 ticker,
        'spot':                   round(spot, 4),
        'velocity_per_sec':       round(velocity_per_sec, 6),
        'velocity_per_min':       round(velocity_per_sec * 60.0, 4),
        'velocity_stable':        bool(velocity_stable),
        'velocity_cv':            round(cv, 4) if cv is not None else None,
        'velocity_samples':       num_samples,
        'distance_to_flip':       round(distance_to_flip, 4) if distance_to_flip is not None else None,
        'gamma_flip':             gamma_flip,
        'hp_gamma_shares_1pct':   round(hp_gamma_shares_1pct, 1),
        'forecasts':              forecasts,
        'observed_5min_actual':   observed_signed_shares,
        'observed_5min_count':    observed_count,
        'observation_window_sec': OBSERVATION_WINDOW_SEC,
        'data_ts':                data_ts,
        'server_time':            now_ts,
        'reason':                 None,
    }

    with _state_lock:
        _state_cache[ticker] = state

    # 9) Outcome ledger (legacy — same-ts forecast+backward-observed)
    _write_ledger({
        'ts':                          now_ts,
        'ticker':                      ticker,
        'spot':                        state['spot'],
        'velocity_per_sec':            state['velocity_per_sec'],
        'velocity_cv':                 state['velocity_cv'],
        'hp_gamma_shares_1pct':        state['hp_gamma_shares_1pct'],
        'forecast_5min_shares':        forecasts['5min']['shares'],
        'forecast_15min_shares':       forecasts['15min']['shares'],
        'forecast_30min_shares':       forecasts['30min']['shares'],
        'observed_5min_actual':        observed_signed_shares,
        'observed_5min_count':         observed_count,
        'distance_to_flip':            state['distance_to_flip'],
    })

    # 9b) Paired-validation ledger (added 2026-05-04 — fc(T) vs observed [T,T+300])
    try:
        _enqueue_pending(ticker, now_ts,
                         forecasts['5min']['shares'],
                         forecasts['15min']['shares'],
                         forecasts['30min']['shares'],
                         state['spot'])
        _flush_matured_pairs(ticker, now_ts)
    except Exception as e:
        log.debug(f"[HEDGE-FC] paired-validation err: {e}")

    return state


def get_state(ticker: str) -> dict:
    """REST handler — return cached state (or compute fresh if cache empty)."""
    ticker = (ticker or '').upper()
    with _state_lock:
        cached = _state_cache.get(ticker)
        if cached:
            return cached
    return compute_forecast(ticker)


# ── Outcome ledger (per-day JSONL) ───────────────────────────────────────────

def _ledger_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f'hedge_forecast_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append per-cycle forecast/observation record. Used offline to compute
    calibration ratio: ratio = observed_5min_actual / forecast_5min_shares,
    targeted to converge to ~1.0 over time."""
    global _ledger_fh, _ledger_date
    today = datetime.now().strftime('%Y%m%d')
    with _ledger_lock:
        if _ledger_fh is None or _ledger_date != today:
            try:
                if _ledger_fh is not None:
                    _ledger_fh.close()
            except Exception:
                pass
            _ledger_fh = open(_ledger_path(), 'a', buffering=1)
            _ledger_date = today
        try:
            _ledger_fh.write(json.dumps(record, separators=(',', ':')) + '\n')
        except Exception as e:
            log.debug(f"[HEDGE-FC] ledger write err: {e}")


# ── Paired ledger (added 2026-05-04) ─────────────────────────────────────────
# This is the "correct" ledger — forecast(T) paired with observed actual flow
# computed over [T, T+window]. Used for valid calibration metrics.

def _paired_ledger_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f'hedge_forecast_paired_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_paired(record: dict) -> None:
    """Append a paired (forecast, forward-window observed) record."""
    global _paired_ledger_fh, _paired_ledger_date
    today = datetime.now().strftime('%Y%m%d')
    with _paired_ledger_lock:
        if _paired_ledger_fh is None or _paired_ledger_date != today:
            try:
                if _paired_ledger_fh is not None:
                    _paired_ledger_fh.close()
            except Exception:
                pass
            _paired_ledger_fh = open(_paired_ledger_path(), 'a', buffering=1)
            _paired_ledger_date = today
        try:
            _paired_ledger_fh.write(json.dumps(record, separators=(',', ':')) + '\n')
        except Exception as e:
            log.debug(f"[HEDGE-FC] paired ledger write err: {e}")


def _enqueue_pending(ticker: str, ts: float, fc_5m: float, fc_15m: float,
                      fc_30m: float, spot: float) -> None:
    """Add the just-computed forecast to the in-flight queue."""
    q = _pending_pairs[ticker]
    q.append((ts, fc_5m, fc_15m, fc_30m, spot))
    while len(q) > PAIR_QUEUE_MAX:
        q.popleft()


def _flush_matured_pairs(ticker: str, now_ts: float) -> int:
    """Pop any forecasts whose 5-min window has elapsed; compute observed
    actual flow over [forecast_ts, forecast_ts+300] and write paired record.
    Returns number of pairs written.
    """
    q = _pending_pairs.get(ticker)
    if not q: return 0
    written = 0
    while q and (now_ts - q[0][0]) >= 305:   # 5s slack so we don't pair too early
        ts0, fc_5m, fc_15m, fc_30m, spot0 = q.popleft()
        # Compute observed actual flow over [ts0, ts0+300]
        try:
            from background_engine.schwab_bridge import lookup_equity_window
            ts_start_ms = int(ts0 * 1000)
            ts_end_ms   = int((ts0 + 300) * 1000)
            prints = lookup_equity_window(ticker, ts_start_ms, ts_end_ms)
            # Each print: (ts_ms, price, size, mic, side_sign)
            obs_signed_shares = sum(int(p[2]) * int(p[4]) for p in prints if len(p) >= 5)
            obs_count = len(prints)
        except Exception as e:
            obs_signed_shares = None; obs_count = 0
        # Sign-hit: was forecast direction right?
        sign_match = None
        if obs_signed_shares is not None and fc_5m and obs_signed_shares != 0:
            sign_match = (fc_5m > 0) == (obs_signed_shares > 0)
        _write_paired({
            'forecast_ts':           ts0,
            'pair_ts':               now_ts,
            'ticker':                ticker,
            'spot_at_forecast':      spot0,
            'forecast_5min_shares':  fc_5m,
            'forecast_15min_shares': fc_15m,
            'forecast_30min_shares': fc_30m,
            'observed_5min_shares':  obs_signed_shares,
            'observed_5min_count':   obs_count,
            'sign_match':            sign_match,
            'calibration_ratio':     (obs_signed_shares / fc_5m) if (fc_5m and obs_signed_shares is not None and abs(fc_5m) > 1) else None,
        })
        written += 1
    return written
