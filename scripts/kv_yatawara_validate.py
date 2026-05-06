#!/usr/bin/env python3
"""
Phase 19.5 Validation — Yatawara (2026) Memory Kernel integration

Tests:
  1. Yatawara weight function: g(0)=1, monotonic decay, equity vs VIX shape
  2. Weighted median: recovers correct value under known weights
  3. KV_HISTORY_DAYS shrunk from 30 → 12
  4. Recency weighting: synthetic data with regime shift — weighted estimator
     should track new regime faster than uniform Theil-Sen
  5. Memory regime classification: VIX→LONG, QQQ→NORMAL, hypothetical→SHORT
  6. Squeeze half-life: QQQ=30min, VIX=90min, TSLA=45min

Run: python scripts/kv_yatawara_validate.py
"""
import os
import sys
import math
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from connectors.kv_estimator import (
    KvEstimator,
    KV_HISTORY_DAYS,
    TICKER_ALPHA,
    ALPHA_DEFAULT,
    ALPHA_DECAY_C,
    _yatawara_weight,
    _weighted_median,
    adjust_delta_for_volatility,
)
from connectors.vol_surface import (
    _memory_regime_from_alpha,
    _half_life_days_from_alpha,
)
from connectors.dte0_squeeze import (
    estimate_squeeze_half_life_min,
    SQUEEZE_DECAY_ANCHOR_MIN,
    SQUEEZE_DECAY_ANCHOR_ALPHA,
)


def test_yatawara_weight():
    """Decay function: monotonic, anchored at age=0, slower for low α."""
    print("=" * 70)
    print(" TEST 1: Yatawara weight function")
    print("=" * 70)
    # g(0) = exp(0) = 1
    w0 = _yatawara_weight(0, 0.30)
    print(f"  g(0, α=0.30) = {w0:.6f}  (expected 1.0)")
    if abs(w0 - 1.0) > 1e-9:
        print("  ❌ Weight at age=0 not 1.0")
        return False

    # Monotonic decay
    weights = [_yatawara_weight(d, 0.30) for d in [1, 5, 10, 20, 30]]
    print(f"  α=0.30 (QQQ):  age=1→{weights[0]:.4f}, 5→{weights[1]:.4f}, "
          f"10→{weights[2]:.4f}, 20→{weights[3]:.4f}, 30→{weights[4]:.4f}")
    if not all(weights[i] >= weights[i+1] for i in range(len(weights)-1)):
        print("  ❌ Not monotonically decreasing")
        return False

    # VIX α=0.10 should decay slower
    vix_weights = [_yatawara_weight(d, 0.10) for d in [1, 5, 10, 20, 30]]
    print(f"  α=0.10 (VIX):  age=1→{vix_weights[0]:.4f}, 5→{vix_weights[1]:.4f}, "
          f"10→{vix_weights[2]:.4f}, 20→{vix_weights[3]:.4f}, 30→{vix_weights[4]:.4f}")
    # Each VIX weight should be ≥ corresponding QQQ weight (longer memory)
    if not all(vix_weights[i] >= weights[i] for i in range(len(weights))):
        print("  ❌ VIX (low α) should have higher weights than QQQ at all ages")
        return False

    print("  ✅ Decay monotonic, low-α has slower decay than high-α")
    return True


def test_weighted_median():
    """Weighted median: skew toward high-weight side."""
    print()
    print("=" * 70)
    print(" TEST 2: Weighted median")
    print("=" * 70)
    # Equal weights → plain median
    pairs1 = [(1.0, 1.0), (2.0, 1.0), (3.0, 1.0), (4.0, 1.0), (5.0, 1.0)]
    m1 = _weighted_median(pairs1)
    print(f"  Equal weights [1,2,3,4,5] → {m1}  (expected 3)")

    # Heavy weight on high values
    pairs2 = [(1.0, 0.1), (2.0, 0.1), (3.0, 0.1), (4.0, 5.0), (5.0, 5.0)]
    m2 = _weighted_median(pairs2)
    print(f"  Heavy on [4,5] → {m2}  (expected 4)")

    # Heavy weight on low values
    pairs3 = [(1.0, 5.0), (2.0, 5.0), (3.0, 0.1), (4.0, 0.1), (5.0, 0.1)]
    m3 = _weighted_median(pairs3)
    print(f"  Heavy on [1,2] → {m3}  (expected 2)")

    if abs(m1 - 3.0) > 1e-9 or m2 != 4.0 or m3 != 2.0:
        print("  ❌ Weighted median incorrect")
        return False
    print("  ✅ Weighted median correct")
    return True


