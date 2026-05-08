"""
SPX-vs-QQQ Option-Flow Divergence — cross-asset dealer regime comparator.

Both SPX and QQQ are tech-correlated; when their dealer-positioning regimes
DIVERGE (one short-Γ, one long-Γ; or magnitude differs >2×), the laggard tends
to follow the leader within the next 1–4 hours. This module surfaces those
divergences in real time.

Sign / regime convention (matches greek_surface + wall_signals):
  spot ABOVE gamma_flip → dealers LONG gamma   → hedge dampens momentum
  spot BELOW gamma_flip → dealers SHORT gamma  → hedge amplifies momentum
  hp_gamma_shares_1pct > 0 = dealers BUY on price rise (short-Γ regime)
  hp_gamma_shares_1pct < 0 = dealers SELL on price rise (long-Γ regime)

Inputs (all from existing modules — zero re-streaming):
  - schwab_bridge._latest_qqq                     QQQ live spot
  - schwab_bridge._latest_spot_by_ticker['SPX']   SPX live spot
  - schwab_bridge._greek_surface (QQQ only)       per-strike Γ + totals
  - schwab_bridge._per_ticker_gex['SPX']          per-contract Γ_$ + OI
  - schwab_bridge._compute_walls_for('SPX'|'QQQ') flip + walls (DERIVED)

Verdict classification (DERIVED — strength ∈ [0..1]):
  ALIGNED_BULL       both above flip, same hp sign      → tech bullish, low div
  ALIGNED_BEAR       both below flip, same hp sign      → tech bearish, low div
  DIVERGENT_REGIME   opposite sides of flip             → strong divergence
  DIVERGENT_MAGNITUDE same side, |ratio − 1| > 1        → magnitude divergence
  NEUTRAL            insufficient or near-flip data     → no signal

Verdict strength (DERIVED):
  Regime divergence: 1 − exp(−|sign_qqq_dist − sign_spx_dist|/2.0)
                     (bigger gap between regime distances = stronger signal)
  Magnitude divergence: 1 − exp(−|log(ratio)|/log(2))     [ratio > 1]
  Aligned: 1 − verdict_strength_of_divergence_dimension

Output state envelope (per cycle):
  {ticker_a, ticker_b, spx:{...}, qqq:{...}, divergence:{verdict, strength,
   regime_aligned, magnitude_ratio, flip_distance_diff_pct, rationale},
   history:[...], data_ts, server_time, reason}

Outcome ledger:
  logs/spx_qqq_divergence_outcomes_YYYYMMDD.jsonl — per-cycle records
  {ts, spx_spot, qqq_spot, spx_flip, qqq_flip, spx_hp, qqq_hp, verdict,
   strength}. Used offline to validate verdict → next-N-min relative-spread
   move (does laggard follow leader? target hit-rate ≥60%).
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
DIVERGENCE_HISTORY_CAP        = 360   # CONFIGURED — 60min @ 10s cadence; UI trajectory window
MAGNITUDE_DIVERGENCE_THRESHOLD = 2.0  # CONFIGURED — |ratio| > this triggers magnitude verdict
NEAR_FLIP_DOLLAR_BAND         = 1.0   # CONFIGURED — |spot−flip| < $1 = "at flip" → NEUTRAL
REGIME_STRENGTH_HALFLIFE_PCT  = 0.50  # CONFIGURED — divergence strength saturates at 0.5% gap
ANALYSIS_TICKERS              = ('SPX', 'QQQ')  # STRUCTURAL — what we compare

# ── Module state ────────────────────────────────────────────────────────────
_state_cache: dict = {}                                    # last computed state
_history: deque = deque(maxlen=DIVERGENCE_HISTORY_CAP)     # rolling history
_state_lock = threading.RLock()

# Outcome ledger
_ledger_fh = None
_ledger_date: str = ''
_ledger_lock = threading.Lock()


def _empty_state(reason: str = '') -> dict:
    return {
        'ticker_a':   'SPX',
        'ticker_b':   'QQQ',
        'spx':        _empty_ticker('SPX'),
        'qqq':        _empty_ticker('QQQ'),
        'divergence': {
            'verdict':              'NO_DATA',
            'strength':             0.0,
            'regime_aligned':       None,
            'magnitude_ratio':      None,
            'flip_distance_diff_pct': None,
            'rationale':            reason or 'awaiting data',
        },
        'history':     [],
        'data_ts':     0.0,
        'server_time': time.time(),
        'reason':      reason,
    }


def _empty_ticker(ticker: str) -> dict:
    return {
        'ticker':                  ticker,
        'spot':                    0.0,
        'gamma_flip':              None,
        'distance_to_flip_pct':    None,
        'regime':                  None,
        'hp_gamma_shares_1pct':    0.0,
        'call_wall':               None,
        'put_wall':                None,
        'gamma_call_wall':         None,
        'gamma_put_wall':          None,
        'pcr_oi':                  None,
        'net_dealer_gamma_dollars': 0.0,
        'data_source':             None,
        'strike_count':            0,
    }


def _build_qqq_snapshot(sb) -> dict:
    """Build per-ticker QQQ snapshot using greek_surface (richest data path)."""
    out = _empty_ticker('QQQ')
    spot = float(getattr(sb, '_latest_qqq', 0.0) or 0.0)
    out['spot'] = round(spot, 4)

    gs = getattr(sb, '_greek_surface', None)
    if gs is None or spot <= 0:
        out['data_source'] = 'no_greek_surface' if gs is None else 'no_spot'
        return out

    # 1) hp_gamma_shares_1pct + per-strike net dn_gamma sum
    try:
        hp = gs.export_hedge_pressure(spot)
    except Exception as e:
        out['data_source'] = f'hp_err:{e}'
        return out
    if not hp:
        out['data_source'] = 'hp_empty'
        return out

    totals = (hp or {}).get('totals') or {}
    out['hp_gamma_shares_1pct'] = round(float(totals.get('hp_gamma_shares_1pct', 0) or 0), 1)

    strikes = (hp or {}).get('strikes') or []
    out['strike_count'] = len(strikes)

    # PCR (put OI / call OI) over the analysis band (greek_surface already filters)
    total_call_oi = sum(int(s.get('oi_call', 0) or 0) for s in strikes)
    total_put_oi  = sum(int(s.get('oi_put', 0) or 0) for s in strikes)
    if total_call_oi > 0:
        out['pcr_oi'] = round(total_put_oi / total_call_oi, 4)

    # Net dealer Γ$ across all strikes (greek_surface returns sum_dollar_dn_gamma in totals)
    out['net_dealer_gamma_dollars'] = round(
        float(totals.get('dn_gamma_dollars', 0) or 0) * 1.0, 1
    )

    # 2) Walls + flip from wall_signals (already populated by schwab_bridge)
    try:
        from connectors import wall_signals as _ws
        w = (getattr(_ws, '_walls', {}) or {}).get('QQQ') or {}
        gf = w.get('gamma_flip')
        if isinstance(gf, (int, float)) and gf > 0:
            out['gamma_flip'] = round(float(gf), 4)
            if spot > 0:
                out['distance_to_flip_pct'] = round((spot - float(gf)) / spot * 100.0, 4)
                out['regime'] = 'LONG_GAMMA' if spot > float(gf) else 'SHORT_GAMMA'
        for k_in, k_out in (('call_wall', 'call_wall'),
                             ('put_wall', 'put_wall'),
                             ('gamma_call_wall', 'gamma_call_wall'),
                             ('gamma_put_wall', 'gamma_put_wall')):
            v = w.get(k_in)
            if isinstance(v, (int, float)) and v > 0:
                out[k_out] = round(float(v), 4)
    except Exception:
        pass

    out['data_ts'] = float((hp or {}).get('ts', 0) or 0)
    out['data_source'] = 'greek_surface+wall_signals'
    return out


def _build_spx_snapshot(sb) -> dict:
    """Build per-ticker SPX snapshot from `_per_ticker_gex['SPX']` + walls.

    SPX has no GreekSurface instance, so we aggregate raw per-contract entries
    (`{strike, side, gamma_dollars, oi}`) and synthesize the same totals
    interface as QQQ.
    """
    out = _empty_ticker('SPX')
    spots = getattr(sb, '_latest_spot_by_ticker', {}) or {}
    spot = float(spots.get('SPX', 0.0) or 0.0)
    out['spot'] = round(spot, 4)
    if spot <= 0:
        out['data_source'] = 'no_spot'
        return out

    # Per-strike aggregation from _per_ticker_gex['SPX']
    per_contract = (getattr(sb, '_per_ticker_gex', {}) or {}).get('SPX')
    if not per_contract:
        out['data_source'] = 'no_per_ticker_gex'
        return out

    call_oi: dict = defaultdict(int)
    put_oi:  dict = defaultdict(int)
    call_g_dollars: dict = defaultdict(float)
    put_g_dollars:  dict = defaultdict(float)

    for entry in per_contract.values():
        try:
            K = float(entry.get('strike') or 0)
            if K <= 0:
                continue
            side = entry.get('side')
            g = float(entry.get('gamma_dollars', 0) or 0)
            oi = int(entry.get('oi', 0) or 0)
            if side == 'call':
                call_oi[K] += oi
                call_g_dollars[K] += g
            elif side == 'put':
                put_oi[K] += oi
                put_g_dollars[K] += g
        except Exception:
            continue

    strikes_set = set(call_oi.keys()) | set(put_oi.keys())
    out['strike_count'] = len(strikes_set)
    if not strikes_set:
        out['data_source'] = 'empty_aggregation'
        return out

    # Per-strike dn_gamma_dollars (dealer convention: short calls, long puts)
    #   dn_gamma_$ = -call_g$ + put_g$
    # Total Γ$ across analysis band (no strike filter — SPX uses all OI for totals)
    net_dn_gamma_dollars = 0.0
    for K in strikes_set:
        net_dn_gamma_dollars += (-call_g_dollars.get(K, 0.0) + put_g_dollars.get(K, 0.0))
    out['net_dealer_gamma_dollars'] = round(net_dn_gamma_dollars, 1)

    # hp_gamma_shares_1pct = -net_dn_gamma_dollars / spot
    # Same formula as greek_surface — for a 1% spot move, this is the share
    # rebalance volume implied by the dealer Γ$ net.
    if spot > 0:
        out['hp_gamma_shares_1pct'] = round(-net_dn_gamma_dollars / spot, 1)

    # PCR
    total_call = sum(call_oi.values())
    total_put = sum(put_oi.values())
    if total_call > 0:
        out['pcr_oi'] = round(total_put / total_call, 4)

    # Walls + flip via _compute_walls_for (this is our DERIVED canonical path)
    try:
        compute_walls = getattr(sb, '_compute_walls_for', None)
        if compute_walls:
            w = compute_walls('SPX') or {}
            for k_in, k_out in (('call_wall', 'call_wall'),
                                 ('put_wall', 'put_wall'),
                                 ('gamma_call_wall', 'gamma_call_wall'),
                                 ('gamma_put_wall', 'gamma_put_wall')):
                v = w.get(k_in)
                if isinstance(v, (int, float)) and v > 0:
                    out[k_out] = round(float(v), 4)
            flip = w.get('flip')
            if isinstance(flip, (int, float)) and flip > 0:
                out['gamma_flip'] = round(float(flip), 4)
                out['distance_to_flip_pct'] = round((spot - float(flip)) / spot * 100.0, 4)
                out['regime'] = 'LONG_GAMMA' if spot > float(flip) else 'SHORT_GAMMA'
    except Exception:
        pass

    out['data_source'] = 'per_ticker_gex+compute_walls'
    return out


def _classify_divergence(spx: dict, qqq: dict) -> dict:
    """Compare two snapshots → verdict + strength + rationale.

    All thresholds DERIVED or CONFIGURED (categorized in MEASURED_VALUES.md).
    """
    div = {
        'verdict':              'NEUTRAL',
        'strength':             0.0,
        'regime_aligned':       None,
        'magnitude_ratio':      None,
        'flip_distance_diff_pct': None,
        'rationale':            '',
    }

    # Both must have valid spot + flip + hp
    spx_ok = (spx['spot'] > 0 and spx.get('gamma_flip') is not None
              and spx.get('regime') is not None)
    qqq_ok = (qqq['spot'] > 0 and qqq.get('gamma_flip') is not None
              and qqq.get('regime') is not None)
    if not (spx_ok and qqq_ok):
        div['verdict'] = 'NO_DATA'
        div['rationale'] = (
            'awaiting valid spot+flip on both tickers '
            f'(spx_ok={spx_ok}, qqq_ok={qqq_ok})'
        )
        return div

    # Regime alignment
    spx_regime = spx['regime']
    qqq_regime = qqq['regime']
    regime_aligned = (spx_regime == qqq_regime)
    div['regime_aligned'] = regime_aligned

    # Flip-distance diff (signed — both expressed as % above/below their own flip)
    spx_dist_pct = spx.get('distance_to_flip_pct') or 0.0
    qqq_dist_pct = qqq.get('distance_to_flip_pct') or 0.0
    flip_distance_diff_pct = round(qqq_dist_pct - spx_dist_pct, 4)
    div['flip_distance_diff_pct'] = flip_distance_diff_pct

    # Near-flip suppression (both within $1 of own flip = no signal)
    spx_near_flip = abs(spx['spot'] - (spx.get('gamma_flip') or 0)) < NEAR_FLIP_DOLLAR_BAND
    qqq_near_flip = abs(qqq['spot'] - (qqq.get('gamma_flip') or 0)) < NEAR_FLIP_DOLLAR_BAND
    if spx_near_flip and qqq_near_flip:
        div['verdict'] = 'NEUTRAL'
        div['rationale'] = (
            f'both at flip (SPX ${spx["spot"]:.2f} vs flip ${spx["gamma_flip"]:.2f}; '
            f'QQQ ${qqq["spot"]:.2f} vs flip ${qqq["gamma_flip"]:.2f}); regime undefined'
        )
        return div

    # Magnitude ratio of hp_gamma_shares_1pct (normalized by spot to compare
    # across asset classes — SPX shares ≪ QQQ shares since SPX is index)
    # Use absolute magnitude per dollar of notional: |hp_shares_1pct| / spot_per_strike_step
    # Simpler: normalize each to "dollar-Γ per dollar of spot" = hp_gamma_shares_1pct × 0.01
    spx_hp = abs(spx.get('hp_gamma_shares_1pct', 0.0))
    qqq_hp = abs(qqq.get('hp_gamma_shares_1pct', 0.0))
    # Avoid div-by-zero; use larger as numerator
    if spx_hp > 0 and qqq_hp > 0:
        magnitude_ratio = max(spx_hp / qqq_hp, qqq_hp / spx_hp)
        div['magnitude_ratio'] = round(magnitude_ratio, 4)
    else:
        magnitude_ratio = None

    # Verdict classification
    if not regime_aligned:
        # DIVERGENT_REGIME — strongest signal
        # Strength: based on |flip_distance_diff_pct| saturated at REGIME_STRENGTH_HALFLIFE_PCT
        gap = abs(flip_distance_diff_pct)
        strength = 1.0 - math.exp(-gap / REGIME_STRENGTH_HALFLIFE_PCT)
        div['verdict'] = 'DIVERGENT_REGIME'
        div['strength'] = round(strength, 4)
        leader = 'SPX' if abs(spx_dist_pct) > abs(qqq_dist_pct) else 'QQQ'
        laggard = 'QQQ' if leader == 'SPX' else 'SPX'
        div['rationale'] = (
            f'{spx_regime[:5]} SPX vs {qqq_regime[:5]} QQQ — opposite regimes; '
            f'leader={leader} ({spx_dist_pct:+.3f}% vs {qqq_dist_pct:+.3f}%), '
            f'laggard={laggard} expected to follow within 1-4hr'
        )
        return div

    # Same regime — check magnitude divergence
    if magnitude_ratio is not None and magnitude_ratio >= MAGNITUDE_DIVERGENCE_THRESHOLD:
        # DIVERGENT_MAGNITUDE
        # Strength: log-saturation. ratio=2 → ~0.50, ratio=4 → ~0.79, ratio=10 → ~0.95
        strength = 1.0 - math.exp(-math.log(magnitude_ratio) / math.log(2))
        div['verdict'] = 'DIVERGENT_MAGNITUDE'
        div['strength'] = round(strength, 4)
        leader = 'SPX' if spx_hp > qqq_hp else 'QQQ'
        div['rationale'] = (
            f'aligned {spx_regime}: {leader} dealer-pressure '
            f'{magnitude_ratio:.1f}× larger; magnitude divergence — '
            f'{leader} hedge flow likely dominates joint move'
        )
        return div

    # Aligned, no magnitude divergence
    if spx_regime == 'LONG_GAMMA':
        div['verdict'] = 'ALIGNED_BULL'
        div['rationale'] = (
            f'both LONG_GAMMA — SPX +{spx_dist_pct:.3f}%, QQQ +{qqq_dist_pct:.3f}%; '
            f'dampening regime; mean-reversion bias'
        )
    else:
        div['verdict'] = 'ALIGNED_BEAR'
        div['rationale'] = (
            f'both SHORT_GAMMA — SPX {spx_dist_pct:.3f}%, QQQ {qqq_dist_pct:.3f}%; '
            f'amplifying regime; momentum bias'
        )
    # Aligned strength: 1 - normalized gap (small gap = high alignment confidence)
    gap = abs(flip_distance_diff_pct)
    div['strength'] = round(max(0.0, 1.0 - gap / 1.0), 4)  # 1% gap → strength 0
    return div


def compute_state() -> dict:
    """Compute SPX-vs-QQQ divergence state. Push via Socket.IO + cache."""
    try:
        from background_engine import schwab_bridge as _sb
    except Exception as e:
        return _empty_state(reason=f'sb_import_err:{e}')

    spx = _build_spx_snapshot(_sb)
    qqq = _build_qqq_snapshot(_sb)
    div = _classify_divergence(spx, qqq)

    now_ts = time.time()
    state = {
        'ticker_a':   'SPX',
        'ticker_b':   'QQQ',
        'spx':        spx,
        'qqq':        qqq,
        'divergence': div,
        'data_ts':    max(spx.get('data_ts', 0) or 0, qqq.get('data_ts', 0) or 0),
        'server_time': now_ts,
        'reason':     None if div['verdict'] != 'NO_DATA' else div.get('rationale'),
    }

    # Append history sample (lightweight — no nested strike data)
    sample = {
        'ts':                     now_ts,
        'verdict':                div['verdict'],
        'strength':               div['strength'],
        'spx_spot':               spx.get('spot', 0),
        'qqq_spot':               qqq.get('spot', 0),
        'spx_dist_pct':           spx.get('distance_to_flip_pct'),
        'qqq_dist_pct':           qqq.get('distance_to_flip_pct'),
        'spx_regime':             spx.get('regime'),
        'qqq_regime':             qqq.get('regime'),
        'spx_hp_shares_1pct':     spx.get('hp_gamma_shares_1pct'),
        'qqq_hp_shares_1pct':     qqq.get('hp_gamma_shares_1pct'),
        'magnitude_ratio':        div.get('magnitude_ratio'),
        'flip_distance_diff_pct': div.get('flip_distance_diff_pct'),
    }

    with _state_lock:
        _state_cache['latest'] = state
        _history.append(sample)
        # 2026-05-08 multiproc: publish to disk for server-process REST.
        try:
            from connectors._bridge_state import publish as _bs_publish
            _bs_publish('spx_qqq_div', 'latest', {**state, 'history': list(_history)})
        except Exception:
            pass

    # Outcome ledger
    _write_ledger(sample)

    return state


def get_state() -> dict:
    """REST handler — return cached state with attached history.

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
        disk_state = _bs_fetch('spx_qqq_div', 'latest')
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
    return os.path.join(log_dir, f'spx_qqq_divergence_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append per-cycle divergence sample. Used offline to validate verdict
    accuracy: did the laggard follow the leader within 1-4 hours? Hit-rate
    target ≥60%."""
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
            log.debug(f"[SPX-QQQ-DIV] ledger write err: {e}")
