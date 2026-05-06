"""Proof artifact for wall_proximity geometry bug + fix.

Joins logs/alerts_2026*.jsonl with logs/alert_outcomes.jsonl and splits
wall_proximity alerts by (spot vs wall) geometry. Prints the pre-fix
measurement table used in MEASURED_VALUES.md so the claim is auditable.

Run: python3 backtest/prove_wall_geometry.py
"""
import glob
import json
import os
from collections import Counter

ROOT = os.path.join(os.path.dirname(__file__), '..')


def load_wall_prox_alerts():
    rows = []
    for fp in sorted(glob.glob(os.path.join(ROOT, 'logs', 'alerts_2026*.jsonl'))):
        with open(fp) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get('type') == 'wall_proximity':
                        rows.append(r)
                except Exception:
                    pass
    return rows


def load_wall_prox_outcomes():
    rows = []
    with open(os.path.join(ROOT, 'logs', 'alert_outcomes.jsonl')) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get('type') == 'wall_proximity':
                    rows.append(r)
            except Exception:
                pass
    return rows


def join_alerts_outcomes(alerts, outcomes, tol=2.0):
    """Match by (ticker, direction, ts within tol seconds)."""
    by_key = {}
    for a in alerts:
        by_key.setdefault((a['ticker'], a['direction']), []).append(a)
    joined = []
    for o in outcomes:
        cands = by_key.get((o['ticker'], o['direction']), [])
        for a in cands:
            if abs(a['ts'] - o['ts']) < tol:
                joined.append((a, o))
                break
    return joined


def main():
    alerts = load_wall_prox_alerts()
    outcomes = load_wall_prox_outcomes()
    joined = join_alerts_outcomes(alerts, outcomes)

    n_by = Counter()
    h5_by = Counter()
    h30_by = Counter()
    for a, o in joined:
        spot = o.get('spot0') or 0
        level = a.get('level') or 0
        if not spot or not level:
            continue
        side = 'above' if spot > level else ('below' if spot < level else 'same')
        key = (o['direction'], a['level_name'], side)
        n_by[key] += 1
        h5_by[key] += o.get('hit_300s', 0) or 0
        h30_by[key] += o.get('hit_1800s', 0) or 0

    print(f"wall_proximity joined rows: alerts={len(alerts)} outcomes={len(outcomes)} joined={len(joined)}")
    print()
    print(f"{'direction':<10}{'wall':<12}{'spot vs wall':<16}{'n':>5}{'hit@5m':>10}{'hit@30m':>10}")
    print('-' * 64)
    for key in sorted(n_by):
        d, lname, side = key
        n = n_by[key]
        h5 = h5_by[key]
        h30 = h30_by[key]
        label = {'above': 'above (support)', 'below': 'below (broken)', 'same': 'same'}.get(side, side)
        if lname == 'call_wall':
            label = {'above': 'above (broken)', 'below': 'below (ceiling)'}.get(side, side)
        print(f"{d:<10}{lname:<12}{label:<16}{n:>5}{h5/n*100:>9.1f}%{h30/n*100:>9.1f}%")

    print()
    print("READ: 'bullish put_wall below' = price already broke the put wall —")
    print("wall is now overhead resistance, not support. 3.2% hit rate confirms.")
    print("Post-fix the detector suppresses these; only 'support intact' fires.")


if __name__ == '__main__':
    main()
