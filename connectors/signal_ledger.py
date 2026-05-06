"""Signal Ledger — ground-truth tracker for wall_signals conviction scores.

Problem: wall_signals produces continuation (C) + fade (F) scores. Are those
scores predictive? Without outcome tracking, nobody knows. This module
closes the loop.

What it does:
  1. On every spot-cross of a known wall, write a ledger entry capturing
     (C, F, QQQ spot, NQ mid, pro-rata ratio, pull count, context)
  2. At 5/10/15 minutes after each entry, record the observed NQ mid delta
  3. Classify outcome: +1 aligned move ≥HIT_DELTA_NQ, 0 noise, −1 opposed
  4. Compute rolling hit rate for the UI strip (actionable C/F > threshold
     vs. non-actionable baseline — the GAP is the edge)

Everything persists to disk (JSONL, one file per day). The in-memory ring
holds up to LEDGER_CAP entries for the REST endpoint.

Zero-guessing discipline:
  - ACTIONABLE_THRESHOLD is CONFIGURED (default 0.3, user override via API)
  - HIT_DELTA_NQ is CONFIGURED to the user's stated "10-30 NQ points" goal
  - WINDOWS_MIN are CONFIGURED to the stated "5-15 min" horizon — any caller
    can pass different windows at query time
  - Cooldown is STRUCTURAL — a new cross on the same wall requires spot to
    leave the proximity band first (the data defines when "a new cross" is
    a distinct event; not a clock)
  - No magnitude thresholds for "is this worth logging" — every cross logs,
    and the actionable vs baseline split happens at query time
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── CONFIGURED constants ───────────────────────────────────────────────────
# All values documented in docs/MEASURED_VALUES.md. User can override via
# REST params at query time where relevant.

# A score ≥ this is considered "actionable" for hit-rate bucketing. Below
# this is the "baseline" bucket. The GAP between them is the edge signal.
# Documented as CONFIGURED; tune after ≥ 200 entries accumulate.
ACTIONABLE_THRESHOLD: float = 0.30

# Minimum NQ move (signed) to count as a "hit" in the aligned direction.
# Set from user's stated goal: "10-30 NQ points in 5-15 min". Floor at 10.
HIT_DELTA_NQ: float = 10.0

# Outcome check windows (minutes after the crossing).
# Stated trade horizon: 5-15 min. Track all three; any hit within counts.
WINDOWS_MIN: tuple = (5, 10, 15)

# In-memory ring cap. Disk JSONL is the source of truth; ring is for REST.
# Sized for ~2 weeks of typical fire density with slack.
LEDGER_CAP: int = 2000

# How long a wall must be "out of proximity" before a new cross is eligible.
# STRUCTURAL: gating on spot movement (data), not clock. 2× proximity_pct
# means spot has to leave the band by its own width before returning, which
# distinguishes a genuine second crossing from oscillation noise.
COOLDOWN_PROX_MULT: float = 2.0

# Disk log root — matches mm_events convention (logs/ dir, date-stamped file).
_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs'
)

# ── State ─────────────────────────────────────────────────────────────────
_ledger: deque = deque(maxlen=LEDGER_CAP)  # in-memory ring
_by_id: dict = {}                           # id → entry (same objects as ring)
_state_lock = Lock()

# Per-wall cross cooldown: (ticker, wall_name) → {'armed': True, 'last_ts': ...}
# Populated by record_crossing; update_arm_state consumed every flush.
_wall_state: dict = {}

# NQ mid feed callback. Set by schwab_bridge at startup so the ledger can
# query the canonical NQ mid (TopStepX L2, per CLAUDE.md) whenever it needs
# to snapshot or finalize an outcome. None means "NQ mid unavailable" and
# entries simply carry null values.
_nq_mid_getter: Optional[Callable[[], float]] = None

# Disk log file handle (opened lazily, rolled at session boundary).
_log_fh = None
_log_date: Optional[str] = None


# ── Session / disk handling ────────────────────────────────────────────────

def _et_date_str(ts: Optional[float] = None) -> str:
    """Return YYYYMMDD in ET (US/Eastern offset handled by standard lib)."""
    if ts is None:
        ts = time.time()
    # ET is UTC-4 (EDT) April-October, UTC-5 (EST) rest of year. Use a naive
    # offset matching CLAUDE.md _utcToET pattern — only used for file naming
    # so sub-hour precision doesn't matter. Errs on the side of "day starts
    # earlier than actual ET midnight" for auto-reset robustness.
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) - timedelta(hours=4)
    return dt.strftime('%Y%m%d')


def _ensure_log_fh() -> None:
    """Open or roll the JSONL log file based on ET date. Idempotent."""
    global _log_fh, _log_date
    today = _et_date_str()
    if _log_date == today and _log_fh is not None:
        return
    try:
        if _log_fh is not None:
            _log_fh.close()
    except Exception:
        pass
    os.makedirs(_LOG_DIR, exist_ok=True)
    path = os.path.join(_LOG_DIR, f'signal_ledger_{today}.jsonl')
    try:
        _log_fh = open(path, 'a', buffering=1)  # line-buffered
        _log_date = today
        log.info(f'[SIGNAL-LEDGER] opened {path}')
    except Exception as e:
        log.warning(f'[SIGNAL-LEDGER] cannot open log: {e}')
        _log_fh = None


def _write_jsonl(entry: dict) -> None:
    """Append a ledger entry to today's JSONL. Safe — swallows errors."""
    _ensure_log_fh()
    if _log_fh is None:
        return
    try:
        _log_fh.write(json.dumps(entry, default=str) + '\n')
    except Exception as e:
        log.debug(f'[SIGNAL-LEDGER] write failed: {e}')


