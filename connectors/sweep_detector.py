"""
Multi-strike option sweep detector — OBSERVATION-ONLY (post-2026-05-04 cleanup).

A "sweep" is the institutional fingerprint of large-block flow: 3+ option prints
walking adjacent strikes within ~500ms, all aggressor-side same direction. Equity-
side sweeps are already detected by `edge_detector.MultiVenueDepletion`; this
module fills the OPTIONS-side gap (no existing detector handles cross-strike
multi-print walks on the option chain).

WHAT WE PRODUCE (descriptive only):
  Identification:  id, underlying, dte_key, expiration
  Observable facts: option_side, direction, leg_count, total_size,
                    venue_sequence, venue_count, strike_range, time_span_ms,
                    first/last_print_ts, legs
  Δ-notional:      Σ size × Δ × 100 (signed by aggressor; pure aggregation,
                   no directional claim attached)

WHAT WE DELIBERATELY DO NOT PRODUCE:
  - expected_hedge_side / expected_hedge_shares: the dealer-hedging hypothesis
    (institutional sweep → dealers must hedge → equity follows) was empirically
    falsified — n=15,902 cleaned sweeps over 5 days, +0.27% edge over base rate
    (noise). Stripped 2026-05-05 along with the hedge_forecaster cross-validator
    that only made sense if the prediction was real. Conviction-score weight
    was zeroed earlier (W_INTEL_SWEEP=0). See sweep_audit.py results.
  - hf_alignment / hf_aligned / hf_side / hf_confidence: removed in same pass
    (the hedge_forecaster predictive output is also W=0).

Measurement-discipline (categorize every literal):
  SWEEP_WINDOW_MS         CONFIGURED  — multi-strike walk timescale
                                        (empirical from logs/dealer_prints_*.jsonl)
  ADJACENCY_DOLLARS       MEASURED    — 3 × QQQ near-ATM strike spacing ($1)
  MIN_LEGS                CONFIGURED  — 3 = institutional fingerprint floor
  MIN_TOTAL_SIZE          MEASURED    — P50 of meaningful sweeps from log analysis
  HISTORY_CAP             CONFIGURED  — UI display capacity
  BUFFER_RETENTION_MS     CONFIGURED  — 2× SWEEP_WINDOW_MS safety margin

Outcome ledger:
  Each completed sweep is also written to logs/sweep_outcomes_YYYYMMDD.jsonl
  for offline validation (predicted_hedge_side hit-rate vs observed equity flow).
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

# ── CONFIGURED / MEASURED constants (all categorized in MEASURED_VALUES.md) ─
SWEEP_WINDOW_MS       = 500     # CONFIGURED — multi-strike walk timescale
ADJACENCY_DOLLARS     = 3.0     # MEASURED — 3 × QQQ near-ATM strike spacing
MIN_LEGS              = 3       # CONFIGURED — institutional fingerprint floor
MIN_TOTAL_SIZE        = 50      # MEASURED — P50 of meaningful sweeps
HISTORY_CAP           = 200     # CONFIGURED — UI display capacity
BUFFER_RETENTION_MS   = 1000    # CONFIGURED — 2× SWEEP_WINDOW_MS retention

# 2026-05-04: out-of-order rejection. Tradier WS sometimes delivers cached
# prints with their original timestamp on (re)connect. If an old print lands
# in a buffer with newer prints, the per-print cutoff math fails and we get
# 62-hour "sweeps" (verified in audit on 22,233 records, 100% VIX/VIXW).
# Reject any print with ts_ms older than this threshold from wall-clock.
MAX_PRINT_AGE_MS      = 5_000   # CONFIGURED — 5s tolerance for normal jitter

# 2026-05-04: skip VIX/VIXW. Quality audit showed 100% of 26,758 VIX-family
# sweeps fail real-sweep criteria (single-venue 'C' on CBOE, sparse trades
# with batch delivery, slow timescale). Predicted-side audit showed 13.5%
# hit rate (catastrophic). Detector model assumes multi-venue SOR spray which
# doesn't apply to VIX microstructure. Re-evaluate if/when VIX gets a
# dedicated detector tuned to its microstructure.
SKIP_UNDERLYINGS      = frozenset({'VIX', 'VIXW'})

# 2026-05-04: TTL fingerprint dedup. Old code only checked the immediately-
# previous emitted sweep, so growing sweeps (legs 5→6→7→8→9 within 500ms)
# emitted 5 alerts. New design: hash each sweep on (root, dte, side, dir,
# strike_min, strike_max, first_print_ts_bucket) with TTL eviction. Same
# fingerprint within DEDUP_TTL_MS = no re-emit, regardless of leg growth.
DEDUP_TTL_MS          = 5_000   # CONFIGURED — typical institutional sweep
                                # finishes in <5s; longer = different parent

# ── Module state ─────────────────────────────────────────────────────────────
# _recent_prints: per-underlying time-bound deque of (ts_ms, sym, strike, side,
#                 size, price, exch, aggr) — pruned to BUFFER_RETENTION_MS
_recent_prints: dict = defaultdict(deque)
_completed_sweeps: deque = deque(maxlen=HISTORY_CAP)
_sweep_id_counter: int = 0
_state_lock = threading.RLock()

# Recently-emitted sweep fingerprints (for dedup). Keyed on the canonical
# fingerprint of the sweep (see _sweep_fingerprint), value is detected_at_ms.
# Pruned on each insert.
_recent_fingerprints: dict = {}

# Diagnostics
_stats: dict = {
    'prints_seen': 0,
    'prints_rejected_stale': 0,    # 2026-05-04: print ts > MAX_PRINT_AGE_MS old
    'prints_rejected_skip_root': 0, # 2026-05-04: VIX/VIXW skipped
    'sweeps_detected': 0,
    'sweeps_dropped_size': 0,
    'sweeps_dropped_legs': 0,
    'sweeps_dropped_mixed_side': 0,
    'sweeps_deduped': 0,           # 2026-05-04: fingerprint match within TTL
}

# Socket.IO registration
_socketio = None

# Outcome-ledger handle (rotating per-day)
_ledger_fh = None
_ledger_date: str = ''
_ledger_lock = threading.Lock()


def init(socketio) -> None:
    """Register the Socket.IO instance for emit_alert. Called once at startup
    from schwab_bridge after _socketio is set."""
    global _socketio
    _socketio = socketio
    log.info(f"[SWEEP] init — window={SWEEP_WINDOW_MS}ms adjacency=${ADJACENCY_DOLLARS} "
             f"min_legs={MIN_LEGS} min_size={MIN_TOTAL_SIZE}")


def on_print(underlying: str,
             occ_symbol: str,
             strike: float,
             option_side: str,
             size: int,
             price: float,
             ts_ms: int,
             exchange: str,
             aggressor: str,
             dte_key: Optional[str] = None) -> None:
    """Process one Tradier option print. May complete a sweep and emit alert.

    Inputs (pre-parsed by schwab_bridge._on_tradier_timesale):
      underlying:   'QQQ', 'SPX', 'AAPL', etc. (root, no padding)
      occ_symbol:   full OCC string (Tradier format, no padding)
      strike:       float — option strike price
      option_side:  'C' or 'P'
      size:         int — contracts traded
      price:        float — print price
      ts_ms:        int — epoch ms of the print
      exchange:     str — exchange MIC (single-letter or short code)
      aggressor:    'BUY' | 'SELL' | 'MID' — tick-rule classification
      dte_key:      optional 'YYMMDD' — parsed from OCC if None
    """
    global _stats

    # Filter: only pure-aggressor prints participate in sweep detection.
    if aggressor not in ('BUY', 'SELL'):
        return
    if size <= 0 or strike <= 0 or option_side not in ('C', 'P'):
        return
    if not underlying:
        return

    # 2026-05-04 — skip underlyings where the detector model doesn't fit.
    # VIX/VIXW microstructure (single dominant venue, sparse batched delivery)
    # produces 100% fake sweeps per the quality audit (n=26,758, all FAKE).
    if underlying in SKIP_UNDERLYINGS:
        _stats['prints_rejected_skip_root'] += 1
        return

    # 2026-05-04 — reject prints arriving with stale timestamps (Tradier WS
    # cached-message replay on reconnect would otherwise inject ts_ms days
    # old into the buffer, breaking the per-print cutoff math and producing
    # 62-hour "sweeps". Audit found 22,233 such records, all VIX-family.)
    now_ms = int(time.time() * 1000)
    if ts_ms <= 0 or (now_ms - ts_ms) > MAX_PRINT_AGE_MS:
        _stats['prints_rejected_stale'] += 1
        return

    # Parse DTE key if not supplied (OCC format: {root}{YYMMDD}{C|P}{strike})
    if dte_key is None:
        try:
            # Find first digit
            for i, ch in enumerate(occ_symbol):
                if ch.isdigit():
                    dte_key = occ_symbol[i:i+6]
                    break
        except Exception:
            return

    if not dte_key or len(dte_key) != 6:
        return

    _stats['prints_seen'] += 1

    record = (ts_ms, occ_symbol, float(strike), option_side, int(size),
              float(price), str(exchange), aggressor, dte_key)

    with _state_lock:
        # Append to per-underlying buffer + prune by retention.
        # 2026-05-04: cutoff is now wall-clock-based (now_ms), not ts_ms-based.
        # Old code used `cutoff = ts_ms - BUFFER_RETENTION_MS` which broke when
        # an old print arrived: cutoff went backwards in time and stale entries
        # stayed in the buffer next to fresh ones. Wall-clock cutoff means
        # nothing older than `now - 1s` ever survives, regardless of arrival
        # order or stale-print injection.
        prints = _recent_prints[underlying]
        prints.append(record)
        wall_cutoff = now_ms - BUFFER_RETENTION_MS
        while prints and prints[0][0] < wall_cutoff:
            prints.popleft()

        # Look for a sweep within the most recent SWEEP_WINDOW_MS.
        # Wall-clock cutoff (same reason as buffer prune above).
        sweep_window_cutoff = now_ms - SWEEP_WINDOW_MS
        window = [p for p in prints if p[0] >= sweep_window_cutoff]
        if len(window) < MIN_LEGS:
            return

        sweep = _find_sweep_in_window(underlying, window)
        if sweep is None:
            return

        # 2026-05-04: TTL fingerprint dedup. Old code only checked the
        # immediately-previous emitted sweep, so a growing sweep (legs 5→6→7)
        # within the 500ms window emitted 3 separate alerts. New design tracks
        # canonical fingerprints in a TTL set — same fingerprint within TTL
        # means same parent order → suppress.
        fp = _sweep_fingerprint(sweep)
        prev_emit_ts = _recent_fingerprints.get(fp)
        if prev_emit_ts is not None and (now_ms - prev_emit_ts) < DEDUP_TTL_MS:
            _stats['sweeps_deduped'] += 1
            return
        # Prune fingerprints older than TTL on each insert (keeps dict bounded)
        stale_fps = [k for k, v in _recent_fingerprints.items()
                     if (now_ms - v) >= DEDUP_TTL_MS]
        for k in stale_fps:
            _recent_fingerprints.pop(k, None)
        _recent_fingerprints[fp] = now_ms

        global _sweep_id_counter
        sweep['id'] = _sweep_id_counter
        _sweep_id_counter += 1
        _completed_sweeps.append(sweep)
        _stats['sweeps_detected'] += 1

        # Persist to outcome ledger (file write under separate lock so the
        # state lock isn't held during disk I/O)
        ledger_record = {
            'sweep_id':              sweep['id'],
            'detected_at_ms':        ts_ms,
            'underlying':            sweep['underlying'],
            'dte_key':               sweep['dte_key'],
            'option_side':           sweep['option_side'],
            'direction':             sweep['direction'],
            'leg_count':             sweep['leg_count'],
            'total_size':            sweep['total_size'],
            'notional_delta':        sweep['notional_delta'],
            # 2026-05-05 — stripped expected_hedge_side + expected_hedge_shares.
            # See note above _build_sweep_record for rationale.
            'venue_sequence':        sweep['venue_sequence'],
            'strike_range':          sweep['strike_range'],
            'time_span_ms':          sweep['time_span_ms'],
        }

    # Outside state lock: write ledger (always — for retrospective analysis)
    _write_ledger(ledger_record)

    # ── UI emission — DESCRIPTIVE ONLY (2026-05-04) ────────────────────────
    # Predictive fields stripped from socket payload because the directional
    # prediction (`expected_hedge_side`) was empirically demonstrated to have
    # ZERO edge over base rate:
    #
    #   Audit evidence (n=15,902 cleaned sweeps over 5 days):
    #     - QQQ ehs=BUY @ 5m:  hit 57.6%, base 57.33%, edge +0.27% (noise)
    #     - distributions of post-event moves are statistically identical
    #       for BUY-predicted and SELL-predicted sweeps (H1 deep-dive)
    #     - sweep size has zero correlation with |move|: r = -0.018 at 5m (H3)
    #     - aligned/inverted magnitudes are symmetric: ratio 1.02 (H2)
    #     - BUY signals UNDERPERFORM drift on tech single-names (H4)
    #
    # Conclusion: the dealer-hedging hypothesis (institutional sweep → dealer
    # must hedge → spot follows) does NOT hold in this market. Modern MMs run
    # delta-neutral books and absorb flow; the "must hedge" assumption is a
    # textbook construct that doesn't match observed behavior.
    #
    # What we KEEP emitting: the OBSERVABLE FACT that a multi-strike sweep
    # happened (root, dte, side, direction of option flow, leg count, total
    # size, strike range, venue spray, time span). All descriptive. The trader
    # forms their own directional view. No predicted_hedge_side is shipped.
    #
    # Disk ledger keeps the predictive fields (notional_delta, hf_alignment)
    # so future analysis can revisit the hypothesis with new data.
    # See: /tmp/sweep_audit.py, /tmp/sweep_quality_audit.py,
    #      /tmp/sweep_audit_v2.py, /tmp/sweep_inverted_audit.py,
    #      /tmp/sweep_zero_edge_deepdive.py
    if _socketio is not None:
        try:
            descriptive = {
                # Identification
                'id':             sweep['id'],
                'underlying':     sweep['underlying'],
                'dte_key':        sweep['dte_key'],
                'expiration':     sweep['expiration'],
                # Observable facts — what happened
                'option_side':    sweep['option_side'],   # 'C' or 'P'
                'direction':      sweep['direction'],     # 'BUY'/'SELL' (option-flow side)
                'leg_count':      sweep['leg_count'],
                'total_size':     sweep['total_size'],
                'venue_sequence': sweep['venue_sequence'],
                'venue_count':    sweep['venue_count'],
                'strike_range':   sweep['strike_range'],
                'time_span_ms':   sweep['time_span_ms'],
                'first_print_ts': sweep['first_print_ts'],
                'last_print_ts':  sweep['last_print_ts'],
                'legs':           sweep['legs'],
                # Δ-notional kept (descriptive — Σ size × Δ × 100, signed by aggressor)
                'notional_delta':   sweep['notional_delta'],
                'delta_resolved':   sweep['delta_resolved'],
                'delta_total_legs': sweep['delta_total_legs'],
                # Tag so consumers know this is observation, not prediction
                'kind':           'observable_event',
            }
            _socketio.emit('intel:sweep_alert', descriptive)
        except Exception as e:
            log.debug(f"[SWEEP] emit failed: {e}")


def _sweep_fingerprint(sweep: dict) -> tuple:
    """Canonical key for sweep dedup. Two sweeps with the same fingerprint are
    considered the same parent order — re-emission suppressed within DEDUP_TTL_MS.

    Includes the full strike range and direction so a *different* sweep on the
    same contract (e.g. one BUY then one SELL, or sweep at K=670-672 then later
    K=670-678 with new wider walk) gets a distinct fingerprint.
    """
    sr = sweep.get('strike_range') or [0, 0]
    return (
        sweep.get('underlying', ''),
        sweep.get('dte_key', ''),
        sweep.get('option_side', ''),
        sweep.get('direction', ''),
        round(float(sr[0]), 2),
        round(float(sr[1]), 2),
    )


def _find_sweep_in_window(underlying: str, window: list) -> Optional[dict]:
    """Pure function — return sweep dict if pattern matches, else None.

    Pattern (all required, all STRUCTURAL — no magnitude thresholds beyond
    the categorized constants above):
      - ≥ MIN_LEGS prints in window
      - All same DTE (same expiration)
      - All same option_side ('C' or 'P')
      - All same aggressor side ('BUY' or 'SELL')
      - Strikes are walking-adjacent (each consecutive pair ≤ ADJACENCY_DOLLARS)
      - At least 2 distinct strikes (walk pattern, not same-strike repeat)
      - Total size ≥ MIN_TOTAL_SIZE
    """
    global _stats

    # Group by (dte_key, option_side, aggressor) so we only consider
    # uniformly-directional, same-expiration, same-side flow.
    by_group: dict = defaultdict(list)
    for p in window:
        # p = (ts_ms, sym, strike, option_side, size, price, exch, aggr, dte)
        key = (p[8], p[3], p[7])  # (dte, option_side, aggressor)
        by_group[key].append(p)

    best: Optional[dict] = None
    for (dte_key, option_side, aggressor), group in by_group.items():
        if len(group) < MIN_LEGS:
            _stats['sweeps_dropped_legs'] += 1
            continue

        # Sort by strike ascending (walking pattern)
        sorted_legs = sorted(group, key=lambda x: x[2])

        # Distinct-strike check: at least 2 different strikes
        strikes = sorted([float(p[2]) for p in sorted_legs])
        unique_strikes = sorted(set(strikes))
        if len(unique_strikes) < 2:
            continue

        # Adjacency check: each consecutive UNIQUE strike pair ≤ ADJACENCY_DOLLARS
        adjacent = all(
            (unique_strikes[i + 1] - unique_strikes[i]) <= ADJACENCY_DOLLARS
            for i in range(len(unique_strikes) - 1)
        )
        if not adjacent:
            continue

        # Size minimum
        total_size = sum(int(p[4]) for p in sorted_legs)
        if total_size < MIN_TOTAL_SIZE:
            _stats['sweeps_dropped_size'] += 1
            continue

        # Build sweep record. Sort legs by ts_ms for proper venue sequence.
        legs_by_ts = sorted(sorted_legs, key=lambda x: x[0])
        record = _build_sweep_record(underlying, dte_key, legs_by_ts)

        # Track the best (most legs, then largest size)
        if best is None or record['leg_count'] > best['leg_count'] or \
           (record['leg_count'] == best['leg_count'] and
            record['total_size'] > best['total_size']):
            best = record

    return best


def _build_sweep_record(underlying: str, dte_key: str, legs: list) -> dict:
    """Build a structured sweep record with predicted follow-through.

    legs is ordered by ts_ms ascending.

    Each leg is (ts_ms, sym, strike, option_side, size, price, exch, aggr, dte).
    """
    direction = legs[0][7]            # 'BUY' or 'SELL'
    option_side = legs[0][3]           # 'C' or 'P'
    is_calls = (option_side == 'C')

    # Sum notional Δ from per-leg Δ × size × 100 (contract multiplier).
    # Pull live Δ from the GreekSurface instance held by schwab_bridge.
    # If unavailable (cold-tail strike not streaming), the leg's Δ is skipped
    # and delta_resolved tracks how many legs contributed; UI shows a partial-Δ
    # confidence indicator when resolved < total.
    notional_delta = 0.0
    delta_resolved = 0
    delta_total_legs = len(legs)
    try:
        # Lazy import — avoids circular dependency at module load time
        from background_engine import schwab_bridge as _sb
        gs = getattr(_sb, '_greek_surface', None)
        if gs is not None:
            for ts, sym, strike, opt_side, size, price, exch, aggr, dte in legs:
                d = gs.get_delta(strike, opt_side)
                if d is None:
                    continue
                sign = 1 if (aggr == 'BUY') else -1
                notional_delta += sign * size * d * 100
                delta_resolved += 1
    except Exception:
        pass

    # 2026-05-05 — REMOVED expected_hedge_side derivation + hf_alignment join.
    # Both were predictive scaffolding for the dealer-hedging hypothesis that
    # was empirically falsified (n=15,902 sweeps, +0.27% edge over base rate).
    # The directional weight in conviction_score was zeroed (W_INTEL_SWEEP=0)
    # 2026-05-04. Now finishing the cleanup: stripped the source of the dead
    # prediction (`expected_hedge_side`, `expected_hedge_shares`) AND its
    # cross-validator (`hf_alignment` / `_resolve_hedge_alignment`) since the
    # cross-validator only made sense if the prediction was real.
    #
    # What stays: notional_delta (descriptive Σ size×Δ×100 — pure observation,
    # no directional claim), leg-by-leg detail, venue spray, time span.

    # Strike range for display
    strikes = [float(p[2]) for p in legs]
    venue_seq = [str(p[6]) for p in legs]

    return {
        'underlying':            underlying,
        'dte_key':               dte_key,
        'expiration':            f"20{dte_key[0:2]}-{dte_key[2:4]}-{dte_key[4:6]}",
        'direction':             direction,
        'option_side':           option_side,
        'leg_count':             len(legs),
        'total_size':            sum(int(p[4]) for p in legs),
        'notional_delta':        round(notional_delta, 1),
        'delta_resolved':        delta_resolved,        # how many legs had live Δ
        'delta_total_legs':      delta_total_legs,
        'venue_sequence':        venue_seq,
        'venue_count':           len(set(venue_seq)),
        'strike_range':          [min(strikes), max(strikes)],
        'time_span_ms':          legs[-1][0] - legs[0][0],
        'first_print_ts':        legs[0][0],
        'last_print_ts':         legs[-1][0],
        'legs': [
            {
                'sym':    p[1],
                'strike': float(p[2]),
                'size':   int(p[4]),
                'price':  float(p[5]),
                'exch':   str(p[6]),
                'ts_ms':  int(p[0]),
            } for p in legs
        ],
    }


# 2026-05-05 — REMOVED `_resolve_hedge_alignment` function. It was the
# cross-validator for `expected_hedge_side` (now also removed). Both belonged
# to the falsified dealer-hedging hypothesis. The function called into
# `hedge_forecaster.get_state()` whose own predictive output was zeroed
# (W_INTEL_HEDGE_FC = 0) — every layer of this stack is now dead.


# ── Outcome ledger (per-day JSONL) ───────────────────────────────────────────

def _ledger_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f'sweep_outcomes_{datetime.now().strftime("%Y%m%d")}.jsonl')


def _write_ledger(record: dict) -> None:
    """Append a sweep-outcome record to the per-day JSONL file. Disk I/O
    happens under _ledger_lock, NOT _state_lock, so detection latency is
    unaffected by file write times."""
    global _ledger_fh, _ledger_date
    today = datetime.now().strftime('%Y%m%d')
    with _ledger_lock:
        if _ledger_fh is None or _ledger_date != today:
            try:
                if _ledger_fh is not None:
                    _ledger_fh.close()
            except Exception:
                pass
            _ledger_fh = open(_ledger_path(), 'a', buffering=1)  # line-buffered
            _ledger_date = today
        try:
            _ledger_fh.write(json.dumps(record, separators=(',', ':')) + '\n')
        except Exception as e:
            log.debug(f"[SWEEP] ledger write err: {e}")


# ── Public read API ──────────────────────────────────────────────────────────

def get_recent_sweeps(limit: int = 50) -> list:
    """Return last N completed sweeps (newest last)."""
    with _state_lock:
        return list(_completed_sweeps)[-limit:]


def get_stats() -> dict:
    """Diagnostic snapshot — for /api/_debug/sweep_detector/stats."""
    with _state_lock:
        return {
            **_stats,
            'completed_sweeps_buffer_size': len(_completed_sweeps),
            'recent_prints_buffers': {
                u: len(d) for u, d in _recent_prints.items()
            },
            'config': {
                'sweep_window_ms':     SWEEP_WINDOW_MS,
                'adjacency_dollars':   ADJACENCY_DOLLARS,
                'min_legs':            MIN_LEGS,
                'min_total_size':      MIN_TOTAL_SIZE,
                'history_cap':         HISTORY_CAP,
                'buffer_retention_ms': BUFFER_RETENTION_MS,
            },
            'last_sweep_id': _sweep_id_counter - 1 if _sweep_id_counter > 0 else -1,
        }
