"""
Gamma Skyline — per-strike dealer Γ$ "city skyline" visualization.

Renders the dealer hedge surface as vertical bars at every active QQQ strike
within the viewable band. Bar height = signed dealer net gamma in dollars
(`dn_gamma` from greek_surface). Bar sign:

  positive dn_gamma → dealers NET LONG γ at K → must SELL on rises (dampening)
  negative dn_gamma → dealers NET SHORT γ at K → must BUY on rises (amplifying)

Plus walls overlay (call_wall / put_wall / gamma_flip / gamma_call_wall /
gamma_put_wall) and spot crosshair.

Inputs (all VERIFIED — already streaming):
  schwab_bridge._latest_qqq                   live spot
  schwab_bridge._greek_surface.export_hedge_pressure(spot)  per-strike dn_*
  wall_signals._walls['QQQ']                  flip + walls overlay

Output state:
  {
    spot, max_pain, gamma_flip, ...,
    band_low, band_high, atm_strike,
    strikes: [
      {K, dn_gamma, dn_vanna, dn_charm, oi_call, oi_put,
       hp_gamma_shares_1pct, dist_pct, dn_gamma_norm},
      ...
    ],
    totals: {hp_gamma_shares_1pct, hp_vanna_shares_1volpt, hp_charm_shares_1hr,
              dn_gamma_max_abs, dn_gamma_dollars},
    walls: {call_wall, put_wall, gamma_flip, gamma_call_wall, gamma_put_wall},
    data_ts, server_time, reason
  }

Outcome ledger:
  logs/gamma_skyline_outcomes_YYYYMMDD.jsonl — periodic snapshot. Used for
  offline replay debugging only (validates that skyline matches actual price
  pinning behaviour over the day).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ── CONFIGURED constants (categorized in MEASURED_VALUES.md) ────────────────
VIEWABLE_BAND_DOLLARS = 25.0    # CONFIGURED — show ATM ±$25 (≈3.75% on QQQ)
ANALYSIS_TICKER       = 'QQQ'   # STRUCTURAL — current focus
HISTORY_CAP           = 240     # CONFIGURED — 20 min @ 5s for sky-evolution chart

# ── Module state ────────────────────────────────────────────────────────────
_state_cache: dict = {}
_history: deque = deque(maxlen=HISTORY_CAP)
_state_lock = threading.RLock()

# Outcome ledger
_ledger_fh = None
_ledger_date: str = ''
_ledger_lock = threading.Lock()


def _empty_state(reason: str = '') -> dict:
    return {
        'ticker':       ANALYSIS_TICKER,
        'spot':         0.0,
        'band_low':     None,
        'band_high':    None,
        'atm_strike':   None,
        'strikes':      [],
        'totals':       {
            'hp_gamma_shares_1pct':   0.0,
            'hp_vanna_shares_1volpt': 0.0,
            'hp_charm_shares_1hr':    0.0,
            'dn_gamma_max_abs':       0.0,
            'dn_gamma_dollars':       0.0,
        },
        'walls':        {},
        'history':      [],
        'data_ts':      0.0,
        'server_time':  time.time(),
        'reason':       reason,
    }


def compute_state() -> dict:
    """Build per-strike skyline snapshot. Cached + history-tracked."""
    try:
        from background_engine import schwab_bridge as _sb
    except Exception as e:
        return _empty_state(reason=f'sb_import_err:{e}')

    spot = float(getattr(_sb, '_latest_qqq', 0.0) or 0.0)
    if spot <= 0:
        return _empty_state(reason='no_spot')

    gs = getattr(_sb, '_greek_surface', None)
    if gs is None:
        return _empty_state(reason='no_greek_surface')

    try:
        hp = gs.export_hedge_pressure(spot)
    except Exception as e:
        return _empty_state(reason=f'hp_err:{e}')
    if not hp or not hp.get('strikes'):
        return _empty_state(reason='hp_empty')

    raw_strikes = hp.get('strikes', [])
    band_low  = spot - VIEWABLE_BAND_DOLLARS
    band_high = spot + VIEWABLE_BAND_DOLLARS

    # Filter to viewable band
    band_rows = []
    for s in raw_strikes:
        try:
            K = float(s.get('K', 0))
            if K <= 0 or K < band_low or K > band_high:
                continue
            band_rows.append(s)
        except Exception:
            continue

    if not band_rows:
        return _empty_state(reason='empty_band')

    # Compute max abs dn_gamma in band — used by frontend for bar normalization
    dn_max_abs = max((abs(float(s.get('dn_gamma', 0) or 0)) for s in band_rows),
                      default=0.0)

    # Find ATM strike — closest to spot
    atm_strike = min(band_rows, key=lambda s: abs(float(s.get('K', 0)) - spot))
    atm_K = float(atm_strike.get('K', 0))

    # Build skyline rows
    skyline = []
    sum_dn_gamma_dollars = 0.0
    for s in band_rows:
        K = float(s.get('K', 0))
        dn_g = float(s.get('dn_gamma', 0) or 0)
        dn_v = float(s.get('dn_vanna', 0) or 0)
        dn_c = float(s.get('dn_charm', 0) or 0)
        oi_c = int(s.get('oi_call', 0) or 0)
        oi_p = int(s.get('oi_put', 0) or 0)
        hp_g = float(s.get('hp_gamma_shares_1pct', 0) or 0)

        sum_dn_gamma_dollars += dn_g

        skyline.append({
            'K':                      round(K, 4),
            'dn_gamma':               round(dn_g, 1),
            'dn_vanna':               round(dn_v, 1),
            'dn_charm':               round(dn_c, 1),
            'oi_call':                oi_c,
            'oi_put':                 oi_p,
            'hp_gamma_shares_1pct':   round(hp_g, 1),
            'dist_pct':               round(((K - spot) / spot) * 100.0, 4) if spot > 0 else None,
            'dn_gamma_norm':          round(dn_g / dn_max_abs, 4) if dn_max_abs > 0 else 0.0,
            'is_atm':                 abs(K - atm_K) < 1e-6,
        })

    # Sort by K (frontend assumes ascending)
    skyline.sort(key=lambda r: r['K'])

    # Walls overlay
    walls_out = {}
    try:
        from connectors import wall_signals as _ws
        w = (getattr(_ws, '_walls', {}) or {}).get(ANALYSIS_TICKER) or {}
        for k_in, k_out in (('call_wall',       'call_wall'),
                             ('put_wall',        'put_wall'),
                             ('gamma_flip',      'gamma_flip'),
                             ('gamma_call_wall', 'gamma_call_wall'),
                             ('gamma_put_wall',  'gamma_put_wall')):
            v = w.get(k_in)
            if isinstance(v, (int, float)) and v > 0:
                walls_out[k_out] = round(float(v), 4)
    except Exception:
        pass

    totals_in = (hp or {}).get('totals') or {}
    state = {
        'ticker':       ANALYSIS_TICKER,
        'spot':         round(spot, 4),
        'band_low':     round(band_low, 4),
        'band_high':    round(band_high, 4),
        'atm_strike':   round(atm_K, 4),
        'strikes':      skyline,
        'totals':       {
            'hp_gamma_shares_1pct':   round(float(totals_in.get('hp_gamma_shares_1pct', 0) or 0), 1),
            'hp_vanna_shares_1volpt': round(float(totals_in.get('hp_vanna_shares_1volpt', 0) or 0), 1),
            'hp_charm_shares_1hr':    round(float(totals_in.get('hp_charm_shares_1hr', 0) or 0), 1),
            'dn_gamma_max_abs':       round(dn_max_abs, 1),
            'dn_gamma_dollars':       round(sum_dn_gamma_dollars, 1),
        },
        'walls':        walls_out,
        'data_ts':      float((hp or {}).get('ts', 0) or 0),
        'server_time':  time.time(),
        'reason':       None,
    }

    # History — store compact summary (skyline shape evolves → operator sees
    # walls migrating during the day)
    sample = {
        'ts':                  state['server_time'],
        'spot':                state['spot'],
        'gamma_flip':          walls_out.get('gamma_flip'),
        'call_wall':           walls_out.get('call_wall'),
        'put_wall':            walls_out.get('put_wall'),
        'hp_gamma_shares_1pct': state['totals']['hp_gamma_shares_1pct'],
        'dn_gamma_dollars':    state['totals']['dn_gamma_dollars'],
        'strike_count':        len(skyline),
    }
    with _state_lock:
        _state_cache['latest'] = state
        _history.append(sample)
        # 2026-05-08 multiproc: publish to disk for server-process REST.
        try:
            from connectors._bridge_state import publish as _bs_publish
            _bs_publish('gamma_skyline', 'latest', {**state, 'history': list(_history)})
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
        disk_state = _bs_fetch('gamma_skyline', 'latest')
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
    return os.path.join(log_dir, f'gamma_skyline_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append per-cycle skyline summary. Used offline for replay debugging
    of dealer regime evolution and pin-pull progression."""
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
            log.debug(f"[GAMMA-SKY] ledger write err: {e}")
