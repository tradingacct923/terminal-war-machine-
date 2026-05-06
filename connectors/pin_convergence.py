"""
Pin Convergence — End-of-day pin location prediction.

The "pin" is the strike where 0DTE price is mechanically pulled toward at
expiration due to dealer gamma exposure. As price approaches a high-OI strike,
dealers (short Γ on 0DTE) hedge AGAINST momentum, dampening volatility and
pulling price toward the strike. Pin pull strengthens dramatically in the
last 30 min as Γ exposure peaks.

Output: per-strike pin_probability + weighted-mean pin_estimate +
confidence band (CI low/high) + time-evolution history for trajectory display.

Inputs (all categorized below — zero re-streaming, all from existing modules):
  - greek_surface.export_hedge_pressure(spot).strikes  per-strike dn_gamma + OI
  - wall_signals._walls[ticker]                         max_pain / gamma_flip / walls
  - mm_attribution._session_open_epoch_for(ts)         09:30 ET session boundary
  - schwab_bridge._latest_qqq                           live spot

Pin score formula (DERIVED — components in MEASURED_VALUES.md):
  gamma_score    = |dn_gamma| / max_|dn_gamma|             [0..1]
  distance_score = exp(-(|K-spot|/5.0)^2)                  [0..1] Gaussian
  oi_score       = (oi_call + oi_put) / max_total_oi       [0..1]
  warehouse_strength = oi_score (v1 simplification)        [0..1]

  time_amp:
    if t_remaining < 1800s: 1.0 + (1800 - t_remaining)/1800   ← 1.0→2.0 pull amplification
    else:                   1.0

  pin_score(K) = (gamma_score * 0.40 +
                  distance_score * 0.30 +
                  oi_score * 0.15 +
                  warehouse_strength * 0.15) * time_amp

  pin_probability(K) = pin_score(K) / Σ pin_score (normalized over analysis band)

Pin estimate (DERIVED):
  pin_estimate = Σ (K × pin_probability(K))                 weighted mean
  pin_confidence = max(pin_probability)                     concentration metric
  weighted_std  = sqrt(Σ pin_probability × (K - pin_estimate)^2)
  ci_low/high   = pin_estimate ± 2 × weighted_std           95% CI

Outcome ledger:
  logs/pin_outcomes_YYYYMMDD.jsonl — per-cycle records {ts, ticker, pin_estimate,
  pin_confidence, ci_low, ci_high, spot, time_remaining_sec}. Reconcile against
  actual_close offline to validate.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ── CONFIGURED constants (categorized in MEASURED_VALUES.md) ────────────────
ANALYSIS_BAND_DOLLARS  = 15.0    # MEASURED — captures 90.5% of 0DTE QQQ volume per chain audit
DISTANCE_GAUSSIAN_SIGMA = 5.0    # MEASURED — P50 of QQQ last-hour drift across recent sessions

# 2026-05-04 — LAST-HOUR MECHANICAL PULL (improved from prior linear last-30-min):
#   Old logic: linear ramp 1.0 → 2.0 over the final 30 min.
#   Problem:   gamma-pinning literature (Avellaneda 2002, De Prado 2018) shows
#              the pin force scales like 1/√(t) as time → 0, NOT linearly.
#   New logic: amp = √(LAST_HOUR / max(t, FLOOR))  applied throughout the
#              last 60 min. FLOOR=300s caps the amplifier to ~3.46× to avoid
#              singular blowup in the final seconds.
#   Calibration check: re-run scripts/regression_runner.py — pin_convergence
#                     R² should rise from prior 0.62 baseline.
TIME_AMP_THRESHOLD_SEC  = 3600   # CONFIGURED — last-60-min mechanical-pull window (was 1800)
TIME_AMP_FLOOR_SEC      = 300    # CONFIGURED — floor on time_remaining inside √-ramp; caps amp at √12 ≈ 3.46

WEIGHT_GAMMA            = 0.40   # CONFIGURED — initial best-guess; tune from outcome ledger
WEIGHT_DISTANCE         = 0.30
WEIGHT_OI               = 0.15
WEIGHT_WAREHOUSE        = 0.15
PIN_HISTORY_CAP         = 480    # CONFIGURED — 2hr @ 15s cadence (UI time-evolution chart)

# Session-close in ET — 16:00 (= 09:30 + 6.5h = 23,400 sec after open)
SESSION_OPEN_TO_CLOSE_SEC = int(6.5 * 3600)

# ── Module state ────────────────────────────────────────────────────────────
_state_cache: dict = {}                          # ticker → state dict
_pin_history: dict = defaultdict(lambda: deque(maxlen=PIN_HISTORY_CAP))
_state_lock = threading.RLock()

# Outcome ledger
_ledger_fh = None
_ledger_date: str = ''
_ledger_lock = threading.Lock()


def seconds_until_session_close(now_ts: Optional[float] = None) -> float:
    """Return seconds until 16:00 ET on the trading day containing now_ts.

    Negative values clamp to 0 (post-close). Reuses
    mm_attribution._session_open_epoch_for to avoid duplicating timezone logic.
    """
    if now_ts is None:
        now_ts = time.time()
    try:
        from connectors.mm_attribution import _session_open_epoch_for
        open_ts = _session_open_epoch_for(now_ts)
        close_ts = open_ts + SESSION_OPEN_TO_CLOSE_SEC
        return max(0.0, close_ts - now_ts)
    except Exception:
        return 0.0


def _empty_state(ticker: str, reason: str = '') -> dict:
    """Return a structured 'no data' state. Caller still gets a valid envelope
    so frontend can render an empty/awaiting-data view (not a 500 error)."""
    return {
        'ticker':              ticker,
        'spot':                0.0,
        'time_remaining_sec':  seconds_until_session_close(),
        'pin_estimate':        None,
        'pin_confidence':      None,
        'expected_close':      None,
        'ci_low':              None,
        'ci_high':              None,
        'strikes':             [],
        'walls':               {},
        'data_ts':             0.0,
        'server_time':         time.time(),
        'reason':              reason,
        'history':             [],
    }


def compute_pin_state(ticker: str) -> dict:
    """Compute pin state for `ticker`. Cached + history-tracked.

    Returns the full state dict (also pushed via Socket.IO 'intel:pin_update').
    """
    ticker = (ticker or '').upper()
    if not ticker:
        return _empty_state(ticker, reason='no_ticker')

    # Lazy imports to avoid circular dependencies at module load time
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

    # 2) Per-strike data from greek_surface
    gs = getattr(_sb, '_greek_surface', None)
    if gs is None:
        return _empty_state(ticker, reason='no_greek_surface')
    try:
        hp = gs.export_hedge_pressure(spot) if ticker == 'QQQ' else None
    except Exception as e:
        return _empty_state(ticker, reason=f'hp_err:{e}')
    if not hp or not hp.get('strikes'):
        return _empty_state(ticker, reason='hp_empty')

    strikes_raw = hp.get('strikes', [])
    data_ts = float(hp.get('ts', 0.0) or 0.0)

    # 3) Filter to analysis band (ATM ±$ANALYSIS_BAND_DOLLARS) with non-zero OI
    atm_band = []
    for s in strikes_raw:
        try:
            K = float(s.get('K', 0))
            if K <= 0:
                continue
            if abs(K - spot) > ANALYSIS_BAND_DOLLARS:
                continue
            oi = int(s.get('oi_call', 0) or 0) + int(s.get('oi_put', 0) or 0)
            if oi <= 0 and (s.get('dn_gamma', 0) or 0) == 0:
                continue
            atm_band.append({
                'K':        K,
                'dn_gamma': float(s.get('dn_gamma', 0) or 0),
                'oi_call':  int(s.get('oi_call', 0) or 0),
                'oi_put':   int(s.get('oi_put', 0) or 0),
                'oi_total': oi,
            })
        except Exception:
            continue

    if not atm_band:
        return _empty_state(ticker, reason='no_atm_band_data')

    # 4) Normalization references (within the analysis band)
    max_gamma = max((abs(s['dn_gamma']) for s in atm_band), default=0)
    max_oi = max((s['oi_total'] for s in atm_band), default=0)
    if max_gamma <= 0 and max_oi <= 0:
        return _empty_state(ticker, reason='zero_max_norms')

    time_remaining = seconds_until_session_close()
    # 2026-05-04 — √-ramp pin force per gamma-pinning literature (was linear 1→2 over 30min).
    if time_remaining < TIME_AMP_THRESHOLD_SEC:
        effective_t = max(time_remaining, TIME_AMP_FLOOR_SEC)
        time_amp = math.sqrt(TIME_AMP_THRESHOLD_SEC / effective_t)
    else:
        time_amp = 1.0

    # 5) Per-strike pin score
    # Warehouse strength upgrade (Phase 8): try to pull MEASURED commitment
    # score from dealer_warehouse. Fall back to oi_score proxy if unavailable
    # (coverage is limited to OPTIONS_BOOK budget — 120 contracts).
    warehouse_provider = None
    warehouse_max = 0.0
    try:
        from connectors import dealer_warehouse as _dw
        warehouse_provider = _dw.get_warehouse_strength
        # Pre-pass: find max commitment score in band so we can normalize
        for s in atm_band:
            try:
                v = warehouse_provider(s['K'])
                if isinstance(v, (int, float)) and v > warehouse_max:
                    warehouse_max = float(v)
            except Exception:
                pass
    except Exception:
        warehouse_provider = None

    scored = []
    for s in atm_band:
        K = s['K']
        gamma_score    = (abs(s['dn_gamma']) / max_gamma) if max_gamma > 0 else 0.0
        distance       = abs(K - spot)
        distance_score = math.exp(-((distance / DISTANCE_GAUSSIAN_SIGMA) ** 2))
        oi_score       = (s['oi_total'] / max_oi) if max_oi > 0 else 0.0

        # Warehouse strength: MEASURED if dealer_warehouse has coverage,
        # else fall back to oi_score proxy (v1 behaviour).
        warehouse_source = 'oi_proxy'
        warehouse = oi_score
        if warehouse_provider is not None and warehouse_max > 0:
            try:
                wv = warehouse_provider(K)
                if isinstance(wv, (int, float)) and wv > 0:
                    warehouse = float(wv) / warehouse_max
                    warehouse_source = 'measured'
            except Exception:
                pass

        score = (gamma_score * WEIGHT_GAMMA +
                 distance_score * WEIGHT_DISTANCE +
                 oi_score * WEIGHT_OI +
                 warehouse * WEIGHT_WAREHOUSE) * time_amp

        scored.append({
            'K':                  K,
            'dn_gamma':           round(s['dn_gamma'], 4),
            'oi_call':            s['oi_call'],
            'oi_put':             s['oi_put'],
            'oi_total':           s['oi_total'],
            'gamma_score':        round(gamma_score, 4),
            'distance_score':     round(distance_score, 4),
            'oi_score':           round(oi_score, 4),
            'warehouse_strength': round(warehouse, 4),
            'warehouse_source':   warehouse_source,
            'time_amplifier':     round(time_amp, 4),
            'pin_score':          round(score, 6),
        })

    # 6) Normalize → pin_probability
    total_score = sum(s['pin_score'] for s in scored)
    if total_score <= 0:
        return _empty_state(ticker, reason='zero_total_score')
    for s in scored:
        s['pin_probability'] = round(s['pin_score'] / total_score, 4)

    # 7) Pin estimate + confidence + CI band
    pin_estimate = sum(s['K'] * s['pin_probability'] for s in scored)
    pin_confidence = max(s['pin_probability'] for s in scored)
    weighted_var = sum(s['pin_probability'] * (s['K'] - pin_estimate) ** 2 for s in scored)
    weighted_std = math.sqrt(max(0.0, weighted_var))

    # 8) Walls overlay
    walls_overlay = {}
    try:
        from connectors import wall_signals as _ws
        w = (getattr(_ws, '_walls', {}) or {}).get(ticker) or {}
        walls_overlay = {
            'max_pain':   w.get('max_pain'),
            'gamma_flip': w.get('gamma_flip'),
            'call_wall':  w.get('call_wall'),
            'put_wall':   w.get('put_wall'),
            'gamma_call_wall': w.get('gamma_call_wall'),
            'gamma_put_wall':  w.get('gamma_put_wall'),
        }
    except Exception:
        pass

    # 9) Build state + cache + history
    state = {
        'ticker':              ticker,
        'spot':                round(spot, 4),
        'time_remaining_sec':  round(time_remaining, 1),
        'pin_estimate':        round(pin_estimate, 2),
        'pin_confidence':      round(pin_confidence, 4),
        'expected_close':      round(pin_estimate, 2),
        'ci_low':              round(pin_estimate - 2 * weighted_std, 2),
        'ci_high':             round(pin_estimate + 2 * weighted_std, 2),
        'strikes':             sorted(scored, key=lambda x: x['K']),
        'walls':               walls_overlay,
        'data_ts':             data_ts,
        'server_time':         time.time(),
        'analysis_band_dollars': ANALYSIS_BAND_DOLLARS,
        'time_amp_threshold_sec': TIME_AMP_THRESHOLD_SEC,
        'reason':              None,
    }

    with _state_lock:
        _state_cache[ticker] = state
        # Append history sample
        _pin_history[ticker].append({
            'ts':              state['server_time'],
            'spot':            state['spot'],
            'pin_estimate':    state['pin_estimate'],
            'pin_confidence':  state['pin_confidence'],
            'ci_low':          state['ci_low'],
            'ci_high':         state['ci_high'],
        })

    # Outcome ledger (file write outside state lock)
    _write_ledger({
        'ts':                state['server_time'],
        'ticker':            ticker,
        'spot':              state['spot'],
        'pin_estimate':      state['pin_estimate'],
        'pin_confidence':    state['pin_confidence'],
        'ci_low':            state['ci_low'],
        'ci_high':           state['ci_high'],
        'time_remaining_sec': state['time_remaining_sec'],
    })

    return state


def get_state(ticker: str) -> dict:
    """REST handler — returns cached state (or computes fresh if cache empty).

    Includes time-evolution history for pin trajectory rendering.
    """
    ticker = (ticker or '').upper()
    with _state_lock:
        cached = _state_cache.get(ticker)
        if cached:
            out = dict(cached)
            out['history'] = list(_pin_history.get(ticker, []))
            return out
    # No cache yet — compute on-demand
    state = compute_pin_state(ticker)
    with _state_lock:
        state['history'] = list(_pin_history.get(ticker, []))
    return state


# ── Outcome ledger (per-day JSONL) ───────────────────────────────────────────

def _ledger_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f'pin_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append a pin-outcome record. Used for offline regression to validate
    score weights and upgrade them from CONFIGURED → MEASURED."""
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
            log.debug(f"[PIN] ledger write err: {e}")