def test_window_shrunk():
    """Phase 19.5 shrinks KV_HISTORY_DAYS from 30 to 12."""
    print()
    print("=" * 70)
    print(" TEST 3: KV_HISTORY_DAYS lookback")
    print("=" * 70)
    print(f"  KV_HISTORY_DAYS = {KV_HISTORY_DAYS}")
    print(f"  Yatawara median half-life: 5 days (paper §5.2)")
    print(f"  Recommended window: 2× half-life = 10-12 days")
    if KV_HISTORY_DAYS != 12:
        print(f"  ❌ Expected 12, got {KV_HISTORY_DAYS}")
        return False
    print("  ✅ Window correctly shrunk to 12 days")
    return True


def test_regime_shift_tracking():
    """Synthetic data with regime shift — weighted should track newer regime."""
    print()
    print("=" * 70)
    print(" TEST 4: Regime-shift tracking (Yatawara-weighted vs uniform)")
    print("=" * 70)
    # Simulate 12 days with regime shift at day 6:
    # - Days 0-5: k_v = 0.50 (low)
    # - Days 6-11: k_v = 1.00 (high) ← new regime
    # Weighted should land closer to 1.00; uniform median = 0.50 (the older
    # regime has more samples in pairwise slopes since transition contributes
    # noise).
    est = KvEstimator()
    spot = 668.0
    iv = 22.0
    now = time.time()

    # Build 12 daily samples; sample i has timestamp now - (12-i)*86400
    samples = []
    for i in range(13):  # 13 samples = 12 daily slopes
        if i < 7:
            kv_true = 0.50  # OLD regime
        else:
            kv_true = 1.00  # NEW regime
        ts = now - (12 - i) * 86400
        # Move spot deterministically by ±1% alternating
        ds_pct = 1.0 if i % 2 == 0 else -1.0
        new_spot = spot * (1 + ds_pct / 100)
        # IV moves by -kv * ds_pct (no noise — clean test)
        new_iv = iv + (-kv_true * ds_pct)
        samples.append((ts, new_spot, new_iv))
        est.add_sample('TEST_QQQ', ts, new_spot, new_iv)
        spot = new_spot
        iv = new_iv

    state = est.get_state('TEST_QQQ')
    estimated_kv = est.get_kv('TEST_QQQ')
    print(f"  Old regime k_v=0.50 (days 0-5)")
    print(f"  New regime k_v=1.00 (days 6-12)")
    print(f"  Estimated k_v: {estimated_kv:.4f}")
    print(f"  α used:        {state.get('alpha', 0):.3f}  (QQQ default 0.30)")
    print(f"  weighted flag: {state.get('alpha_weighted', False)}")
    print(f"  samples used:  {state.get('samples_used', 0)}")
    # With Yatawara α=0.30 weighting + recent regime, estimate should bias
    # toward 1.00. Without weighting it'd be ~0.50 (pure median of mix).
    # Tolerance: estimate in [0.6, 1.0] proves recency bias is working.
    if 0.60 < estimated_kv < 1.05:
        print(f"  ✅ Estimator tracked toward NEW regime (target: bias > 0.50)")
        return True
    elif estimated_kv <= 0.55:
        print(f"  ❌ Estimator stuck on OLD regime — weighting not effective")
        return False
    else:
        print(f"  ⚠ Estimator outside expected range, investigate")
        return False


