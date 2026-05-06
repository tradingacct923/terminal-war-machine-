"""
SVI Volatility Surface Engine — Phase 20A (2026-05-01)

Stochastic Volatility Inspired (SVI) parametric surface calibration with
arbitrage-free constraints, adapted from Nguyen (2025) "Regime-Adaptive
Volatility Surface Arbitrage" (UC Berkeley working paper).

═══════════════════════════════════════════════════════════════════════════
 BACKGROUND
═══════════════════════════════════════════════════════════════════════════

The SVI parametrization (Gatheral 2004) expresses total implied variance
w(k) = σ²(k) · T as a function of log-moneyness k = log(K/F):

    w(k; a, b, ρ, m, σ) = a + b · [ρ·(k-m) + sqrt((k-m)² + σ²)]

5 parameters with clean geometric interpretations:
    a   : level (vertical shift)
    b   : wing slope (≥ 0)
    ρ   : skew (-1, 1)
    m   : ATM offset
    σ   : curvature (> 0)

Arbitrage-free conditions (Gatheral & Jacquier 2014):
    - Butterfly: g(k) ≥ 0 where g is the Gatheral-Jacquier density proxy
    - Calendar:  w(k; T1) ≤ w(k; T2) for T1 < T2

The "surface residual" r(k) = observed_iv(k) - svi_implied_vol(k) is the
trading signal: positive r ⟹ market is rich vs model; negative ⟹ cheap.
Per Nguyen §3.4, the vega-weighted z-score of these residuals mean-reverts
with a half-life of ~4 days (daily SPX 0DTE data).

═══════════════════════════════════════════════════════════════════════════
 DEVIATION FROM NGUYEN (2025)
═══════════════════════════════════════════════════════════════════════════

Nguyen's reference implementation (github.com/JamesNguyen915/vol-surface-
arbitrage) calibrates by minimising the IV-space residual:

    obj(p) = mean( (sqrt(max(svi_total_variance(k, p), 1e-10)/T) - market_iv)^2 )

This DEGENERATES at small T (0DTE: T ≈ 7e-4 yr) because the optimizer can
drive total variance very negative — which clips to 1e-10 — producing a
trivial fit (model_iv ≈ 0.04% across all strikes, RMSE ~2000bp).

We replace it with a total-variance-space objective:

    target_w = market_iv² · T
    obj(p)   = sum( (svi_total_variance(k, p) - target_w)² ) + λ·neg_w_penalty

This is well-conditioned at any T. Validated on live Schwab QQQ chains:
RMSE 18-41bp across DTEs [0, 3, 14, 21]. See scripts/svi_live_smoke.py.

═══════════════════════════════════════════════════════════════════════════
 USAGE
═══════════════════════════════════════════════════════════════════════════

    from connectors.svi_surface import compute_svi_state

    # Called from REST endpoint with raw chain data already fetched:
    state = compute_svi_state(
        ticker='QQQ',
        exp_date='2026-05-08',
        spot=667.74,
        chain=raw_chain,    # list of contract dicts (strike, type, iv, oi, volume, delta)
    )
    # state = {
    #   'params': {'a': ..., 'b': ..., 'rho': ..., 'm': ..., 'sigma': ...},
    #   'rmse_bp': 22.5,
    #   'strikes': [
    #       {'K': 660, 'k': -0.012, 'side': 'call', 'iv_obs': 0.221,
    #        'iv_fit': 0.214, 'residual_bp': 70, 'vega_weight': 0.83}, ...
    #   ],
    #   'aggregate_z': -1.32,        # vega-weighted z-score (rolling 20d)
    #   'butterfly_arb': False,
    #   'data_ts': 1730510400.0,
    # }
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from scipy.optimize import Bounds, minimize

log = logging.getLogger(__name__)


# ── CONFIGURED CONSTANTS ───────────────────────────────────────────────────
# Pass-band for residual fit quality (used by alerts, not calibration).
SVI_RMSE_PASS_BP = 50.0       # MEASURED — paper §3.4 cites <50bp typical fit; our smoke test confirms
# Z-score window (paper §3.4 standardises over 20-day rolling window).
SVI_ZSCORE_WINDOW_DAYS = 20   # CONFIGURED — Nguyen (2025) §3.4
# Vega weight clip (avoid ATM dominance under tiny-T at 0DTE).
SVI_VEGA_FLOOR = 0.1          # CONFIGURED — keeps wings contributing to z-score
# Butterfly density tolerance (small negative tolerance for fp noise).
BUTTERFLY_TOL = -1e-4         # CONFIGURED — Nguyen src/svi.py default
# Plausibility filter for chain rows.
MIN_DELTA_FILTER = 0.05       # CONFIGURED — paper §3 standard skew filter (drop deep ITM/OTM)
MAX_DELTA_FILTER = 0.95
MIN_IV_PCT = 0.5
MAX_IV_PCT = 500.0


# ─── DATA CLASSES ─────────────────────────────────────────────────────────
@dataclass
class SVIParams:
    """5-parameter SVI slice container."""
    a: float
    b: float
    rho: float
    m: float
    sigma: float

    def to_array(self) -> np.ndarray:
        return np.array([self.a, self.b, self.rho, self.m, self.sigma])

    @classmethod
    def from_array(cls, arr: np.ndarray) -> "SVIParams":
        return cls(a=float(arr[0]), b=float(arr[1]), rho=float(arr[2]),
                   m=float(arr[3]), sigma=float(arr[4]))

    def is_valid(self) -> bool:
        return (self.b >= 0 and -1.0 < self.rho < 1.0 and self.sigma > 0)

    def to_dict(self) -> dict:
        return {'a': self.a, 'b': self.b, 'rho': self.rho,
                'm': self.m, 'sigma': self.sigma}


# ─── CORE SVI MODEL (Gatheral 2004) ───────────────────────────────────────
def svi_total_variance(k: np.ndarray, params: SVIParams) -> np.ndarray:
    """w(k) = a + b · [ρ·(k-m) + sqrt((k-m)² + σ²)]"""
    xi = k - params.m
    return params.a + params.b * (params.rho * xi + np.sqrt(xi**2 + params.sigma**2))


def svi_implied_vol(k: np.ndarray, T: float, params: SVIParams) -> np.ndarray:
    """Convert total variance to IV: σ_iv = sqrt(w/T)."""
    if T <= 0:
        raise ValueError(f"T must be positive. Got T={T}")
    w = svi_total_variance(k, params)
    w = np.maximum(w, 1e-10)   # numerical safety
    return np.sqrt(w / T)


# ─── ARBITRAGE-FREE CHECKS (Gatheral & Jacquier 2014) ─────────────────────
def svi_butterfly_density(k: np.ndarray, params: SVIParams) -> np.ndarray:
    """g(k) = (1 - kw'/(2w))² - (w'/2)² · (1/w + 1/4) + w''/2

    Must be ≥ 0 everywhere for no butterfly arbitrage.
    """
    xi = k - params.m
    sqrt_term = np.sqrt(xi**2 + params.sigma**2)
    w = params.a + params.b * (params.rho * xi + sqrt_term)
    w_prime = params.b * (params.rho + xi / sqrt_term)
    w_double_prime = params.b * params.sigma**2 / (sqrt_term**3)
    w = np.maximum(w, 1e-10)
    return ((1.0 - 0.5 * k * w_prime / w) ** 2
            - 0.25 * w_prime**2 * (1.0 / w + 0.25)
            + 0.5 * w_double_prime)


def has_butterfly_arbitrage(k: np.ndarray, params: SVIParams,
                             tol: float = BUTTERFLY_TOL) -> bool:
    g = svi_butterfly_density(k, params)
    return bool(np.any(g < tol))


# ─── CALIBRATION (TOTAL-VARIANCE OBJECTIVE — OUR FIX) ─────────────────────
def calibrate_svi(k: np.ndarray, market_iv: np.ndarray, T: float,
                   n_restarts: int = 8) -> Tuple[SVIParams, float]:
    """Fit SVI parameters to (k, iv) data in TOTAL-VARIANCE space.

    Uses sum-of-squared-error in total variance rather than IV space, which
    is well-conditioned at any T (avoids the small-T degeneracy in Nguyen's
    reference implementation).

    Args:
        k:          log-moneyness array, shape (N,) — N ≥ 5
        market_iv:  observed implied volatility (decimal, e.g. 0.20 for 20%)
        T:          time to expiry in years (must be > 0)
        n_restarts: number of random restarts for global search

    Returns:
        (params, rmse_decimal) — params are the best-fit SVIParams,
        rmse_decimal is RMSE in IV space (decimal units).
    """
    if len(k) < 5:
        raise ValueError(f"Need ≥5 strikes for SVI fit. Got {len(k)}.")
    if T <= 0:
        raise ValueError(f"T must be positive. Got T={T}")

    target_w = market_iv ** 2 * T

    bounds = Bounds(
        lb=[-np.inf, 0.0,    -0.999, -np.inf, 1e-6],
        ub=[ np.inf, np.inf,  0.999,  np.inf, np.inf],
    )

    # Data-driven initial candidates
    atm_iv = float(np.interp(0.0, k, market_iv))
    atm_var = atm_iv ** 2 * T
    candidates = [
        np.array([atm_var * 0.5, atm_var * 5.0,  -0.30, 0.0, 0.05]),
        np.array([atm_var * 0.3, atm_var * 10.0, -0.30, 0.0, 0.10]),
        np.array([atm_var * 0.5, atm_var * 5.0,  -0.10, 0.0, 0.05]),
    ]
    rng = np.random.default_rng(seed=42)
    for _ in range(n_restarts):
        candidates.append(np.array([
            rng.uniform(0.0, atm_var * 1.5),
            rng.uniform(0.0, atm_var * 50.0),
            rng.uniform(-0.9, 0.0),
            rng.uniform(-0.05, 0.05),
            rng.uniform(0.01, 0.30),
        ]))

    def objective(p):
        try:
            params = SVIParams.from_array(p)
            w = svi_total_variance(k, params)
            neg_pen = float(np.sum(np.maximum(-w, 0) ** 2)) * 1e6
            return float(np.sum((w - target_w) ** 2)) + neg_pen
        except Exception:
            return 1e20

    best_cost = np.inf
    best_x = None
    for x0 in candidates:
        try:
            r = minimize(objective, x0, method="L-BFGS-B", bounds=bounds,
                         options={"maxiter": 5000, "ftol": 1e-14, "gtol": 1e-10})
            if r.fun < best_cost:
                best_cost = r.fun
                best_x = r.x
        except Exception:
            continue

    if best_x is None:
        raise RuntimeError("SVI calibration failed for all starting points.")

    params = SVIParams.from_array(best_x)
    iv_model = svi_implied_vol(k, T, params)
    rmse = float(np.sqrt(np.mean((iv_model - market_iv) ** 2)))
    return params, rmse


# ─── BSM VEGA (per-strike weight) ─────────────────────────────────────────
def _bsm_vega(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """BS vega per 1.0-σ-decimal move (paper convention).

    vega = S · φ(d1) · sqrt(T)
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return S * phi * math.sqrt(T)


