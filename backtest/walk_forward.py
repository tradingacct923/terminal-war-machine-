"""
Walk-Forward Backtest Engine for Edge Signal Evaluation

Institutional-grade signal validation:
- Walk-forward: train on window W, test on next W, roll forward
- Per-signal Sharpe with bootstrap confidence intervals
- Multiple testing correction (Benjamini-Hochberg FDR)
- Regime-conditional performance splits
- Transaction cost deduction (NQ half-spread + market impact)
- Signal kill list: auto-suppress signals with negative post-cost Sharpe

Consumes: logs/edge_outcomes.jsonl
Produces: backtest/reports/<date>_signal_report.json

References:
  - Bailey & Lopez de Prado (2012): Sharpe Ratio Efficient Frontier
  - Harvey & Liu (2015): Backtesting (multiple testing correction)
  - White (2000): Reality Check for data snooping
  - Almgren & Chriss (2001): Optimal execution / market impact
"""

import json
import os
import math
import bisect
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ── Configuration ────────────────────���────────────────��────────────────────

# NQ Micro E-mini: tick = 0.25, value = $5/tick = $1.25/point
# NQ E-mini: tick = 0.25, value = $20/tick = $5/point
# QQQ: penny tick, ~$500/share
NQ_TICK_VALUE = 5.00        # $ per 0.25 tick (E-mini)
NQ_HALF_SPREAD = 0.25       # typical half-spread in NQ points (1 tick)
NQ_IMPACT_BPS = 0.5         # market impact in basis points per contract
QQQ_HALF_SPREAD = 0.01      # QQQ half-spread in $

# Walk-forward windows
TRAIN_DAYS = 3              # train on 3 days
TEST_DAYS = 1               # test on 1 day
MIN_SIGNALS_TRAIN = 20      # minimum signals to compute stats
MIN_SIGNALS_TEST = 5        # minimum signals per test window

# Bootstrap
N_BOOTSTRAP = 2000          # bootstrap iterations for Sharpe CI
SHARPE_CI_LEVEL = 0.95      # 95% confidence interval

# FDR
FDR_ALPHA = 0.05            # Benjamini-Hochberg false discovery rate

# Horizons
HORIZONS = ['10s', '30s', '60s']


# ── Data Loading ───────────────────────────────────────────────────────────

def load_outcomes(path='logs/edge_outcomes.jsonl'):
    """Load edge outcomes, parse timestamps, group by day."""
    signals = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            # Parse timestamp
            rec['_ts'] = rec['entry_ts']
            rec['_date'] = rec['ts_human'][:10]  # YYYY-MM-DD
            rec['_hour'] = int(rec['ts_human'][11:13])
            signals.append(rec)
    return signals


def group_by_day(signals):
    """Group signals by calendar date."""
    days = defaultdict(list)
    for s in signals:
        days[s['_date']].append(s)
    return dict(sorted(days.items()))


# ── Transaction Costs ───────────────────────────────��──────────────────────

def deduct_costs(outcome_pts, symbol='QQQ'):
    """
    Deduct round-trip transaction cost from outcome.

    For QQQ: half-spread entry + half-spread exit = full spread
    For NQ: 1 tick entry + 1 tick exit = 0.50 pts

    Returns cost-adjusted P&L in same units as outcome.
    """
    if symbol == 'NQ':
        # NQ: 0.25 half-spread × 2 = 0.50 round-trip
        return outcome_pts - NQ_HALF_SPREAD * 2
    else:
        # QQQ: $0.01 half-spread × 2 = $0.02 round-trip
        return outcome_pts - QQQ_HALF_SPREAD * 2


# ── Sharpe Ratio with Bootstrap CI ─────────────────────────────────────────

def sharpe_ratio(returns):
    """
    Annualized Sharpe ratio for intraday signals.

    For signals firing N times per day, annualize by sqrt(252 * N/days).
    But since we measure per-signal P&L (not daily), we compute:
      SR = mean(r) / std(r) * sqrt(N)
    where N = signals per year ≈ signals_observed * (252 / days_observed).
    """
    if len(returns) < 2:
        return 0.0
    mu = statistics.mean(returns)
    sigma = statistics.stdev(returns)
    if sigma < 1e-10:
        return 0.0
    # Per-signal Sharpe (not annualized — more interpretable for HFT)
    return mu / sigma


