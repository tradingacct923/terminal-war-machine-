"""
0DTE Wing Tracker — far-OTM call/put activity classifier.

The "wings" of the option chain are strikes meaningfully far from spot. On 0DTE
specifically, wing activity is highly informative:

  Call wing buying     = lottery / gamma-squeeze setup (asymmetric risk)
  Put wing buying      = tail hedge / fear bid
  Wing aggressor BUY:SELL skew → flow direction
  Wing-vs-ATM ratio    → regime: when wings rip, dealers face convex hedge

Zones (DERIVED — distance from live spot):
  ATM        |K − spot| ≤ 1.0% × spot
  NEAR_WING  1.0% < |K − spot| ≤ 2.5% × spot
  DEEP_WING  2.5% < |K − spot| ≤ 5.0% × spot
  TAIL       |K − spot| > 5.0% × spot

Inputs (every print already classified upstream):
  schwab_bridge._on_tradier_timesale → wing_tracker.on_print(...)
  Tradier per-print fields: occ, strike, side, size, price, exchange, aggressor

State per active zone × side (call/put):
  - volume_today
  - premium_today  (= Σ size × price × 100)
  - aggressor_buy_count, aggressor_sell_count
  - aggressor_buy_size, aggressor_sell_size
  - top strikes by volume (last N)

Regime classifier (DERIVED):
  NORMAL    wing_volume / atm_volume < 0.30
  ACTIVE    0.30 ≤ ratio < 1.00 OR per-zone deep-wing buy spike
  EXTREME   wing_volume / atm_volume ≥ 1.00 OR tail BUY-aggressor spike

Output endpoint /api/intel/wing_tracker returns:
  spot, dte_key, zones[ATM/NEAR_WING/DEEP_WING/TAIL × call/put], regime,
  regime_strength, top_active_strikes (with aggressor breakdown),
  recent_prints (last 20), session_age, server_time.

Outcome ledger:
  logs/wing_outcomes_YYYYMMDD.jsonl — per-cycle snapshot. Used to validate
  wing-flow → next-15m spot move (call wing BUY rip → spot drift up; target
  hit-rate ≥55%).
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
ZONE_ATM_PCT          = 0.010   # CONFIGURED — within 1.0% of spot = ATM
ZONE_NEAR_WING_PCT    = 0.025   # CONFIGURED — 1.0–2.5% = NEAR_WING
ZONE_DEEP_WING_PCT    = 0.050   # CONFIGURED — 2.5–5.0% = DEEP_WING; >5% = TAIL
WING_RATIO_ACTIVE     = 0.30    # CONFIGURED — wing/ATM ratio for ACTIVE
WING_RATIO_EXTREME    = 1.00    # CONFIGURED — wing/ATM ratio for EXTREME
RECENT_PRINTS_CAP     = 30      # CONFIGURED — recent wing prints buffer
TOP_STRIKES_PER_ZONE  = 5       # CONFIGURED — UI display cap per zone
ANALYSIS_TICKER       = 'QQQ'   # STRUCTURAL — current focus
TAIL_BUY_TRIGGER_SIZE = 50      # CONFIGURED — single tail print ≥50 contracts

# Zone keys (STRUCTURAL)
ZONES = ('ATM', 'NEAR_WING', 'DEEP_WING', 'TAIL')

# ── Module state ────────────────────────────────────────────────────────────
def _new_zone_state():
    return {
        'volume_today':         0,
        'premium_today':        0.0,
        'buy_count':            0,
        'sell_count':           0,
        'buy_size':             0,
        'sell_size':            0,
        'last_print_ts':        0.0,
        'strike_volumes':       defaultdict(int),   # strike → volume
        'strike_buy_size':      defaultdict(int),   # strike → buy aggressor size
        'strike_sell_size':     defaultdict(int),   # strike → sell aggressor size
    }

# Top-level state: zone × side → state
_state: dict = {}      # ('ATM','C') → state dict
_state_lock = threading.RLock()
_recent_prints: deque = deque(maxlen=RECENT_PRINTS_CAP)
_session_start_ts: float = 0.0
_session_dte_key: str = ''       # current 0DTE key (YYMMDD) — resets at session boundary
_total_print_count: int = 0
_skipped_print_count: int = 0    # prints rejected (not 0DTE / wrong ticker / no spot)

# Outcome ledger
_ledger_fh = None
_ledger_date: str = ''
_ledger_lock = threading.Lock()


def _ensure_zone(zone: str, side: str) -> dict:
    key = (zone, side)
    if key not in _state:
        _state[key] = _new_zone_state()
    return _state[key]


def _classify_zone(strike: float, spot: float) -> str:
    """Return zone label for `strike` vs live `spot`."""
    if spot <= 0:
        return 'TAIL'
    pct = abs(strike - spot) / spot
    if pct <= ZONE_ATM_PCT:        return 'ATM'
    if pct <= ZONE_NEAR_WING_PCT:  return 'NEAR_WING'
    if pct <= ZONE_DEEP_WING_PCT:  return 'DEEP_WING'
    return 'TAIL'


def _today_dte_key() -> str:
    """Today's YYMMDD — used as the session boundary marker for state resets."""
    return datetime.now().strftime('%y%m%d')


