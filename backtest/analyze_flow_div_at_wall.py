"""
Slice alert_outcomes.jsonl by wall-distance bucket to test the 0DTHero thesis:
  flow_divergence × wall_proximity ≫ flow_divergence alone

Two wall definitions compared side-by-side:
  OI wall    — strike with max open interest (historical positioning)
  gamma wall — strike with max dollar-gamma (live dealer hedging load)

Buckets raw outcomes into:
  at_wall     — nearest_pct < 0.3
  near_wall   — 0.3 ≤ nearest_pct < 1.0
  far_wall    — ≥ 1.0
  no_wall_data — None (pre-capture)

Reports hit rate + signed expectancy per (type, direction, wall_kind, bucket, horizon).
Back-compat: also reads the legacy 'nearest_wall_pct' field from pre-2026-04-23
outcome rows and reports it under wall_kind='legacy'.
"""
import json
import os
import statistics
from collections import defaultdict

OUTCOMES = os.path.join(os.path.dirname(__file__), '..', 'logs', 'alert_outcomes.jsonl')
HORIZONS = (300, 900, 1800)
WALL_BUCKETS = [
    ('at_wall',      lambda d: d is not None and d < 0.3),
    ('near_wall',    lambda d: d is not None and 0.3 <= d < 1.0),
    ('far_wall',     lambda d: d is not None and d >= 1.0),
    ('no_wall_data', lambda d: d is None),
]


def bucket_of(dist):
    for name, pred in WALL_BUCKETS:
        if pred(dist):
            return name
    return 'no_wall_data'


def main():
    if not os.path.exists(OUTCOMES):
        print(f"No outcomes file at {OUTCOMES}")
        return

    rows = []
    with open(OUTCOMES) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass

    # (type, direction, wall_kind, bucket, horizon) -> list of delta_pcts
    # Each row counted under exactly one wall_kind to avoid double-counting:
    #   'oi'+'gamma' — row has the new tagged fields (post-2026-04-23 change)
    #   'legacy'     — row predates the change (no wall fields at all)
    buckets = defaultdict(list)
    for r in rows:
        t = r.get('type')
        d = r.get('direction')
        if d not in ('bullish', 'bearish'):
            continue
        has_new = ('nearest_oi_wall_pct' in r) or ('nearest_gamma_wall_pct' in r)
        if has_new:
            slicings = [
                ('oi',    r.get('nearest_oi_wall_pct')),
                ('gamma', r.get('nearest_gamma_wall_pct')),
            ]
        else:
            slicings = [('legacy', r.get('nearest_wall_pct'))]  # None for truly pre-capture rows
        for wall_kind, dist in slicings:
            b = bucket_of(dist)
            for h in HORIZONS:
                dp = r.get(f'delta_{h}s_pct')
                if dp is None:
                    continue
                buckets[(t, d, wall_kind, b, h)].append(dp)

    print(f"Loaded {len(rows)} outcomes from {OUTCOMES}")
    print("=" * 102)
    print(f"{'type':<18} {'dir':<8} {'kind':<7} {'bucket':<14} {'horizon':<8} {'n':>4}  {'hit':>6}  {'avg%':>8}  {'E[align]%':>10}")
    print("-" * 102)

    bucket_order = [b[0] for b in WALL_BUCKETS]
    kind_order = ['oi', 'gamma', 'legacy']
    def sort_key(k):
        t, d, kind, b, h = k
        return (t, d, kind_order.index(kind), bucket_order.index(b), h)

    for key in sorted(buckets.keys(), key=sort_key):
        (t, d, kind, b, h) = key
        vals = buckets[key]
        n = len(vals)
        if n < 3:
            continue
        hits = sum(1 for v in vals if (v > 0 if d == 'bullish' else v < 0))
        hit_rate = hits / n
        mean = sum(vals) / n
        expectancy = mean if d == 'bullish' else -mean
        print(f"{t:<18} {d:<8} {kind:<7} {b:<14} t+{h:<5d} {n:>4}  {hit_rate:>5.1%}  {mean:>+7.3f}  {expectancy:>+9.4f}")

    # Headline test: OI-wall at_wall vs gamma-wall at_wall — which one
    # carries the real flow_divergence edge?
    print("\n" + "=" * 102)
    print("HEADLINE: flow_divergence — OI wall vs gamma wall at_wall (<0.3%) edge comparison")
    print("=" * 102)
    for d in ('bullish', 'bearish'):
        for h in HORIZONS:
            oi  = buckets.get(('flow_divergence', d, 'oi',    'at_wall', h), [])
            gm  = buckets.get(('flow_divergence', d, 'gamma', 'at_wall', h), [])
            far = buckets.get(('flow_divergence', d, 'gamma', 'far_wall', h), [])
            if len(oi) < 3 and len(gm) < 3 and len(far) < 3:
                continue
            def _stat(vs, dd):
                if not vs:
                    return "n=0"
                hits = sum(1 for v in vs if (v > 0 if dd == 'bullish' else v < 0))
                mean = sum(vs) / len(vs)
                exp = mean if dd == 'bullish' else -mean
                return f"n={len(vs):>3}  hit={hits/len(vs):.1%}  E={exp:+.3f}%"
            print(f"  {d:<8} t+{h:>4}s  OI@wall:  {_stat(oi,d):<28}  γ@wall:  {_stat(gm,d):<28}  γ-far:  {_stat(far,d)}")


if __name__ == '__main__':
    main()