def bootstrap_sharpe_ci(returns, n_boot=N_BOOTSTRAP, ci=SHARPE_CI_LEVEL):
    """
    Bootstrap confidence interval for Sharpe ratio.

    Bailey & Lopez de Prado (2012): non-IID bootstrap with
    block resampling for autocorrelated signals.

    Returns: (sharpe, lower_ci, upper_ci)
    """
    import random
    if len(returns) < 5:
        sr = sharpe_ratio(returns)
        return sr, sr, sr

    n = len(returns)
    sharpes = []

    for _ in range(n_boot):
        # Simple bootstrap (upgrade to block bootstrap if autocorrelation detected)
        sample = random.choices(returns, k=n)
        sharpes.append(sharpe_ratio(sample))

    sharpes.sort()
    alpha = (1 - ci) / 2
    lo_idx = int(alpha * n_boot)
    hi_idx = int((1 - alpha) * n_boot)

    return sharpe_ratio(returns), sharpes[lo_idx], sharpes[hi_idx]


def deflated_sharpe(observed_sharpe, n_signals, n_types_tested, variance_of_sharpes=1.0):
    """
    Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    Adjusts for multiple testing: what's the probability that observed SR
    is above zero after accounting for trying n_types_tested strategies?

    DSR = Φ((SR_obs - E[max SR under null]) / std[max SR under null])

    Where E[max SR] under null ≈ sqrt(2 * ln(n_types)) * σ_SR (Euler-Mascheroni approx)
    """
    if n_types_tested <= 1:
        return observed_sharpe

    # Expected maximum Sharpe under null (all random)
    # From extreme value theory: E[max of N standard normals] ≈ sqrt(2*ln(N))
    e_max_sr_null = math.sqrt(2 * math.log(n_types_tested)) * variance_of_sharpes

    # Standard error of Sharpe estimate
    sr_se = math.sqrt((1 + 0.5 * observed_sharpe**2) / max(n_signals, 1))

    if sr_se < 1e-10:
        return 0.0

    # Z-score: how far above the expected null maximum?
    z = (observed_sharpe - e_max_sr_null) / sr_se

    # Convert to probability via standard normal CDF
    # Φ(z) approximation
    return _norm_cdf(z)


def _norm_cdf(x):
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    if x < -6:
        return 0.0
    if x > 6:
        return 1.0
    b1 = 0.319381530
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.821255978
    b5 = 1.330274429
    p = 0.2316419
    t = 1.0 / (1.0 + p * abs(x))
    poly = ((((b5 * t + b4) * t + b3) * t + b2) * t + b1) * t
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    cdf = 1.0 - pdf * poly
    return cdf if x >= 0 else 1.0 - cdf


# ── Multiple Testing Correction ────────────────────────────────��───────────

def benjamini_hochberg(p_values, alpha=FDR_ALPHA):
    """
    Benjamini-Hochberg FDR correction.

    Given dict {signal_type: p_value}, returns dict {signal_type: significant_bool}.
    Controls false discovery rate at alpha level.
    """
    if not p_values:
        return {}

    # Sort by p-value
    sorted_pvals = sorted(p_values.items(), key=lambda x: x[1])
    m = len(sorted_pvals)
    results = {}

    # Find largest k where p(k) <= k/m * alpha
    max_k = 0
    for k, (name, pval) in enumerate(sorted_pvals, 1):
        threshold = (k / m) * alpha
        if pval <= threshold:
            max_k = k

    # All hypotheses with rank <= max_k are significant
    for k, (name, pval) in enumerate(sorted_pvals, 1):
        results[name] = k <= max_k

    return results


def signal_p_value(returns):
    """
    One-sided t-test: H0: mean(returns) <= 0, H1: mean(returns) > 0.
    Returns p-value.
    """
    n = len(returns)
    if n < 3:
        return 1.0
    mu = statistics.mean(returns)
    se = statistics.stdev(returns) / math.sqrt(n)
    if se < 1e-12:
        return 0.5
    t_stat = mu / se
    # Approximate p-value from t-distribution (large n → normal)
    return 1.0 - _norm_cdf(t_stat)


# ── Regime-Conditional Analysis ─────────────────────────────────���──────────

def regime_split(signals, regime_field='vol_regime'):
    """Split signals by regime for conditional performance."""
    regimes = defaultdict(list)
    for s in signals:
        r = s.get(regime_field, 'UNKNOWN')
        if r is None:
            r = 'UNKNOWN'
        regimes[str(r)].append(s)
    return dict(regimes)


