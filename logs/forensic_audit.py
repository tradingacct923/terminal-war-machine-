#!/usr/bin/env python3
"""
CIO Forensic Audit — Altaris Signal Quality Analysis
Analyzes iceberg_outcomes.jsonl and edge_outcomes.jsonl for:
  1. Alpha Decay & Slippage Surface
  2. Win-Rate Illusion vs Realized Expectancy
  3. Contextual Adverse Selection
  4. System Upgrade Requirements
"""
import json, math, os, sys
from collections import defaultdict

LOGS_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load datasets ──
def load_jsonl(fname):
    path = os.path.join(LOGS_DIR, fname)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]

ice = load_jsonl("iceberg_outcomes.jsonl")
edge = load_jsonl("edge_outcomes.jsonl")

# NQ tick size = 0.25, tick value = $5.00
NQ_TICK = 0.25
NQ_TICK_VALUE = 5.00
# QQQ tick = $0.01
QQQ_TICK = 0.01

print("=" * 80)
print("  ALTARIS FORENSIC AUDIT — RTH SESSION")
print(f"  Iceberg signals: {len(ice)} | Edge signals: {len(edge)}")
print("=" * 80)

# ══════════════════════════════════════════════════════════════════════
# 1. ALPHA DECAY & SLIPPAGE SURFACE
# ══════════════════════════════════════════════════════════════════════
print("\n" + "═" * 80)
print("  §1  ALPHA DECAY & SLIPPAGE SURFACE")
print("═" * 80)

