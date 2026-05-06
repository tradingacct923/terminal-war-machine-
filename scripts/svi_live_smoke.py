#!/usr/bin/env python3
"""
Phase 20A Smoke Test — Nguyen (2025) SVI calibration on LIVE QQQ 0DTE chain

Purpose: verify the SVI calibration from external/vol-surface-arbitrage
runs against our actual Schwab chain (not synthetic) and produces
acceptable RMSE.

Pass criteria: RMSE < 50bp on calls + puts together (post-arb-clean).

Usage:
    source venv/bin/activate
    python scripts/svi_live_smoke.py
"""
import os
import sys
import time
import json
import math
import hmac
import hashlib
import urllib.request

# Make Nguyen's repo importable
NGUYEN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'external/vol-surface-arbitrage'
)
if NGUYEN_PATH not in sys.path:
    sys.path.insert(0, NGUYEN_PATH)

from src.svi import (
    SVIParams,
    calibrate_svi,
    svi_implied_vol,
    svi_total_variance,
    svi_butterfly_density,
    has_butterfly_arbitrage,
)
import numpy as np
from scipy.optimize import minimize, Bounds


def calibrate_svi_total_variance(k, market_iv, T, n_restarts=8):
    """Variant of Nguyen's calibration that fits in TOTAL-VARIANCE space.

    Their calibration fits in IV space with `iv = sqrt(max(w, 1e-10) / T)` —
    at small T (0DTE: T ~ 7e-4) the optimizer drives w negative, which gets
    clipped to 1e-10, producing iv ≈ 0.04% trivial degenerate solution.

    Solution: optimize sum((w_model - w_target)^2) where:
      w_target = market_iv^2 * T
      w_model  = a + b * (rho*(k-m) + sqrt((k-m)^2 + sigma^2))

    No more sqrt blow-up; objective is well-conditioned at any T.

    Returns (params, rmse_in_iv_bp).
    """
    if len(k) < 5:
        raise ValueError(f"Need ≥5 strikes, got {len(k)}")

    target_w = market_iv ** 2 * T   # target total variance at each strike

    bounds = Bounds(
        lb=[-np.inf, 0.0, -0.999, -np.inf, 1e-6],
        ub=[ np.inf, np.inf,  0.999,  np.inf, np.inf],
    )

    # Data-driven init: ATM total variance
    atm_iv = float(np.interp(0.0, k, market_iv))
    atm_var = atm_iv ** 2 * T
    candidates = [
        # Centered, low wings
        np.array([atm_var * 0.5, atm_var * 5.0,  -0.30, 0.0, 0.05]),
        # Wider sigma
        np.array([atm_var * 0.3, atm_var * 10.0, -0.30, 0.0, 0.10]),
        # Less skew
        np.array([atm_var * 0.5, atm_var * 5.0,  -0.10, 0.0, 0.05]),
    ]
    rng = np.random.default_rng(seed=42)
    for _ in range(n_restarts):
        a0 = rng.uniform(0.0, atm_var * 1.5)
        b0 = rng.uniform(0.0, atm_var * 50.0)
        rho0 = rng.uniform(-0.9, 0.0)
        m0 = rng.uniform(-0.05, 0.05)
        sigma0 = rng.uniform(0.01, 0.30)
        candidates.append(np.array([a0, b0, rho0, m0, sigma0]))

    def objective(p):
        params = SVIParams.from_array(p)
        try:
            w = svi_total_variance(k, params)
            # Penalize negative w (no-arb): butterfly density check on data points
            neg_pen = np.sum(np.maximum(-w, 0) ** 2) * 1e6
            # Fit in total-variance space (well-conditioned)
            return np.sum((w - target_w) ** 2) + neg_pen
        except Exception:
            return 1e20

    best_cost = np.inf
    best_x = None
    for x0 in candidates:
        try:
            r = minimize(objective, x0, method='L-BFGS-B', bounds=bounds,
                          options={'maxiter': 5000, 'ftol': 1e-14, 'gtol': 1e-10})
            if r.fun < best_cost:
                best_cost = r.fun
                best_x = r.x
        except Exception:
            continue

    if best_x is None:
        raise RuntimeError("Total-variance SVI calibration failed")
    params = SVIParams.from_array(best_x)
    iv_model = svi_implied_vol(k, T, params)
    rmse_decimal = float(np.sqrt(np.mean((iv_model - market_iv) ** 2)))
    return params, rmse_decimal


