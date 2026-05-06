#!/usr/bin/env python3
"""
BSM solver speed benchmark — how fast can we compute per-print Greeks?

Benchmarks:
  1. Pure bsm_greeks (already-known σ): pure formula, no solver
  2. solve_iv only (Newton-Raphson convergence)
  3. Full pipeline: compute_greeks_from_market (IV solve + Greeks)

Tests across realistic QQQ-like inputs spanning 0DTE to LEAPS, ATM to deep wings.

Goal: confirm sub-millisecond compute per print (target: <100 microsec).
At 156 prints/sec sustained throughput, we have 6.4ms budget per print.
"""
import os
import sys
import time
import statistics
import random

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from connectors.bsm_solver import bsm_greeks, solve_iv, compute_greeks_from_market


def gen_realistic_input():
    """Generate a realistic QQQ option input — random within typical ranges."""
    spot = 668.0 + random.uniform(-3, 3)
    strike_offset = random.choice([-50, -25, -10, -5, -2, 0, 2, 5, 10, 25, 50])
    K = round(spot) + strike_offset

    # DTE distribution: weight toward 0-30 (high volume)
    dte_choice = random.choices(
        [0, 7, 30, 90, 365, 730],
        weights=[40, 25, 15, 10, 5, 5]
    )[0]
    if dte_choice == 0:
        T = 0.5 / (365 * 24)  # 30 min for 0DTE
    else:
        T = dte_choice / 365.0

    # Realistic vol
    sigma = random.uniform(0.12, 0.30)

    side = random.choice(['C', 'P'])
    r = 0.044
    q = 0.005

    # Get true price via BSM, then add some noise
    from connectors.bsm_solver import bsm_price
    true_price = bsm_price(spot, K, T, r, q, sigma, side)
    noise = random.uniform(-0.005, 0.005) * max(0.02, true_price)
    market_price = max(0.01, true_price + noise)

    return spot, K, T, r, q, market_price, side


def main():
    print('═' * 80)
    print(' BSM Solver Speed Benchmark')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")}')
    print('═' * 80)
    print()

    # Generate test cases (realistic QQQ-like inputs)
    N = 10_000
    print(f'Generating {N:,} realistic test inputs...')
    test_cases = [gen_realistic_input() for _ in range(N)]
    print(f'Sample input: {test_cases[0]}')
    print()

    # ── Test 1: Pure bsm_greeks (no solver) ──────────────────────────
    print('Test 1: Pure bsm_greeks formula (no IV solver — baseline)')
    print('─' * 80)
    times_greeks = []
    for spot, K, T, r, q, mkt, side in test_cases:
        t0 = time.perf_counter()
        # Use known sigma=0.20 — just test formula speed
        bsm_greeks(spot, K, T, r, q, 0.20, side)
        t1 = time.perf_counter()
        times_greeks.append((t1 - t0) * 1_000_000)  # microseconds

    print(f'  N={N:,}')
    print(f'  Min:           {min(times_greeks):.2f} µs')
    print(f'  Mean:          {statistics.mean(times_greeks):.2f} µs')
    print(f'  Median:        {statistics.median(times_greeks):.2f} µs')
    print(f'  P95:           {statistics.quantiles(times_greeks, n=20)[18]:.2f} µs')
    print(f'  P99:           {statistics.quantiles(times_greeks, n=100)[98]:.2f} µs')
    print(f'  Max:           {max(times_greeks):.2f} µs')
    print(f'  Throughput:    {1_000_000 / statistics.mean(times_greeks):,.0f} ops/sec')
    print()

    # ── Test 2: solve_iv only ────────────────────────────────────────
    print('Test 2: solve_iv only (Newton-Raphson convergence)')
    print('─' * 80)
    times_iv = []
    convergence_failures = 0
    for spot, K, T, r, q, mkt, side in test_cases:
        t0 = time.perf_counter()
        result = solve_iv(spot, K, T, r, q, mkt, side)
        t1 = time.perf_counter()
        times_iv.append((t1 - t0) * 1_000_000)
        if result is None:
            convergence_failures += 1

    print(f'  N={N:,}, convergence_failures={convergence_failures} '
          f'({100*convergence_failures/N:.2f}%)')
    print(f'  Min:           {min(times_iv):.2f} µs')
    print(f'  Mean:          {statistics.mean(times_iv):.2f} µs')
    print(f'  Median:        {statistics.median(times_iv):.2f} µs')
    print(f'  P95:           {statistics.quantiles(times_iv, n=20)[18]:.2f} µs')
    print(f'  P99:           {statistics.quantiles(times_iv, n=100)[98]:.2f} µs')
    print(f'  Max:           {max(times_iv):.2f} µs')
    print(f'  Throughput:    {1_000_000 / statistics.mean(times_iv):,.0f} ops/sec')
    print()

    # ── Test 3: Full pipeline (compute_greeks_from_market) ──────────
    print('Test 3: FULL PIPELINE — compute_greeks_from_market (IV + Greeks + boundary)')
    print('─' * 80)
    times_full = []
    full_failures = 0
    boundary_count = 0
    bsm_count = 0
    for spot, K, T, r, q, mkt, side in test_cases:
        t0 = time.perf_counter()
        result = compute_greeks_from_market(spot, K, T, r, q, mkt, side)
        t1 = time.perf_counter()
        times_full.append((t1 - t0) * 1_000_000)
        if result is None:
            full_failures += 1
        elif result.get('source', '').startswith('boundary'):
            boundary_count += 1
        else:
            bsm_count += 1

    print(f'  N={N:,}')
    print(f'  Full BSM (Newton-Raphson):  {bsm_count}')
    print(f'  Boundary (deep ITM/OTM):    {boundary_count}')
    print(f'  Failures:                   {full_failures}')
    print()
    print(f'  Min:           {min(times_full):.2f} µs')
    print(f'  Mean:          {statistics.mean(times_full):.2f} µs')
    print(f'  Median:        {statistics.median(times_full):.2f} µs')
    print(f'  P95:           {statistics.quantiles(times_full, n=20)[18]:.2f} µs')
    print(f'  P99:           {statistics.quantiles(times_full, n=100)[98]:.2f} µs')
    print(f'  Max:           {max(times_full):.2f} µs')
    print(f'  Throughput:    {1_000_000 / statistics.mean(times_full):,.0f} ops/sec')
    print()

    # ── Verdict — what budget does this use? ─────────────────────────
    print('═' * 80)
    print(' VERDICT — feasibility for live deployment')
    print('═' * 80)
    mean_full_us = statistics.mean(times_full)
    p99_full_us = statistics.quantiles(times_full, n=100)[98]

    target_throughput = 156   # current observed prints/sec on FLOW
    burst_throughput = 500    # peak burst

    target_budget_us = 1_000_000 / target_throughput
    burst_budget_us = 1_000_000 / burst_throughput

    print(f'  Per-print budget at 156/sec sustained: {target_budget_us:,.0f} µs')
    print(f'  Per-print budget at 500/sec burst:     {burst_budget_us:,.0f} µs')
    print(f'  Our P99 latency:                       {p99_full_us:.2f} µs')
    print(f'  Headroom at sustained:                 {target_budget_us / mean_full_us:,.0f}× faster than needed')
    print(f'  Headroom at burst:                     {burst_budget_us / mean_full_us:,.0f}× faster than needed')
    print()
    if mean_full_us < target_budget_us:
        print(f'  ✅ READY for live deployment — {target_budget_us/mean_full_us:,.0f}× faster than needed')
    else:
        print(f'  ❌ TOO SLOW — would cause backlog')


if __name__ == '__main__':
    main()
