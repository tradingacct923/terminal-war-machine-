#!/usr/bin/env python3
"""
B2+B4 inversion live validator.

Runs at the +22h wakeup and reports whether the inverted gates are hitting
the backtest-predicted 30.1% hit rate.

Method
------
1. Load today's `investigation/bar_signals/bar_signals_YYYYMMDD.jsonl`
2. Filter to emits AFTER inversion shipped (use INVERSION_SHIPPED_TS env or
   default to today 10:39 EDT — when PID 70643 came up with new gates)
3. For each fired absorption event, find the next 3 bars in
   `investigation/candle_bp/candle_bp_YYYYMMDD.jsonl` (or `logs/bp_NQ_*.jsonl`
   as fallback) and score "level held ±1 tick over 3 bars" — the same metric
   used in the 24,121-bar backtest.
4. Report: total emits, scored, hit rate, breakdown by side, vs backtest 30.1%.

Run:  python3 scripts/validate_abs_inversion.py
      INVERSION_SHIPPED_TS=1777905600 python3 scripts/validate_abs_inversion.py
"""
import json, glob, os, datetime, sys

REPO = "/Users/kaali/Desktop/altaris-dev"
TICK = 0.25
TOL_TICKS = 1

# Default inversion shipped timestamp = 10:39 EDT 2026-05-04 = 14:39 UTC = epoch 1777905540
# (the bar BEFORE the first emit at bar_t=1777905600 came in as the first post-restart).
DEFAULT_INVERSION_TS = 1777905540
INVERSION_SHIPPED_TS = float(os.getenv("INVERSION_SHIPPED_TS", DEFAULT_INVERSION_TS))


def safe_float(v, default=0.0):
    try: return float(v)
    except: return default


def parse_bp_h_l(bp):
    """Return (high, low) from bp keys (since bp doesn't store o/h/l/c directly)."""
    if not bp: return None, None
    try:
        prices = [float(k) for k in bp.keys()]
        if not prices: return None, None
        return max(prices), min(prices)
    except Exception:
        return None, None


def load_candles_indexed():
    """Build {bar_t: (h, l)} from BOTH candle_bp investigation logs and bp_NQ logs.

    Today's data may live in either or both:
    - investigation/candle_bp/candle_bp_YYYYMMDD.jsonl   (full schema with ohlc)
    - logs/bp_NQ_YYYYMMDD.jsonl                          (slim {t, bp})
    """
    by_t = {}
    today = datetime.datetime.utcnow().strftime("%Y%m%d")

    # Try investigation log first (richer schema with explicit ohlc)
    inv_paths = sorted(glob.glob(f"{REPO}/investigation/candle_bp/candle_bp_*.jsonl"))
    for p in inv_paths:
        try:
            with open(p) as f:
                for line in f:
                    try: d = json.loads(line)
                    except: continue
                    if d.get('symbol') != 'NQ' or d.get('tf') != '1m': continue
                    bar_t = d.get('bar_t')
                    ohlc = d.get('ohlc') or {}
                    h = safe_float(ohlc.get('h'))
                    l = safe_float(ohlc.get('l'))
                    if h <= 0 or l <= 0:
                        # Fall back to bp keys
                        h, l = parse_bp_h_l(d.get('bp'))
                    if bar_t and h and l:
                        by_t[int(bar_t)] = (float(h), float(l))
        except Exception as e:
            print(f"[warn] couldn't parse {p}: {e}", file=sys.stderr)

    # Augment from bp_NQ files (covers any bars not in investigation log)
    bp_paths = sorted(glob.glob(f"{REPO}/logs/bp_NQ_*.jsonl"))
    for p in bp_paths:
        try:
            with open(p) as f:
                for line in f:
                    try: d = json.loads(line)
                    except: continue
                    bar_t = d.get('t')
                    if not bar_t or int(bar_t) in by_t: continue
                    h, l = parse_bp_h_l(d.get('bp'))
                    if h and l:
                        by_t[int(bar_t)] = (float(h), float(l))
        except Exception as e:
            print(f"[warn] couldn't parse {p}: {e}", file=sys.stderr)

    return by_t


def score_emit(emit, candles_by_t):
    """Return True (held) / False (broken) / None (insufficient lookahead).

    SUCCESS metric (matches 24K-bar backtest):
      side='bullish' (level near low, expect support):
        next 3 bars' min(low) >= signal_price - TOL
      side='bearish' (level near high, expect resistance):
        next 3 bars' max(high) <= signal_price + TOL
    """
    bar_t = int(emit['bar_t'])
    tol = TOL_TICKS * TICK
    side = emit.get('side')
    sig_px = emit.get('price')
    if side not in ('bullish', 'bearish') or not sig_px: return None

    # Need 3 sequential bars after the signal bar (60s candles → +60, +120, +180)
    fut = []
    for i in range(1, 4):
        next_t = bar_t + 60 * i
        hl = candles_by_t.get(next_t)
        if hl is None: return None
        fut.append(hl)
    if len(fut) < 3: return None

    if side == 'bullish':
        return min(l for _, l in fut) >= sig_px - tol
    else:
        return max(h for h, _ in fut) <= sig_px + tol


