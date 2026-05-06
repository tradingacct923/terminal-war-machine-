#!/usr/bin/env python3
"""Phase 1 calibration: scan options_book_*.jsonl and produce distributions."""
import json
import sys
import glob
from collections import Counter, defaultdict
from statistics import mean, median, quantiles

path = sorted(glob.glob('/Users/kaali/Desktop/altaris-dev/logs/options_book_*.jsonl'))[-1]
print(f"Analyzing: {path}\n")

n = 0
t_first, t_last = None, None
syms = Counter()
mm_counts_bid, mm_counts_ask = [], []
sizes_bid, sizes_ask = [], []
mm_names = Counter()
levels_per_side = []

# Track Δmm_count per (sym, price, side) for quote-pull detection
prev_state = {}  # (sym, price, side) -> mm_count
pulls = defaultdict(list)  # (sym, price, side) -> list of (ts, drop_magnitude)
pull_drops = []  # flat list of all Δmm_count drops

with open(path) as f:
    for line in f:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        n += 1
        ts = rec['ts']
        sym = rec['sym'].strip()
        if t_first is None:
            t_first = ts
        t_last = ts
        syms[sym] += 1
        b = rec.get('b', [])
        a = rec.get('a', [])
        levels_per_side.append(len(b))
        levels_per_side.append(len(a))

        for lvl in b:
            if len(lvl) >= 4:
                price, size, mmc, mms = lvl[0], lvl[1], lvl[2], lvl[3]
                mm_counts_bid.append(mmc)
                sizes_bid.append(size)
                for mm in mms:
                    mm_names[mm] += 1
                key = (sym, price, 'b')
                if key in prev_state:
                    delta = mmc - prev_state[key]
                    if delta < 0:
                        pulls[key].append((ts, -delta))
                        pull_drops.append(-delta)
                prev_state[key] = mmc

        for lvl in a:
            if len(lvl) >= 4:
                price, size, mmc, mms = lvl[0], lvl[1], lvl[2], lvl[3]
                mm_counts_ask.append(mmc)
                sizes_ask.append(size)
                for mm in mms:
                    mm_names[mm] += 1
                key = (sym, price, 'a')
                if key in prev_state:
                    delta = mmc - prev_state[key]
                    if delta < 0:
                        pulls[key].append((ts, -delta))
                        pull_drops.append(-delta)
                prev_state[key] = mmc

dur_min = (t_last - t_first) / 60000.0

print(f"=== CAPTURE SUMMARY ===")
print(f"Snapshots:    {n:,}")
print(f"Time span:    {dur_min:.1f} minutes")
print(f"Rate:         {n/dur_min:.1f} snapshots/min")
print(f"Unique syms:  {len(syms)}")
print(f"Top 5 syms:   {[s for s,c in syms.most_common(5)]}")
print()

print(f"=== LEVEL STRUCTURE ===")
print(f"Levels per side (min/median/max): {min(levels_per_side)}/{int(median(levels_per_side))}/{max(levels_per_side)}")
print()

def pct(xs):
    if not xs:
        return "n/a"
    qs = quantiles(xs, n=100)
    return f"p50={qs[49]:.1f} p75={qs[74]:.1f} p90={qs[89]:.1f} p95={qs[94]:.1f} p99={qs[98]:.1f} max={max(xs)}"

print(f"=== MM_COUNT per level (bid side) ===")
print(f"  mean={mean(mm_counts_bid):.2f}  {pct(mm_counts_bid)}")
print(f"=== MM_COUNT per level (ask side) ===")
print(f"  mean={mean(mm_counts_ask):.2f}  {pct(mm_counts_ask)}")
print()

print(f"=== SIZE per level (bid contracts) ===")
print(f"  mean={mean(sizes_bid):.1f}  {pct(sizes_bid)}")
print(f"=== SIZE per level (ask contracts) ===")
print(f"  mean={mean(sizes_ask):.1f}  {pct(sizes_ask)}")
print()

print(f"=== MM PARTICIPATION (top 15) ===")
total_mm = sum(mm_names.values())
for mm, cnt in mm_names.most_common(15):
    print(f"  {mm:6s}  {cnt:>10,}  ({100.0*cnt/total_mm:.1f}%)")
print()

print(f"=== QUOTE-PULL EVENTS (Δmm_count < 0) ===")
print(f"Total pull events:  {len(pull_drops):,}")
print(f"Events per minute:  {len(pull_drops)/dur_min:.1f}")
print(f"Drop magnitude:     {pct(pull_drops)}")
# Distribution of drop sizes
pull_dist = Counter(pull_drops)
print(f"Drop size counts:")
for d in sorted(pull_dist.keys())[:8]:
    print(f"  -{d:2d} MMs:  {pull_dist[d]:>8,}  ({100.0*pull_dist[d]/len(pull_drops):.1f}%)")
print()

# Contracts with most pulls
sym_pulls = Counter()
for (sym, price, side), evts in pulls.items():
    sym_pulls[sym] += len(evts)
print(f"=== TOP 10 CONTRACTS BY PULL COUNT ===")
for sym, cnt in sym_pulls.most_common(10):
    print(f"  {sym}  pulls={cnt}")