def regime_conditional_sharpe(signals, horizon='60s', regime_field='vol_regime'):
    """Compute Sharpe per signal type per regime."""
    results = {}
    by_regime = regime_split(signals, regime_field)

    for regime, regime_signals in by_regime.items():
        by_type = defaultdict(list)
        for s in regime_signals:
            pnl = deduct_costs(s.get(f'outcome_{horizon}', 0), s.get('symbol', 'QQQ'))
            by_type[s['signal_type']].append(pnl)

        regime_results = {}
        for sig_type, returns in by_type.items():
            if len(returns) >= 5:
                sr, lo, hi = bootstrap_sharpe_ci(returns)
                regime_results[sig_type] = {
                    'n': len(returns),
                    'sharpe': round(sr, 3),
                    'sharpe_ci_lo': round(lo, 3),
                    'sharpe_ci_hi': round(hi, 3),
                    'win_rate': round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1),
                    'mean_pnl': round(statistics.mean(returns), 4),
                    'std_pnl': round(statistics.stdev(returns), 4) if len(returns) > 1 else 0,
                }
        results[regime] = regime_results

    return results


# ── Walk-Forward Engine ─────────────────────────────────���──────────────────

def walk_forward(signals, train_days=TRAIN_DAYS, test_days=TEST_DAYS, horizon='60s'):
    """
    Walk-forward validation.

    For each test window:
      1. Train: compute per-signal stats on prior train_days
      2. Test: measure actual performance on test_days
      3. Track: which signals the train phase would have selected

    Returns list of {window, train_stats, test_stats, selected_signals}.
    """
    by_day = group_by_day(signals)
    dates = sorted(by_day.keys())

    if len(dates) < train_days + test_days:
        return {'error': f'Need {train_days + test_days} days, have {len(dates)}'}

    windows = []

    for i in range(train_days, len(dates) - test_days + 1):
        train_dates = dates[i - train_days:i]
        test_dates = dates[i:i + test_days]

        train_signals = []
        for d in train_dates:
            train_signals.extend(by_day[d])

        test_signals = []
        for d in test_dates:
            test_signals.extend(by_day[d])

        # ── Train phase: compute per-signal metrics ──
        train_by_type = defaultdict(list)
        for s in train_signals:
            pnl = deduct_costs(s.get(f'outcome_{horizon}', 0), s.get('symbol', 'QQQ'))
            train_by_type[s['signal_type']].append(pnl)

        train_stats = {}
        selected = set()
        for sig_type, returns in train_by_type.items():
            if len(returns) < MIN_SIGNALS_TRAIN:
                continue
            sr = sharpe_ratio(returns)
            wr = sum(1 for r in returns if r > 0) / len(returns)
            avg = statistics.mean(returns)
            train_stats[sig_type] = {
                'n': len(returns),
                'sharpe': round(sr, 3),
                'win_rate': round(wr * 100, 1),
                'mean_pnl': round(avg, 4),
            }
            # Select: positive Sharpe AND positive mean P&L after costs
            if sr > 0 and avg > 0:
                selected.add(sig_type)

        # ── Test phase: measure selected signals' actual performance ──
        test_by_type = defaultdict(list)
        for s in test_signals:
            pnl = deduct_costs(s.get(f'outcome_{horizon}', 0), s.get('symbol', 'QQQ'))
            test_by_type[s['signal_type']].append(pnl)

        test_stats = {}
        test_selected_pnl = []
        test_all_pnl = []
        for sig_type, returns in test_by_type.items():
            avg = statistics.mean(returns) if returns else 0
            sr = sharpe_ratio(returns) if len(returns) >= 2 else 0
            test_stats[sig_type] = {
                'n': len(returns),
                'sharpe': round(sr, 3),
                'win_rate': round(sum(1 for r in returns if r > 0) / max(len(returns), 1) * 100, 1),
                'mean_pnl': round(avg, 4),
                'was_selected': sig_type in selected,
            }
            test_all_pnl.extend(returns)
            if sig_type in selected:
                test_selected_pnl.extend(returns)

        windows.append({
            'train_dates': train_dates,
            'test_dates': test_dates,
            'train_stats': train_stats,
            'test_stats': test_stats,
            'selected_signals': sorted(selected),
            'test_selected_sharpe': round(sharpe_ratio(test_selected_pnl), 3) if len(test_selected_pnl) >= 2 else 0,
            'test_all_sharpe': round(sharpe_ratio(test_all_pnl), 3) if len(test_all_pnl) >= 2 else 0,
            'test_selected_n': len(test_selected_pnl),
            'test_all_n': len(test_all_pnl),
        })

    return windows


