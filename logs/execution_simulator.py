#!/usr/bin/env python3
"""
Phase 7: Execution Simulator v2 — Dynamic Exit Architecture
CIO Directive: Deploy SL = max(3.0, CV*100) against Phase 6 sealed signals.

This simulator runs 6 strategies against the full dataset AND the filtered dataset
to prove the combined impact of Kill Filter + CV Gate + Dynamic Stop.
"""

import json
import os
import math

LOG_PATH = "/Users/kaali/Desktop/altaris-dev/logs/iceberg_outcomes.jsonl"

# Phase 6 Kill Combos
KILL_COMBOS = {
    ('long_gamma_stable', 's'),
    ('transition', 'b'),
    ('short_gamma_volatile', 'b'),
}
CV_GATE = 0.04
CV_GATE_WARMUP = 250  # Kalman needs 250 obs to converge


def load_data():
    signals = []
    with open(LOG_PATH) as f:
        for line in f:
            if not line.strip() or not line.startswith('{'):
                continue
            try:
                r = json.loads(line)
                if r.get('mfe_30s') is not None and r.get('mae_30s') is not None:
                    signals.append(r)
            except:
                pass
    return signals


def apply_phase6_filter(signals):
    """Apply Kill Filter + CV Gate to isolate alpha-only signals."""
    filtered = []
    killed = {'kill_combo': 0, 'cv_gate': 0}
    for s in signals:
        regime = s.get('regime', '')
        side = s.get('side', '')
        cv = s.get('kalman_cv', 0)

        if (regime, side) in KILL_COMBOS:
            killed['kill_combo'] += 1
            continue
        if cv < CV_GATE:
            killed['cv_gate'] += 1
            continue
        filtered.append(s)
    return filtered, killed


def simulate_strategy(signals, name, exit_fn):
    """Run a strategy's exit function against signals, return metrics."""
    trades = []
    for s in signals:
        pnl = exit_fn(s)
        trades.append(pnl)

    if not trades:
        return None

    wins = sum(1 for t in trades if t > 0)
    losses = sum(1 for t in trades if t <= 0)
    gross_win = sum(t for t in trades if t > 0)
    gross_loss = abs(sum(t for t in trades if t < 0))
    net = sum(trades)
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    wr = wins / len(trades) * 100

    # Sharpe approximation (annualized)
    if len(trades) > 1:
        mean_pnl = net / len(trades)
        var = sum((t - mean_pnl)**2 for t in trades) / (len(trades) - 1)
        std = math.sqrt(var) if var > 0 else 0.01
        sharpe = (mean_pnl / std) * math.sqrt(252 * 20)  # ~20 trades/day
    else:
        sharpe = 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)

    return {
        'name': name,
        'trades': len(trades),
        'wins': wins,
        'win_rate': wr,
        'gross_win': gross_win,
        'gross_loss': gross_loss,
        'pf': pf,
        'net': net,
        'avg_pnl': net / len(trades),
        'sharpe': sharpe,
        'max_dd': max_dd,
    }


def print_metrics(m):
    if not m:
        print("  No trades.\n")
        return
    arrow = '🟢' if m['net'] > 0 else '🔴'
    print(f"  {m['name']}")
    print(f"  Trades: {m['trades']}  |  Win Rate: {m['win_rate']:.1f}%  |  "
          f"PF: {m['pf']:.2f}  |  Sharpe: {m['sharpe']:.2f}")
    print(f"  Gross Win: +{m['gross_win']:.2f}  |  Gross Loss: -{m['gross_loss']:.2f}  |  "
          f"Max DD: -{m['max_dd']:.2f}")
    print(f"  {arrow} NET PNL: {m['net']:+.2f} pts  |  Avg: {m['avg_pnl']:+.2f}/trade")
    print()


