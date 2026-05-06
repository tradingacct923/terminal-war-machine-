#!/usr/bin/env python3
"""
Audit fix #7 — Kalman MLE re-fit Q/R from historical k_v proxy data.

Kalman process noise Q and observation noise R were initially CONFIGURED:
   KALMAN_Q_INIT = 4e-4
   KALMAN_R_INIT = 0.25
   KALMAN_P0     = 0.5

Pull QQQ + VIX daily history from yfinance, build (ΔS%, ΔIV_pp) pairs
that approximate the k_v observation model:

    Δ(VIX)_pp = -k_v · Δ(QQQ)% + noise
                ↓
    x_t = -ΔQQQ%   (regressor)
    y_t = ΔVIX_pp  (observation)

VIX is the QQQ IV proxy here. Not perfect (VIX is SPX, not QQQ) but the
QQQ-vs-VIX correlation is ~0.95 historically, so the magnitudes track.

Run scipy MLE over a (Q, R) grid to maximize innovation log-likelihood.
Output the empirically optimal Q, R for use as new defaults.

Run: source venv/bin/activate && python scripts/kalman_mle_refit.py
"""
import os
import sys
import json
import warnings
warnings.filterwarnings('ignore')

import numpy as np
try:
    import yfinance as yf
except ImportError:
    print("Need yfinance"); sys.exit(1)
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from connectors.kalman_filter import (
    KalmanHedgeFilter,
    estimate_noise_params,
    kalman_batch,
    rolling_ols_beta,
)


def fetch_pairs(start='2018-01-01', end=None):
    """Pull QQQ + VIX daily, return (ds_pct_arr, div_pp_arr)."""
    if end is None:
        from datetime import date
        end = date.today().isoformat()
    print(f"  Fetching QQQ + VIX daily {start} → {end}...")
    qqq = yf.download('QQQ',  start=start, end=end, auto_adjust=True, progress=False)['Close']
    vix = yf.download('^VIX', start=start, end=end, auto_adjust=True, progress=False)['Close']
    qqq_s = pd.Series(qqq.values.flatten(), index=qqq.index, name='QQQ')
    vix_s = pd.Series(vix.values.flatten(), index=vix.index, name='VIX')
    df = pd.concat([qqq_s, vix_s], axis=1, join='inner').dropna()
    print(f"  Loaded {len(df)} aligned trading days")
    # Build daily diffs
    df['ds_pct'] = 100.0 * df['QQQ'].pct_change()      # QQQ daily return %
    df['div_pp'] = df['VIX'].diff()                     # VIX daily change (pp)
    df = df.dropna()
    return df


