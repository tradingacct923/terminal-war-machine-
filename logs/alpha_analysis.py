#!/usr/bin/env python3
"""
Phase 6: Quantitative Alpha Extraction Analysis
CIO Directive — Diagnose PnL bleed and compute optimal execution parameters.
"""

import json
import os
import math
from collections import defaultdict

LOG_PATH = "/Users/kaali/Desktop/altaris-dev/logs/iceberg_outcomes.jsonl"

def load_data():
    signals = []
    with open(LOG_PATH) as f:
        for line in f:
            if not line.strip() or not line.startswith('{'):
                continue
            try:
                r = json.loads(line)
                # Only signals with full state vector + MAE/MFE
                if 'kalman_cv' in r and r.get('mfe_30s') is not None:
                    signals.append(r)
            except:
                pass
    return signals

def percentile(values, pct):
    s = sorted(values)
    idx = int(pct / 100.0 * (len(s) - 1))
    return s[max(0, min(idx, len(s) - 1))]

def main():
    signals = load_data()
    if not signals:
        print("No signals with state vector + MAE/MFE found.")
        return

    high = [s for s in signals if s.get('confidence') == 'high']
    nq_high = [s for s in high if s.get('symbol') == 'NQ']

    print("=" * 70)
    print("  PHASE 6: QUANTITATIVE ALPHA EXTRACTION ANALYSIS")
    print("=" * 70)
    print(f"\nDataset: {len(signals)} total | {len(high)} high-conf | {len(nq_high)} NQ high-conf")
    print()

    # Use high-confidence signals for analysis (or all NQ if few high-conf)
    data = nq_high if len(nq_high) >= 30 else high if len(high) >= 30 else signals
    dataset_label = "NQ High-Conf" if data == nq_high else "All High-Conf" if data == high else "All Signals"
    print(f"Analysis dataset: {dataset_label} ({len(data)} signals)")
    print()

    # ═══════════════════════════════════════════════════════════════
    # 1. VECTOR CORRELATION TO PEAK EXCURSION
    # ═══════════════════════════════════════════════════════════════
    print("━" * 70)
    print("  1. STATE VECTOR → MFE/MAE SKEW CORRELATION")
    print("━" * 70)

    # Compute MFE-MAE skew for each signal
    for s in data:
        mfe = abs(s.get('mfe_30s', 0))
        mae = abs(s.get('mae_30s', 0))
        s['_skew'] = mfe - mae  # positive = good trade, negative = bad trade
        s['_rr'] = mfe / max(mae, 0.01)  # reward-to-risk ratio

    # Continuous features to test
    features = ['kalman_cv', 'vclock_bucket', 'urgency', 'stickiness', 'absorption_ratio']
    
    print(f"\n{'Feature':<22} {'Corr(→Skew)':<14} {'Corr(→RR)':<14} {'Interpretation'}")
    print("-" * 70)

    for feat in features:
        vals = [(s.get(feat, 0), s['_skew']) for s in data if s.get(feat) is not None]
        if len(vals) < 10:
            print(f"{feat:<22} {'N/A':<14} {'N/A':<14} Insufficient data")
            continue

        xs = [v[0] for v in vals]
        ys = [v[1] for v in vals]
        rrs = [(s.get(feat, 0), s['_rr']) for s in data if s.get(feat) is not None]
        rr_ys = [v[1] for v in rrs]

        # Pearson correlation
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        mean_rr = sum(rr_ys) / n

        cov_xy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / n
        std_x = math.sqrt(sum((x - mean_x)**2 for x in xs) / n)
        std_y = math.sqrt(sum((y - mean_y)**2 for y in ys) / n)

        cov_rr = sum((xs[i] - mean_x) * (rr_ys[i] - mean_rr) for i in range(n)) / n
        std_rr = math.sqrt(sum((y - mean_rr)**2 for y in rr_ys) / n)

        corr_skew = cov_xy / (std_x * std_y) if std_x > 0 and std_y > 0 else 0
        corr_rr = cov_rr / (std_x * std_rr) if std_x > 0 and std_rr > 0 else 0

        interp = ""
        if abs(corr_skew) > 0.15:
            interp = "★ SIGNIFICANT" if corr_skew > 0 else "★ INVERSE"
        elif abs(corr_skew) > 0.05:
            interp = "Weak signal"
        else:
            interp = "No signal"

        print(f"{feat:<22} {corr_skew:+.4f}       {corr_rr:+.4f}       {interp}")

    # Regime breakdown
    print(f"\n{'Regime':<26} {'Count':<8} {'Win%':<8} {'AvgSkew':<10} {'AvgMFE':<10} {'AvgMAE':<10}")
    print("-" * 70)
    regimes = defaultdict(list)
    for s in data:
        regimes[s.get('regime', 'unknown')].append(s)

    for regime, sigs in sorted(regimes.items(), key=lambda x: -len(x[1])):
        wins = sum(1 for s in sigs if s.get('outcome_30s', 0) > 0)
        avg_skew = sum(s['_skew'] for s in sigs) / len(sigs)
        avg_mfe = sum(abs(s.get('mfe_30s', 0)) for s in sigs) / len(sigs)
        avg_mae = sum(abs(s.get('mae_30s', 0)) for s in sigs) / len(sigs)
        wr = wins / len(sigs) * 100
        print(f"{regime:<26} {len(sigs):<8} {wr:<8.1f} {avg_skew:<+10.2f} {avg_mfe:<10.2f} {avg_mae:<10.2f}")

    # ═══════════════════════════════════════════════════════════════
    # 2. OPTIMAL MAE THRESHOLD (Noise vs Structural Failure)
    # ═══════════════════════════════════════════════════════════════
    print()
    print("━" * 70)
    print("  2. MAE THRESHOLD: NOISE vs STRUCTURAL FAILURE BOUNDARY")
    print("━" * 70)

    maes = [abs(s.get('mae_30s', 0)) for s in data]
    mfes = [abs(s.get('mfe_30s', 0)) for s in data]

    print(f"\nMAE Distribution (absolute adverse excursion within 30s):")
    for p in [25, 50, 75, 90, 95, 99]:
        print(f"  P{p}: {percentile(maes, p):.2f} pts")

    # Find the MAE threshold that maximizes net PnL
    print(f"\n{'SL Threshold':<14} {'Stopped%':<10} {'WinRate%':<10} {'NetPnL':<10} {'PF':<8} {'Verdict'}")
    print("-" * 70)

    best_pnl = -999999
    best_sl = 0
    outcomes_30s = [s.get('outcome_30s', 0) for s in data]

    for sl in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0, 999]:
        trades = []
        stopped = 0
        for s in data:
            mae = abs(s.get('mae_30s', 0))
            mfe = abs(s.get('mfe_30s', 0))
            outcome = s.get('outcome_30s', 0)

            if mae >= sl:
                trades.append(-sl)
                stopped += 1
            else:
                trades.append(outcome)

        wins = sum(1 for t in trades if t > 0)
        gross_win = sum(t for t in trades if t > 0)
        gross_loss = abs(sum(t for t in trades if t < 0))
        net = sum(trades)
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        wr = wins / len(trades) * 100
        stop_pct = stopped / len(trades) * 100

        label = ""
        if sl == 999:
            label = "(BASELINE)"
        elif net > best_pnl and sl != 999:
            best_pnl = net
            best_sl = sl
            label = "← OPTIMAL" if net > sum(outcomes_30s) else ""

        print(f"  {sl:<12.1f} {stop_pct:<10.1f} {wr:<10.1f} {net:<+10.2f} {pf:<8.2f} {label}")

    print(f"\n  ► Optimal SL: {best_sl} pts (net PnL: {best_pnl:+.2f})")

    # ═══════════════════════════════════════════════════════════════
    # 3. KALMAN-SCALED DYNAMIC STOP OPTIMIZATION
    # ═══════════════════════════════════════════════════════════════
    print()
    print("━" * 70)
    print("  3. KALMAN-SCALED DYNAMIC STOP FORMULA OPTIMIZATION")
    print("━" * 70)

    # Test: SL = max(floor, kalman_cv * multiplier)
    print(f"\n{'Formula':<40} {'NetPnL':<10} {'WinRate%':<10} {'PF':<8}")
    print("-" * 70)

    best_dyn_pnl = -999999
    best_formula = ""

    for floor in [1.0, 1.5, 2.0, 2.5, 3.0]:
        for mult in [25, 50, 75, 100, 150, 200]:
            trades = []
            for s in data:
                cv = s.get('kalman_cv', 0.05)
                dyn_sl = max(floor, cv * mult)
                mae = abs(s.get('mae_30s', 0))
                outcome = s.get('outcome_30s', 0)

                if mae >= dyn_sl:
                    trades.append(-dyn_sl)
                else:
                    trades.append(outcome)

            net = sum(trades)
            wins = sum(1 for t in trades if t > 0)
            gross_win = sum(t for t in trades if t > 0)
            gross_loss = abs(sum(t for t in trades if t < 0))
            pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
            wr = wins / len(trades) * 100

            formula = f"max({floor}, CV*{mult})"
            label = ""
            if net > best_dyn_pnl:
                best_dyn_pnl = net
                best_formula = formula
                label = " ← BEST"

            # Only print interesting results
            if net > best_pnl * 0.8 or label:
                print(f"  SL={formula:<36} {net:<+10.2f} {wr:<10.1f} {pf:<8.2f}{label}")

    print(f"\n  ► Best Dynamic Formula: SL = {best_formula}")
    print(f"  ► Dynamic Net PnL: {best_dyn_pnl:+.2f} vs Fixed Best: {best_pnl:+.2f}")

    # ═══════════════════════════════════════════════════════════════
    # 4. REGIME × SIDE FILTERING (Kill the Losers)
    # ═══════════════════════════════════════════════════════════════
    print()
    print("━" * 70)
    print("  4. REGIME × SIDE MATRIX (Signal Filtering)")
    print("━" * 70)

    print(f"\n{'Regime':<26} {'Side':<6} {'N':<6} {'Win%':<8} {'AvgPnL':<10} {'NetPnL':<10} {'Verdict'}")
    print("-" * 70)

    combos = defaultdict(list)
    for s in data:
        key = (s.get('regime', 'unknown'), s.get('side', '?'))
        combos[key].append(s)

    kill_list = []
    for (regime, side), sigs in sorted(combos.items(), key=lambda x: sum(s.get('outcome_30s', 0) for s in x[1])):
        if len(sigs) < 3:
            continue
        wins = sum(1 for s in sigs if s.get('outcome_30s', 0) > 0)
        net = sum(s.get('outcome_30s', 0) for s in sigs)
        avg = net / len(sigs)
        wr = wins / len(sigs) * 100

        verdict = ""
        if wr < 40 and net < 0:
            verdict = "🔴 KILL"
            kill_list.append((regime, side))
        elif wr < 45 and avg < -1:
            verdict = "🟡 WEAK"
        elif wr > 55 and avg > 1:
            verdict = "🟢 ALPHA"
        else:
            verdict = "⚪ NEUTRAL"

        side_label = "LONG" if side == 'b' else "SHORT" if side == 's' else side
        print(f"  {regime:<24} {side_label:<6} {len(sigs):<6} {wr:<8.1f} {avg:<+10.2f} {net:<+10.2f} {verdict}")

    if kill_list:
        print(f"\n  ► KILL LIST (suppress these regime×side combos):")
        for regime, side in kill_list:
            side_label = "LONG" if side == 'b' else "SHORT" if side == 's' else side
            print(f"    - {regime} + {side_label}")

    # ═══════════════════════════════════════════════════════════════
    # 5. URGENCY × KALMAN QUADRANT ANALYSIS
    # ═══════════════════════════════════════════════════════════════
    print()
    print("━" * 70)
    print("  5. URGENCY × KALMAN CV QUADRANT ANALYSIS")
    print("━" * 70)

    cv_median = percentile([s.get('kalman_cv', 0) for s in data], 50)
    urg_median = percentile([s.get('urgency', 0) for s in data], 50)

    quadrants = {
        'LowCV+LowUrg': [], 'LowCV+HighUrg': [],
        'HighCV+LowUrg': [], 'HighCV+HighUrg': []
    }

    for s in data:
        cv = s.get('kalman_cv', 0)
        urg = s.get('urgency', 0)
        if cv <= cv_median:
            if urg <= urg_median:
                quadrants['LowCV+LowUrg'].append(s)
            else:
                quadrants['LowCV+HighUrg'].append(s)
        else:
            if urg <= urg_median:
                quadrants['HighCV+LowUrg'].append(s)
            else:
                quadrants['HighCV+HighUrg'].append(s)

    print(f"\n  CV median: {cv_median:.4f} | Urgency median: {urg_median:.3f}")
    print(f"\n{'Quadrant':<22} {'N':<6} {'Win%':<8} {'AvgPnL':<10} {'NetPnL':<10} {'AvgMFE':<10} {'AvgMAE':<10}")
    print("-" * 80)

    for qname, sigs in quadrants.items():
        if not sigs:
            continue
        wins = sum(1 for s in sigs if s.get('outcome_30s', 0) > 0)
        net = sum(s.get('outcome_30s', 0) for s in sigs)
        avg = net / len(sigs)
        wr = wins / len(sigs) * 100
        avg_mfe = sum(abs(s.get('mfe_30s', 0)) for s in sigs) / len(sigs)
        avg_mae = sum(abs(s.get('mae_30s', 0)) for s in sigs) / len(sigs)
        print(f"  {qname:<20} {len(sigs):<6} {wr:<8.1f} {avg:<+10.2f} {net:<+10.2f} {avg_mfe:<10.2f} {avg_mae:<10.2f}")

    # ═══════════════════════════════════════════════════════════════
    # 6. FINAL SUMMARY: DEPLOYMENT PARAMETERS
    # ═══════════════════════════════════════════════════════════════
    print()
    print("━" * 70)
    print("  6. DEPLOYMENT PARAMETERS")
    print("━" * 70)

    baseline_pnl = sum(s.get('outcome_30s', 0) for s in data)
    print(f"\n  Baseline (unmanaged):   {baseline_pnl:+.2f} pts")
    print(f"  Best Fixed SL:          {best_pnl:+.2f} pts (SL={best_sl})")
    print(f"  Best Dynamic SL:        {best_dyn_pnl:+.2f} pts ({best_formula})")

    improvement = ((best_dyn_pnl - baseline_pnl) / abs(baseline_pnl) * 100) if baseline_pnl != 0 else 0
    print(f"\n  Dynamic vs Baseline:    {improvement:+.1f}% change")

    if kill_list:
        # Calculate PnL impact of kill list
        killed_pnl = sum(s.get('outcome_30s', 0) for s in data
                        if (s.get('regime', ''), s.get('side', '')) in kill_list)
        print(f"  Kill List PnL drag:     {killed_pnl:+.2f} pts (removing these adds {-killed_pnl:+.2f})")

    print()
    print("=" * 70)
    print("  END OF ANALYSIS")
    print("=" * 70)


if __name__ == "__main__":
    main()
