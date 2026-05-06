"""
Black-Scholes-Merton Greeks Solver

Pure-Python implementation (no scipy dependency). Computes:
  - bsm_price(S, K, T, r, q, sigma, opt_type)  → option price
  - bsm_greeks(...)                             → {delta, gamma, theta, vega, rho}
  - solve_iv(...)                               → implied volatility via Newton-Raphson

Conventions:
  S         = underlying spot price
  K         = strike price
  T         = time to expiration in YEARS (e.g., 0.5 = 6 months)
  r         = risk-free rate (decimal: 0.04 = 4%)
  q         = continuous dividend yield (decimal)
  sigma     = volatility (decimal: 0.20 = 20%)
  opt_type  = 'C' or 'P'

Vega and rho are reported PER 1.0 UNIT of σ/r (so vega = 100x what most retail platforms show).
Theta is per CALENDAR DAY (so theta = annual_theta / 365).

Designed for sub-millisecond compute per contract, suitable for inline use in
per-print FLOW accumulator without network/IO.
"""
import math
from typing import Optional


# ── Standard Normal CDF + PDF ─────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── BSM price ─────────────────────────────────────────────────────────────
def bsm_price(S: float, K: float, T: float, r: float, q: float,
              sigma: float, opt_type: str) -> float:
    """Black-Scholes-Merton option price for European-style options.

    For very small T or very small sigma, returns intrinsic value to avoid
    division-by-zero. American-exercise premium not included (small for QQQ/SPY).
    """
    # Edge cases — return intrinsic value
    if T <= 0 or sigma <= 0:
        if opt_type.upper().startswith('C'):
            return max(S - K, 0.0)
        else:
            return max(K - S, 0.0)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    discount_S = S * math.exp(-q * T)
    discount_K = K * math.exp(-r * T)

    if opt_type.upper().startswith('C'):
        return discount_S * _norm_cdf(d1) - discount_K * _norm_cdf(d2)
    else:
        return discount_K * _norm_cdf(-d2) - discount_S * _norm_cdf(-d1)


# ── BSM Greeks ────────────────────────────────────────────────────────────
def bsm_greeks(S: float, K: float, T: float, r: float, q: float,
               sigma: float, opt_type: str) -> dict:
    """Return all Greeks for a BSM-priced option.

    Returns:
        {
            'delta': dV/dS,
            'gamma': d²V/dS²,
            'vega':  dV/dσ        (per 1.0 unit σ; divide by 100 for "per 1%")
            'theta': dV/dt        (per CALENDAR day; per-year ÷ 365)
            'rho':   dV/dr        (per 1.0 unit r; divide by 100 for "per 1%")
        }

    Edge cases (T → 0 or σ → 0): returns degenerate Greeks (mostly zero except Δ).
    """
    is_call = opt_type.upper().startswith('C')

    if T <= 0 or sigma <= 0:
        # Degenerate — Δ is binary (0 or ±1), other Greeks are zero
        if is_call:
            delta = 1.0 if S > K else (0.5 if S == K else 0.0)
        else:
            delta = -1.0 if S < K else (-0.5 if S == K else 0.0)
        return {'delta': delta, 'gamma': 0.0, 'vega': 0.0, 'theta': 0.0, 'rho': 0.0}

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    pdf_d1 = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)
    cdf_neg_d1 = _norm_cdf(-d1)
    cdf_neg_d2 = _norm_cdf(-d2)

    discount_S = math.exp(-q * T)
    discount_K = math.exp(-r * T)

    # Δ (delta)
    if is_call:
        delta = discount_S * cdf_d1
    else:
        delta = -discount_S * cdf_neg_d1   # equivalent to discount_S * (cdf_d1 - 1)

    # Γ (gamma) — same for call and put
    gamma = discount_S * pdf_d1 / (S * sigma * sqrt_T)

    # ν (vega) — same for call and put, per 1.0 unit σ
    vega = S * discount_S * pdf_d1 * sqrt_T

    # Θ (theta) — per year, then divide by 365 for per-day
    if is_call:
        theta_annual = (-S * discount_S * pdf_d1 * sigma / (2.0 * sqrt_T)
                        - r * K * discount_K * cdf_d2
                        + q * S * discount_S * cdf_d1)
    else:
        theta_annual = (-S * discount_S * pdf_d1 * sigma / (2.0 * sqrt_T)
                        + r * K * discount_K * cdf_neg_d2
                        - q * S * discount_S * cdf_neg_d1)
    theta = theta_annual / 365.0

    # ρ (rho) — per 1.0 unit r
    if is_call:
        rho = K * T * discount_K * cdf_d2
    else:
        rho = -K * T * discount_K * cdf_neg_d2

    return {
        'delta': delta,
        'gamma': gamma,
        'vega':  vega,
        'theta': theta,
        'rho':   rho,
    }