def main():
    print("=" * 78)
    print(" Audit fix #7 — Kalman MLE re-fit on historical (QQQ, VIX) data")
    print("=" * 78)

    df = fetch_pairs(start='2018-01-01')
    if len(df) < 200:
        print("Not enough data; aborting"); return 1

    # Filter same as kv_estimator: discard tiny moves (noise) and giant moves (events)
    KV_MIN_DS_PCT = 0.05
    KV_LARGE_MOVE_PCT = 5.0
    pre = len(df)
    df = df[(df['ds_pct'].abs() >= KV_MIN_DS_PCT) & (df['ds_pct'].abs() <= KV_LARGE_MOVE_PCT)]
    print(f"  After noise/event filter: {len(df)}/{pre} samples")

    # Build observation arrays
    # Model: y_t = x_t · k_v + noise   where x_t = -ds_pct, y_t = div_pp
    x = -df['ds_pct'].values
    y = df['div_pp'].values
    print()
    print(f"  Observation stats:")
    print(f"    x (=-ΔQQQ%) mean={x.mean():+.4f} std={x.std():.4f}")
    print(f"    y (ΔVIX_pp) mean={y.mean():+.4f} std={y.std():.4f}")

    # Quick rolling OLS sanity check — what's the empirical k_v?
    pd_x = pd.Series(x, index=df.index)
    pd_y = pd.Series(y, index=df.index)
    cov = pd_x.rolling(60).cov(pd_y)
    var = pd_x.rolling(60).var()
    rolling_kv = cov / var.replace(0, np.nan)
    valid = rolling_kv.dropna()
    print(f"  Rolling-OLS k_v (60d window):")
    print(f"    p10={valid.quantile(0.10):.3f}  p50={valid.quantile(0.50):.3f}  p90={valid.quantile(0.90):.3f}")
    print(f"    overall mean={valid.mean():.3f}  std={valid.std():.3f}")

    # ── MLE Q/R re-fit ──
    print()
    print("  Running MLE Q/R re-fit (this takes ~30s)...")
    Q_opt, R_opt = estimate_noise_params(x, y, beta_0=0.7, P_0=0.5)
    print(f"  Empirical optimal: Q={Q_opt:.6e}  R={R_opt:.4f}")
    print()

    # ── Compare against current defaults ──
    KALMAN_Q_INIT = 4e-4
    KALMAN_R_INIT = 0.25
    print(f"  Current defaults: Q={KALMAN_Q_INIT:.6e}  R={KALMAN_R_INIT:.4f}")
    print()

    # ── Run Kalman with both old and new params, compare RMSE vs rolling OLS ──
    beta_old, _ = kalman_batch(x, y, Q=KALMAN_Q_INIT, R=KALMAN_R_INIT, beta_0=0.7, P_0=0.5)
    beta_new, _ = kalman_batch(x, y, Q=Q_opt, R=R_opt, beta_0=0.7, P_0=0.5)

    rolling_kv_arr = rolling_kv.fillna(method='ffill').fillna(0.7).values
    # Compare last 200 days
    n_test = min(200, len(beta_old) - 100)
    test_slice = slice(-n_test, None)

    rmse_old = float(np.sqrt(np.mean((beta_old[test_slice] - rolling_kv_arr[test_slice])**2)))
    rmse_new = float(np.sqrt(np.mean((beta_new[test_slice] - rolling_kv_arr[test_slice])**2)))
    print(f"  Tracking RMSE vs rolling-OLS reference (last {n_test}d):")
    print(f"    OLD Q/R: {rmse_old:.4f}")
    print(f"    NEW Q/R: {rmse_new:.4f}  (improvement: {100*(rmse_old-rmse_new)/rmse_old:+.1f}%)")

    # ── Generate code suggestion ──
    print()
    print("=" * 78)
    print(" GENERATED CODE — apply to connectors/kv_estimator.py")
    print("=" * 78)
    print()
    if rmse_new < rmse_old:
        print(f"# Recompute (2026-05-01) — MEASURED via MLE on n={len(x)} historical")
        print(f"# (QQQ, VIX) daily pairs (yfinance 2018-2024). Improves Kalman tracking")
        print(f"# RMSE by {100*(rmse_old-rmse_new)/rmse_old:.1f}% vs initial CONFIGURED defaults.")
        print(f"KALMAN_Q_INIT      = {Q_opt:.4e}     # MEASURED — MLE-fit on historical k_v proxy")
        print(f"KALMAN_R_INIT      = {R_opt:.4f}     # MEASURED — MLE-fit observation noise")
        print(f"KALMAN_P0          = 0.5            # CONFIGURED — large initial uncertainty for fast convergence")
    else:
        print(f"# MLE didn't beat current defaults on this dataset.")
        print(f"# Keeping initial CONFIGURED Q={KALMAN_Q_INIT}, R={KALMAN_R_INIT}.")
        print(f"# Empirical Q={Q_opt:.6e} R={R_opt:.4f} performed slightly worse")
        print(f"# (RMSE_new={rmse_new:.4f} vs RMSE_old={rmse_old:.4f}).")

    # Save full diagnostic
    out = {
        'n_samples':           int(len(x)),
        'date_range':          [str(df.index.min().date()), str(df.index.max().date())],
        'old_Q':               KALMAN_Q_INIT,
        'old_R':               KALMAN_R_INIT,
        'mle_Q':               float(Q_opt),
        'mle_R':               float(R_opt),
        'rmse_old':            rmse_old,
        'rmse_new':            rmse_new,
        'improvement_pct':     100*(rmse_old-rmse_new)/rmse_old if rmse_old > 0 else 0,
        'rolling_ols_kv_p50':  float(valid.quantile(0.50)),
        'rolling_ols_kv_p10':  float(valid.quantile(0.10)),
        'rolling_ols_kv_p90':  float(valid.quantile(0.90)),
    }
    out_path = os.path.join(ROOT, 'logs', 'kalman_mle_refit.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)
    print()
    print(f"  Diagnostic written to {out_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