def _maybe_rotate_session():
    """Reset state at midnight when DTE boundary changes."""
    global _session_start_ts, _session_dte_key, _state, _recent_prints
    global _total_print_count, _skipped_print_count
    today = _today_dte_key()
    if _session_dte_key != today:
        _state = {}
        _recent_prints = deque(maxlen=RECENT_PRINTS_CAP)
        _session_start_ts = time.time()
        _session_dte_key = today
        _total_print_count = 0
        _skipped_print_count = 0


# Phase 10D — diagnostic skip-reason counters (for debug endpoint)
_skip_reasons: dict = defaultdict(int)


def on_print(underlying: str, occ_symbol: str, strike: float, option_side: str,
             size: int, price: float, ts_ms: int, exchange: str, aggressor: str,
             dte_key: Optional[str] = None) -> None:
    """Process one Tradier print. Filters to 0DTE on `ANALYSIS_TICKER` only.

    Aggressor must be 'BUY' or 'SELL' (MID prints are skipped — no directional
    info). Strike + spot must be valid; otherwise the print is counted as
    skipped for diagnostics.
    """
    # Phase 10D fix: `_session_start_ts` is mutated in-function; without the
    # global declaration Python treats it as local, raising UnboundLocalError
    # at the read on line 202 — caught silently by the bridge's try/except,
    # leaving 4900+ prints/hour silently dropped before reaching state mutation.
    global _total_print_count, _skipped_print_count, _session_start_ts
    _total_print_count += 1

    if underlying != ANALYSIS_TICKER:
        _skipped_print_count += 1
        _skip_reasons['wrong_ticker'] += 1
        return
    if aggressor not in ('BUY', 'SELL'):
        _skipped_print_count += 1
        _skip_reasons['mid_aggressor'] += 1
        return
    if not (size > 0 and price > 0 and strike > 0):
        _skipped_print_count += 1
        _skip_reasons['zero_size_price_strike'] += 1
        return

    # 0DTE filter: dte_key must match today
    today_key = _today_dte_key()
    if not dte_key or dte_key != today_key:
        _skipped_print_count += 1
        _skip_reasons['not_0dte'] += 1
        _skip_reasons[f'dte_seen_{dte_key or "none"}'] += 1
        return

    # Get live spot (from schwab_bridge)
    spot = 0.0
    try:
        from background_engine import schwab_bridge as _sb
        spot = float(getattr(_sb, '_latest_qqq', 0.0) or 0.0)
        if spot <= 0:
            spots = getattr(_sb, '_latest_spot_by_ticker', {}) or {}
            spot = float(spots.get(ANALYSIS_TICKER, 0.0) or 0.0)
    except Exception:
        pass
    if spot <= 0:
        _skipped_print_count += 1
        _skip_reasons['no_spot'] += 1
        return

    # Got past all filters — count accepted
    _skip_reasons['accepted'] += 1

    # Normalise side
    side_norm = 'C' if option_side and option_side.upper().startswith('C') else 'P'

    zone = _classify_zone(strike, spot)
    is_buy = (aggressor == 'BUY')
    premium = float(size) * float(price) * 100.0   # × 100 multiplier

    with _state_lock:
        _maybe_rotate_session()
        if _session_start_ts == 0:
            _session_start_ts = time.time()

        z = _ensure_zone(zone, side_norm)
        z['volume_today']  += int(size)
        z['premium_today'] += premium
        z['last_print_ts']  = ts_ms / 1000.0
        z['strike_volumes'][strike] += int(size)
        if is_buy:
            z['buy_count'] += 1
            z['buy_size']  += int(size)
            z['strike_buy_size'][strike] += int(size)
        else:
            z['sell_count'] += 1
            z['sell_size']  += int(size)
            z['strike_sell_size'][strike] += int(size)

        # Recent prints buffer
        _recent_prints.append({
            'ts':        ts_ms / 1000.0,
            'occ':       occ_symbol,
            'strike':    strike,
            'side':      side_norm,
            'size':      int(size),
            'price':     round(price, 4),
            'premium':   round(premium, 2),
            'exch':      exchange or '',
            'aggressor': aggressor,
            'zone':      zone,
            'dist_pct':  round(((strike - spot) / spot) * 100.0, 4),
        })


