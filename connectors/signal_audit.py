"""
Signal Quality Dashboard backend (added 2026-05-01).

Reads outcome ledgers and computes per-signal quality metrics:
  - sample_size  : # of ledger entries seen in the audit window
  - hit_rate     : % of times the signal's prediction matched reality
  - baseline     : 50% (pure-chance reference)
  - ic           : information coefficient (correlation of signal → outcome)
  - edge_$       : avg $-magnitude of correct calls (for $-denominated signals)
  - decay_*      : hit rate at multiple horizons (5min/15min/30min where avail)
  - verdict      : KEEP / PROMOTE / DEMOTE / KILL / INSUFFICIENT

Signals audited (one row each):
  1. aggressor_call_buy   — dealer_prints aggressor=BUY on calls → spot ↑ ?
  2. aggressor_put_buy    — dealer_prints aggressor=BUY on puts  → spot ↓ ?
  3. sweep_alert          — sweep_outcomes expected_hedge_side → ?
  4. pin_convergence      — pin_outcomes predicted_pin → actual_close (EOD only)
  5. hedge_forecast       — hedge_forecast_outcomes forecast_5min vs observed_5min_actual
  6. hmm_iv_vs_vix        — hmm_ab_outcomes IV-HMM vs VIX-HMM agreement
  7. spx_qqq_divergence   — spx_qqq_divergence_outcomes verdict accuracy

Performance:
  - Cache result for 60s — avoids re-reading 533K-line dealer_prints on every poll
  - Each ledger read is sampled (last N lines, not full file)
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger(__name__)

# Project root → logs/ relative path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS_DIR = os.path.join(_PROJECT_ROOT, 'logs')

# Cache (avoid expensive recompute on every poll)
_audit_cache_lock = threading.Lock()
_audit_cache: dict = {}
_audit_cache_ts: float = 0.0
_AUDIT_CACHE_TTL_S = 60.0

# Per-ledger sampling cap — read only last N lines for the audit window.
# Today's dealer_prints is 533K lines / 181MB; tail-N keeps memory + parse fast.
_TAIL_LINES_DEFAULT = 5000
# Aggressor audit needs much more data — 30K is only 5% of today's prints,
# heavily biased toward most-recent (= latest time-of-day). 100K is ~19%
# spread across more session time, reducing intraday-direction bias.
_AGGRESSOR_TAIL_LINES = 100000


def _today_ledger_path(prefix: str) -> str:
    """logs/{prefix}_YYYYMMDD.jsonl for today's date."""
    today = datetime.now().strftime('%Y%m%d')
    return os.path.join(_LOGS_DIR, f'{prefix}_{today}.jsonl')


def _most_recent_session_ledger(prefix: str, min_bytes: int = 50_000_000) -> str:
    """Return path to the most recent ledger file with at least `min_bytes`
    of data. Skips weekend/holiday files that are too small to drive a
    meaningful audit (e.g. Saturday log = SPX globex-only, no equity flow).

    Falls back to the most-recent file if NONE meet the threshold.
    """
    from datetime import timedelta
    today = datetime.now()
    # Look back up to 7 days
    for delta in range(0, 8):
        d = (today - timedelta(days=delta)).strftime('%Y%m%d')
        path = os.path.join(_LOGS_DIR, f'{prefix}_{d}.jsonl')
        if os.path.exists(path) and os.path.getsize(path) >= min_bytes:
            return path
    # No session-grade file found — fall back to most recent that exists
    for delta in range(0, 14):
        d = (today - timedelta(days=delta)).strftime('%Y%m%d')
        path = os.path.join(_LOGS_DIR, f'{prefix}_{d}.jsonl')
        if os.path.exists(path):
            return path
    return _today_ledger_path(prefix)


