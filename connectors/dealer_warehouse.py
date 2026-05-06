"""
Dealer Warehouse Quality — per-strike commitment scorer.

For every QQQ strike that has active OPTIONS_BOOK depth (≤120 contracts in the
Schwab budget), this module quantifies how *genuine* the dealer commitment is:

  COMMITTED   high posted_time AND high catch-at-top rate
              → dealer is actually willing to absorb flow
  PHANTOM     high posted_time AND low catch-at-top rate
              → defensive over-quoting / HFT depth that vanishes when challenged
  ACTIVE      low posted_time AND high catch rate
              → in-and-out aggressive; quotes pulled fast, hits often
  INACTIVE    low both

Why this matters for 0DTE pinning:
  Pin Convergence amplifies pull when there's heavy γ × OI at a strike.
  But γ × OI assumes dealers will defend — if the depth is PHANTOM, the wall
  is paper. Warehouse Quality shows which walls the dealer is *committed* to.

Data path (every value already streaming, no new subscriptions):

  Schwab OPTIONS_BOOK             → mm_attribution._update_capture(...)
                                  → mm_attribution._capture[occ_sym]
  Schwab TIMESALE_OPTIONS / Tradier
                                  → mm_attribution._update_capture caught_at_top
                                  → mm_attribution._capture[occ_sym][exch].caught_at_top
  schwab_bridge._latest_qqq        → spot for distance scoring

Aggregation:
  For each captured `occ_sym`:
    1. Parse → (strike, side ∈ {C,P}, dte_key)
    2. Aggregate across exchanges:
        posted_time_s_total   = Σ (posted_bid_time + posted_ask_time)
        caught_count_total    = Σ caught_count
        caught_at_top_total   = Σ caught_at_top
        caught_at_level_total = Σ caught_at_level
        top_exch              = exch with max posted_share
    3. Score:
        catch_rate = caught_at_top_total / posted_time_s_total       (events/sec)
        commitment = caught_at_top_total · (caught_at_top_total / posted_time_s_total)
                     [DERIVED — rewards both volume AND fill rate]

Then group at the (strike, side) level by summing across DTE expirations.

Output state:
  {
    spot, ticker, contract_count, strike_count,
    strikes: [
      {K, side, dist_pct, posted_time_s, caught_count, caught_at_top,
       catch_rate, commitment_score, classification, top_exch, dte_count},
      ...
    ],
    top_committed: [...top 5 by commitment_score...],
    top_phantom:   [...top 5 by phantom_score...],
    totals: {posted_time_s, caught_at_top, contract_count},
    data_ts, server_time, reason
  }

Outcome ledger:
  logs/dealer_warehouse_outcomes_YYYYMMDD.jsonl — per-cycle compact summary.
  Used to validate that COMMITTED strikes hold (price doesn't break through)
  more often than PHANTOM strikes (target hit-rate ≥60%).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ── CONFIGURED constants (categorized in MEASURED_VALUES.md) ────────────────
COMMITTED_POSTED_MIN_S    = 60.0   # CONFIGURED — ≥60s of posted time = "established"
COMMITTED_CATCH_RATE_MIN  = 0.05   # CONFIGURED — ≥0.05 catches/sec at top = real fills
PHANTOM_POSTED_MIN_S      = 120.0  # CONFIGURED — ≥120s posted with low catch = phantom
PHANTOM_CATCH_RATE_MAX    = 0.005  # CONFIGURED — <0.005/s = effectively no fills
ANALYSIS_TICKER           = 'QQQ'  # STRUCTURAL — only QQQ has OPTIONS_BOOK budget
TOP_LIST_SIZE             = 5      # CONFIGURED — UI display cap for committed/phantom lists
HISTORY_CAP               = 240    # CONFIGURED — 20 min @ 5s, evolution debug

# OSI regex (matches wall_signals._OSI_RE convention — 6-char ticker padded)
_OSI_RE = re.compile(r"^([A-Z]{1,6})\s*(\d{6})([CP])(\d{8})$")

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
        'ticker':           ANALYSIS_TICKER,
        'spot':             0.0,
        'contract_count':   0,
        'strike_count':     0,
        'strikes':          [],
        'top_committed':    [],
        'top_phantom':      [],
        'totals':           {
            'posted_time_s':   0.0,
            'caught_at_top':   0,
            'contract_count':  0,
        },
        'history':          [],
        'data_ts':          0.0,
        'server_time':      time.time(),
        'reason':           reason,
    }


def _parse_osi(sym: str) -> Optional[dict]:
    """Extract {ticker, yymmdd, side, strike} from an OSI symbol."""
    if not sym:
        return None
    m = _OSI_RE.match(sym)
    if not m:
        return None
    return {
        'ticker': m.group(1),
        'yymmdd': m.group(2),
        'side':   m.group(3),
        'strike': int(m.group(4)) / 1000.0,
    }


def _classify_quality(posted_time_s: float, caught_at_top: int,
                        catch_rate: float) -> str:
    """Bucket commitment quality. See module docstring."""
    if posted_time_s >= PHANTOM_POSTED_MIN_S and catch_rate < PHANTOM_CATCH_RATE_MAX:
        return 'PHANTOM'
    if posted_time_s >= COMMITTED_POSTED_MIN_S and catch_rate >= COMMITTED_CATCH_RATE_MIN:
        return 'COMMITTED'
    if posted_time_s < COMMITTED_POSTED_MIN_S and caught_at_top > 0 and catch_rate >= COMMITTED_CATCH_RATE_MIN:
        return 'ACTIVE'
    return 'INACTIVE'


def compute_state() -> dict:
    """Build per-strike warehouse-quality snapshot from mm_attribution._capture."""
    try:
        from background_engine import schwab_bridge as _sb
    except Exception as e:
        return _empty_state(reason=f'sb_import_err:{e}')

    spot = float(getattr(_sb, '_latest_qqq', 0.0) or 0.0)

    # Pull capture state from mm_attribution
    try:
        from connectors import mm_attribution as _mma
    except Exception as e:
        return _empty_state(reason=f'mma_import_err:{e}')

    # Snapshot under mm_attribution's own lock for consistency
    capture_snapshot: dict = {}
    capture_totals_snapshot: dict = {}
    try:
        with _mma._state_lock:
            for sym, exchs in (_mma._capture or {}).items():
                capture_snapshot[sym] = {
                    e: dict(rec) for e, rec in (exchs or {}).items()
                }
            for sym, tots in (_mma._capture_totals or {}).items():
                capture_totals_snapshot[sym] = dict(tots)
    except Exception as e:
        return _empty_state(reason=f'capture_snapshot_err:{e}')

    if not capture_snapshot:
        return _empty_state(reason='no_capture_data')

    # Aggregate per-contract → per-(strike, side) across DTE expirations
    # Keyed by (strike, side, dte_key)
    contracts: list = []
    for sym, exchs in capture_snapshot.items():
        parsed = _parse_osi(sym.strip())
        if not parsed:
            continue
        if parsed['ticker'].strip() != ANALYSIS_TICKER:
            continue

        # Aggregate across exchanges
        posted_time_s    = 0.0
        caught_count     = 0
        caught_at_top    = 0
        caught_at_level  = 0
        per_exch_posted: dict = {}
        for exch, rec in exchs.items():
            pt = float(rec.get('posted_bid_time', 0) or 0) + \
                 float(rec.get('posted_ask_time', 0) or 0)
            posted_time_s    += pt
            caught_count     += int(rec.get('caught_count', 0) or 0)
            caught_at_top    += int(rec.get('caught_at_top', 0) or 0)
            caught_at_level  += int(rec.get('caught_at_level', 0) or 0)
            per_exch_posted[exch] = pt

        if posted_time_s <= 0 and caught_count == 0:
            continue

        catch_rate = (caught_at_top / posted_time_s) if posted_time_s > 0 else 0.0
        # Top venue by posted time
        top_exch = max(per_exch_posted, key=per_exch_posted.get) if per_exch_posted else ''

        # Commitment score (DERIVED — rewards both volume × fill rate)
        # commitment = caught_at_top × catch_rate
        # Higher score = more genuine commitment
        commitment_score = float(caught_at_top) * catch_rate

        # Phantom score (DERIVED — penalizes high posted with low fills)
        # phantom = posted_time × (1 - catch_rate / target_rate_threshold)
        # Higher = more phantom-like behaviour
        phantom_score = posted_time_s * max(0.0, 1.0 - (catch_rate / max(COMMITTED_CATCH_RATE_MIN, 1e-9)))

        classification = _classify_quality(posted_time_s, caught_at_top, catch_rate)

        contracts.append({
            'occ':              sym,
            'K':                parsed['strike'],
            'side':             parsed['side'],
            'dte_key':          parsed['yymmdd'],
            'posted_time_s':    round(posted_time_s, 1),
            'caught_count':     caught_count,
            'caught_at_top':    caught_at_top,
            'caught_at_level':  caught_at_level,
            'catch_rate':       round(catch_rate, 4),
            'commitment_score': round(commitment_score, 2),
            'phantom_score':    round(phantom_score, 2),
            'classification':   classification,
            'top_exch':         top_exch,
        })

    if not contracts:
        return _empty_state(reason='no_qqq_contracts_in_capture')

    # Aggregate at (strike, side) — sum over DTE expirations
    by_strike: dict = {}
    for c in contracts:
        key = (c['K'], c['side'])
        agg = by_strike.setdefault(key, {
            'K':                  c['K'],
            'side':               c['side'],
            'posted_time_s':      0.0,
            'caught_count':       0,
            'caught_at_top':      0,
            'caught_at_level':    0,
            'commitment_score':   0.0,
            'phantom_score':      0.0,
            'top_exch_posts':     defaultdict(float),  # exch → total posted
            'classifications':    defaultdict(int),    # classif → count
            'dte_count':          0,
        })
        agg['posted_time_s']    += c['posted_time_s']
        agg['caught_count']     += c['caught_count']
        agg['caught_at_top']    += c['caught_at_top']
        agg['caught_at_level']  += c['caught_at_level']
        agg['commitment_score'] += c['commitment_score']
        agg['phantom_score']    += c['phantom_score']
        agg['top_exch_posts'][c['top_exch']] += c['posted_time_s']
        agg['classifications'][c['classification']] += 1
        agg['dte_count']        += 1

    # Finalize aggregated rows
    strikes_out = []
    for (K, side), agg in by_strike.items():
        catch_rate = (agg['caught_at_top'] / agg['posted_time_s']) if agg['posted_time_s'] > 0 else 0.0
        # Strike-level classification: take the most common per-DTE classification,
        # else recompute from aggregated totals
        if agg['classifications']:
            top_class = max(agg['classifications'], key=agg['classifications'].get)
        else:
            top_class = _classify_quality(agg['posted_time_s'], agg['caught_at_top'], catch_rate)
        top_exch = max(agg['top_exch_posts'], key=agg['top_exch_posts'].get) \
            if agg['top_exch_posts'] else ''
        strikes_out.append({
            'K':                round(K, 4),
            'side':             side,
            'dist_pct':         round(((K - spot) / spot) * 100.0, 4) if spot > 0 else None,
            'posted_time_s':    round(agg['posted_time_s'], 1),
            'caught_count':     agg['caught_count'],
            'caught_at_top':    agg['caught_at_top'],
            'caught_at_level':  agg['caught_at_level'],
            'catch_rate':       round(catch_rate, 4),
            'commitment_score': round(agg['commitment_score'], 2),
            'phantom_score':    round(agg['phantom_score'], 2),
            'classification':   top_class,
            'top_exch':         top_exch,
            'dte_count':        agg['dte_count'],
        })

    # Sort by absolute distance from spot (closest first) for the main table
    strikes_out.sort(key=lambda r: abs(r.get('dist_pct') or 0))

    # Top committed and top phantom rankings
    top_committed = sorted(strikes_out, key=lambda r: -r['commitment_score'])[:TOP_LIST_SIZE]
    top_phantom   = sorted(strikes_out, key=lambda r: -r['phantom_score'])[:TOP_LIST_SIZE]

    totals = {
        'posted_time_s':   round(sum(r['posted_time_s'] for r in strikes_out), 1),
        'caught_at_top':   sum(r['caught_at_top'] for r in strikes_out),
        'contract_count':  len(contracts),
    }

    now_ts = time.time()
    state = {
        'ticker':           ANALYSIS_TICKER,
        'spot':             round(spot, 4) if spot > 0 else 0.0,
        'contract_count':   len(contracts),
        'strike_count':     len(strikes_out),
        'strikes':          strikes_out,
        'top_committed':    top_committed,
        'top_phantom':      top_phantom,
        'totals':           totals,
        'data_ts':          now_ts,
        'server_time':      now_ts,
        'reason':           None,
    }

    # Compact history sample
    sample = {
        'ts':              now_ts,
        'spot':            state['spot'],
        'strike_count':    state['strike_count'],
        'contract_count':  state['contract_count'],
        'total_posted_time_s': totals['posted_time_s'],
        'total_caught_at_top': totals['caught_at_top'],
        'top_committed_K':  (top_committed[0]['K'] if top_committed else None),
        'top_phantom_K':    (top_phantom[0]['K'] if top_phantom else None),
    }
    with _state_lock:
        _state_cache['latest'] = state
        _history.append(sample)
    _write_ledger(sample)
    return state


def get_state() -> dict:
    """REST handler — return cached state with history attached."""
    with _state_lock:
        cached = _state_cache.get('latest')
        if cached:
            out = dict(cached)
            out['history'] = list(_history)
            return out
    state = compute_state()
    with _state_lock:
        state['history'] = list(_history)
    return state


def get_warehouse_strength(strike: float, side: str = None) -> Optional[float]:
    """External API — returns commitment_score for a (K, side) pair, or
    average across both sides if `side` is None.

    Designed for `pin_convergence._strike_pin_score` to upgrade its
    `warehouse_strength` from oi_score-proxy to true MEASURED dealer commitment.
    Returns None if the strike is not in the cached state (no OPTIONS_BOOK
    coverage for that contract).
    """
    with _state_lock:
        state = _state_cache.get('latest')
        if not state:
            return None
        strikes = state.get('strikes') or []
        if side:
            for r in strikes:
                if abs(r['K'] - strike) < 1e-6 and r['side'] == side.upper():
                    return float(r['commitment_score'])
            return None
        # Both sides
        scores = [float(r['commitment_score']) for r in strikes
                   if abs(r['K'] - strike) < 1e-6]
        if not scores:
            return None
        return sum(scores) / len(scores)


# ── Outcome ledger (per-day JSONL) ───────────────────────────────────────────

def _ledger_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f'dealer_warehouse_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append per-cycle warehouse summary. Used offline to validate that
    COMMITTED strikes hold (don't break) and PHANTOM strikes break — target
    hit-rate ≥60% over 2 weeks."""
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
            log.debug(f"[WAREHOUSE] ledger write err: {e}")
