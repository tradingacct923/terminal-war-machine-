#!/usr/bin/env python3
"""
Regression Runner — converts outcome ledgers (JSONL) into per-panel hit-rates.

Run weekly to upgrade CONFIGURED constants → MEASURED with cited statistics.
Reads `logs/*_outcomes_YYYYMMDD.jsonl` for the past N days, computes
predictive hit-rates against future-snapshot lookups within the same ledger,
and writes a markdown report to `logs/regression_report_YYYYMMDD.md`.

Per-panel validation (each module's docstring documents its target):

  Pin Convergence     — predicted_pin vs actual_close R² target ≥ 0.5
  Hedge Forecaster    — forecast_5min_shares vs observed_5min_actual
                        sign-hit-rate ≥ 65%, calibration ratio 0.7..1.3
  Sweep Detector      — predicted_hedge_side vs observed_5min equity flow
                        hit-rate ≥ 65%
  SPX-vs-QQQ Div      — DIVERGENT_REGIME → 4hr spread convergence ≥ 60%
  Vol Regime          — STRESS_BACKWARDATION → 4hr SPY drawdown ≥ 0.5% ≥ 55%
  Wing Tracker        — EXTREME → 15min QQQ move ≥ 0.10% ≥ 55%
  Dealer Warehouse    — COMMITTED-vs-PHANTOM 60min wall-hold differential ≥ 20pp

Output: human-readable markdown report. Also writes a tiny JSON sidecar
`logs/regression_summary.json` with the headline stats — read by the
`/api/_debug/regression` REST endpoint for on-screen surfacing.

Usage:
    python scripts/regression_runner.py [--days N] [--ledger-dir PATH]

Default: last 14 days, logs/ dir.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# Ensure we can be invoked from the repo root or scripts/ directly
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(SCRIPT_DIR)
DEFAULT_LEDGER_DIR = os.path.join(REPO_ROOT, 'logs')

# ── CONFIGURED targets (mirrors per-panel docstrings) ──────────────────────
TARGETS = {
    'pin_convergence':    {'metric': 'r_squared_pin_vs_close',  'target': 0.50},
    'hedge_forecaster':   {'metric': 'sign_hit_rate_5min',       'target': 0.65},
    'sweep_detector':     {'metric': 'hedge_side_hit_rate',      'target': 0.65},
    'spx_qqq_divergence': {'metric': 'div_regime_4h_convergence_rate', 'target': 0.60},
    'vix_regime':         {'metric': 'stress_back_4h_drawdown_rate',   'target': 0.55},
    'wing_tracker':       {'metric': 'extreme_15min_move_rate',  'target': 0.55},
    'dealer_warehouse':   {'metric': 'committed_vs_phantom_diff_pct',  'target': 0.20},
}


def _load_jsonl(path: str) -> list:
    """Read all valid JSON lines from a file."""
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        sys.stderr.write(f"[regression] failed to read {path}: {e}\n")
    return rows


def _load_ledger(ledger_dir: str, ledger_name: str, days: int) -> list:
    """Load all matching ledgers from `ledger_dir` for the past `days`."""
    rows = []
    today = datetime.now().date()
    for d in range(days):
        date = today - timedelta(days=d)
        date_str = date.strftime('%Y%m%d')
        path = os.path.join(ledger_dir, f'{ledger_name}_outcomes_{date_str}.jsonl')
        rows.extend(_load_jsonl(path))
    return rows


# ── Per-panel validators ────────────────────────────────────────────────────

def _validate_pin(rows: list) -> dict:
    """Pin Convergence — predicted_pin vs actual_close R² + ledger size + CI hits.

    Without a separate close-price feed, the simplest validator: for each pin
    record, compute "actual_close" as the LAST pin_estimate of that day's
    ledger (since the final pin estimate at session-close ≈ actual close).
    Then R² of all earlier predictions vs that EOD anchor.
    """
    out = {
        'samples':     len(rows),
        'days':        0,
        'r_squared':   None,
        'mean_abs_err': None,
        'ci_hit_rate': None,
        'note':        '',
    }
    # Group by date
    by_date: dict = defaultdict(list)
    for r in rows:
        ts = r.get('ts')
        if not isinstance(ts, (int, float)):
            continue
        d = datetime.fromtimestamp(ts).date().isoformat()
        by_date[d].append(r)
    out['days'] = len(by_date)
    if not by_date:
        out['note'] = 'no records'
        return out

    pairs = []        # list of (predicted_pin, actual_close)
    ci_hits = 0
    ci_total = 0
    for d, day_rows in by_date.items():
        # Sort by ts ascending
        day_rows.sort(key=lambda x: x.get('ts', 0))
        # Last record's pin_estimate is our best EOD anchor
        last = day_rows[-1]
        actual_close = last.get('pin_estimate')
        if not isinstance(actual_close, (int, float)):
            continue
        # Skip the first 30 min of records (regime not stable yet)
        # 30 min = 30*60 = 1800 sec from session open
        # We don't know the open; just use the first record + 1800
        first_ts = day_rows[0].get('ts', 0)
        for r in day_rows[:-1]:
            if r.get('ts', 0) < first_ts + 1800:
                continue
            p = r.get('pin_estimate')
            cl = r.get('ci_low')
            ch = r.get('ci_high')
            if not isinstance(p, (int, float)):
                continue
            pairs.append((p, actual_close))
            # CI band hit-rate: did the actual_close fall within the predicted CI?
            if isinstance(cl, (int, float)) and isinstance(ch, (int, float)):
                ci_total += 1
                if cl <= actual_close <= ch:
                    ci_hits += 1
    if not pairs:
        out['note'] = 'no pairs after 30-min warmup filter'
        return out
    # R²
    preds  = [p[0] for p in pairs]
    actual = [p[1] for p in pairs]
    if len(set(actual)) < 2:
        out['note'] = 'actual_close has zero variance — single day or no movement'
    else:
        try:
            mean_a = statistics.mean(actual)
            ss_tot = sum((a - mean_a) ** 2 for a in actual)
            ss_res = sum((a - p) ** 2 for p, a in pairs)
            if ss_tot > 0:
                out['r_squared'] = round(1.0 - ss_res / ss_tot, 4)
        except Exception:
            pass
    out['mean_abs_err']  = round(statistics.mean(abs(a - p) for p, a in pairs), 4)
    if ci_total > 0:
        out['ci_hit_rate'] = round(ci_hits / ci_total, 4)
    return out


def _validate_hedge_forecaster(rows: list, paired_rows: list = None) -> dict:
    """Hedge Forecaster — forecast vs forward-window observed.

    Prefers `paired_rows` from `hedge_forecast_paired_*.jsonl` (added 2026-05-04)
    where forecast(T) is correctly matched to observed flow over [T, T+300].
    Falls back to `rows` (legacy `hedge_forecast_outcomes_*.jsonl`) where forecast
    and observed share the same timestamp — useful for activity counts but the
    sign-hit-rate computed from it is meaningless (chance-level by construction).
    """
    use_paired = paired_rows and len(paired_rows) > 0
    src = paired_rows if use_paired else rows
    fc_key  = 'forecast_5min_shares'
    obs_key = 'observed_5min_shares' if use_paired else 'observed_5min_actual'
    out = {
        'samples':             len(src),
        'source':              'paired' if use_paired else 'legacy_same_ts',
        'sign_hit_rate':       None,
        'calibration_ratio':   None,
        'mean_abs_err_shares': None,
    }
    sign_hits = 0
    sign_total = 0
    ratios = []
    abs_errs = []
    for r in src:
        fc = r.get(fc_key)
        ob = r.get(obs_key)
        if not isinstance(fc, (int, float)) or not isinstance(ob, (int, float)):
            continue
        if abs(fc) < 1.0:
            continue
        sign_total += 1
        if (fc > 0) == (ob > 0):
            sign_hits += 1
        if abs(fc) > 100:                      # avoid noise-on-noise calibration
            ratios.append(ob / fc)
            abs_errs.append(abs(ob - fc))
    if sign_total > 0:
        out['sign_hit_rate'] = round(sign_hits / sign_total, 4)
    if ratios:
        out['calibration_ratio'] = round(statistics.median(ratios), 4)
    if abs_errs:
        out['mean_abs_err_shares'] = round(statistics.mean(abs_errs), 1)
    return out


def _validate_sweep(rows: list) -> dict:
    """Sweep Detector — predicted_hedge_side vs observed equity flow.

    Sweep records don't carry observed flow inline. We look up the
    `hedge_forecast_outcomes` ledger for the same day + nearest timestamp
    and use its `observed_5min_actual` to validate sweep direction.

    Without that join (single-pass minimum), we report a simpler proxy:
    sweeps with `hf_aligned == True` (Phase 10A) vs `hf_aligned == False`.
    Hit-rate of HF-aligned sweeps SHOULD be higher.
    """
    out = {
        'samples':            len(rows),
        'hf_aligned_count':   0,
        'hf_misaligned_count': 0,
        'hf_unknown_count':   0,
        'hedge_side_hit_rate': None,
        'note':               'see logs for full validation; this is the alignment-rate proxy',
    }
    for r in rows:
        ha = r.get('hf_aligned')
        if ha is True:   out['hf_aligned_count'] += 1
        elif ha is False: out['hf_misaligned_count'] += 1
        else:             out['hf_unknown_count'] += 1
    total_resolved = out['hf_aligned_count'] + out['hf_misaligned_count']
    if total_resolved > 0:
        out['hedge_side_hit_rate'] = round(out['hf_aligned_count'] / total_resolved, 4)
    return out


def _validate_spxqqq_div(rows: list) -> dict:
    """SPX-vs-QQQ Divergence — DIVERGENT_REGIME → next-4hr spread convergence.

    For each DIVERGENT_REGIME record at time T, find the record ≥4hr later
    in the same ledger and check whether |flip_distance_diff_pct| decreased
    (spread converged).
    """
    out = {
        'samples':                 len(rows),
        'divergent_regime_count':  0,
        'aligned_count':           0,
        'div_regime_4h_convergence_rate': None,
        'note':                    'requires intra-day coverage of ≥4hr',
    }
    by_date: dict = defaultdict(list)
    for r in rows:
        ts = r.get('ts')
        if not isinstance(ts, (int, float)):
            continue
        d = datetime.fromtimestamp(ts).date().isoformat()
        by_date[d].append(r)
    convergence_hits = 0
    convergence_total = 0
    for d, day_rows in by_date.items():
        day_rows.sort(key=lambda x: x.get('ts', 0))
        for i, r in enumerate(day_rows):
            v = r.get('verdict')
            if v == 'DIVERGENT_REGIME':
                out['divergent_regime_count'] += 1
                # Find a record ≥ 4hr later
                t0 = r.get('ts', 0)
                d0 = abs(r.get('flip_distance_diff_pct') or 0)
                for j in range(i + 1, len(day_rows)):
                    rj = day_rows[j]
                    if rj.get('ts', 0) - t0 >= 4 * 3600:
                        d1 = abs(rj.get('flip_distance_diff_pct') or 0)
                        convergence_total += 1
                        if d1 < d0:
                            convergence_hits += 1
                        break
            elif v in ('ALIGNED_BULL', 'ALIGNED_BEAR'):
                out['aligned_count'] += 1
    if convergence_total > 0:
        out['div_regime_4h_convergence_rate'] = round(convergence_hits / convergence_total, 4)
    return out


def _validate_vix_regime(rows: list) -> dict:
    """Vol Regime — STRESS_BACKWARDATION → 4hr SPY drawdown ≥ 0.5%.

    Without SPY price feed in the ledger, validate as a proxy: count regime
    transitions FROM STRESS_BACKWARDATION → other regimes within the next
    4hr window (regime persistence/decay).
    """
    out = {
        'samples':                  len(rows),
        'stress_back_count':        0,
        'calm_contango_count':      0,
        'elevated_count':           0,
        'normal_count':             0,
        'stress_back_4h_decay_rate': None,
        'note':                     'true SPY-drawdown validation needs external price feed',
    }
    by_date: dict = defaultdict(list)
    for r in rows:
        ts = r.get('ts')
        if not isinstance(ts, (int, float)):
            continue
        # Tally regime distribution
        regime = r.get('regime') or ''
        if regime == 'STRESS_BACKWARDATION':
            out['stress_back_count'] += 1
        elif regime == 'CALM_CONTANGO':
            out['calm_contango_count'] += 1
        elif regime == 'ELEVATED':
            out['elevated_count'] += 1
        elif regime == 'NORMAL':
            out['normal_count'] += 1
        d = datetime.fromtimestamp(ts).date().isoformat()
        by_date[d].append(r)

    decay_hits = 0
    decay_total = 0
    for d, day_rows in by_date.items():
        day_rows.sort(key=lambda x: x.get('ts', 0))
        for i, r in enumerate(day_rows):
            if r.get('regime') != 'STRESS_BACKWARDATION':
                continue
            t0 = r.get('ts', 0)
            for j in range(i + 1, len(day_rows)):
                rj = day_rows[j]
                if rj.get('ts', 0) - t0 >= 4 * 3600:
                    decay_total += 1
                    if rj.get('regime') != 'STRESS_BACKWARDATION':
                        decay_hits += 1
                    break
    if decay_total > 0:
        out['stress_back_4h_decay_rate'] = round(decay_hits / decay_total, 4)
    return out


def _validate_wing(rows: list) -> dict:
    """Wing Tracker — EXTREME → 15min QQQ move ≥ 0.10%.

    Each row has `spot` at the time of the snapshot. For each EXTREME record,
    find the record ≥15 min later in the same ledger and compute |spot move|.
    """
    out = {
        'samples':                len(rows),
        'extreme_count':          0,
        'active_count':           0,
        'normal_count':           0,
        'extreme_15min_move_rate': None,
        'extreme_avg_15min_move_pct': None,
    }
    by_date: dict = defaultdict(list)
    for r in rows:
        ts = r.get('ts')
        if not isinstance(ts, (int, float)):
            continue
        regime = r.get('regime') or ''
        if regime == 'EXTREME':  out['extreme_count'] += 1
        elif regime == 'ACTIVE': out['active_count']  += 1
        elif regime == 'NORMAL': out['normal_count']  += 1
        d = datetime.fromtimestamp(ts).date().isoformat()
        by_date[d].append(r)

    hit = 0
    total = 0
    moves = []
    for d, day_rows in by_date.items():
        day_rows.sort(key=lambda x: x.get('ts', 0))
        for i, r in enumerate(day_rows):
            if r.get('regime') != 'EXTREME':
                continue
            spot0 = r.get('spot')
            if not isinstance(spot0, (int, float)) or spot0 <= 0:
                continue
            t0 = r.get('ts', 0)
            for j in range(i + 1, len(day_rows)):
                rj = day_rows[j]
                if rj.get('ts', 0) - t0 >= 15 * 60:
                    spot1 = rj.get('spot')
                    if not isinstance(spot1, (int, float)) or spot1 <= 0:
                        break
                    move_pct = abs(spot1 - spot0) / spot0 * 100
                    moves.append(move_pct)
                    total += 1
                    if move_pct >= 0.10:
                        hit += 1
                    break
    if total > 0:
        out['extreme_15min_move_rate'] = round(hit / total, 4)
    if moves:
        out['extreme_avg_15min_move_pct'] = round(statistics.mean(moves), 4)
    return out


def _validate_warehouse(rows: list) -> dict:
    """Dealer Warehouse — COMMITTED-vs-PHANTOM differential. Compact stats only;
    full per-strike wall-hold validation requires equity tape join.
    """
    out = {
        'samples':       len(rows),
        'days':          0,
        'committed_strike_count': 0,
        'phantom_strike_count':   0,
        'avg_total_posted_time_s': None,
        'avg_total_caught_at_top': None,
        'note':          'full wall-hold validation needs equity tape join',
    }
    by_date: dict = defaultdict(list)
    posted_times = []
    caught_top = []
    for r in rows:
        ts = r.get('ts')
        if not isinstance(ts, (int, float)):
            continue
        d = datetime.fromtimestamp(ts).date().isoformat()
        by_date[d].append(r)
        if isinstance(r.get('total_posted_time_s'), (int, float)):
            posted_times.append(r['total_posted_time_s'])
        if isinstance(r.get('total_caught_at_top'), (int, float)):
            caught_top.append(r['total_caught_at_top'])
    out['days'] = len(by_date)
    if posted_times:
        out['avg_total_posted_time_s'] = round(statistics.mean(posted_times), 1)
    if caught_top:
        out['avg_total_caught_at_top'] = round(statistics.mean(caught_top), 1)
    return out


# ── Report builder ──────────────────────────────────────────────────────────

def _build_report(results: dict, days: int) -> tuple:
    """Return (markdown, summary_dict)."""
    md_lines = []
    md_lines.append(f"# Regression Report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md_lines.append("")
    md_lines.append(f"Window: last {days} days · ledger dir: `logs/`")
    md_lines.append("")
    md_lines.append("## Summary")
    md_lines.append("")
    md_lines.append("| Panel | Metric | Result | Target | Pass |")
    md_lines.append("|---|---|---|---|---|")
    summary = {}
    for panel, target_def in TARGETS.items():
        m_key = target_def['metric']
        target = target_def['target']
        result = results.get(panel, {})
        actual = result.get(m_key)
        if isinstance(actual, (int, float)):
            actual_str = f"{actual:.3f}"
            passed = actual >= target
            pass_str = '✅' if passed else '❌'
        else:
            actual_str = '—'
            passed = None
            pass_str = '⏳'
        md_lines.append(f"| `{panel}` | `{m_key}` | {actual_str} | ≥{target:.2f} | {pass_str} |")
        summary[panel] = {
            'metric': m_key,
            'actual': actual,
            'target': target,
            'passed': passed,
        }
    md_lines.append("")
    md_lines.append("## Per-panel detail")
    md_lines.append("")
    for panel in TARGETS.keys():
        result = results.get(panel, {})
        md_lines.append(f"### `{panel}`")
        md_lines.append("")
        md_lines.append("```json")
        md_lines.append(json.dumps(result, indent=2))
        md_lines.append("```")
        md_lines.append("")
    return ('\n'.join(md_lines), summary)


# ── Main ────────────────────────────────────────────────────────────────────

def run(ledger_dir: str = DEFAULT_LEDGER_DIR, days: int = 14) -> dict:
    """Run the regression. Returns the summary dict."""
    results: dict = {}
    # Each panel's ledger filename prefix
    panels = {
        'pin_convergence':    'pin',
        'hedge_forecaster':   'hedge_forecast',
        'sweep_detector':     'sweep',
        'spx_qqq_divergence': 'spx_qqq_divergence',
        'vix_regime':         'vix_regime',
        'wing_tracker':       'wing',
        'dealer_warehouse':   'dealer_warehouse',
    }
    for panel_name, ledger_prefix in panels.items():
        rows = _load_ledger(ledger_dir, ledger_prefix, days)
        if panel_name == 'pin_convergence':       results[panel_name] = _validate_pin(rows)
        elif panel_name == 'hedge_forecaster':
            # Also load the paired ledger (added 2026-05-04, prefix 'hedge_forecast_paired')
            paired_rows = []
            today = datetime.now().date()
            for d in range(days):
                date = today - timedelta(days=d)
                pp = os.path.join(ledger_dir, f'hedge_forecast_paired_{date.strftime("%Y%m%d")}.jsonl')
                paired_rows.extend(_load_jsonl(pp))
            results[panel_name] = _validate_hedge_forecaster(rows, paired_rows)
        elif panel_name == 'sweep_detector':      results[panel_name] = _validate_sweep(rows)
        elif panel_name == 'spx_qqq_divergence':  results[panel_name] = _validate_spxqqq_div(rows)
        elif panel_name == 'vix_regime':          results[panel_name] = _validate_vix_regime(rows)
        elif panel_name == 'wing_tracker':        results[panel_name] = _validate_wing(rows)
        elif panel_name == 'dealer_warehouse':    results[panel_name] = _validate_warehouse(rows)

    md, summary = _build_report(results, days)

    # Write markdown report
    out_md_path = os.path.join(
        ledger_dir,
        f'regression_report_{datetime.now().strftime("%Y%m%d")}.md'
    )
    with open(out_md_path, 'w') as f:
        f.write(md)

    # Write JSON sidecar (consumed by /api/_debug/regression)
    out_json_path = os.path.join(ledger_dir, 'regression_summary.json')
    with open(out_json_path, 'w') as f:
        json.dump({
            'generated_ts':  datetime.now().timestamp(),
            'window_days':   days,
            'summary':       summary,
            'detail':        results,
            'report_path':   out_md_path,
        }, f, indent=2)

    return summary


def main():
    parser = argparse.ArgumentParser(description='Regression runner')
    parser.add_argument('--days', type=int, default=14, help='Lookback window (days)')
    parser.add_argument('--ledger-dir', type=str, default=DEFAULT_LEDGER_DIR,
                        help='Directory containing *_outcomes_*.jsonl')
    args = parser.parse_args()
    summary = run(ledger_dir=args.ledger_dir, days=args.days)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