def _classify_regime(zones_summary: dict, recent_prints: list) -> tuple:
    """Return (regime, strength, rationale).

    Regime ∈ {NORMAL, ACTIVE, EXTREME, NO_DATA}.
    """
    atm_vol = zones_summary.get('ATM', {}).get('total_volume', 0)
    wing_vol = (zones_summary.get('NEAR_WING', {}).get('total_volume', 0) +
                zones_summary.get('DEEP_WING', {}).get('total_volume', 0) +
                zones_summary.get('TAIL', {}).get('total_volume', 0))

    if atm_vol == 0 and wing_vol == 0:
        return ('NO_DATA', 0.0, 'no 0DTE QQQ prints yet')

    ratio = (wing_vol / atm_vol) if atm_vol > 0 else float('inf')

    # Tail BUY trigger — single fat tail buy
    tail_buy_trigger = False
    tail_trigger_print = None
    for p in reversed(recent_prints):
        if p['zone'] == 'TAIL' and p['aggressor'] == 'BUY' and p['size'] >= TAIL_BUY_TRIGGER_SIZE:
            tail_buy_trigger = True
            tail_trigger_print = p
            break

    if ratio >= WING_RATIO_EXTREME or tail_buy_trigger:
        # EXTREME — strength scales with how much above threshold
        if ratio == float('inf'):
            strength = 1.0
        else:
            strength = min(1.0, (ratio - WING_RATIO_EXTREME) / 1.0 + 0.6)
        rat = (
            f'wing/ATM volume ratio {ratio:.2f}× ≥ {WING_RATIO_EXTREME:.2f}: '
            f'institutional wing activity dominating chain'
        )
        if tail_buy_trigger and tail_trigger_print:
            rat += (
                f'; tail BUY {tail_trigger_print["size"]}@'
                f'${tail_trigger_print["strike"]:.2f} '
                f'({tail_trigger_print["dist_pct"]:+.2f}%) '
                f'{tail_trigger_print["side"]}'
            )
        return ('EXTREME', round(strength, 4), rat)

    if ratio >= WING_RATIO_ACTIVE:
        strength = min(1.0, (ratio - WING_RATIO_ACTIVE) / (WING_RATIO_EXTREME - WING_RATIO_ACTIVE))
        return (
            'ACTIVE',
            round(strength, 4),
            f'wing/ATM volume ratio {ratio:.2f}× — wings building'
        )

    return (
        'NORMAL',
        round(min(1.0, ratio / WING_RATIO_ACTIVE), 4),
        f'wing/ATM ratio {ratio:.2f}× — concentrated near ATM'
    )