def main():
    signals = load_data()
    if not signals:
        print("No signals found.")
        return

    # Split by confidence
    high_conf = [s for s in signals if s.get('confidence') == 'high']
    nq_high = [s for s in high_conf if s.get('symbol') == 'NQ']

    data = nq_high if len(nq_high) >= 30 else high_conf if len(high_conf) >= 30 else signals
    label = "NQ High-Conf" if data == nq_high else "All High-Conf" if data == high_conf else "All"

    # Apply Phase 6 filter
    filtered, killed = apply_phase6_filter(data)

    print("=" * 74)
    print("  PHASE 7: EXECUTION SIMULATOR v2 — DYNAMIC EXIT ARCHITECTURE")
    print("=" * 74)
    print(f"\n  Dataset: {len(data)} {label} signals")
    print(f"  Phase 6 Filter: {killed['kill_combo']} killed (regime×side) + "
          f"{killed['cv_gate']} blocked (CV gate)")
    print(f"  Tradeable Signals: {len(filtered)} ({len(filtered)/len(data)*100:.0f}% pass rate)")
    print()

    # ═══════════════════════════════════════════════════════════════
    # DEFINE EXIT STRATEGIES
    # ═══════════════════════════════════════════════════════════════

    def baseline_exit(s):
        """Strategy 1: Dumb T+30s buzzer exit."""
        return s.get('outcome_30s', 0)

    def fixed_bracket_4pt(s):
        """Strategy 2: Fixed 4pt SL (Phase 6 optimal)."""
        mae = abs(s.get('mae_30s', 0))
        outcome = s.get('outcome_30s', 0)
        if mae >= 4.0:
            return -4.0
        return outcome

    def fixed_bracket_3_8(s):
        """Strategy 3: Fixed bracket 3pt SL / 8pt TP."""
        mae = abs(s.get('mae_30s', 0))
        mfe = abs(s.get('mfe_30s', 0))
        outcome = s.get('outcome_30s', 0)
        if mae >= 3.0:
            return -3.0
        if mfe >= 8.0:
            return 8.0
        return outcome

    def dynamic_sl_cv100(s):
        """Strategy 4: SL = max(3.0, CV*100) — THE ALPHA FORMULA."""
        cv = s.get('kalman_cv', 0.05)
        sl = max(3.0, cv * 100)
        mae = abs(s.get('mae_30s', 0))
        outcome = s.get('outcome_30s', 0)
        if mae >= sl:
            return -sl
        return outcome

    def dynamic_sl_cv100_tp(s):
        """Strategy 5: SL = max(3.0, CV*100), TP = SL × 2.5."""
        cv = s.get('kalman_cv', 0.05)
        sl = max(3.0, cv * 100)
        tp = sl * 2.5
        mae = abs(s.get('mae_30s', 0))
        mfe = abs(s.get('mfe_30s', 0))
        outcome = s.get('outcome_30s', 0)
        if mae >= sl:
            return -sl
        if mfe >= tp:
            return tp
        return outcome

    def dynamic_trailing_cv(s):
        """Strategy 6: Dynamic trailing stop — max(3.0, CV*100) as initial,
        then trail at 50% of MFE once MFE > SL (lock in profit)."""
        cv = s.get('kalman_cv', 0.05)
        sl = max(3.0, cv * 100)
        mae = abs(s.get('mae_30s', 0))
        mfe = abs(s.get('mfe_30s', 0))
        outcome = s.get('outcome_30s', 0)

        # If we hit the MAE, we're stopped out
        if mae >= sl:
            return -sl
        # If MFE ran far enough, simulate trailing stop
        # Trail at 50% of peak: if MFE=10 and outcome=2, trail would lock 5
        if mfe > sl and outcome < mfe * 0.5:
            return max(outcome, mfe * 0.5)
        return outcome

    strategies = [
        (baseline_exit, "1. Baseline (Unmanaged T+30s Buzzer)"),
        (fixed_bracket_4pt, "2. Fixed SL=4.0pt (Phase 6 Optimal)"),
        (fixed_bracket_3_8, "3. Fixed Bracket (SL=3, TP=8)"),
        (dynamic_sl_cv100, "4. Dynamic SL = max(3.0, CV×100) ★"),
        (dynamic_sl_cv100_tp, "5. Dynamic SL + TP (SL=CV×100, TP=SL×2.5)"),
        (dynamic_trailing_cv, "6. Dynamic Trailing (lock 50% of MFE) ★★"),
    ]

    # ═══════════════════════════════════════════════════════════════
    # RUN A: UNFILTERED (all signals)
    # ═══════════════════════════════════════════════════════════════
    print("━" * 74)
    print(f"  RUN A: UNFILTERED ({len(data)} signals — no regime/CV gates)")
    print("━" * 74)
    print()

    unfiltered_results = []
    for fn, name in strategies:
        m = simulate_strategy(data, name, fn)
        print_metrics(m)
        unfiltered_results.append(m)

    # ═══════════════════════════════════════════════════════════════
    # RUN B: PHASE 6 FILTERED (alpha-only signals)
    # ═══════════════════════════════════════════════════════════════
    print("━" * 74)
    print(f"  RUN B: PHASE 6 FILTERED ({len(filtered)} signals — kill filter + CV gate)")
    print("━" * 74)
    print()

    filtered_results = []
    for fn, name in strategies:
        m = simulate_strategy(filtered, name, fn)
        print_metrics(m)
        filtered_results.append(m)

    # ═══════════════════════════════════════════════════════════════
    # COMPARISON TABLE
    # ═══════════════════════════════════════════════════════════════
    print("━" * 74)
    print("  COMPARISON: UNFILTERED vs FILTERED + DYNAMIC EXIT")
    print("━" * 74)
    print()

    print(f"{'Strategy':<42} {'Unfilt PnL':<12} {'Filt PnL':<12} {'Δ PnL':<12} {'Filt WR%':<10}")
    print("-" * 74)

    for i, (fn, name) in enumerate(strategies):
        u = unfiltered_results[i]
        f = filtered_results[i]
        if u and f:
            delta = f['net'] - u['net']
            short_name = name.split('. ')[1] if '. ' in name else name
            print(f"  {short_name:<40} {u['net']:+10.2f}  {f['net']:+10.2f}  "
                  f"{delta:+10.2f}  {f['win_rate']:8.1f}%")

    # ═══════════════════════════════════════════════════════════════
    # FINAL CIO VERDICT
    # ═══════════════════════════════════════════════════════════════
    print()
    print("━" * 74)
    print("  CIO VERDICT: OPTIMAL DEPLOYMENT CONFIGURATION")
    print("━" * 74)

    # Find the best filtered strategy
    best = max(filtered_results, key=lambda x: x['net'] if x else -9999)
    baseline_unfiltered = unfiltered_results[0]

    if best and baseline_unfiltered:
        improvement = best['net'] - baseline_unfiltered['net']
        pct_improvement = (improvement / abs(baseline_unfiltered['net']) * 100) if baseline_unfiltered['net'] != 0 else 0

        print(f"""
  BEFORE (unfiltered baseline):  {baseline_unfiltered['net']:+.2f} pts | {baseline_unfiltered['win_rate']:.1f}% WR | {baseline_unfiltered['trades']} trades
  AFTER  (filtered + {best['name'].split('. ')[1]}):
                                 {best['net']:+.2f} pts | {best['win_rate']:.1f}% WR | {best['trades']} trades
  
  IMPROVEMENT:                   {improvement:+.2f} pts ({pct_improvement:+.1f}%)
  SHARPE:                        {best['sharpe']:.2f}
  MAX DRAWDOWN:                  -{best['max_dd']:.2f} pts
  PROFIT FACTOR:                 {best['pf']:.2f}
""")

    print("=" * 74)
    print("  END OF SIMULATION")
    print("=" * 74)


if __name__ == "__main__":
    main()