# ── NQ mid feed wiring ─────────────────────────────────────────────────────

def set_nq_mid_getter(fn: Callable[[], float]) -> None:
    """schwab_bridge calls this at startup with a reference to `_get_nq_mid`.
    Without this set, entries still log but carry null NQ mid / null outcomes.
    """
    global _nq_mid_getter
    _nq_mid_getter = fn
    log.info('[SIGNAL-LEDGER] NQ mid getter wired')


def _get_nq_mid() -> Optional[float]:
    if _nq_mid_getter is None:
        return None
    try:
        v = _nq_mid_getter()
        return float(v) if v and v > 0 else None
    except Exception:
        return None


# ── Public API ─────────────────────────────────────────────────────────────

def record_crossing(*, ticker: str, wall: str, strike: float, direction: str,
                    C: float, F: float, qqq_spot: float,
                    prorata_ratio: float, prints_at_strike: int,
                    venues_pulled: int, proximity_pct: float,
                    regime: str = 'unknown',
                    dealer_net_at_strike: float = 0.0,
                    dealer_net_normalized: float = 0.0,
                    expected_direction: Optional[str] = None,
                    strike_side: Optional[str] = None,
                    gamma_flip: float = 0.0,
                    now_ts: Optional[float] = None) -> Optional[str]:
    """Log a wall-crossing event.

    Called by the wall_signals flush loop when a wall's `just_crossed`
    transitions from False→True (a new crossing was seen since last flush).

    Cooldown rule (STRUCTURAL): if this wall previously crossed and hasn't
    since moved `COOLDOWN_PROX_MULT × proximity_pct` out of the band, skip
    the log (same event, not a new crossing).

    Signed-gamma context (new, optional — defaults preserve existing callers):
      regime:                'long_gamma' | 'short_gamma' | 'unknown' (spot vs flip)
      dealer_net_at_strike:  signed dollar gamma AT this wall's strike
      dealer_net_normalized: -1..+1 (AT-strike / peak across all strikes)
      expected_direction:    'up' | 'down' | None — what the sign convention
                             PREDICTS NQ should do given the cross direction +
                             dealer_net sign at this strike
      strike_side:           'above_flip' | 'below_flip' — strike position
      gamma_flip:            flip strike at fire time

    Returns the new entry id, or None if skipped by cooldown.
    """
    now = now_ts if now_ts is not None else time.time()

    with _state_lock:
        # Cooldown check — same wall, still in proximity → same event, skip.
        state = _wall_state.get((ticker, wall))
        if state is not None and state.get('armed') is False:
            distance_pct = abs(qqq_spot - strike) / strike if strike else 0
            if distance_pct <= proximity_pct * COOLDOWN_PROX_MULT:
                return None
            # Far enough away — re-arm.
            state['armed'] = True

        # Fresh crossing: write ledger entry.
        nq_mid = _get_nq_mid()
        entry_id = uuid.uuid4().hex[:12]
        entry = {
            'id': entry_id,
            'fired_ts': now,
            'ticker': ticker,
            'wall': wall,
            'strike': float(strike),
            'direction': direction,
            'C_at_fire': float(C or 0),
            'F_at_fire': float(F or 0),
            'qqq_spot': float(qqq_spot or 0),
            'nq_mid': nq_mid,
            'prorata_ratio': float(prorata_ratio or 0),
            'prints_at_strike': int(prints_at_strike or 0),
            'venues_pulled': int(venues_pulled or 0),
            'proximity_pct': float(proximity_pct),
            # ── Signed-gamma context at fire time ──
            'regime':                regime or 'unknown',
            'dealer_net_at_strike':  float(dealer_net_at_strike or 0),
            'dealer_net_normalized': float(dealer_net_normalized or 0),
            'expected_direction':    expected_direction,
            'strike_side':           strike_side,
            'gamma_flip_at_fire':    float(gamma_flip or 0),
            # Outcome slots — filled by finalize_outcomes.
            'outcomes': {str(m): None for m in WINDOWS_MIN},  # nq_mid_at_window
            'deltas':   {str(m): None for m in WINDOWS_MIN},  # signed nq_delta
            'outcome': None,   # +1 / 0 / -1 once any window hits or all finalize
            'outcome_vs_sign': None,  # +1/0/-1 using expected_direction (signed-gamma) as truth
            'finalized_ts': None,
        }
        _ledger.append(entry)
        _by_id[entry_id] = entry
        # Disarm this wall until spot moves out of the band.
        _wall_state[(ticker, wall)] = {'armed': False, 'last_ts': now}
        # Persist.
        _write_jsonl({'type': 'crossing', **entry})
        return entry_id