def mint_token():
    SECRET = b'wm-greeksite-secret-key-2024'
    USER = 'Kaali4426'
    ts = int(time.time())
    ts_hex = format(ts, 'x')
    user_hex = USER.encode().hex()
    msg = f"{ts_hex}.{user_hex}".encode()
    sig = hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:32]
    return f"{ts_hex}.{user_hex}.{sig}"


def fetch_chain(ticker='QQQ', exp=None):
    """Fetch chain. If exp is None, returns nearest expiry."""
    token = mint_token()
    if exp:
        url = f"http://localhost:3001/api/chain?ticker={ticker}&exp={exp}"
    else:
        url = f"http://localhost:3001/api/chain?ticker={ticker}"
    req = urllib.request.Request(url, headers={'X-Auth-Token': token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def list_expirations(ticker='QQQ'):
    """Return list of (date_str, dte) tuples."""
    chain_data = fetch_chain(ticker)
    return [(e['date'], e['dte']) for e in chain_data.get('expirations', [])]


def build_iv_arrays(chain_data, side='call'):
    """Build (k_array, iv_array, T) for one side of the chain.

    Filters:
    - Side matches (call vs put)
    - IV is parseable and positive
    - OI > 0 OR volume > 0 (live trading interest, not stale)
    - |delta| ∈ [0.05, 0.95] — exclude deep ITM/OTM where IV is unreliable

    Returns:
        k:   np.array of log-moneyness k = log(K/F), F = spot (T≈0)
        iv:  np.array of decimal IV (e.g., 0.18 for 18%)
        T:   time to expiry in years (computed from dte field; 0DTE → small but >0)
        meta: dict with diagnostics
    """
    spot = float(chain_data['spot'])
    dte = int(chain_data.get('dte', 0))

    # 0DTE: post-close → use full next-day window (16h to next open).
    # 1+DTE: full days + half-day for current session
    if dte == 0:
        days_left = 0.25
    else:
        days_left = float(dte) + 0.5  # current session has time left
    T = days_left / 365.0
    F = spot  # T tiny, r tiny → forward ≈ spot

    chain = chain_data['chain']
    k_list, iv_list = [], []
    skipped = {'wrong_side': 0, 'no_iv': 0, 'no_interest': 0, 'extreme_delta': 0}

    for opt in chain:
        if opt.get('type', '').lower() != side:
            skipped['wrong_side'] += 1
            continue

        try:
            iv_raw = opt.get('iv')
            if iv_raw is None or iv_raw == '' or iv_raw == 'N/A':
                skipped['no_iv'] += 1
                continue
            iv_pct = float(iv_raw)
            if iv_pct <= 0 or iv_pct > 500:
                skipped['no_iv'] += 1
                continue
        except (ValueError, TypeError):
            skipped['no_iv'] += 1
            continue

        oi = int(opt.get('oi', 0) or 0)
        volume = int(opt.get('volume', 0) or 0)
        if oi == 0 and volume == 0:
            skipped['no_interest'] += 1
            continue

        delta = abs(float(opt.get('delta', 0) or 0))
        if delta < 0.05 or delta > 0.95:
            skipped['extreme_delta'] += 1
            continue

        strike = float(opt['strike'])
        if strike <= 0:
            continue

        k = math.log(strike / F)
        iv = iv_pct / 100.0  # percent → decimal
        k_list.append(k)
        iv_list.append(iv)

    return (
        np.array(k_list),
        np.array(iv_list),
        T,
        {
            'spot':       spot,
            'forward':    F,
            'T_years':    T,
            'days_left':  days_left,
            'n_used':     len(k_list),
            'n_total':    len(chain),
            'skipped':    skipped,
        },
    )


def smoke_test(side, exp_date=None, expected_dte=0):
    print()
    print("=" * 78)
    print(f" SVI CALIBRATION SMOKE TEST — QQQ {side.upper()}S exp={exp_date} (~{expected_dte}DTE)")
    print("=" * 78)

    # Fetch live chain
    print(f"  Fetching /api/chain?ticker=QQQ&exp={exp_date} ...")
    t0 = time.time()
    chain_data = fetch_chain('QQQ', exp=exp_date)
    t_fetch = time.time() - t0
    print(f"  Fetched in {t_fetch*1000:.0f}ms — spot=${chain_data['spot']}, "
          f"chain size={len(chain_data['chain'])}")

    # Build IV arrays
    k, iv, T, meta = build_iv_arrays(chain_data, side=side)
    print()
    print(f"  Filtered: {meta['n_used']}/{meta['n_total']} contracts kept ({side}s)")
    print(f"  Skipped: wrong_side={meta['skipped']['wrong_side']}, "
          f"no_iv={meta['skipped']['no_iv']}, "
          f"no_interest={meta['skipped']['no_interest']}, "
          f"extreme_delta={meta['skipped']['extreme_delta']}")
    print(f"  T = {T:.6f} years ({meta['days_left']:.2f} days)")
    if len(k) < 5:
        print(f"  ❌ Not enough strikes ({len(k)}) for SVI fit (need ≥5)")
        return False, None

    print(f"  k range: [{k.min():.4f}, {k.max():.4f}] (log-moneyness)")
    print(f"  IV range: [{iv.min()*100:.1f}%, {iv.max()*100:.1f}%]")

    # === Approach A: Nguyen's stock calibrate_svi (IV-space objective) ===
    print()
    print(f"  [A] Nguyen stock calibration (IV-space)...")
    t0 = time.time()
    try:
        params_A, rmse_A_decimal = calibrate_svi(
            k, iv, T,
            butterfly_penalty=1e4,
            n_restarts=5,
        )
        t_A = time.time() - t0
        rmse_A_bp = rmse_A_decimal * 10000
        print(f"      params: a={params_A.a:.4e}  b={params_A.b:.4e}  rho={params_A.rho:.3f}")
        print(f"      RMSE_A: {rmse_A_bp:.1f}bp  (calib {t_A*1000:.0f}ms)")
    except Exception as e:
        print(f"      ❌ FAILED: {e}")
        params_A, rmse_A_bp, t_A = None, float('inf'), 0

    # === Approach B: total-variance objective (our fix) ===
    print(f"  [B] Total-variance objective (small-T-stable)...")
    t0 = time.time()
    try:
        params_B, rmse_B_decimal = calibrate_svi_total_variance(k, iv, T, n_restarts=8)
        t_B = time.time() - t0
        rmse_B_bp = rmse_B_decimal * 10000
        print(f"      params: a={params_B.a:.4e}  b={params_B.b:.4e}  rho={params_B.rho:.3f}")
        print(f"      RMSE_B: {rmse_B_bp:.1f}bp  (calib {t_B*1000:.0f}ms)")
    except Exception as e:
        print(f"      ❌ FAILED: {e}")
        params_B, rmse_B_bp, t_B = None, float('inf'), 0

    # Pick the better one for downstream analysis
    if rmse_B_bp < rmse_A_bp:
        params = params_B
        rmse_decimal = rmse_B_bp / 10000
        chosen = 'B (total-variance)'
    else:
        params = params_A
        rmse_decimal = rmse_A_bp / 10000
        chosen = 'A (IV-space)'

    print(f"  Chosen: {chosen}")
    t_calib = max(t_A, t_B)
    rmse_bp = rmse_decimal * 10000
    print(f"  Calibration completed in {t_calib*1000:.0f}ms")
    print()
    print(f"  Fitted parameters:")
    print(f"    a     = {params.a:>8.5f}  (level)")
    print(f"    b     = {params.b:>8.5f}  (wing slope)")
    print(f"    rho   = {params.rho:>8.5f}  (skew)")
    print(f"    m     = {params.m:>8.5f}  (ATM offset)")
    print(f"    sigma = {params.sigma:>8.5f}  (curvature)")
    print(f"    valid = {params.is_valid()}")

    # Per-strike residuals
    iv_model = svi_implied_vol(k, T, params)
    residuals_decimal = iv - iv_model
    residuals_bp = residuals_decimal * 10000
    abs_residuals_bp = np.abs(residuals_bp)

    print()
    print(f"  RMSE: {rmse_bp:.1f}bp  (PASS threshold: <50bp)")
    print(f"  Mean abs residual: {abs_residuals_bp.mean():.1f}bp")
    print(f"  P50 abs residual:  {np.percentile(abs_residuals_bp, 50):.1f}bp")
    print(f"  P90 abs residual:  {np.percentile(abs_residuals_bp, 90):.1f}bp")
    print(f"  P99 abs residual:  {np.percentile(abs_residuals_bp, 99):.1f}bp")
    print(f"  Max abs residual:  {abs_residuals_bp.max():.1f}bp")

    # Show worst strikes
    print()
    print(f"  Top 5 worst-fit strikes:")
    worst_idx = np.argsort(-abs_residuals_bp)[:5]
    F = meta['forward']
    for i in worst_idx:
        K = F * math.exp(k[i])
        print(f"    K=${K:>7.2f}  k={k[i]:>+7.4f}  "
              f"obs={iv[i]*100:>5.2f}%  fit={iv_model[i]*100:>5.2f}%  "
              f"resid={residuals_bp[i]:>+6.1f}bp")

    # Arbitrage check
    print()
    k_grid = np.linspace(k.min() - 0.05, k.max() + 0.05, 200)
    butterfly_violation = has_butterfly_arbitrage(k_grid, params)
    g = svi_butterfly_density(k_grid, params)
    print(f"  No-arbitrage check:")
    print(f"    Butterfly violation: {'❌ YES' if butterfly_violation else '✅ NO'}")
    print(f"    g(k) range: [{g.min():.4f}, {g.max():.4f}]  (must be ≥ 0)")
    if butterfly_violation:
        bad_count = (g < -1e-4).sum()
        print(f"    Strikes with negative density: {bad_count}/{len(k_grid)}")

    # Verdict for this side
    pass_rmse = rmse_bp < 50.0
    pass_arb = not butterfly_violation
    pass_overall = pass_rmse and pass_arb
    print()
    if pass_overall:
        print(f"  ✅ PASS — {side}s SVI fit is production-quality")
    elif pass_rmse and not pass_arb:
        print(f"  ⚠ MARGINAL — RMSE OK but arbitrage violation present")
    elif not pass_rmse:
        print(f"  ❌ FAIL — RMSE {rmse_bp:.1f}bp exceeds 50bp threshold")

    return pass_overall, {
        'side':              side,
        'rmse_bp':           rmse_bp,
        'mean_abs_bp':       float(abs_residuals_bp.mean()),
        'p90_bp':            float(np.percentile(abs_residuals_bp, 90)),
        'max_bp':            float(abs_residuals_bp.max()),
        'butterfly_arb':     butterfly_violation,
        'n_strikes':         len(k),
        't_calib_ms':        t_calib * 1000,
        'params':            (params.a, params.b, params.rho, params.m, params.sigma),
    }


def main():
    print('\n📐 Phase 20A Smoke Test — Nguyen (2025) SVI on LIVE QQQ chain')

    # Probe available expirations
    print("  Listing available QQQ expirations...")
    exps = list_expirations('QQQ')
    print(f"  Found {len(exps)} expirations: {[e[0] for e in exps[:6]]}...")

    # Pick representative DTEs: 0 (today), ~3 (this week), ~14 (mid-month), ~21 (longer)
    target_dtes = [0, 3, 14, 21]
    cases = []
    for target in target_dtes:
        # Find closest available DTE
        best = min(exps, key=lambda x: abs(x[1] - target))
        cases.append((best[0], best[1], 'call'))
        cases.append((best[0], best[1], 'put'))

    results = []
    for exp_date, dte, side in cases:
        try:
            ok, summary = smoke_test(side, exp_date=exp_date, expected_dte=dte)
            results.append((dte, side, ok, summary))
        except Exception as e:
            import traceback
            print(f"  ❌ EXCEPTION on {dte}DTE {side}s: {e}")
            traceback.print_exc()
            results.append((dte, side, False, None))

    # Final verdict
    print()
    print("=" * 78)
    print(" FINAL VERDICT")
    print("=" * 78)
    for dte, side, ok, summary in results:
        label = f"{dte}DTE {side}"
        if summary:
            print(f"  {'✅' if ok else '❌'} {label:<11s}  "
                  f"RMSE={summary['rmse_bp']:>6.1f}bp  "
                  f"P90={summary['p90_bp']:>6.1f}bp  "
                  f"max={summary['max_bp']:>6.1f}bp  "
                  f"calib={summary['t_calib_ms']:>4.0f}ms  "
                  f"arb={'❌YES' if summary['butterfly_arb'] else '✅NO'}")
        else:
            print(f"  ❌ {label:<11s}  FAILED")

    all_pass = all(ok for _, _, ok, _ in results)
    print()
    if all_pass:
        print("  ✅✅ PHASE 20A IS GO — SVI calibration runs cleanly on live Schwab data")
        return 0
    else:
        print("  ⚠ One or more sides failed — review residuals before lift")
        return 1


if __name__ == '__main__':
    sys.exit(main())
