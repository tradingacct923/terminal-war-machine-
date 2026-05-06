#!/usr/bin/env python3
"""
Honest lead-lag: separate the pull's immediate mid impact from any
CONTINUED drift afterward.

For each pull event at snapshot i:
  mid_at_pull   = mid at snapshot i (post-pull state)
  mid_forward_Xs = mid at i + X seconds

True "heads up" requires: mid_forward_Xs ≠ mid_at_pull in the same
direction as the pull side. If they're equal, the pull WAS the move
and there's no forward signal.
"""
import json
import glob
import random
from collections import defaultdict
from statistics import mean

path = sorted(glob.glob('/Users/kaali/Desktop/altaris-dev/logs/options_book_*.jsonl'))[-1]
print(f"Analyzing: {path}\n")

WINDOWS_MS = [1000, 5000, 15000, 30000]
series = defaultdict(list)

def sum_mmc(levels, k=3):
    return sum(lvl[2] for lvl in levels[:k] if len(lvl) >= 3)

with open(path) as f:
    for line in f:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        sym = rec['sym'].strip()
        if '00654000' not in sym:
            continue
        ts = rec['ts']
        b = rec.get('b', [])
        a = rec.get('a', [])
        best_bid = b[0][0] if b else 0
        best_ask = a[0][0] if a else 0
        series[sym].append((ts, best_bid, best_ask,
                            sum_mmc(b, 3), sum_mmc(a, 3)))

print(f"654-strike contracts: {len(series)}")

PULL_THRESH = 5

# Detect pulls and record POST-pull mid (not pre-pull)
events = []
for sym, pts in series.items():
    for i in range(1, len(pts)):
        t0, bb0, ba0, bmmc0, ammc0 = pts[i-1]
        t1, bb1, ba1, bmmc1, ammc1 = pts[i]
        if t1 - t0 > 2000:
            continue
        if bb1 == 0 or ba1 == 0:
            continue
        mid_at_pull = (bb1 + ba1) / 2.0  # ← post-pull mid
        if bmmc0 - bmmc1 >= PULL_THRESH:
            events.append((t1, sym, 'bid', mid_at_pull))
        if ammc0 - ammc1 >= PULL_THRESH:
            events.append((t1, sym, 'ask', mid_at_pull))

print(f"Cluster pulls: {len(events):,}\n")

def find_mid_at(sym, target_ts):
    pts = series[sym]
    lo, hi = 0, len(pts) - 1
    while lo < hi:
        m = (lo + hi) // 2
        if pts[m][0] < target_ts:
            lo = m + 1
        else:
            hi = m
    if lo >= len(pts):
        return None
    _, bb, ba, _, _ = pts[lo]
    if bb == 0 or ba == 0:
        return None
    return (bb + ba) / 2.0

print("=== HONEST TEST: continued drift AFTER pull settles ===")
print("(mid_forward - mid_at_pull) / mid_at_pull\n")

for side in ('bid', 'ask'):
    evs = [e for e in events if e[2] == side]
    print(f"{side.upper()}-side pulls (n={len(evs):,}):")
    for w in WINDOWS_MS:
        deltas = []
        for t_pull, sym, _, mid_at_pull in evs:
            mid_f = find_mid_at(sym, t_pull + w)
            if mid_f is None:
                continue
            deltas.append((mid_f - mid_at_pull) / mid_at_pull)
        if not deltas:
            continue
        signed = mean(deltas) * 100
        abs_moves = sorted(abs(d)*100 for d in deltas)
        p50 = abs_moves[len(abs_moves)//2]
        p90 = abs_moves[int(len(abs_moves)*0.9)]
        print(f"  t+{w/1000:>2.0f}s:  signed_mean={signed:+.3f}%   "
              f"|Δ| p50={p50:.2f}%  p90={p90:.2f}%   (n={len(deltas)})")
    print()

# Baseline
print("=== BASELINE: random non-pull timestamps (same method) ===")
random.seed(42)
event_keys = {(e[1], e[0]) for e in events}
baseline = []
while len(baseline) < 5000:
    sym = random.choice(list(series.keys()))
    if not series[sym]:
        continue
    idx = random.randint(0, len(series[sym]) - 1)
    ts, bb, ba, _, _ = series[sym][idx]
    if bb == 0 or ba == 0 or (sym, ts) in event_keys:
        continue
    baseline.append((ts, sym, (bb + ba) / 2.0))

for w in WINDOWS_MS:
    deltas = []
    for ts, sym, mid0 in baseline:
        mid_f = find_mid_at(sym, ts + w)
        if mid_f is None:
            continue
        deltas.append((mid_f - mid0) / mid0)
    signed = mean(deltas) * 100
    abs_moves = sorted(abs(d)*100 for d in deltas)
    p50 = abs_moves[len(abs_moves)//2]
    p90 = abs_moves[int(len(abs_moves)*0.9)]
    print(f"  t+{w/1000:>2.0f}s:  signed_mean={signed:+.3f}%   "
          f"|Δ| p50={p50:.2f}%  p90={p90:.2f}%   (n={len(deltas)})")