def update_arm_state(*, ticker: str, wall: str, distance_pct: float,
                     proximity_pct: float) -> None:
    """Called on every flush for every wall. Re-arms a wall once spot has
    moved out of the cooldown band (distance > COOLDOWN_PROX_MULT * prox).
    Cheap — just a state bump.
    """
    with _state_lock:
        state = _wall_state.get((ticker, wall))
        if state is None or state.get('armed') is True:
            return
        if distance_pct > proximity_pct * COOLDOWN_PROX_MULT:
            state['armed'] = True


def finalize_outcomes(now_ts: Optional[float] = None) -> int:
    """Iterate non-finalized entries; for each window that has elapsed,
    record the current NQ mid and signed delta. Finalize the entry once
    the longest window has passed.

    Returns the number of entries updated this pass.
    """
    now = now_ts if now_ts is not None else time.time()
    updated = 0
    max_window_sec = max(WINDOWS_MIN) * 60
    with _state_lock:
        # Iterate over a snapshot since we're mutating entries in place.
        to_check = [e for e in _ledger if e.get('finalized_ts') is None]

    for entry in to_check:
        age_sec = now - entry['fired_ts']
        if age_sec < 60:
            # Too fresh — smallest window is 5min. Skip.
            continue
        nq_now = _get_nq_mid()
        changed = False
        # Track both: outcome vs raw-direction (naive) and outcome vs
        # expected_direction (signed-gamma prediction). The gap between the
        # two tells us whether the sign convention holds in practice.
        any_hit_dir = False
        any_opp_dir = False
        any_hit_sig = False
        any_opp_sig = False
        dir_sign = 1 if entry.get('direction') == 'up' else -1
        ed = entry.get('expected_direction')
        if ed == 'up':
            sig_sign = 1
        elif ed == 'down':
            sig_sign = -1
        else:
            sig_sign = 0   # no sign prediction available — outcome_vs_sign stays None

        for m in WINDOWS_MIN:
            window_key = str(m)
            if entry['outcomes'].get(window_key) is not None:
                # Already recorded this window — re-evaluate hit/opp flags.
                delta_already = entry['deltas'].get(window_key)
                if delta_already is not None:
                    s_dir = delta_already * dir_sign
                    if s_dir >= HIT_DELTA_NQ:   any_hit_dir = True
                    elif s_dir <= -HIT_DELTA_NQ: any_opp_dir = True
                    if sig_sign != 0:
                        s_sig = delta_already * sig_sign
                        if s_sig >= HIT_DELTA_NQ:   any_hit_sig = True
                        elif s_sig <= -HIT_DELTA_NQ: any_opp_sig = True
                continue
            # Window not yet recorded — has enough time passed?
            if age_sec < m * 60:
                continue
            # Record.
            if nq_now is None or entry.get('nq_mid') is None:
                entry['outcomes'][window_key] = nq_now
                entry['deltas'][window_key] = None
            else:
                entry['outcomes'][window_key] = nq_now
                delta = nq_now - entry['nq_mid']
                entry['deltas'][window_key] = round(delta, 2)
                s_dir = delta * dir_sign
                if s_dir >= HIT_DELTA_NQ:   any_hit_dir = True
                elif s_dir <= -HIT_DELTA_NQ: any_opp_dir = True
                if sig_sign != 0:
                    s_sig = delta * sig_sign
                    if s_sig >= HIT_DELTA_NQ:   any_hit_sig = True
                    elif s_sig <= -HIT_DELTA_NQ: any_opp_sig = True
            changed = True

        if age_sec >= max_window_sec:
            # Finalize both outcome channels.
            if any_hit_dir:   entry['outcome'] = 1
            elif any_opp_dir: entry['outcome'] = -1
            else:             entry['outcome'] = 0
            if sig_sign != 0:
                if any_hit_sig:   entry['outcome_vs_sign'] = 1
                elif any_opp_sig: entry['outcome_vs_sign'] = -1
                else:             entry['outcome_vs_sign'] = 0
            entry['finalized_ts'] = now
            _write_jsonl({'type': 'finalized', 'id': entry['id'],
                          'outcome': entry['outcome'],
                          'outcome_vs_sign': entry['outcome_vs_sign'],
                          'deltas': entry['deltas']})
            changed = True

        if changed:
            updated += 1

    return updated