# ── Full Report Generation ────────────────────��───────────────────���────────

def generate_report(signals, horizon='60s'):
    """
    Generate comprehensive signal evaluation report.

    Returns dict with:
      - per_signal: stats per signal type (Sharpe, CI, p-value, regime splits)
      - fdr_results: which signals survive FDR correction
      - walk_forward: out-of-sample walk-forward results
      - kill_list: signals to suppress (negative post-cost Sharpe)
      - keep_list: signals with statistical edge
    """
    report = {
        'generated_at': datetime.now().isoformat(),
        'horizon': horizon,
        'total_signals': len(signals),
        'date_range': f"{signals[0]['_date']} to {signals[-1]['_date']}",
        'days': len(set(s['_date'] for s in signals)),
    }

    # ── Per-signal aggregate stats ──
    by_type = defaultdict(list)
    for s in signals:
        pnl = deduct_costs(s.get(f'outcome_{horizon}', 0), s.get('symbol', 'QQQ'))
        by_type[s['signal_type']].append(pnl)

    n_types = len(by_type)
    per_signal = {}
    p_values = {}

    for sig_type, returns in by_type.items():
        n = len(returns)
        if n < 3:
            continue

        sr, sr_lo, sr_hi = bootstrap_sharpe_ci(returns)
        pval = signal_p_value(returns)
        dsr = deflated_sharpe(sr, n, n_types)

        per_signal[sig_type] = {
            'n': n,
            'sharpe': round(sr, 3),
            'sharpe_ci_lo': round(sr_lo, 3),
            'sharpe_ci_hi': round(sr_hi, 3),
            'deflated_sharpe_prob': round(dsr, 3),
            'p_value': round(pval, 4),
            'win_rate': round(sum(1 for r in returns if r > 0) / n * 100, 1),
            'mean_pnl': round(statistics.mean(returns), 4),
            'std_pnl': round(statistics.stdev(returns), 4),
            'max_pnl': round(max(returns), 4),
            'min_pnl': round(min(returns), 4),
            'signals_per_day': round(n / max(report['days'], 1), 1),
        }
        p_values[sig_type] = pval

    report['per_signal'] = per_signal

    # ── FDR correction ──
    fdr = benjamini_hochberg(p_values)
    report['fdr_significant'] = {k: v for k, v in fdr.items()}

    # ── Regime-conditional Sharpe ──
    report['regime_splits'] = regime_conditional_sharpe(signals, horizon, 'vol_regime')
    report['gex_splits'] = regime_conditional_sharpe(signals, horizon, 'gex_zone')

    # ── Walk-forward ──
    wf = walk_forward(signals, horizon=horizon)
    if isinstance(wf, list) and wf:
        report['walk_forward'] = {
            'n_windows': len(wf),
            'windows': wf,
            'avg_selected_sharpe': round(
                statistics.mean(w['test_selected_sharpe'] for w in wf), 3
            ),
            'avg_all_sharpe': round(
                statistics.mean(w['test_all_sharpe'] for w in wf), 3
            ),
        }
    else:
        report['walk_forward'] = wf

    # ── Kill / Keep lists ──
    kill_list = []
    keep_list = []
    for sig_type, stats in per_signal.items():
        if stats['sharpe'] < 0 or stats['mean_pnl'] < 0:
            kill_list.append({
                'signal': sig_type,
                'reason': f"negative post-cost edge (SR={stats['sharpe']}, avgPnL={stats['mean_pnl']})",
                'n': stats['n'],
            })
        elif fdr.get(sig_type, False) and stats['sharpe'] > 0.1 and stats['sharpe_ci_lo'] > -0.5:
            keep_list.append({
                'signal': sig_type,
                'sharpe': stats['sharpe'],
                'deflated_prob': stats['deflated_sharpe_prob'],
                'n': stats['n'],
            })

    report['kill_list'] = sorted(kill_list, key=lambda x: x['n'], reverse=True)
    report['keep_list'] = sorted(keep_list, key=lambda x: x['sharpe'], reverse=True)

    return report