# ─── ROLLING Z-SCORE STATE (per-ticker, per-expiry) ───────────────────────
@dataclass
class _ResidualHistory:
    """Rolling history of (vega-weighted aggregate residual) values for z-scoring."""
    values: deque = field(default_factory=lambda: deque(maxlen=SVI_ZSCORE_WINDOW_DAYS))

    def add(self, value: float) -> None:
        self.values.append(value)

    def zscore(self, current: float) -> Optional[float]:
        n = len(self.values)
        if n < 5:
            return None
        arr = np.array(self.values)
        mu = float(arr.mean())
        sigma = float(arr.std(ddof=1))
        if sigma < 1e-10:
            return 0.0
        return (current - mu) / sigma


_residual_histories: dict = {}    # key: f"{ticker}:{exp_date}" → _ResidualHistory
_state_lock = threading.RLock()


# ─── PUBLIC ENTRY: COMPUTE FULL STATE FROM RAW CHAIN ──────────────────────
def compute_svi_state(ticker: str, exp_date: str, spot: float,
                       chain: list, dte: int = 0,
                       n_restarts: int = 8) -> dict:
    """Calibrate SVI to a chain and return full state dict.

    Args:
        ticker:    'QQQ', 'SPX', etc.
        exp_date:  expiration in 'YYYY-MM-DD'
        spot:      underlying spot price
        chain:     list of dicts as returned by /api/chain — needs at least
                   {'strike', 'type', 'iv', 'oi', 'volume', 'delta'}
        dte:       days to expiry (passed in to compute T; if 0, treated as 0DTE)
        n_restarts: SVI optimizer restarts

    Returns:
        dict with keys: 'ticker', 'exp_date', 'spot', 'T_years', 'dte',
                        'params' (SVIParams.to_dict()), 'rmse_bp',
                        'strikes' (list of per-strike dicts),
                        'aggregate_residual', 'aggregate_z', 'aggregate_z_window',
                        'butterfly_arb', 'data_ts', 'samples_used'

        On insufficient data, returns dict with 'error' key.
    """
    data_ts = time.time()

    # Compute T
    if dte == 0:
        days_left = 0.25   # ~6 hours nominal for 0DTE post-fetch
    else:
        days_left = float(dte) + 0.5
    T = days_left / 365.0
    F = float(spot)

    # Build calls + puts arrays separately (we fit them together — same
    # underlying, same expiry, IVs from the same surface)
    rows = []
    for opt in chain:
        try:
            iv_raw = opt.get('iv')
            if iv_raw is None or iv_raw == '' or iv_raw == 'N/A':
                continue
            iv_pct = float(iv_raw)
            if iv_pct < MIN_IV_PCT or iv_pct > MAX_IV_PCT:
                continue
            oi = int(opt.get('oi', 0) or 0)
            vol = int(opt.get('volume', 0) or 0)
            if oi == 0 and vol == 0:
                continue
            delta = abs(float(opt.get('delta', 0) or 0))
            if delta < MIN_DELTA_FILTER or delta > MAX_DELTA_FILTER:
                continue
            strike = float(opt['strike'])
            if strike <= 0:
                continue
            side = str(opt.get('type', '')).lower()
            if side not in ('call', 'put'):
                continue
        except (ValueError, TypeError, KeyError):
            continue

        k = math.log(strike / F)
        iv = iv_pct / 100.0
        rows.append({'K': strike, 'k': k, 'iv': iv, 'side': side,
                     'oi': oi, 'volume': vol, 'delta': delta})

    if len(rows) < 5:
        return {
            'ticker':    ticker,
            'exp_date':  exp_date,
            'error':     f'Insufficient data: {len(rows)} valid contracts (need ≥5)',
            'data_ts':   data_ts,
            'samples_used': len(rows),
        }

    # Fit SVI to combined surface
    k_arr = np.array([r['k'] for r in rows])
    iv_arr = np.array([r['iv'] for r in rows])

    try:
        params, rmse_decimal = calibrate_svi(k_arr, iv_arr, T, n_restarts=n_restarts)
    except Exception as e:
        return {
            'ticker':    ticker,
            'exp_date':  exp_date,
            'error':     f'Calibration failed: {e}',
            'data_ts':   data_ts,
            'samples_used': len(rows),
        }

    # Compute per-strike residuals + vega weights
    iv_fit = svi_implied_vol(k_arr, T, params)
    residuals = (iv_arr - iv_fit)        # decimal IV
    residuals_bp = residuals * 10000.0   # bp

    # Compute vega for each strike (used as residual weight)
    vegas = np.array([_bsm_vega(F, r['K'], T, r['iv']) for r in rows])
    if vegas.max() > 0:
        vega_weights = np.maximum(vegas / vegas.max(), SVI_VEGA_FLOOR)
    else:
        vega_weights = np.ones_like(vegas)

    # Vega-weighted aggregate residual (the "signal" per Nguyen §3.4)
    aggregate_residual_bp = float(np.sum(residuals_bp * vega_weights) /
                                    np.sum(vega_weights))

    # Update rolling z-score history
    key = f"{ticker}:{exp_date}"
    with _state_lock:
        hist = _residual_histories.setdefault(key, _ResidualHistory())
        z = hist.zscore(aggregate_residual_bp)
        hist.add(aggregate_residual_bp)
        z_window = len(hist.values)

    # Arbitrage check on extended grid
    k_grid = np.linspace(k_arr.min() - 0.05, k_arr.max() + 0.05, 200)
    butterfly_arb = bool(has_butterfly_arbitrage(k_grid, params))

    # Build per-strike output
    strikes_out = []
    for i, r in enumerate(rows):
        strikes_out.append({
            'K':           round(r['K'], 2),
            'k':           round(float(k_arr[i]), 5),
            'side':        r['side'],
            'iv_obs':      round(float(iv_arr[i]), 4),
            'iv_fit':      round(float(iv_fit[i]), 4),
            'residual_bp': round(float(residuals_bp[i]), 1),
            'vega':        round(float(vegas[i]), 3),
            'vega_weight': round(float(vega_weights[i]), 3),
            'oi':          r['oi'],
            'volume':      r['volume'],
            'delta':       round(r['delta'], 4),
        })

    # Sort by strike for consistent rendering
    strikes_out.sort(key=lambda s: s['K'])

    return {
        'ticker':              ticker,
        'exp_date':            exp_date,
        'spot':                round(F, 4),
        'forward':             round(F, 4),       # T tiny + r=0 → F ≈ spot
        'T_years':             round(T, 6),
        'dte':                 dte,
        'params':              params.to_dict(),
        'rmse_bp':             round(rmse_decimal * 10000, 2),
        'pass_threshold_bp':   SVI_RMSE_PASS_BP,
        'pass_rmse':           bool(rmse_decimal * 10000 < SVI_RMSE_PASS_BP),
        'butterfly_arb':       butterfly_arb,
        'samples_used':        len(rows),
        'strikes':             strikes_out,
        'aggregate_residual':  round(aggregate_residual_bp, 2),
        'aggregate_z':         (round(z, 3) if z is not None else None),
        'aggregate_z_window':  z_window,
        'aggregate_z_window_max': SVI_ZSCORE_WINDOW_DAYS,
        'data_ts':             round(data_ts, 3),
    }


# ─── OUTCOME LEDGER ───────────────────────────────────────────────────────
def append_outcome_record(record: dict, log_dir: Optional[str] = None) -> None:
    """Append one observation to logs/svi_outcomes_YYYYMMDD.jsonl.

    Records (timestamp, exp_date, aggregate_residual_bp, aggregate_z,
    spot, params) for offline mean-reversion analysis (paper claims 4-day
    half-life on SPX 0DTE — validate on our QQQ data).
    """
    import json
    from datetime import datetime
    if log_dir is None:
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'logs',
        )
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    path = os.path.join(log_dir, f'svi_outcomes_{today}.jsonl')
    try:
        with open(path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        log.warning(f'[SVI] outcome ledger write failed: {e}')


def get_diagnostic_state() -> dict:
    """Return current rolling-history depths per (ticker, exp) for diagnostics."""
    with _state_lock:
        return {
            key: {'samples': len(hist.values),
                  'window_max': SVI_ZSCORE_WINDOW_DAYS}
            for key, hist in _residual_histories.items()
        }
