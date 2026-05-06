#!/usr/bin/env python3
"""
Phase 19 Validation — Kobayashi (2025) Volatility-Delta

Tests:
  1. Smoke test: paper's numerical example reproduces exactly
     Setup: IV=30%, S=$30, Δ=0.55, Vega=0.12, k_v=0.7
     Expected: Δ_vol = 0.5472

  2. Realistic QQQ test: typical 0DTE ATM with k_v=0.7
  3. k_v estimator test: feed synthetic (spot, IV) data, verify slope recovery
  4. Edge cases: k_v=0, vega=0, spot=0

Run: python scripts/kv_validate.py
"""
import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from connectors.kv_estimator import (
    KvEstimator,
    get_kv_estimator,
    adjust_delta_for_volatility,
    TICKER_DEFAULT_KV,
)


def test_paper_example():
    """Reproduce Kobayashi (2025) §6.2 numerical example."""
    print("=" * 70)
    print(" TEST 1: Paper's numerical example")
    print("=" * 70)
    # Paper says: Δ=0.55, Vega=0.12 (in per-1.0-σ-decimal), k_v=0.7, S_ref=$30
    # Δ_vol = 0.55 - 0.12 × 0.7/30 = 0.5472
    #
    # Our function takes Vega in PER-PP units (Schwab convention).
    # Paper's "Vega = 0.12 per decimal" = "per pp" ÷ 100 = 0.0012 per pp
    # Equivalently: paper's value × 100 = our internal scaling
    delta = 0.55
    vega_per_decimal = 0.12   # Paper's notation
    vega_per_pp = vega_per_decimal / 100  # convert to per-pp (Schwab convention)
    k_v = 0.7
    S_ref = 30.0

    delta_vol = adjust_delta_for_volatility(delta, vega_per_pp, k_v, S_ref)
    expected = 0.55 - 0.12 * 0.7 / 30  # = 0.5472

    print(f"  Inputs:    Δ={delta}, Vega(per pp)={vega_per_pp}, k_v={k_v}, S_ref=${S_ref}")
    print(f"  Computed Δ_vol: {delta_vol:.4f}")
    print(f"  Expected:        {expected:.4f}  (paper §6.2)")
    diff = abs(delta_vol - expected)
    if diff < 1e-4:
        print(f"  ✅ MATCH — diff {diff:.2e}")
        return True
    else:
        print(f"  ❌ MISMATCH — diff {diff:.4f}")
        return False


def test_realistic_qqq():
    """QQQ ATM 0DTE call — typical case our terminal would see."""
    print()
    print("=" * 70)
    print(" TEST 2: Realistic QQQ ATM 0DTE call")
    print("=" * 70)
    # QQQ at $668, ATM call, σ=20%, Vega=0.30 per pp (typical Schwab field)
    delta = 0.50
    vega_per_pp = 0.30
    k_v = 0.7
    spot = 668.0

    delta_vol = adjust_delta_for_volatility(delta, vega_per_pp, k_v, spot)
    correction = delta - delta_vol

    print(f"  Inputs:    Δ={delta}, Vega(per pp)={vega_per_pp}, k_v={k_v}, spot=${spot}")
    print(f"  Δ_vol:     {delta_vol:.4f}")
    print(f"  Correction: {correction:.6f}  ({100*correction/delta:.3f}% of Δ)")
    print()
    print(f"  Manual verification:")
    print(f"     ΔIV per 1% spot = -k_v = -{k_v} pp = {-k_v/100:.4f} decimal")
    print(f"     If spot moves +1% (= +$6.68), IV moves -{k_v} pp")
    print(f"     ΔC from spot:  Δ × ΔS = {delta} × $6.68 = ${delta*6.68:.4f}")
    print(f"     ΔC from vol:   Vega × ΔIV = {vega_per_pp} × -{k_v} = ${vega_per_pp*-k_v:.4f}")
    total_dc = delta * 6.68 + vega_per_pp * -k_v
    delta_implied = total_dc / 6.68
    print(f"     Total ΔC:     ${total_dc:.4f}")
    print(f"     Implied Δ_vol: {delta_implied:.4f}")
    print(f"     Our Δ_vol:    {delta_vol:.4f}")
    if abs(delta_implied - delta_vol) < 1e-6:
        print(f"  ✅ INTERNAL CONSISTENCY")
        return True
    else:
        print(f"  ⚠ Implied vs computed differ — investigate")
        return False