def analyze_decay(signals, label, tick_size, tick_value):
    if not signals:
        print(f"\n  [{label}] No signals to analyze.")
        return

    # Extract outcome vectors
    o10 = [s["outcome_10s"] for s in signals if s["outcome_10s"] is not None]
    o30 = [s["outcome_30s"] for s in signals if s["outcome_30s"] is not None]
    o60 = [s["outcome_60s"] for s in signals if s["outcome_60s"] is not None]

    if not o10:
        print(f"\n  [{label}] All outcomes are None.")
        return

    avg10 = sum(o10) / len(o10)
    avg30 = sum(o30) / len(o30) if o30 else 0
    avg60 = sum(o60) / len(o60) if o60 else 0

    med10 = sorted(o10)[len(o10)//2]
    med30 = sorted(o30)[len(o30)//2] if o30 else 0
    med60 = sorted(o60)[len(o60)//2] if o60 else 0

    print(f"\n  [{label}] n={len(signals)}")
    print(f"  {'Window':<12} {'Mean':>10} {'Median':>10} {'Ticks':>10} {'$ per signal':>14}")
    print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*14}")
    
    for lbl, avg, med, data in [("T+10s", avg10, med10, o10), 
                                  ("T+30s", avg30, med30, o30), 
                                  ("T+60s", avg60, med60, o60)]:
        ticks = avg / tick_size
        dollars = ticks * tick_value
        print(f"  {lbl:<12} {avg:>+10.4f} {med:>+10.4f} {ticks:>+10.2f} {dollars:>+14.2f}")

    # ── Slippage Surface: 0.5 tick entry + 0.5 tick exit = 1 tick total drag ──
    slippage_drag = 1.0 * tick_size  # 1 full tick round-trip
    print(f"\n  Slippage drag (0.5 tick entry + 0.5 tick exit): {slippage_drag} points")
    print(f"  {'Window':<12} {'Gross':>10} {'Net (post-slip)':>16} {'Survives?':>12}")
    print(f"  {'-'*12} {'-'*10} {'-'*16} {'-'*12}")
    
    for lbl, avg in [("T+10s", avg10), ("T+30s", avg30), ("T+60s", avg60)]:
        net = avg - slippage_drag
        survives = "✅ YES" if net > 0 else "❌ NO"
        print(f"  {lbl:<12} {avg:>+10.4f} {net:>+16.4f} {survives:>12}")

    # ── Peak Alpha Window ──
    # Interpolate between 10s and 30s/60s
    if avg10 > avg30 and avg10 > avg60:
        peak = "T+10s (alpha front-loaded, decays after)"
    elif avg30 > avg10 and avg30 > avg60:
        peak = "T+30s (alpha peaks mid-term, decays by 60s)"
    elif avg60 > avg30:
        peak = "T+60s (alpha still growing — hold longer)"
    else:
        peak = "FLAT (no clear alpha structure)"
    
    print(f"\n  Peak Alpha Window: {peak}")
    
    # Alpha crosses zero?
    if avg10 > 0 and avg60 < 0:
        # Linear interpolation: when does it cross zero?
        # avg10 at t=10, avg60 at t=60
        zero_cross = 10 + (avg10 / (avg10 - avg60)) * 50
        print(f"  ⚠️  Alpha crosses zero at approximately T+{zero_cross:.0f}s")
    elif avg10 < 0:
        print(f"  ⚠️  Alpha is NEGATIVE from T+10s — no edge detected")
    else:
        print(f"  ✅ Alpha remains positive through T+60s")

    return o10, o30, o60

print("\n  ── ICEBERG SIGNALS (NQ Futures) ──")
ice_data = analyze_decay(ice, "ICE", NQ_TICK, NQ_TICK_VALUE)

print("\n  ── EDGE SIGNALS (QQQ + NQ) ──")
edge_data = analyze_decay(edge, "EDGE", QQQ_TICK, 1.0)  # QQQ = $1 per $1 move

# ══════════════════════════════════════════════════════════════════════
# 2. WIN-RATE ILLUSION vs REALIZED EXPECTANCY
# ══════════════════════════════════════════════════════════════════════
print("\n" + "═" * 80)
print("  §2  WIN-RATE ILLUSION vs REALIZED EXPECTANCY")
print("═" * 80)

def win_loss_analysis(signals, label, tick_size, tick_value, timeframe="outcome_30s"):
    outcomes = [s[timeframe] for s in signals if s[timeframe] is not None]
    if not outcomes:
        print(f"\n  [{label}] No data for {timeframe}.")
        return

    wins = [o for o in outcomes if o > 0]
    losses = [o for o in outcomes if o < 0]
    flat = [o for o in outcomes if o == 0]

    n = len(outcomes)
    n_wins = len(wins)
    n_losses = len(losses)
    wr = n_wins / n * 100 if n > 0 else 0

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    
    # Expectancy = (WR × AvgWin) + ((1-WR) × AvgLoss)
    expectancy = (wr/100 * avg_win) + ((1 - wr/100) * avg_loss)
    
    # Profit Factor = Gross Wins / Gross Losses
    gross_wins = sum(wins) if wins else 0
    gross_losses = abs(sum(losses)) if losses else 0.001
    profit_factor = gross_wins / gross_losses

    # Sharpe approximation (annualized at ~6.5hr trading day, ~252 days)
    mean_r = sum(outcomes) / n
    var_r = sum((o - mean_r)**2 for o in outcomes) / n
    std_r = math.sqrt(var_r) if var_r > 0 else 0.001
    
    # Signals per day estimate
    signals_per_day = n  # current count is from partial session
    sharpe_daily = (mean_r / std_r) * math.sqrt(signals_per_day) if std_r > 0 else 0
    
    # Sortino (downside deviation only)
    downside = [o for o in outcomes if o < 0]
    downside_var = sum(o**2 for o in downside) / n if downside else 0.001
    downside_std = math.sqrt(downside_var)
    sortino_daily = (mean_r / downside_std) * math.sqrt(signals_per_day) if downside_std > 0 else 0

    print(f"\n  [{label}] @ {timeframe.replace('outcome_', 'T+')} | n={n}")
    print(f"  {'Metric':<30} {'Value':>15}")
    print(f"  {'-'*30} {'-'*15}")
    print(f"  {'Win Rate':<30} {wr:>14.1f}%")
    print(f"  {'Wins / Losses / Flat':<30} {f'{n_wins} / {n_losses} / {len(flat)}':>15}")
    print(f"  {'Avg Win (points)':<30} {avg_win:>+15.4f}")
    print(f"  {'Avg Loss (points)':<30} {avg_loss:>+15.4f}")
    print(f"  {'Win/Loss Ratio':<30} {abs(avg_win/avg_loss) if avg_loss != 0 else 999:>15.2f}")
    print(f"  {'Expectancy (per signal)':<30} {expectancy:>+15.4f}")
    print(f"  {'Profit Factor':<30} {profit_factor:>15.2f}")
    print(f"  {'Sharpe (daily approx)':<30} {sharpe_daily:>15.2f}")
    print(f"  {'Sortino (daily approx)':<30} {sortino_daily:>15.2f}")

    # ── Distribution Tail Analysis ──
    if losses:
        max_loss = min(losses)
        p5_loss = sorted(losses)[max(0, int(len(losses) * 0.05))]
        p95_loss = sorted(losses)[min(len(losses)-1, int(len(losses) * 0.95))]
        loss_ticks = [l / tick_size for l in losses]
        
        print(f"\n  LOSS TAIL DISTRIBUTION:")
        print(f"  {'Max adverse excursion':<30} {max_loss:>+15.4f} ({max_loss/tick_size:>+.0f} ticks)")
        print(f"  {'5th pctl loss':<30} {p5_loss:>+15.4f}")
        print(f"  {'95th pctl loss':<30} {p95_loss:>+15.4f}")
        print(f"  {'Avg loss / Avg win':<30} {abs(avg_loss/avg_win) if avg_win != 0 else 999:>15.2f}x")
        
        # Fat tail check: are losses > 3x avg_loss frequent?
        if avg_loss != 0:
            extreme_losses = [l for l in losses if l < 3 * avg_loss]  # avg_loss is negative
            pct_extreme = len(extreme_losses) / len(losses) * 100
            print(f"  {'Extreme losses (>3x avg)':<30} {len(extreme_losses):>12} ({pct_extreme:.1f}%)")
            if pct_extreme > 10:
                print(f"  ⚠️  FAT TAIL WARNING: {pct_extreme:.0f}% of losses are extreme")
            else:
                print(f"  ✅ Loss distribution is bounded")

    return outcomes

print("\n  ── ICEBERG (NQ) ──")
for tf in ["outcome_10s", "outcome_30s", "outcome_60s"]:
    win_loss_analysis(ice, "ICE", NQ_TICK, NQ_TICK_VALUE, tf)

print("\n  ── EDGE SIGNALS ──")
for tf in ["outcome_10s", "outcome_30s", "outcome_60s"]:
    win_loss_analysis(edge, "EDGE", QQQ_TICK, 1.0, tf)

# ══════════════════════════════════════════════════════════════════════
# 3. CONTEXTUAL ADVERSE SELECTION
# ══════════════════════════════════════════════════════════════════════
print("\n" + "═" * 80)
print("  §3  CONTEXTUAL ADVERSE SELECTION")
print("═" * 80)

# ── Iceberg: size_rank vs outcome ──
print("\n  ── Iceberg: size_rank × outcome_30s ──")
by_rank = defaultdict(list)
for s in ice:
    rank = s.get("size_rank", "unknown")
    if s["outcome_30s"] is not None:
        by_rank[rank].append(s["outcome_30s"])

if by_rank:
    print(f"  {'Size Rank':<15} {'n':>6} {'WR 30s':>10} {'Avg Move':>12} {'Toxic?':>10}")
    print(f"  {'-'*15} {'-'*6} {'-'*10} {'-'*12} {'-'*10}")
    for rank, outcomes in sorted(by_rank.items(), key=lambda x: -len(x[1])):
        n = len(outcomes)
        wr = sum(1 for o in outcomes if o > 0) / n * 100
        avg = sum(outcomes) / n
        toxic = "🚩 YES" if wr < 50 or avg < 0 else "✅ NO"
        print(f"  {rank:<15} {n:>6} {wr:>9.1f}% {avg:>+12.4f} {toxic:>10}")

# ── Iceberg: confidence vs outcome ──
print("\n  ── Iceberg: confidence × outcome_30s ──")
by_conf = defaultdict(list)
for s in ice:
    conf = s.get("confidence", "unknown")
    if s["outcome_30s"] is not None:
        by_conf[conf].append(s["outcome_30s"])

if by_conf:
    print(f"  {'Confidence':<15} {'n':>6} {'WR 30s':>10} {'Avg Move':>12} {'Toxic?':>10}")
    print(f"  {'-'*15} {'-'*6} {'-'*10} {'-'*12} {'-'*10}")
    for conf, outcomes in sorted(by_conf.items(), key=lambda x: -len(x[1])):
        n = len(outcomes)
        wr = sum(1 for o in outcomes if o > 0) / n * 100
        avg = sum(outcomes) / n
        toxic = "🚩 YES" if wr < 50 or avg < 0 else "✅ NO"
        print(f"  {conf:<15} {n:>6} {wr:>9.1f}% {avg:>+12.4f} {toxic:>10}")

# ── Iceberg: side bias ──
print("\n  ── Iceberg: Side Bias (Long vs Short) ──")
by_side = defaultdict(list)
for s in ice:
    side = "LONG (buy-side)" if s["side"] == "b" else "SHORT (sell-side)"
    if s["outcome_30s"] is not None:
        by_side[side].append(s["outcome_30s"])

for side, outcomes in sorted(by_side.items()):
    n = len(outcomes)
    wr = sum(1 for o in outcomes if o > 0) / n * 100 if n else 0
    avg = sum(outcomes) / n if n else 0
    print(f"  {side:<20} n={n:>5} | WR={wr:>5.1f}% | Avg={avg:>+.4f}")

# ── Edge: signal_type × direction ──
print("\n  ── Edge: Signal Type × Direction × Win Rate ──")
by_type_dir = defaultdict(list)
for s in edge:
    key = f"{s['signal_type']} ({'LONG' if s['is_long'] else 'SHORT'})"
    if s["outcome_30s"] is not None:
        by_type_dir[key].append(s["outcome_30s"])

if by_type_dir:
    print(f"  {'Signal (Direction)':<45} {'n':>5} {'WR':>8} {'Avg':>10} {'Toxic?':>8}")
    print(f"  {'-'*45} {'-'*5} {'-'*8} {'-'*10} {'-'*8}")
    for key, outcomes in sorted(by_type_dir.items(), key=lambda x: -len(x[1])):
        n = len(outcomes)
        wr = sum(1 for o in outcomes if o > 0) / n * 100
        avg = sum(outcomes) / n
        toxic = "🚩" if wr < 50 or avg < 0 else "✅"
        print(f"  {key:<45} {n:>5} {wr:>7.1f}% {avg:>+10.4f} {toxic:>8}")

# ── Beta adjustment ──
print("\n  ── Beta Adjustment: Did we just ride the trend? ──")
# Check if NQ moved directionally today
if ice:
    first_price = ice[0]["price"]
    last_price = ice[-1]["price"]
    nq_move = last_price - first_price
    nq_direction = "UP" if nq_move > 0 else "DOWN"
    print(f"  NQ session move: {first_price:.2f} → {last_price:.2f} ({nq_move:+.2f} pts, {nq_direction})")
    
    # If NQ went UP, long signals should win more. Check if short signals also won.
    long_signals = [s for s in ice if s["side"] == "b" and s["outcome_30s"] is not None]
    short_signals = [s for s in ice if s["side"] == "s" and s["outcome_30s"] is not None]
    
    long_wr = sum(1 for s in long_signals if s["outcome_30s"] > 0) / len(long_signals) * 100 if long_signals else 0
    short_wr = sum(1 for s in short_signals if s["outcome_30s"] > 0) / len(short_signals) * 100 if short_signals else 0
    
    # If both sides win above 50%, we have true alpha, not beta
    if long_wr > 55 and short_wr > 55:
        print(f"  ✅ TRUE ALPHA: Both sides profitable (Long WR={long_wr:.0f}%, Short WR={short_wr:.0f}%)")
        print(f"     Signal edge is NOT driven by directional beta.")
    elif nq_direction == "UP" and long_wr > 60 and short_wr < 50:
        print(f"  🚩 BETA CONTAMINATION: Long WR={long_wr:.0f}% but Short WR={short_wr:.0f}%")
        print(f"     NQ rose {nq_move:+.0f} pts. Long wins are likely trend-following, not iceberg detection.")
    elif nq_direction == "DOWN" and short_wr > 60 and long_wr < 50:
        print(f"  🚩 BETA CONTAMINATION: Short WR={short_wr:.0f}% but Long WR={long_wr:.0f}%")
        print(f"     NQ fell {nq_move:+.0f} pts. Short wins are likely trend-following, not iceberg detection.")
    else:
        print(f"  ⚠️  MIXED: Long WR={long_wr:.0f}%, Short WR={short_wr:.0f}%")
        print(f"     Inconclusive — need larger sample or stronger directional day.")

# ══════════════════════════════════════════════════════════════════════
# 4. SYSTEM UPGRADE REQUIREMENTS
# ══════════════════════════════════════════════════════════════════════
print("\n" + "═" * 80)
print("  §4  SYSTEM UPGRADE REQUIREMENTS")
print("═" * 80)

# ── Hold time optimization ──
print("\n  ── Optimal Hold Time ──")
if ice:
    o10_avg = sum(s["outcome_10s"] for s in ice if s["outcome_10s"] is not None) / max(len([s for s in ice if s["outcome_10s"] is not None]), 1)
    o30_avg = sum(s["outcome_30s"] for s in ice if s["outcome_30s"] is not None) / max(len([s for s in ice if s["outcome_30s"] is not None]), 1)
    o60_avg = sum(s["outcome_60s"] for s in ice if s["outcome_60s"] is not None) / max(len([s for s in ice if s["outcome_60s"] is not None]), 1)
    
    decay_10_30 = o30_avg - o10_avg
    decay_30_60 = o60_avg - o30_avg
    
    print(f"  Avg PnL: T+10s={o10_avg:+.4f} | T+30s={o30_avg:+.4f} | T+60s={o60_avg:+.4f}")
    print(f"  Alpha growth 10→30s: {decay_10_30:+.4f} pts")
    print(f"  Alpha growth 30→60s: {decay_30_60:+.4f} pts")
    
    if o10_avg > o30_avg and o10_avg > o60_avg:
        print(f"  📋 RECOMMENDATION: Cut hold time to 10-15s. Alpha decays after initial pop.")
        print(f"     Dynamic exit: Close on first adverse tick after T+5s")
    elif o30_avg > o10_avg and o30_avg > o60_avg:
        print(f"  📋 RECOMMENDATION: Hold time target = 25-35s. Alpha peaks at T+30s.")
        print(f"     Dynamic exit: Close on OB imbalance flip after T+15s")
    elif o60_avg > o30_avg:
        print(f"  📋 RECOMMENDATION: Extend hold time beyond 60s. Alpha still building.")
        print(f"     Add T+90s and T+120s outcome checks.")
    else:
        print(f"  📋 RECOMMENDATION: Variable hold — exit on opposite-side orderbook dominance flip.")

# ── Signal-level toxicity ──
print("\n  ── Signal Toxicity Audit ──")
if edge:
    print(f"  {'Signal Type':<35} {'Lose %':>8} {'Avg Loss':>10} {'TOXIC':>8}")
    print(f"  {'-'*35} {'-'*8} {'-'*10} {'-'*8}")
    
    by_sig = defaultdict(list)
    for s in edge:
        if s["outcome_30s"] is not None:
            by_sig[s["signal_type"]].append(s["outcome_30s"])
    
    for sig, outcomes in sorted(by_sig.items(), key=lambda x: sum(1 for o in x[1] if o < 0)/max(len(x[1]),1), reverse=True):
        n = len(outcomes)
        losses = [o for o in outcomes if o < 0]
        lose_pct = len(losses) / n * 100
        avg_loss = sum(losses) / len(losses) if losses else 0
        toxic = "🚩 CUT" if lose_pct > 50 else ("⚠️ WATCH" if lose_pct > 40 else "✅ KEEP")
        print(f"  {sig:<35} {lose_pct:>7.1f}% {avg_loss:>+10.4f} {toxic:>8}")

# ── Confidence threshold calibration ──
print("\n  ── Confidence Threshold Calibration ──")
if edge:
    conf_buckets = defaultdict(list)
    for s in edge:
        conf = s.get("confidence", 0)
        if s["outcome_30s"] is not None:
            bucket = f"P{int(conf//10)*10}-{int(conf//10)*10+10}"
            conf_buckets[bucket].append(s["outcome_30s"])
    
    if conf_buckets:
        print(f"  {'Confidence Pctl':<20} {'n':>5} {'WR':>8} {'Avg':>10} {'Action':>12}")
        print(f"  {'-'*20} {'-'*5} {'-'*8} {'-'*10} {'-'*12}")
        for bucket, outcomes in sorted(conf_buckets.items()):
            n = len(outcomes)
            wr = sum(1 for o in outcomes if o > 0) / n * 100
            avg = sum(outcomes) / n
            action = "🚩 FILTER" if wr < 50 else ("⚠️ RAISE" if wr < 60 else "✅ OK")
            print(f"  {bucket:<20} {n:>5} {wr:>7.1f}% {avg:>+10.4f} {action:>12}")

# ══════════════════════════════════════════════════════════════════════
# EXECUTIVE SUMMARY
# ══════════════════════════════════════════════════════════════════════
print("\n" + "═" * 80)
print("  EXECUTIVE SUMMARY")
print("═" * 80)

total_signals = len(ice) + len(edge)
all_outcomes_30 = []
for s in ice:
    if s["outcome_30s"] is not None:
        all_outcomes_30.append(s["outcome_30s"])
for s in edge:
    if s["outcome_30s"] is not None:
        all_outcomes_30.append(s["outcome_30s"])

if all_outcomes_30:
    total_wr = sum(1 for o in all_outcomes_30 if o > 0) / len(all_outcomes_30) * 100
    total_avg = sum(all_outcomes_30) / len(all_outcomes_30)
    print(f"\n  Total signals analyzed: {total_signals}")
    print(f"  Combined WR @30s:      {total_wr:.1f}%")
    print(f"  Combined Avg Move:     {total_avg:+.4f}")
    
    if total_wr > 60 and total_avg > 0:
        print(f"\n  VERDICT: ✅ STRUCTURAL EDGE DETECTED")
        print(f"  The system shows positive expectancy. Proceed to execution optimization.")
    elif total_wr > 50 and total_avg > 0:
        print(f"\n  VERDICT: ⚠️  MARGINAL EDGE — NEEDS CALIBRATION")
        print(f"  Win rate is above 50% but not robust. Filter low-confidence and toxic signals.")
    else:
        print(f"\n  VERDICT: ❌ NO EDGE — DO NOT DEPLOY")
        print(f"  System is providing liquidity, not extracting alpha.")