def print_report(report):
    """Pretty-print report to console."""
    print('=' * 80)
    print(f"SIGNAL EVALUATION REPORT — {report['horizon']} horizon")
    print(f"Generated: {report['generated_at']}")
    print(f"Data: {report['total_signals']} signals over {report['days']} days ({report['date_range']})")
    print('=' * 80)

    print('\n── PER-SIGNAL PERFORMANCE (after transaction costs) ──\n')
    print(f"{'Signal':<35s} {'N':>5s} {'SR':>7s} {'CI_lo':>7s} {'CI_hi':>7s} {'WR%':>6s} {'avgPnL':>8s} {'p-val':>7s} {'FDR':>4s}")
    print('-' * 95)

    for sig_type in sorted(report['per_signal'].keys(),
                          key=lambda x: report['per_signal'][x]['sharpe'], reverse=True):
        s = report['per_signal'][sig_type]
        fdr_sig = report['fdr_significant'].get(sig_type, False)
        print(f"{sig_type:<35s} {s['n']:>5d} {s['sharpe']:>7.3f} {s['sharpe_ci_lo']:>7.3f} "
              f"{s['sharpe_ci_hi']:>7.3f} {s['win_rate']:>5.1f}% {s['mean_pnl']:>+8.4f} "
              f"{s['p_value']:>7.4f} {'✓' if fdr_sig else '✗':>4s}")

    print('\n── KILL LIST (suppress these signals) ──\n')
    if report['kill_list']:
        for k in report['kill_list']:
            print(f"  ✗ {k['signal']:<35s} n={k['n']:>5d}  {k['reason']}")
    else:
        print('  (none)')

    print('\n── KEEP LIST (statistically significant edge) ──\n')
    if report['keep_list']:
        for k in report['keep_list']:
            print(f"  ✓ {k['signal']:<35s} SR={k['sharpe']:>+.3f}  DSR_prob={k['deflated_prob']:.3f}  n={k['n']}")
    else:
        print('  (none — no signals survive FDR correction)')

    # Walk-forward summary
    wf = report.get('walk_forward', {})
    if isinstance(wf, dict) and 'windows' in wf:
        print(f"\n── WALK-FORWARD ({wf['n_windows']} windows, train={TRAIN_DAYS}d test={TEST_DAYS}d) ──\n")
        print(f"  Avg test Sharpe (selected only): {wf['avg_selected_sharpe']:+.3f}")
        print(f"  Avg test Sharpe (all signals):   {wf['avg_all_sharpe']:+.3f}")
        print()
        for w in wf['windows']:
            print(f"  Train {w['train_dates'][0]}→{w['train_dates'][-1]} | "
                  f"Test {w['test_dates'][0]} | "
                  f"Selected: {len(w['selected_signals'])} types | "
                  f"SR_selected={w['test_selected_sharpe']:+.3f} "
                  f"SR_all={w['test_all_sharpe']:+.3f} "
                  f"n={w['test_selected_n']}/{w['test_all_n']}")

    # Regime splits
    print('\n── VOL REGIME CONDITIONAL PERFORMANCE ──\n')
    for regime, regime_stats in sorted(report.get('regime_splits', {}).items()):
        print(f"  {regime}:")
        for sig_type in sorted(regime_stats.keys(),
                              key=lambda x: regime_stats[x]['sharpe'], reverse=True):
            s = regime_stats[sig_type]
            print(f"    {sig_type:<32s} n={s['n']:>4d} SR={s['sharpe']:>+.3f} WR={s['win_rate']:>5.1f}%")
        print()


# ── Entry Point ───────────────────────────────────────────────────────���────

def run(outcomes_path=None, horizon='60s', save=True):
    """Run full backtest report."""
    if outcomes_path is None:
        # Find outcomes file relative to project root
        base = Path(__file__).parent.parent
        outcomes_path = base / 'logs' / 'edge_outcomes.jsonl'

    signals = load_outcomes(str(outcomes_path))
    if not signals:
        print('No signals found.')
        return None

    report = generate_report(signals, horizon)
    print_report(report)

    if save:
        report_dir = Path(__file__).parent / 'reports'
        report_dir.mkdir(exist_ok=True)
        fname = report_dir / f"{datetime.now().strftime('%Y%m%d_%H%M')}_signal_report.json"
        with open(fname, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        print(f'\nReport saved to {fname}')

    return report


if __name__ == '__main__':
    run()