def test_estimator_slope_recovery():
    """Feed synthetic data with known k_v, verify estimator recovers it."""
    print()
    print("=" * 70)
    print(" TEST 3: KvEstimator slope recovery on synthetic data")
    print("=" * 70)

    est = KvEstimator()
    true_k_v = 0.85  # we'll feed data with this true coefficient

    # Generate 30 days of (spot, IV) pairs satisfying ΔIV_pp = -k_v × ΔS%
    import random
    random.seed(42)
    spot = 668.0
    iv_pct = 22.0
    for day in range(30):
        # Random ΔS% in ±2% range
        ds_pct = random.uniform(-2.0, 2.0)
        # IV moves opposite according to true_k_v (with some noise)
        div_pp = -true_k_v * ds_pct + random.gauss(0, 0.05)
        spot = spot * (1 + ds_pct / 100)
        iv_pct = iv_pct + div_pp
        est.add_sample('TEST', day * 86400, spot, iv_pct)

    estimated_kv = est.get_kv('TEST')
    state = est.get_state('TEST')
    print(f"  True k_v:        {true_k_v}")
    print(f"  Estimated k_v:   {estimated_kv:.4f}")
    print(f"  Samples used:    {state.get('samples_used', 0)}")
    diff = abs(estimated_kv - true_k_v)
    if diff < 0.10:  # within 10% (synthetic noise is small)
        print(f"  ✅ PASS — within 10% of true value")
        return True
    else:
        print(f"  ⚠ Larger error than expected: {diff:.3f}")
        return False


def test_edge_cases():
    """k_v=0, vega=0, spot=0 should all return delta unchanged."""
    print()
    print("=" * 70)
    print(" TEST 4: Edge cases (graceful fallback)")
    print("=" * 70)

    cases = [
        ("k_v=0",   adjust_delta_for_volatility(0.5, 0.3, 0.0, 668), 0.5),
        ("vega=0",  adjust_delta_for_volatility(0.5, 0.0, 0.7, 668), 0.5),
        ("spot=0",  adjust_delta_for_volatility(0.5, 0.3, 0.7, 0),   0.5),
        ("normal",  adjust_delta_for_volatility(0.5, 0.3, 0.7, 668), None),
    ]

    all_pass = True
    for name, result, expected in cases:
        if expected is None:
            ok = result != 0.5  # should differ from raw
            print(f"  {name:<12s}  → {result:.4f}  {'✓' if ok else '❌'} (different from raw)")
        else:
            ok = abs(result - expected) < 1e-9
            print(f"  {name:<12s}  → {result:.4f}  expected {expected}  {'✓' if ok else '❌'}")
        if not ok:
            all_pass = False
    return all_pass


def test_default_kvs():
    """Show the default k_v values per ticker."""
    print()
    print("=" * 70)
    print(" TEST 5: Default k_v per ticker (Phase 19 baseline)")
    print("=" * 70)
    for ticker, kv in sorted(TICKER_DEFAULT_KV.items()):
        sign_note = " (REVERSE — vol of vol)" if kv < 0 else ""
        print(f"  {ticker:<6s}  {kv:+.2f} pp/%{sign_note}")
    return True


def main():
    print(f'\n📐 Phase 19 Validation — Kobayashi (2025) Volatility-Delta\n')
    results = {
        'Paper example': test_paper_example(),
        'Realistic QQQ': test_realistic_qqq(),
        'Slope recovery': test_estimator_slope_recovery(),
        'Edge cases':     test_edge_cases(),
        'Default k_v table': test_default_kvs(),
    }
    print()
    print("=" * 70)
    print(" SUMMARY")
    print("=" * 70)
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    if all(results.values()):
        print()
        print("  ✅✅ ALL TESTS PASS — Phase 19 ready for production")
        return 0
    else:
        print()
        print("  ⚠ One or more tests failed — investigate before deploying")
        return 1


if __name__ == '__main__':
    sys.exit(main())