# ── Implied Volatility solver (Newton-Raphson + bisection fallback) ───────
def solve_iv(S: float, K: float, T: float, r: float, q: float,
             market_price: float, opt_type: str,
             tol: float = 1e-4, max_iter: int = 50,
             sigma_init: float = 0.20) -> Optional[float]:
    """Solve for implied volatility given market price.

    Uses Newton-Raphson with vega as the derivative. Falls back to bisection
    if vega becomes too small (deep ITM/OTM where Newton-Raphson is unstable).

    Returns None if solver fails to converge or inputs are invalid.

    For typical options, converges in 5-10 iterations to <0.0001 tolerance.
    Total cost: ~50 floating-point ops + ~10 transcendental calls = <10 microsec.
    """
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return None

    # Sanity check — market price must be ≥ intrinsic value
    if opt_type.upper().startswith('C'):
        intrinsic = max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    else:
        intrinsic = max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0)
    if market_price < intrinsic - 1e-6:
        return None  # arb opportunity or bad data

    sigma = sigma_init

    # Newton-Raphson
    for _ in range(max_iter):
        try:
            price = bsm_price(S, K, T, r, q, sigma, opt_type)
            sqrt_T = math.sqrt(T)
            d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
            vega = S * math.exp(-q * T) * _norm_pdf(d1) * sqrt_T
        except (ValueError, ZeroDivisionError, OverflowError):
            return None

        diff = price - market_price
        if abs(diff) < tol:
            return sigma

        if vega < 1e-8:
            # Vega too small — switch to bisection fallback
            return _bisection_iv(S, K, T, r, q, market_price, opt_type)

        sigma = sigma - diff / vega

        # Constrain to sane range — IVs above 500% are nonsense
        if sigma <= 0.0001:
            sigma = 0.0001
        elif sigma > 5.0:
            sigma = 5.0

    # Newton-Raphson didn't converge — try bisection
    return _bisection_iv(S, K, T, r, q, market_price, opt_type)


def _bisection_iv(S: float, K: float, T: float, r: float, q: float,
                  market_price: float, opt_type: str) -> Optional[float]:
    """Bisection IV solver — slower but always converges if a solution exists.

    Robust fallback for deep ITM/OTM where Newton-Raphson struggles with
    near-zero vega.
    """
    lo, hi = 0.0001, 5.0
    for _ in range(100):
        mid = (lo + hi) / 2
        try:
            price = bsm_price(S, K, T, r, q, mid, opt_type)
        except (ValueError, ZeroDivisionError):
            return None
        if abs(price - market_price) < 1e-4:
            return mid
        if price < market_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            return mid
    return None


# ── Convenience: full Greek pack including IV from market price ──────────
def compute_greeks_from_market(S: float, K: float, T: float, r: float, q: float,
                                market_price: float, opt_type: str,
                                extrinsic_tol: float = 0.02) -> dict:
    """Self-contained: solve IV from market price, then return all Greeks.

    Handles boundary cases robustly:
      - If extrinsic value ≤ extrinsic_tol (price at/below intrinsic):
        Returns degenerate Greeks with Δ at the boundary (±1 deep ITM, 0 deep OTM).
        This handles 0DTE deep-ITM where IV is undefined but Δ converges to ±1.

    Returns:
        {iv, delta, gamma, vega, theta, rho, source}
        where source ∈ {'bsm', 'boundary_itm', 'boundary_otm'}
        or None if inputs invalid.
    """
    if T <= 0 or S <= 0 or K <= 0 or market_price < 0:
        return None

    is_call = opt_type.upper().startswith('C')

    # Compute intrinsic value (with discount factors)
    if is_call:
        intrinsic = max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    else:
        intrinsic = max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0)
    extrinsic = market_price - intrinsic

    # ── Boundary handler: price at/below intrinsic ──────────────────────
    # This typically happens for deep-ITM 0DTE contracts where:
    #   - Time premium has decayed to ~zero
    #   - Δ converges to ±1 (call ITM) or 0 (call OTM); ±1 (put ITM) or 0 (put OTM)
    #   - IV is undefined (no σ produces sub-intrinsic price)
    # Return the limiting Greeks at exercise boundary.
    if extrinsic <= extrinsic_tol:
        if is_call:
            if S > K:
                delta, source = 1.0, 'boundary_itm'
            elif S < K:
                delta, source = 0.0, 'boundary_otm'
            else:
                delta, source = 0.5, 'boundary_atm'
        else:  # put
            if S < K:
                delta, source = -1.0, 'boundary_itm'
            elif S > K:
                delta, source = 0.0, 'boundary_otm'
            else:
                delta, source = -0.5, 'boundary_atm'
        return {
            'iv':    0.0,        # undefined for boundary cases (flag with 0)
            'delta': delta,
            'gamma': 0.0,
            'vega':  0.0,
            'theta': 0.0,
            'rho':   0.0,
            'source': source,
        }

    # ── Normal case: solve IV, compute full Greeks ──────────────────────
    iv = solve_iv(S, K, T, r, q, market_price, opt_type)
    if iv is None:
        return None
    greeks = bsm_greeks(S, K, T, r, q, iv, opt_type)
    return {'iv': iv, **greeks, 'source': 'bsm'}


def delta_from_market(S: float, K: float, T: float, r: float, q: float,
                      market_price: float, opt_type: str) -> Optional[float]:
    """Convenience: just return Δ from market price. Handles boundary cases.

    Designed for inline use in FlowAccumulator: takes a Tradier print's
    market_price and returns Δ (or None if undeterminable).
    """
    result = compute_greeks_from_market(S, K, T, r, q, market_price, opt_type)
    return result['delta'] if result else None
