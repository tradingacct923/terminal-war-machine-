#!/usr/bin/env python3
"""
Lead-lag test: when QQQ 654-strike cluster pulls, does option mid move?

For each contract, compute mid trajectory. For each "cluster pull" event
(≥3 levels pulled simultaneously within 1s window), measure the forward
Δmid over [1s, 5s, 15s, 30s]. Compare to random-timestamp baseline.

If cluster pulls lead moves, post-pull |Δmid| >> baseline.
"""
import json
import glob
from collections import defaultdict
from statistics import mean, median, stdev

path = sorted(glob.glob('/Users/kaali/Desktop/altaris-dev/logs/options_book_*.jsonl'))[-1]
print(f"Analyzing: {path}\n")

# Target: all 654-strike QQQ contracts (from Phase 1 top-10)
TARGETS = set()  # will auto-populate from top-pulled
WINDOWS_MS = [1000, 5000, 15000, 30000]

# Per-contract time-indexed state
# contract -> list of (ts, best_bid, best_ask, total_bid_mmc, total_ask_mmc)
series = defaultdict(list)

# Helper: sum mm_count across top 3 levels on each side
def sum_mmc(levels, k=3):
    return sum(lvl[2] for lvl in levels[:k] if len(lvl) >= 3)

n = 0
with open(path) as f:
    for line in f:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        n += 1
        sym = rec['sym'].strip()
        # Filter to 654-strike only (key finding from Phase 1)
        if '00654000' not in sym:
            continue
        TARGETS.add(sym)
        ts = rec['ts']
        b = rec.get('b', [])
        a = rec.get('a', [])
        best_bid = b[0][0] if b else 0
        best_ask = a[0][0] if a else 0
        bid_mmc = sum_mmc(b, 3)
        ask_mmc = sum_mmc(a, 3)
        series[sym].append((ts, best_bid, best_ask, bid_mmc, ask_mmc))

print(f"Records scanned: {n:,}")
print(f"654-strike contracts: {len(TARGETS)}")
print(f"Points per contract: {mean(len(s) for s in series.values()):.0f} mean, "
      f"{max(len(s) for s in series.values())} max\n")

# Detect cluster pull events: on a single contract, sum_mmc drop ≥ THRESH
# between consecutive snapshots. This already proxies "multi-level pull".
PULL_THRESH = 5  # Δ(sum of top-3 level mm_counts) ≥ 5 MMs lost

events = []  # (ts, sym, side, mid_at_t)
for sym, pts in series.items():
    for i in range(1, len(pts)):
        t0, bb0, ba0, bmmc0, ammc0 = pts[i-1]
        t1, bb1, ba1, bmmc1, ammc1 = pts[i]
        # Only consider if inter-sample gap is small (active trading)
        if t1 - t0 > 2000:
            continue
        dbid = bmmc0 - bmmc1
        dask = ammc0 - ammc1
        if dbid >= PULL_THRESH:
            mid0 = (bb0 + ba0) / 2.0
            if mid0 > 0:
                events.append((t1, sym, 'bid', mid0))
        if dask >= PULL_THRESH:
            mid0 = (bb0 + ba0) / 2.0
            if mid0 > 0:
                events.append((t1, sym, 'ask', mid0))

print(f"Cluster pull events (Δsum_top3_mmc ≥ {PULL_THRESH}): {len(events):,}")
print(f"  bid-side pulls: {sum(1 for e in events if e[2] == 'bid'):,}")
print(f"  ask-side pulls: {sum(1 for e in events if e[2] == 'ask'):,}\n")

# For each event, measure forward |Δmid| at each window
def find_mid_at(sym, target_ts):
    """Binary search for mid at or just after target_ts."""
    pts = series[sym]
    lo, hi = 0, len(pts) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid][0] < target_ts:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(pts):
        return None, None
    ts, bb, ba, _, _ = pts[lo]
    if bb == 0 or ba == 0:
        return None, None
    return ts, (bb + ba) / 2.0

# Treatment: post-pull move
print("=== POST-PULL Δmid by side ===")
for side in ('bid', 'ask'):
    side_events = [e for e in events if e[2] == side]
    if not side_events:
        continue
    print(f"\n{side.upper()}-side pulls (n={len(side_events):,}):")
    for w in WINDOWS_MS:
        deltas = []
        for t0, sym, _, mid0 in side_events:
            _, mid_f = find_mid_at(sym, t0 + w)
            if mid_f is None or mid0 == 0:
                continue
            # Signed pct move
            d = (mid_f - mid0) / mid0
            deltas.append(d)
        if not deltas:
            continue
        pct_moves = [abs(d)*100 for d in deltas]
        signed_moves = [d*100 for d in deltas]
        pct_moves.sort()
        p50 = pct_moves[len(pct_moves)//2]
        p90 = pct_moves[int(len(pct_moves)*0.9)]
        mean_signed = mean(signed_moves)
        print(f"  t+{w/1000:.0f}s:  |Δmid| p50={p50:.2f}%  p90={p90:.2f}%   "
              f"signed_mean={mean_signed:+.3f}%   (n={len(deltas)})")

# Baseline: random timestamps (NOT at pull events)
print("\n=== BASELINE: random non-pull timestamps ===")
import random
random.seed(42)
# Pick N random (sym, ts) from series, excluding event timestamps
event_keys = {(e[1], e[0]) for e in events}
baseline_samples = []
attempts = 0
while len(baseline_samples) < min(len(events), 5000) and attempts < 20000:
    attempts += 1
    sym = random.choice(list(TARGETS))
    if not series[sym]:
        continue
    idx = random.randint(0, len(series[sym]) - 1)
    ts, bb, ba, _, _ = series[sym][idx]
    if bb == 0 or ba == 0:
        continue
    if (sym, ts) in event_keys:
        continue
    baseline_samples.append((ts, sym, 'none', (bb + ba) / 2.0))

print(f"Baseline samples: {len(baseline_samples)}\n")
for w in WINDOWS_MS:
    deltas = []
    for t0, sym, _, mid0 in baseline_samples:
        _, mid_f = find_mid_at(sym, t0 + w)
        if mid_f is None or mid0 == 0:
            continue
        d = (mid_f - mid0) / mid0
        deltas.append(d)
    if not deltas:
        continue
    pct_moves = [abs(d)*100 for d in deltas]
    signed_moves = [d*100 for d in deltas]
    pct_moves.sort()
    p50 = pct_moves[len(pct_moves)//2]
    p90 = pct_moves[int(len(pct_moves)*0.9)]
    mean_signed = mean(signed_moves)
    print(f"  t+{w/1000:.0f}s:  |Δmid| p50={p50:.2f}%  p90={p90:.2f}%   "
          f"signed_mean={mean_signed:+.3f}%   (n={len(deltas)})")
