"""
Cross-Asset Vol Regime Dashboard — VIX-family + cross-asset vol comparator.

Schwab does not stream VIX9D/VIX3M/VIX6M term-structure points (those are
CBOE-only, not on the Schwab streamer). What we DO stream:
  Front-of-curve term:    $VIX1D (1d) vs VIX (30d)
  Cross-asset (30d) vols: $VXN (NDX) / $RVX (R2K) / $VXD (DJI) / $VXEEM (EM)
  Vol-of-vol & tail:      $VVIX (vol of VIX options) / $SKEW (CBOE Skew)
  Commodity vols:         $OVX (oil) / $GVZ (gold)
  Rates context:          $TNX (10y × 10)

These together give a richer regime read than a single VIX term-structure curve.

Outputs:
  - Per-ticker live spot
  - Front-curve ratio: vix1d / vix          (>1 = backwardation = front-loaded stress)
  - Vol-of-vol ratio: vvix / vix            (>8 = institutional tail-hedge bid)
  - Cross-vol spreads: vxn-vix, rvx-vix, vxd-vix, vxeem-vix
  - Skew level (>135 = elevated tail premium)
  - Composite regime classifier (DERIVED — see formula below)
  - History trajectory for chart

Regime classifier (DERIVED):
  CALM_CONTANGO        VIX < 16  AND vix1d/vix < 1.0  AND SKEW < 135
  NORMAL               VIX 16-22  AND ratios neutral
  TECH_DIVERGENCE      |vxn-vix| > 4 (tech vs broad-market vol gap)
  ELEVATED             VIX 22-30 OR  SKEW > 145
  STRESS_CONTANGO      VIX > 30  AND vix1d < vix     (term-stress, calm front)
  STRESS_BACKWARDATION VIX > 22  AND vix1d > vix     (event-driven front spike)
  VVIX_DIVERGENCE      vvix/vix > 9  AND VIX < 18    (institutional bid at low VIX)

Inputs (all VERIFIED — Schwab LEVELONE_EQUITIES last-price field 1):
  schwab_bridge._latest_spot_by_ticker[<ticker>]
  Tickers consumed: VIX, VIX1D, VVIX, VXN, RVX, VXD, VXEEM, SKEW, OVX, GVZ, TNX

Outcome ledger:
  logs/vix_regime_outcomes_YYYYMMDD.jsonl — per-cycle records.
  Validates: did regime correctly anticipate next 4-hour SPX move
  (STRESS_BACKWARDATION → SPY drawdown ≥0.5% in 4h, hit-rate target ≥55%)?
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

# ── CONFIGURED constants (categorized in MEASURED_VALUES.md) ─────────────────
HISTORY_CAP                      = 360     # CONFIGURED — 60min @ 10s cadence
VIX_CALM_THRESHOLD               = 16.0    # CONFIGURED — 30y CBOE percentile of "calm" zone
VIX_NORMAL_UPPER                 = 22.0    # CONFIGURED — 30y P75 of VIX distribution
VIX_ELEVATED_UPPER               = 30.0    # CONFIGURED — historical "stress" boundary
SKEW_ELEVATED_THRESHOLD          = 135.0   # CONFIGURED — CBOE Skew "elevated tail" mark
SKEW_HIGH_THRESHOLD              = 145.0   # CONFIGURED — high tail-premium mark
VVIX_RATIO_INSTITUTIONAL_BID     = 9.0     # CONFIGURED — vvix/vix ratio: institutional tail-bid
VXN_VIX_DIVERGENCE_POINTS        = 4.0     # CONFIGURED — abs(vxn-vix) for tech-divergence flag
BACKWARDATION_RATIO_THRESHOLD    = 1.00    # CONFIGURED — vix1d/vix > 1 = front-loaded

# Vol-family symbols this module reads. STRUCTURAL — driven by what
# schwab_bridge subscribes to in LEVELONE_EQUITIES.
VOL_TICKERS = (
    'VIX', 'VIX1D', 'VVIX', 'VXN', 'RVX', 'VXD', 'VXEEM',
    'SKEW', 'OVX', 'GVZ', 'TNX',
)

# ── Module state ─────────────────────────────────────────────────────────────
_state_cache: dict = {}
_history: deque = deque(maxlen=HISTORY_CAP)
_state_lock = threading.RLock()

# Outcome ledger
_ledger_fh = None
_ledger_date: str = ''
_ledger_lock = threading.Lock()


def _empty_state(reason: str = '') -> dict:
    return {
        'tickers':     {t: None for t in VOL_TICKERS},
        'spreads':     {},
        'ratios':      {},
        'regime':      'NO_DATA',
        'regime_strength': 0.0,
        'rationale':   reason or 'awaiting data',
        'history':     [],
        'data_ts':     0.0,
        'server_time': time.time(),
        'reason':      reason,
    }


def _classify_regime(spots: dict, spreads: dict, ratios: dict) -> tuple:
    """Classify vol regime → (regime, strength, rationale).

    Returns:
        regime:    str — one of {CALM_CONTANGO, NORMAL, TECH_DIVERGENCE,
                                  ELEVATED, STRESS_CONTANGO,
                                  STRESS_BACKWARDATION, VVIX_DIVERGENCE,
                                  NO_DATA}
        strength:  float in [0..1]
        rationale: human-readable explanation
    """
    vix = spots.get('VIX')
    vix1d = spots.get('VIX1D')
    vvix = spots.get('VVIX')
    vxn = spots.get('VXN')
    skew = spots.get('SKEW')

    if not (isinstance(vix, (int, float)) and vix > 0):
        return ('NO_DATA', 0.0, 'no live VIX spot')

    backwardation_ratio = ratios.get('vix1d_over_vix')
    vvix_ratio = ratios.get('vvix_over_vix')
    vxn_vix_spread = spreads.get('vxn_minus_vix')

    # ── STRESS_BACKWARDATION (highest priority) ─────────────────────────
    if (vix >= VIX_NORMAL_UPPER and
        backwardation_ratio is not None and
        backwardation_ratio > BACKWARDATION_RATIO_THRESHOLD):
        # Strength: scaled by both VIX level and ratio excess
        ratio_excess = backwardation_ratio - 1.0
        vix_excess = (vix - VIX_NORMAL_UPPER) / VIX_NORMAL_UPPER
        strength = min(1.0, ratio_excess * 5.0 + vix_excess)
        return (
            'STRESS_BACKWARDATION',
            round(max(0.3, strength), 4),
            f'VIX {vix:.2f} + VIX1D/VIX {backwardation_ratio:.3f} '
            f'> 1: front-loaded event/gap risk; institutions buying short-term hedges'
        )

    # ── STRESS_CONTANGO ─────────────────────────────────────────────────
    if vix >= VIX_ELEVATED_UPPER:
        # No backwardation but high absolute level
        strength = min(1.0, (vix - VIX_ELEVATED_UPPER) / 10.0 + 0.5)
        return (
            'STRESS_CONTANGO',
            round(strength, 4),
            f'VIX {vix:.2f} > {VIX_ELEVATED_UPPER}: term stress; '
            f'sustained risk-off bid into back-of-curve'
        )

    # ── VVIX_DIVERGENCE — institutional tail-bid even at low VIX ────────
    if (vvix_ratio is not None and
        vvix_ratio >= VVIX_RATIO_INSTITUTIONAL_BID and
        vix < 18.0):
        strength = min(1.0, (vvix_ratio - VVIX_RATIO_INSTITUTIONAL_BID) / 3.0 + 0.4)
        return (
            'VVIX_DIVERGENCE',
            round(strength, 4),
            f'VVIX/VIX {vvix_ratio:.2f} ≥ {VVIX_RATIO_INSTITUTIONAL_BID} '
            f'with VIX {vix:.2f}: institutional tail-bid despite calm spot — '
            f'desks pricing convex risk'
        )

    # ── ELEVATED ────────────────────────────────────────────────────────
    if (vix >= VIX_NORMAL_UPPER or
        (skew is not None and skew >= SKEW_HIGH_THRESHOLD)):
        if skew is not None and skew >= SKEW_HIGH_THRESHOLD:
            strength = min(1.0, (skew - SKEW_HIGH_THRESHOLD) / 20.0 + 0.5)
            rat = (f'SKEW {skew:.1f} ≥ {SKEW_HIGH_THRESHOLD}: tail-risk '
                   f'premium elevated; OTM SPX puts richly bid')
        else:
            strength = min(1.0, (vix - VIX_NORMAL_UPPER) / (VIX_ELEVATED_UPPER - VIX_NORMAL_UPPER))
            rat = f'VIX {vix:.2f} in elevated band [{VIX_NORMAL_UPPER}-{VIX_ELEVATED_UPPER}]'
        return ('ELEVATED', round(strength, 4), rat)

    # ── TECH_DIVERGENCE ────────────────────────────────────────────────
    if (vxn_vix_spread is not None and
        abs(vxn_vix_spread) >= VXN_VIX_DIVERGENCE_POINTS):
        leader = 'NDX' if vxn_vix_spread > 0 else 'SPX'
        strength = min(1.0, abs(vxn_vix_spread) / 8.0)
        return (
            'TECH_DIVERGENCE',
            round(strength, 4),
            f'VXN−VIX = {vxn_vix_spread:+.2f}: {leader} vol leading; '
            f'{("tech stress" if vxn_vix_spread > 0 else "tech calm vs broad")}'
        )

    # ── CALM_CONTANGO ──────────────────────────────────────────────────
    if (vix < VIX_CALM_THRESHOLD and
        backwardation_ratio is not None and
        backwardation_ratio < BACKWARDATION_RATIO_THRESHOLD and
        (skew is None or skew < SKEW_ELEVATED_THRESHOLD)):
        # Calm: strength scales inversely with VIX level
        strength = (VIX_CALM_THRESHOLD - vix) / VIX_CALM_THRESHOLD
        return (
            'CALM_CONTANGO',
            round(min(1.0, strength + 0.3), 4),
            f'VIX {vix:.2f} < {VIX_CALM_THRESHOLD}, VIX1D < VIX, SKEW normal: '
            f'mean-reversion regime; vol-selling envelope'
        )

    # ── NORMAL fallback ─────────────────────────────────────────────────
    rationale_parts = [f'VIX {vix:.2f}']
    if backwardation_ratio is not None:
        rationale_parts.append(f'VIX1D/VIX {backwardation_ratio:.3f}')
    if skew is not None:
        rationale_parts.append(f'SKEW {skew:.1f}')
    return ('NORMAL', 0.5, ' · '.join(rationale_parts) + ': mixed signals')


def compute_state() -> dict:
    """Compute current vol regime state. Cached + history-tracked."""
    try:
        from background_engine import schwab_bridge as _sb
    except Exception as e:
        return _empty_state(reason=f'sb_import_err:{e}')

    spots_cache = getattr(_sb, '_latest_spot_by_ticker', {}) or {}

    # 1) Snapshot all vol tickers
    tickers: dict = {}
    for t in VOL_TICKERS:
        v = spots_cache.get(t)
        tickers[t] = float(v) if isinstance(v, (int, float)) and v > 0 else None

    vix = tickers.get('VIX')
    vix1d = tickers.get('VIX1D')
    vvix = tickers.get('VVIX')
    vxn = tickers.get('VXN')
    rvx = tickers.get('RVX')
    vxd = tickers.get('VXD')
    vxeem = tickers.get('VXEEM')

    # Refuse to render anything if VIX itself is missing
    if vix is None or vix <= 0:
        st = _empty_state(reason='no_vix_spot')
        with _state_lock:
            _state_cache['latest'] = st
        return st

    # 2) Cross-asset spreads (DERIVED)
    spreads: dict = {}
    if vxn   is not None: spreads['vxn_minus_vix']   = round(vxn - vix, 4)
    if rvx   is not None: spreads['rvx_minus_vix']   = round(rvx - vix, 4)
    if vxd   is not None: spreads['vxd_minus_vix']   = round(vxd - vix, 4)
    if vxeem is not None: spreads['vxeem_minus_vix'] = round(vxeem - vix, 4)
    if vix1d is not None: spreads['vix1d_minus_vix'] = round(vix1d - vix, 4)

    # 3) Ratios (DERIVED)
    ratios: dict = {}
    if vix1d is not None and vix > 0:
        ratios['vix1d_over_vix'] = round(vix1d / vix, 4)
    if vvix is not None and vix > 0:
        ratios['vvix_over_vix'] = round(vvix / vix, 4)

    # 4) Regime
    regime, strength, rationale = _classify_regime(tickers, spreads, ratios)

    now_ts = time.time()
    state = {
        'tickers':     {t: round(v, 4) if v is not None else None for t, v in tickers.items()},
        'spreads':     spreads,
        'ratios':      ratios,
        'regime':      regime,
        'regime_strength': strength,
        'rationale':   rationale,
        'data_ts':     now_ts,
        'server_time': now_ts,
        'reason':      None if regime != 'NO_DATA' else rationale,
    }

    # 5) History sample (compact)
    sample = {
        'ts':         now_ts,
        'regime':     regime,
        'strength':   strength,
        'vix':        tickers.get('VIX'),
        'vix1d':      tickers.get('VIX1D'),
        'vvix':       tickers.get('VVIX'),
        'vxn':        tickers.get('VXN'),
        'skew':       tickers.get('SKEW'),
        'vix1d_over_vix': ratios.get('vix1d_over_vix'),
        'vvix_over_vix':  ratios.get('vvix_over_vix'),
        'vxn_minus_vix':  spreads.get('vxn_minus_vix'),
    }
    with _state_lock:
        _state_cache['latest'] = state
        _history.append(sample)
        # 2026-05-08 multiproc: publish to disk for server-process REST.
        try:
            from connectors._bridge_state import publish as _bs_publish
            _bs_publish('vix_term', 'latest', {**state, 'history': list(_history)})
        except Exception:
            pass

    _write_ledger(sample)

    return state


def get_state() -> dict:
    """REST handler — return cached state with history attached.

    2026-05-08 multiproc fallback: read bridge's published state from disk
    if our in-process cache is empty (because compute runs in bridge.py).
    """
    with _state_lock:
        cached = _state_cache.get('latest')
        if cached:
            out = dict(cached)
            out['history'] = list(_history)
            return out
    try:
        from connectors._bridge_state import fetch as _bs_fetch
        disk_state = _bs_fetch('vix_term', 'latest')
        if disk_state:
            return disk_state
    except Exception:
        pass
    state = compute_state()
    with _state_lock:
        state['history'] = list(_history)
    return state


# ── Outcome ledger (per-day JSONL) ───────────────────────────────────────────

def _ledger_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f'vix_regime_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append per-cycle regime sample. Used offline to validate that
    STRESS_BACKWARDATION precedes SPY drawdowns ≥0.5% in next 4h
    (target hit-rate ≥55%)."""
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
            log.debug(f"[VIX-REGIME] ledger write err: {e}")