def compute_state() -> dict:
    """Snapshot current wing-tracker state. Called from intel compute loop."""
    now_ts = time.time()
    with _state_lock:
        _maybe_rotate_session()

        # Get spot (for header display)
        spot = 0.0
        try:
            from background_engine import schwab_bridge as _sb
            spot = float(getattr(_sb, '_latest_qqq', 0.0) or 0.0)
        except Exception:
            pass

        # Build zone summaries
        zones_summary = {}
        top_active_strikes = []
        for zone in ZONES:
            for side in ('C', 'P'):
                key = (zone, side)
                z = _state.get(key)
                if z is None:
                    continue
                z_summary = zones_summary.setdefault(zone, {
                    'total_volume': 0, 'total_premium': 0.0,
                    'buy_count':    0, 'sell_count': 0,
                    'buy_size':     0, 'sell_size':  0,
                    'call_volume':  0, 'put_volume': 0,
                    'top_strikes':  [],
                })
                z_summary['total_volume']  += z['volume_today']
                z_summary['total_premium'] += z['premium_today']
                z_summary['buy_count']     += z['buy_count']
                z_summary['sell_count']    += z['sell_count']
                z_summary['buy_size']      += z['buy_size']
                z_summary['sell_size']     += z['sell_size']
                if side == 'C':
                    z_summary['call_volume'] += z['volume_today']
                else:
                    z_summary['put_volume']  += z['volume_today']

                # Top strikes within this zone × side
                strikes_sorted = sorted(z['strike_volumes'].items(),
                                         key=lambda x: x[1], reverse=True)
                for K, vol in strikes_sorted[:TOP_STRIKES_PER_ZONE]:
                    bs = z['strike_buy_size'].get(K, 0)
                    ss = z['strike_sell_size'].get(K, 0)
                    skew = (bs - ss) / max(1, bs + ss)
                    top_active_strikes.append({
                        'strike':    round(float(K), 2),
                        'side':      side,
                        'zone':      zone,
                        'volume':    int(vol),
                        'buy_size':  int(bs),
                        'sell_size': int(ss),
                        'aggressor_skew': round(skew, 4),
                        'dist_pct':  round(((K - spot) / spot) * 100.0, 4) if spot > 0 else None,
                    })

        # Sort overall top strikes by volume (cap to 10 for the panel)
        top_active_strikes.sort(key=lambda x: x['volume'], reverse=True)
        top_active_strikes = top_active_strikes[:10]

        # Round zone summaries for JSON
        for zone in zones_summary:
            zs = zones_summary[zone]
            zs['total_premium'] = round(zs['total_premium'], 2)

        # Aggregate net dealer Δ-equivalent (DERIVED estimate using -0.5 puts, +0.5 calls)
        # For wings, |delta| < 0.5 — rough proxy: 0.20 deep-wing, 0.10 tail
        # This is best-effort — true delta requires per-contract Greeks.
        zone_delta_proxy = {'ATM': 0.5, 'NEAR_WING': 0.30, 'DEEP_WING': 0.15, 'TAIL': 0.05}
        net_dealer_delta_est = 0.0
        for (zone, side), z in _state.items():
            d_proxy = zone_delta_proxy.get(zone, 0.0)
            if side == 'C':
                # Call BUY → dealer short → must buy underlying (positive hedge bias)
                net_dealer_delta_est += (z['buy_size'] - z['sell_size']) * d_proxy * 100
            else:
                # Put BUY → dealer long puts → -delta → must sell underlying (negative bias)
                net_dealer_delta_est -= (z['buy_size'] - z['sell_size']) * d_proxy * 100

        # Recent prints (snapshot copy)
        recent = list(_recent_prints)

        # Regime
        regime, strength, rationale = _classify_regime(zones_summary, recent)

        state = {
            'ticker':              ANALYSIS_TICKER,
            'spot':                round(spot, 4) if spot > 0 else 0.0,
            'dte_key':             _session_dte_key,
            'session_start_ts':    _session_start_ts,
            'session_age_sec':     round(now_ts - _session_start_ts, 1) if _session_start_ts else 0,
            'zones':               zones_summary,
            'top_strikes':         top_active_strikes,
            'recent_prints':       recent[-20:],
            'regime':              regime,
            'regime_strength':     strength,
            'rationale':           rationale,
            'net_dealer_delta_est_shares': round(net_dealer_delta_est, 1),
            'total_print_count':   _total_print_count,
            'skipped_print_count': _skipped_print_count,
            'data_ts':             now_ts,
            'server_time':         now_ts,
            'reason':              None if regime != 'NO_DATA' else rationale,
        }

    _write_ledger({
        'ts':                  now_ts,
        'dte_key':             state['dte_key'],
        'spot':                state['spot'],
        'regime':              regime,
        'strength':            strength,
        'atm_volume':          (zones_summary.get('ATM', {}) or {}).get('total_volume', 0),
        'near_wing_volume':    (zones_summary.get('NEAR_WING', {}) or {}).get('total_volume', 0),
        'deep_wing_volume':    (zones_summary.get('DEEP_WING', {}) or {}).get('total_volume', 0),
        'tail_volume':         (zones_summary.get('TAIL', {}) or {}).get('total_volume', 0),
        'net_dealer_delta_est_shares': state['net_dealer_delta_est_shares'],
    })

    return state


def get_state() -> dict:
    """REST handler — return current state."""
    return compute_state()


def get_stats() -> dict:
    """Diagnostic counters — used by /api/_debug/wing_tracker/stats."""
    with _state_lock:
        return {
            'total_print_count':   _total_print_count,
            'skipped_print_count': _skipped_print_count,
            'session_dte_key':     _session_dte_key,
            'today_dte_key':       _today_dte_key(),
            'session_start_ts':    _session_start_ts,
            'state_keys':          list(_state.keys()) if _state else [],
            'recent_prints_size':  len(_recent_prints),
            'skip_reasons':        dict(_skip_reasons),
        }


# ── Outcome ledger (per-day JSONL) ───────────────────────────────────────────

def _ledger_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f'wing_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append per-cycle wing snapshot. Used offline to validate that
    EXTREME regime → next-15min spot move ≥0.10% (target hit-rate ≥55%)."""
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
            log.debug(f"[WING] ledger write err: {e}")