def get_hit_rate(hours: float = 24.0,
                 actionable_threshold: Optional[float] = None,
                 hit_delta_nq: Optional[float] = None) -> dict:
    """Summary stats for the UI strip.

    Returns counts + rates split by actionable / baseline so the GAP
    (actionable rate − baseline rate) is visible. The gap is the signal.
    """
    at = actionable_threshold if actionable_threshold is not None else ACTIONABLE_THRESHOLD
    hd = hit_delta_nq if hit_delta_nq is not None else HIT_DELTA_NQ
    now = time.time()
    cutoff = now - hours * 3600

    with _state_lock:
        entries = [e for e in _ledger if e.get('fired_ts', 0) >= cutoff]

    actionable = {'n': 0, 'hit': 0, 'miss': 0, 'opp': 0, 'pending': 0,
                  'avg_aligned_pts': 0.0, 'avg_opposed_pts': 0.0}
    baseline   = {'n': 0, 'hit': 0, 'miss': 0, 'opp': 0, 'pending': 0,
                  'avg_aligned_pts': 0.0, 'avg_opposed_pts': 0.0}

    aligned_pts_a: list = []
    opposed_pts_a: list = []
    aligned_pts_b: list = []
    opposed_pts_b: list = []

    for e in entries:
        C = e.get('C_at_fire') or 0
        F = e.get('F_at_fire') or 0
        bucket = actionable if max(C, F) >= at else baseline
        bucket['n'] += 1
        # Determine outcome using user-supplied hit_delta_nq (might differ
        # from entry's fire-time HIT_DELTA_NQ if query-time override).
        if e.get('outcome') is None:
            # Possibly finalized with different threshold — recompute from deltas.
            deltas = (e.get('deltas') or {})
            any_hit = False
            any_opp = False
            dir_sign = 1 if e.get('direction') == 'up' else -1
            for m, d in deltas.items():
                if d is None:
                    continue
                signed = d * dir_sign
                if signed >= hd:
                    any_hit = True
                elif signed <= -hd:
                    any_opp = True
            if e.get('finalized_ts') is not None:
                if any_hit:   bucket['hit'] += 1
                elif any_opp: bucket['opp'] += 1
                else:         bucket['miss'] += 1
            else:
                bucket['pending'] += 1
        else:
            # Already finalized with default threshold.
            # Reclassify at user's hit_delta_nq if they overrode.
            deltas = (e.get('deltas') or {})
            any_hit = False
            any_opp = False
            dir_sign = 1 if e.get('direction') == 'up' else -1
            for m, d in deltas.items():
                if d is None:
                    continue
                signed = d * dir_sign
                if signed >= hd: any_hit = True
                elif signed <= -hd: any_opp = True
            if any_hit:   bucket['hit'] += 1
            elif any_opp: bucket['opp'] += 1
            else:         bucket['miss'] += 1

        # Accumulate direction-signed max-window delta for avg calcs.
        deltas = e.get('deltas') or {}
        dir_sign = 1 if e.get('direction') == 'up' else -1
        best_aligned = None
        worst_opposed = None
        for d in deltas.values():
            if d is None: continue
            signed = d * dir_sign
            if signed > 0 and (best_aligned is None or signed > best_aligned):
                best_aligned = signed
            if signed < 0 and (worst_opposed is None or signed < worst_opposed):
                worst_opposed = signed
        pts_a = aligned_pts_a if bucket is actionable else aligned_pts_b
        pts_o = opposed_pts_a if bucket is actionable else opposed_pts_b
        if best_aligned is not None:
            pts_a.append(best_aligned)
        if worst_opposed is not None:
            pts_o.append(worst_opposed)

    def _avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else 0.0

    actionable['avg_aligned_pts'] = _avg(aligned_pts_a)
    actionable['avg_opposed_pts'] = _avg(opposed_pts_a)
    baseline['avg_aligned_pts']   = _avg(aligned_pts_b)
    baseline['avg_opposed_pts']   = _avg(opposed_pts_b)

    def _rate(b):
        finalized = b['hit'] + b['opp'] + b['miss']
        return round(b['hit'] / finalized, 4) if finalized else 0.0

    a_rate = _rate(actionable)
    b_rate = _rate(baseline)
    edge = round(a_rate - b_rate, 4)

    # ── Regime breakdown + sign-convention validation ──────────────────────
    # Split entries by regime + compute hit rates using BOTH outcome channels
    # (naive direction vs signed-gamma expected_direction). The delta between
    # the two is our evidence that the sign convention holds (or doesn't).
    regime_stats: dict = {'long_gamma': {'n': 0, 'hit': 0, 'miss': 0, 'opp': 0, 'pending': 0,
                                          'hit_sig': 0, 'miss_sig': 0, 'opp_sig': 0},
                          'short_gamma':{'n': 0, 'hit': 0, 'miss': 0, 'opp': 0, 'pending': 0,
                                          'hit_sig': 0, 'miss_sig': 0, 'opp_sig': 0},
                          'unknown':    {'n': 0, 'hit': 0, 'miss': 0, 'opp': 0, 'pending': 0,
                                          'hit_sig': 0, 'miss_sig': 0, 'opp_sig': 0}}
    for e in entries:
        r = e.get('regime') or 'unknown'
        if r not in regime_stats:
            r = 'unknown'
        rb = regime_stats[r]
        rb['n'] += 1
        # Naive-direction outcome tally.
        out = e.get('outcome')
        if e.get('finalized_ts') is None:
            rb['pending'] += 1
        elif out == 1:
            rb['hit'] += 1
        elif out == -1:
            rb['opp'] += 1
        else:
            rb['miss'] += 1
        # Signed-gamma outcome tally (only entries with expected_direction set).
        out_sig = e.get('outcome_vs_sign')
        if out_sig == 1:
            rb['hit_sig'] += 1
        elif out_sig == -1:
            rb['opp_sig'] += 1
        elif out_sig == 0:
            rb['miss_sig'] += 1
        # out_sig None → no prediction was made; skip counting

    def _rate_key(b, hit_key, miss_key, opp_key):
        finalized = b.get(hit_key, 0) + b.get(opp_key, 0) + b.get(miss_key, 0)
        return round(b[hit_key] / finalized, 4) if finalized else 0.0

    for r in regime_stats:
        rb = regime_stats[r]
        rb['hit_rate'] = _rate_key(rb, 'hit', 'miss', 'opp')
        rb['hit_rate_sig'] = _rate_key(rb, 'hit_sig', 'miss_sig', 'opp_sig')

    # Sign-convention edge: how much better does the signed-gamma prediction
    # perform vs the naive cross-direction assumption? Positive = sign convention
    # holds; negative = sign convention is backwards; ~0 = no discernible effect.
    total_finalized_sig_hit = sum(r.get('hit_sig', 0) for r in regime_stats.values())
    total_finalized_sig_tot = sum(r.get('hit_sig', 0) + r.get('miss_sig', 0) + r.get('opp_sig', 0)
                                   for r in regime_stats.values())
    total_finalized_dir_hit = sum(r.get('hit', 0) for r in regime_stats.values())
    total_finalized_dir_tot = sum(r.get('hit', 0) + r.get('miss', 0) + r.get('opp', 0)
                                   for r in regime_stats.values())
    sig_hit_rate = round(total_finalized_sig_hit / total_finalized_sig_tot, 4) if total_finalized_sig_tot else 0.0
    dir_hit_rate = round(total_finalized_dir_hit / total_finalized_dir_tot, 4) if total_finalized_dir_tot else 0.0
    sign_convention_edge = round(sig_hit_rate - dir_hit_rate, 4)

    return {
        'hours': hours,
        'actionable_threshold': at,
        'hit_delta_nq': hd,
        'windows_min': list(WINDOWS_MIN),
        'total': len(entries),
        'actionable': {**actionable, 'hit_rate': a_rate},
        'baseline':   {**baseline,   'hit_rate': b_rate},
        'edge_gap':   edge,
        # Signed-gamma regime breakdown (new).
        'regimes': regime_stats,
        'sig_hit_rate': sig_hit_rate,
        'dir_hit_rate': dir_hit_rate,
        'sign_convention_edge': sign_convention_edge,
        'last_fire':  (entries[-1] if entries else None),
    }


def get_recent(limit: int = 20) -> list:
    """Most recent `limit` entries, newest first."""
    with _state_lock:
        out = list(_ledger)[-limit:]
    out.reverse()
    return out


def get_wall_state(ticker: str) -> dict:
    """Armed/disarmed state for each wall on `ticker`. Used by the pane's
    state-machine line."""
    with _state_lock:
        return {
            wname: {
                'armed': s.get('armed', True),
                'last_ts': s.get('last_ts', 0),
            }
            for (tk, wname), s in _wall_state.items()
            if tk == ticker
        }


def on_session_open() -> None:
    """Called at 09:30 ET session boundary. Rolls the log file but keeps
    in-memory ring (we want to see the last few hours' hits when the market
    opens)."""
    _ensure_log_fh()
    log.info('[SIGNAL-LEDGER] session opened — log rolled')