def test_memory_regime_classification():
    """LONG/NORMAL/SHORT memory regime by α."""
    print()
    print("=" * 70)
    print(" TEST 5: Memory regime classification")
    print("=" * 70)
    cases = [
        (0.10, 'LONG_MEMORY',  'VIX'),
        (0.27, 'NORMAL',       'IWM'),
        (0.30, 'NORMAL',       'QQQ'),
        (0.32, 'NORMAL',       'SPY'),
        (0.45, 'SHORT_MEMORY', 'hypothetical FX'),
        (0.80, 'SHORT_MEMORY', 'currency-like'),
    ]
    all_pass = True
    for alpha, expected, label in cases:
        got = _memory_regime_from_alpha(alpha)
        hl = _half_life_days_from_alpha(alpha)
        ok = got == expected
        mark = '✓' if ok else '❌'
        print(f"  α={alpha:.2f} ({label:<18s}) → {got:<14s} {mark}  half-life={hl:.1f}d")
        if not ok:
            all_pass = False
    return all_pass


def test_squeeze_half_life():
    """Yatawara α-derived squeeze persistence per ticker."""
    print()
    print("=" * 70)
    print(" TEST 6: 0DTE squeeze half-life from α")
    print("=" * 70)
    print(f"  Anchor: QQQ α={SQUEEZE_DECAY_ANCHOR_ALPHA} → {SQUEEZE_DECAY_ANCHOR_MIN} min (MEASURED)")
    print()
    cases = [
        ('QQQ',  30.0),
        ('SPY',  28.125),  # 30 × 0.30/0.32
        ('IWM',  33.333),  # 30 × 0.30/0.27
        ('VIX',  90.0),    # 30 × 0.30/0.10
        ('TSLA', 45.0),    # 30 × 0.30/0.20
    ]
    all_pass = True
    for ticker, expected in cases:
        got = estimate_squeeze_half_life_min(ticker)
        ok = abs(got - expected) < 0.5
        mark = '✓' if ok else '❌'
        alpha = TICKER_ALPHA.get(ticker, ALPHA_DEFAULT)
        print(f"  {ticker:<5s}  α={alpha:.2f}  →  {got:>6.1f} min  (expected {expected:>6.1f}) {mark}")
        if not ok:
            all_pass = False
    return all_pass


def test_phase19_still_passes():
    """Phase 19 paper example must still reproduce after the changes."""
    print()
    print("=" * 70)
    print(" TEST 7: Phase 19 paper example still reproduces (regression guard)")
    print("=" * 70)
    delta = 0.55
    vega_per_pp = 0.0012
    k_v = 0.7
    S_ref = 30.0
    delta_vol = adjust_delta_for_volatility(delta, vega_per_pp, k_v, S_ref)
    expected = 0.55 - 0.12 * 0.7 / 30
    print(f"  Δ_vol computed: {delta_vol:.4f}")
    print(f"  Expected:       {expected:.4f}")
    if abs(delta_vol - expected) < 1e-4:
        print("  ✅ Phase 19 still passes")
        return True
    print("  ❌ Phase 19 regression!")
    return False


def main():
    print(f'\n📐 Phase 19.5 Validation — Yatawara (2026) Memory Kernel\n')
    results = {
        'Yatawara weight':           test_yatawara_weight(),
        'Weighted median':           test_weighted_median(),
        'Window shrunk to 12d':      test_window_shrunk(),
        'Regime-shift tracking':     test_regime_shift_tracking(),
        'Memory regime classify':    test_memory_regime_classification(),
        'Squeeze half-life':         test_squeeze_half_life(),
        'Phase 19 regression':       test_phase19_still_passes(),
    }
    print()
    print("=" * 70)
    print(" SUMMARY")
    print("=" * 70)
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    if all(results.values()):
        print()
        print("  ✅✅ ALL TESTS PASS — Phase 19.5 ready")
        return 0
    else:
        print()
        print("  ⚠ One or more tests failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