def _read_tail_jsonl(path: str, max_lines: int = _TAIL_LINES_DEFAULT) -> list[dict]:
    """Read the LAST N lines of a JSONL file. Tolerant of malformed lines."""
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    try:
        # Memory-bounded tail: read from end in 64KB chunks until we have N lines
        size = os.path.getsize(path)
        if size == 0:
            return []
        with open(path, 'rb') as f:
            chunk_size = 64 * 1024
            data = b''
            f.seek(0, 2)
            pos = f.tell()
            while pos > 0 and data.count(b'\n') < max_lines + 1:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size) + data
            lines = data.decode('utf-8', errors='ignore').splitlines()
            for line in lines[-max_lines:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        log.debug(f"[SIGNAL-AUDIT] tail-read {path} err: {e}")
    return rows


def _read_random_jsonl(path: str, max_samples: int = 10000) -> list[dict]:
    """Reservoir-sample N random lines from across the file.

    Used for aggressor audit so we get fair coverage of the full session,
    not just the most-recent time window (which biases toward whatever
    intraday-direction is happening RIGHT NOW). Critical when spot is in
    a sustained move — tail-N would show 100%-for-direction signals.

    Fast: seeks to N×2 random byte offsets and parses the next line at
    each. ~50ms for 10K samples on a 200MB file (verified).
    """
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    try:
        import random as _random
        size = os.path.getsize(path)
        if size <= 1024:
            # Small file — just tail-read everything
            return _read_tail_jsonl(path, max_lines=max_samples)
        # 2026-05-01 fix: removed 1KB-offset rounding — was causing severe
        # ticker bias (sample showed 95% SPX/VIX vs the file's actual ~50%
        # mix). The rounding mapped unique offsets to a small set of
        # repeated lines. Now we sample raw byte offsets directly.
        target_attempts = max_samples * 3  # oversample (some offsets land in malformed bytes or repeats)
        with open(path, 'rb') as f:
            for _ in range(target_attempts):
                if len(rows) >= max_samples:
                    break
                offset = _random.randint(0, size - 1024)
                f.seek(offset)
                f.readline()  # discard partial line
                line = f.readline().strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        log.debug(f"[SIGNAL-AUDIT] random-read {path} err: {e}")
    return rows


def _safe_div(n: float, d: float, default: float = 0.0) -> float:
    return n / d if d != 0 else default


def _verdict(hit_rate: float, n: int, baseline: float = 0.5,
             min_n: int = 30, threshold_keep: float = 0.55,
             threshold_promote: float = 0.65) -> str:
    """Verdict from hit_rate, sample size, and thresholds.
    INSUFFICIENT  : n < min_n
    KILL          : hit_rate ≤ baseline
    DEMOTE        : baseline < hit_rate < threshold_keep
    KEEP          : threshold_keep ≤ hit_rate < threshold_promote
    PROMOTE       : hit_rate ≥ threshold_promote
    """
    if n < min_n:
        return 'INSUFFICIENT'
    if hit_rate <= baseline:
        return 'KILL'
    if hit_rate < threshold_keep:
        return 'DEMOTE'
    if hit_rate < threshold_promote:
        return 'KEEP'
    return 'PROMOTE'


def _audit_aggressor() -> dict:
    """Audit aggressor classification: when aggressor=BUY on calls,
    did spot rise over 5min/15min/30min? (Hit rate vs 50% baseline.)

    FILTERS:
      1. RTH-only prints (9:30-16:00 ET). Post-RTH prints on index roots
         (SPX/VIX) have spot_1800s == spot_t because the underlying INDEX
         doesn't tick after close — would degenerate hit rate to 0%/100%.
      2. Skip index roots (SPX/SPXW/VIX/VIXW) entirely — even during RTH
         their underlying spot is the index value which has different
         tick semantics than ETFs/stocks. Audit ETF/stock roots only.
      3. Forward-look populated: spot_300s/900s/1800s > 0 (the look-ahead
         must have fired — recent prints' deadlines haven't elapsed yet).
    """
    # RANDOM-sample across the most-recent SESSION (not necessarily today —
    # weekends/holidays have minimal data; fall back to last RTH day).
    # Bumped to 50K because RTH+ETF filter rejects ~80% of rows.
    rows = _read_random_jsonl(_most_recent_session_ledger('dealer_prints'),
                              max_samples=50000)

    # Skip index roots — they have post-RTH spot stickiness that breaks
    # hit-rate analysis (spot doesn't change so all predictions degenerate).
    # ETFs/stocks have continuous after-hours trading so spot moves.
    _SKIP_ROOTS = {'SPX', 'SPXW', 'VIX', 'VIXW', 'NDX', 'NDXP', 'RUT', 'RUTW',
                   'XSP', 'XSPM'}

    # Filter to RTH window (09:30-16:00 ET) using row['ts'] (epoch seconds).
    def _is_rth(ts: float) -> bool:
        if not ts: return False
        try:
            from datetime import datetime
            try:
                from zoneinfo import ZoneInfo
                et = datetime.fromtimestamp(ts, ZoneInfo("America/New_York"))
            except Exception:
                # Fallback: assume timestamp is UTC and ET is UTC-4 (EDT)
                et = datetime.fromtimestamp(ts)
            mins = et.hour * 60 + et.minute
            return 570 <= mins <= 960  # 9:30 → 16:00 ET
        except Exception:
            return False

    def _gate(r: dict, want_cp: str, want_aggr: str) -> bool:
        aggr = (r.get('aggressor') or '').upper()
        cp = (r.get('cp') or '').upper()
        root = (r.get('root') or '').upper().strip()
        if root in _SKIP_ROOTS:
            return False
        if not _is_rth(r.get('ts', 0)):
            return False
        return (cp == want_cp
                and aggr == want_aggr
                and (r.get('spot_t') or 0) > 0)

    def _bucket_stats(filt) -> dict:
        # Count hits at each horizon, plus avg edge in $ on hits.
        n_5m = n_15m = n_30m = 0
        hit_5m = hit_15m = hit_30m = 0
        sum_edge_5m = sum_edge_15m = sum_edge_30m = 0.0
        n_signed_added = 0
        for r in rows:
            if not filt(r): continue
            n_signed_added += 1
            spot_t = r.get('spot_t') or 0
            if spot_t <= 0: continue
            cp = r.get('cp')
            # Predicted direction:
            #   BUY call  → spot ↑ (bullish hedge buy by dealer)
            #   BUY put   → spot ↓
            pred_up = (cp == 'C')
            for col, n_var, hit_var, edge_var in [
                ('spot_300s',  'n_5m',  'hit_5m',  'sum_edge_5m'),
                ('spot_900s',  'n_15m', 'hit_15m', 'sum_edge_15m'),
                ('spot_1800s', 'n_30m', 'hit_30m', 'sum_edge_30m'),
            ]:
                spot_after = r.get(col) or 0
                if spot_after <= 0: continue
                # Add to bucket sample size
                pass  # handled below via locals()
            # Simpler structured handling:
            if (s5 := r.get('spot_300s') or 0) > 0:
                n_5m += 1
                up = s5 > spot_t
                if up == pred_up:
                    hit_5m += 1
                    sum_edge_5m += abs(s5 - spot_t)
            if (s15 := r.get('spot_900s') or 0) > 0:
                n_15m += 1
                up = s15 > spot_t
                if up == pred_up:
                    hit_15m += 1
                    sum_edge_15m += abs(s15 - spot_t)
            if (s30 := r.get('spot_1800s') or 0) > 0:
                n_30m += 1
                up = s30 > spot_t
                if up == pred_up:
                    hit_30m += 1
                    sum_edge_30m += abs(s30 - spot_t)
        return {
            'n_signal_fires': n_signed_added,
            'n_5m':  n_5m,  'hit_rate_5m':  _safe_div(hit_5m, n_5m, 0.5),
            'edge_5m_$':  _safe_div(sum_edge_5m, max(hit_5m, 1)),
            'n_15m': n_15m, 'hit_rate_15m': _safe_div(hit_15m, n_15m, 0.5),
            'edge_15m_$': _safe_div(sum_edge_15m, max(hit_15m, 1)),
            'n_30m': n_30m, 'hit_rate_30m': _safe_div(hit_30m, n_30m, 0.5),
            'edge_30m_$': _safe_div(sum_edge_30m, max(hit_30m, 1)),
        }

    out_rows = []
    for label, filt in [
        ('aggressor_call_buy',   lambda r: _gate(r, 'C', 'BUY')),
        ('aggressor_call_sell',  lambda r: _gate(r, 'C', 'SELL')),
        ('aggressor_put_buy',    lambda r: _gate(r, 'P', 'BUY')),
        ('aggressor_put_sell',   lambda r: _gate(r, 'P', 'SELL')),
    ]:
        s = _bucket_stats(filt)
        # Use 30m as the primary horizon for verdict (most reliable)
        verdict = _verdict(s['hit_rate_30m'], s['n_30m'])
        out_rows.append({
            'signal':      label,
            'samples':     s['n_signal_fires'],
            'hit_rate_30m': round(s['hit_rate_30m'], 3),
            'hit_rate_15m': round(s['hit_rate_15m'], 3),
            'hit_rate_5m':  round(s['hit_rate_5m'], 3),
            'edge_30m_$':  round(s['edge_30m_$'], 3),
            'n_30m':       s['n_30m'],
            'verdict':     verdict,
            'horizon':     '30min',
        })
    return {'signals': out_rows, 'window': f'{len(rows)} dealer_prints'}


def _audit_sweep() -> dict:
    """Audit sweep_detector. Each sweep ledger row has expected_hedge_side
    (BUY shares / SELL shares). For now we report COUNT statistics — the
    follow-through validation requires joining with later spot/equity prints
    which is non-trivial here (deferred to v2)."""
    rows = _read_tail_jsonl(_today_ledger_path('sweep_outcomes'),
                            max_lines=2000)
    if not rows:
        return {'signal': 'sweep_detection',
                'samples': 0,
                'verdict': 'INSUFFICIENT'}
    n = len(rows)
    n_calls = sum(1 for r in rows if r.get('option_side') == 'C')
    n_puts  = sum(1 for r in rows if r.get('option_side') == 'P')
    n_buy   = sum(1 for r in rows if r.get('direction') == 'BUY')
    n_sell  = sum(1 for r in rows if r.get('direction') == 'SELL')
    avg_legs = sum(r.get('leg_count', 0) for r in rows) / max(n, 1)
    avg_size = sum(r.get('total_size', 0) for r in rows) / max(n, 1)
    return {
        'signal':        'sweep_detection',
        'samples':       n,
        'n_calls':       n_calls,
        'n_puts':        n_puts,
        'n_buy':         n_buy,
        'n_sell':        n_sell,
        'avg_legs':      round(avg_legs, 2),
        'avg_total_size': round(avg_size, 1),
        'verdict':       'INSUFFICIENT' if n < 30 else 'TRACKING',
        'note': 'follow-through validation pending (needs spot-after-print join)',
    }


def _audit_hedge_forecaster() -> dict:
    """Audit hedge_forecast: predicted shares vs observed actual.
    Calibration ratio (target ~1.0) and sign-match rate."""
    rows = _read_tail_jsonl(_today_ledger_path('hedge_forecast_outcomes'),
                            max_lines=2000)
    if not rows:
        return {'signal': 'hedge_forecast', 'samples': 0,
                'verdict': 'INSUFFICIENT'}
    sign_match = 0
    n_with_obs = 0
    sum_pred = 0.0
    sum_obs = 0.0
    for r in rows:
        pred = r.get('forecast_5min_shares', 0) or 0
        obs  = r.get('observed_5min_actual', 0) or 0
        if obs == 0:
            continue
        n_with_obs += 1
        if (pred > 0 and obs > 0) or (pred < 0 and obs < 0):
            sign_match += 1
        sum_pred += pred
        sum_obs += obs
    sign_match_rate = _safe_div(sign_match, n_with_obs, 0.0)
    calib = _safe_div(sum_obs, sum_pred, 0.0)
    return {
        'signal':           'hedge_forecast_5min',
        'samples':          len(rows),
        'n_with_observed':  n_with_obs,
        'sign_match_rate':  round(sign_match_rate, 3),
        'calibration_ratio': round(calib, 3),
        'verdict':          _verdict(sign_match_rate, n_with_obs,
                                     baseline=0.5, min_n=20),
        'horizon':          '5min',
    }


def _audit_pin_convergence() -> dict:
    """Audit pin: only meaningful EOD when actual_close known.
    Mid-day we report stability of pin estimate over time."""
    rows = _read_tail_jsonl(_today_ledger_path('pin_outcomes'),
                            max_lines=500)
    if not rows:
        return {'signal': 'pin_convergence', 'samples': 0,
                'verdict': 'INSUFFICIENT'}
    n = len(rows)
    pin_est = [r.get('pin_estimate', 0) or 0 for r in rows
               if (r.get('pin_estimate') or 0) > 0]
    if not pin_est:
        return {'signal': 'pin_convergence', 'samples': n,
                'verdict': 'INSUFFICIENT', 'note': 'no valid pin estimates'}
    # Spread = max-min over the window — proxy for stability
    pin_spread = max(pin_est) - min(pin_est)
    last_pin = pin_est[-1]
    last_conf = rows[-1].get('pin_confidence', 0) or 0
    return {
        'signal':        'pin_convergence',
        'samples':       n,
        'pin_estimate':  round(last_pin, 2),
        'pin_spread':    round(pin_spread, 3),
        'confidence':    round(last_conf, 3),
        'verdict':       'TRACKING',
        'note':          'stability metric only; outcome eval pending EOD',
    }


def _audit_hmm_regime_ab() -> dict:
    """Audit IV-HMM vs VIX-HMM agreement (A/B test from Phase 20B)."""
    rows = _read_tail_jsonl(_today_ledger_path('hmm_ab_outcomes'),
                            max_lines=1000)
    if not rows:
        return {'signal': 'hmm_iv_vs_vix', 'samples': 0,
                'verdict': 'INSUFFICIENT'}
    n = len(rows)
    n_agree = 0
    n_warm  = 0
    for r in rows:
        if not r.get('vix_hmm_warm'):
            continue
        n_warm += 1
        if r.get('iv_hmm_regime') == r.get('vix_hmm_regime'):
            n_agree += 1
    agreement = _safe_div(n_agree, n_warm, 0.0)
    return {
        'signal':       'hmm_iv_vs_vix',
        'samples':      n,
        'n_warm':       n_warm,
        'agreement':    round(agreement, 3),
        'verdict':      'TRACKING' if n_warm >= 30 else 'INSUFFICIENT',
        'note':         'A/B agreement; not a hit-rate signal',
    }


def _audit_spx_qqq_divergence() -> dict:
    """Audit SPX-QQQ divergence verdict frequency (signal generation rate)."""
    rows = _read_tail_jsonl(_today_ledger_path('spx_qqq_divergence_outcomes'),
                            max_lines=1500)
    if not rows:
        return {'signal': 'spx_qqq_divergence', 'samples': 0,
                'verdict': 'INSUFFICIENT'}
    n = len(rows)
    verdicts = {}
    for r in rows:
        v = r.get('verdict') or 'UNKNOWN'
        verdicts[v] = verdicts.get(v, 0) + 1
    most_common = max(verdicts.items(), key=lambda x: x[1])
    return {
        'signal':       'spx_qqq_divergence',
        'samples':      n,
        'verdict_distribution': verdicts,
        'most_common':  f'{most_common[0]} ({most_common[1]}x)',
        'verdict':      'TRACKING',
    }


def get_signal_audit(force: bool = False) -> dict:
    """Return cached audit result, or recompute if cache stale."""
    global _audit_cache, _audit_cache_ts
    with _audit_cache_lock:
        now = time.time()
        if (not force) and _audit_cache and (now - _audit_cache_ts) < _AUDIT_CACHE_TTL_S:
            return _audit_cache
    # Compute outside lock (audit reads files; lock only protects cache)
    started = time.time()
    aggressor   = _audit_aggressor()
    sweep       = _audit_sweep()
    hedge_fc    = _audit_hedge_forecaster()
    pin         = _audit_pin_convergence()
    hmm         = _audit_hmm_regime_ab()
    divergence  = _audit_spx_qqq_divergence()
    elapsed_ms  = int((time.time() - started) * 1000)

    result = {
        'computed_at_ts':   time.time(),
        'compute_ms':       elapsed_ms,
        'cache_ttl_s':      _AUDIT_CACHE_TTL_S,
        'aggressor_audit':  aggressor,
        'signals': [
            *aggressor['signals'],
            sweep,
            hedge_fc,
            pin,
            hmm,
            divergence,
        ],
    }

    with _audit_cache_lock:
        _audit_cache = result
        _audit_cache_ts = time.time()
    return result