def main():
    today = datetime.datetime.utcnow().strftime("%Y%m%d")

    # Load today's signal events
    sig_path = f"{REPO}/investigation/bar_signals/bar_signals_{today}.jsonl"
    if not os.path.exists(sig_path):
        # try yesterday in case 22h spans midnight
        yest = (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        sig_path = f"{REPO}/investigation/bar_signals/bar_signals_{yest}.jsonl"
    if not os.path.exists(sig_path):
        print(f"ERROR: bar_signals log not found", file=sys.stderr)
        sys.exit(1)

    print(f"=" * 70)
    print(f"B2+B4 INVERSION LIVE VALIDATION")
    print(f"=" * 70)
    print(f"Signal log:        {sig_path}")
    print(f"Inversion shipped: bar_t >= {int(INVERSION_SHIPPED_TS)} ({datetime.datetime.fromtimestamp(INVERSION_SHIPPED_TS).strftime('%Y-%m-%d %H:%M ET')})")
    print(f"Outcome metric:    level held within ±1 tick over next 3 bars")
    print(f"Backtest expectation: 30.1% (B2≥0.50 + B4≤0.40 + B1=P75, 23 sessions)")
    print()

    candles_by_t = load_candles_indexed()
    print(f"Indexed {len(candles_by_t):,} bars for outcome lookup")
    print()

    # Walk signals
    fired = []
    no_sig = 0
    with open(sig_path) as f:
        for line in f:
            try: d = json.loads(line)
            except: continue
            if d.get('symbol') != 'NQ': continue
            bar_t = d.get('bar_t')
            if not bar_t or int(bar_t) < INVERSION_SHIPPED_TS: continue
            phase = d.get('phase', '')
            if phase == 'fired' and d.get('absorption'):
                for a in d['absorption']:
                    fired.append({**a, 'bar_t': bar_t})
            elif phase == 'no_signal':
                no_sig += 1

    total = len(fired)
    if total == 0:
        print("No absorption emits found in window. Either:")
        print("  - INVERSION_SHIPPED_TS was set too late, OR")
        print("  - Inverted gates are too restrictive (rare; investigate floor params)")
        print()
        print(f"no_signal bars in window: {no_sig:,}")
        return

    # Side breakdown
    bullish = [e for e in fired if e.get('side') == 'bullish']
    bearish = [e for e in fired if e.get('side') == 'bearish']

    # Score outcomes
    scored, hits = 0, 0
    bull_scored, bull_hits = 0, 0
    bear_scored, bear_hits = 0, 0
    unscorable = 0

    for e in fired:
        r = score_emit(e, candles_by_t)
        if r is None:
            unscorable += 1
            continue
        scored += 1
        if r: hits += 1
        if e.get('side') == 'bullish':
            bull_scored += 1
            if r: bull_hits += 1
        elif e.get('side') == 'bearish':
            bear_scored += 1
            if r: bear_hits += 1

    print(f"EMITS")
    print(f"  Total fired:     {total:,}")
    print(f"  Bullish:         {len(bullish):,} (level near bar low)")
    print(f"  Bearish:         {len(bearish):,} (level near bar high)")
    print(f"  no_signal bars:  {no_sig:,} (in window)")
    print(f"  Emit rate:       {total / max(no_sig + total, 1) * 100:.1f}% of bars")
    print()
    print(f"OUTCOMES (level held ±1tick over 3 bars)")
    print(f"  Scorable:        {scored:,} (excludes last 3 bars + missing lookups)")
    print(f"  Unscorable:      {unscorable:,}")
    if scored > 0:
        rate = hits / scored
        print(f"  Hit rate:        {hits}/{scored} = {rate:.1%}")
        if bull_scored: print(f"    bullish:       {bull_hits}/{bull_scored} = {bull_hits/bull_scored:.1%}")
        if bear_scored: print(f"    bearish:       {bear_hits}/{bear_scored} = {bear_hits/bear_scored:.1%}")
        print()
        print(f"COMPARISON")
        print(f"  Pre-inversion (24K-bar backtest):  16.9%")
        print(f"  Post-inversion backtest predict:   30.1%")
        print(f"  Live observed:                     {rate:.1%}")
        if rate >= 0.27:
            print(f"  ✅ Live matches backtest (within 3pp). Inversion validated.")
        elif rate >= 0.20:
            print(f"  ⚠ Live is between pre-inversion (16.9%) and backtest (30.1%).")
            print(f"     Could be sample noise (need more emits), or live conditions")
            print(f"     differ from historical (regime, B3/B5 selection effects).")
        else:
            print(f"  ❌ Live below 20% — backtest gain not reproducing.")
            print(f"     Investigate: B3 (refill_class) interaction, regime, sample bias.")

    # Histogram of strengths
    strengths = sorted([e.get('strength', 0) for e in fired if e.get('strength')])
    if strengths:
        n = len(strengths)
        print()
        print(f"STRENGTH DIST (n={n}): min={strengths[0]:.2f} med={strengths[n//2]:.2f} max={strengths[-1]:.2f}")

    # Imbalance dist (sanity check that B2 inverted is taking effect)
    imbs = sorted([e.get('imbalance', 0) for e in fired if e.get('imbalance')])
    if imbs:
        n = len(imbs)
        print(f"IMBALANCE DIST (n={n}): min={imbs[0]:.2f} med={imbs[n//2]:.2f} max={imbs[-1]:.2f}")
        if imbs[0] < 0.50:
            print(f"  ⚠ At least one emit had imbalance < 0.50 — B2 inversion may not be in effect")
        else:
            print(f"  ✅ All emits have imbalance ≥ 0.50 (new B2 active)")

    # Extreme proximity dist (sanity check B4)
    eps = sorted([e.get('extreme_proximity', 0) for e in fired if e.get('extreme_proximity') is not None])
    if eps:
        n = len(eps)
        print(f"EP DIST (n={n}):       min={eps[0]:.2f} med={eps[n//2]:.2f} max={eps[-1]:.2f}")
        if eps[-1] > 0.40:
            print(f"  ⚠ At least one emit had ep > 0.40 — B4 inversion may not be in effect")
        else:
            print(f"  ✅ All emits have ep ≤ 0.40 (new B4 active)")


if __name__ == '__main__':
    main()
