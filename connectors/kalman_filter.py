"""
kalman_filter.py — Phase 20C (2026-05-01)

Kalman filter for dynamic hedge-ratio estimation. Lifted verbatim from
Nguyen (2025) `external/vol-surface-arbitrage/src/kalman.py` — math is
standard (Kalman 1960 + Welch & Bishop 2006), no modifications needed.
Their 14 unit tests cover it.

═══════════════════════════════════════════════════════════════════════════
 OUR USE CASES (2026-05-01)
═══════════════════════════════════════════════════════════════════════════

1. **k_v drift adaptation** (parallel to Theil-Sen in kv_estimator.py)
   State: k_v_t (volatility-delta coefficient)
   Observation: ΔIV_pp = -ΔS% · k_v + noise
                → x_t = -ds_pct, y_t = div_pp, β_t = k_v
   State equation: k_v_t = k_v_{t-1} + w_t (random walk)

2. (future) SVI parameter drift tracking
3. (future) Per-strike residual mean-reversion β estimation

═══════════════════════════════════════════════════════════════════════════
 ORIGINAL DOCSTRING (Nguyen 2025)
═══════════════════════════════════════════════════════════════════════════

The Problem
-----------
A static rolling-OLS beta estimate has two problems:
    1. It uses a fixed lookback that is too short (noisy) or too long (stale).
    2. It treats all observations in the window equally, ignoring that recent
       observations are more informative about the current state.

The Kalman Filter Solution
--------------------------
We model the hedge ratio as a latent state variable beta_t that evolves
via a random walk (state equation), and the realised P&L as a noisy linear
function of beta_t and the signal (observation equation).

State equation:
    beta_t = beta_{t-1} + w_t,   w_t ~ N(0, Q)

Observation equation:
    y_t = x_t * beta_t + v_t,    v_t ~ N(0, R)

where:
    beta_t : latent hedge ratio at time t
    y_t    : realised delta-hedged P&L at time t
    x_t    : IV surface residual signal (the "regressor")
    Q      : process noise variance (how fast beta_t drifts)
    R      : observation noise variance (noise in the P&L measurement)

The Kalman filter gives the optimal (minimum variance) linear estimate of
beta_t given all observations up to time t.

Reference:
    Kalman, R.E. (1960). A new approach to linear filtering and prediction
    problems. Journal of Basic Engineering, 82(1), 35–45.

    Welch, G. & Bishop, G. (2006). An Introduction to the Kalman Filter.
    University of North Carolina Technical Report TR 95-041.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# State Container
# ---------------------------------------------------------------------------

@dataclass
class KalmanState:
    """
    Kalman filter state at a single time step.

    Attributes
    ----------
    beta_hat : posterior mean estimate of the hedge ratio
    P        : posterior variance of the estimate
    innovation : y_t - x_t * beta_prior (prediction error)
    S          : innovation variance (x_t^2 * P_prior + R)
    K          : Kalman gain
    """
    beta_hat: float
    P: float
    innovation: float = 0.0
    S: float = 1.0
    K: float = 0.0


# ---------------------------------------------------------------------------
# Kalman Filter (Univariate, Scalar Observation)
# ---------------------------------------------------------------------------

class KalmanHedgeFilter:
    """
    Univariate Kalman filter for dynamic hedge ratio estimation.

    This is a scalar (1D state, 1D observation) linear Gaussian filter.
    The simplicity is intentional: a more complex filter would require
    more data to estimate, and the additional parameters would likely
    overfit over the training window.

    Parameters
    ----------
    Q : float
        Process noise variance. Controls how fast the hedge ratio is
        allowed to drift. Higher Q → more responsive but noisier estimates.
        Default 1e-4 is appropriate for daily data where beta drifts slowly.

    R : float
        Observation noise variance. Controls trust in the P&L signal.
        Higher R → smoother beta estimates (less trust in observations).

    beta_0 : float
        Initial hedge ratio estimate (default -1.0, i.e. fully delta-hedged
        short vol position for a sold straddle).

    P_0 : float
        Initial estimation uncertainty. Large value means we are uncertain
        about the initial beta (filter converges quickly from the data).
    """

    def __init__(
        self,
        Q: float = 1e-4,
        R: float = 1e-2,
        beta_0: float = -1.0,
        P_0: float = 1.0,
    ):
        self.Q = Q
        self.R = R
        self.beta_0 = beta_0
        self.P_0 = P_0

        # Current state
        self._beta = beta_0
        self._P = P_0

        # History
        self._history: List[KalmanState] = []

    @property
    def beta(self) -> float:
        """Current hedge ratio estimate."""
        return self._beta

    @property
    def uncertainty(self) -> float:
        """Current posterior variance of the hedge ratio."""
        return self._P

    def update(self, x_t: float, y_t: float) -> KalmanState:
        """
        Perform one Kalman filter prediction + update step.

        Parameters
        ----------
        x_t : IV surface residual signal at time t (the regressor)
        y_t : realised delta-hedged P&L at time t (the observation)

        Returns
        -------
        KalmanState : posterior state after incorporating observation y_t

        Algorithm:
            1. Predict:
                beta_prior = beta_{t-1}   (random walk: no drift)
                P_prior    = P_{t-1} + Q  (variance grows with time)

            2. Innovation:
                innovation = y_t - x_t * beta_prior

            3. Kalman gain:
                S = x_t^2 * P_prior + R
                K = P_prior * x_t / S

            4. Update:
                beta_hat = beta_prior + K * innovation
                P_post   = (1 - K * x_t) * P_prior
        """
        # Step 1: Predict
        beta_prior = self._beta
        P_prior = self._P + self.Q

        # Step 2: Innovation
        innovation = y_t - x_t * beta_prior

        # Step 3: Kalman gain
        S = x_t**2 * P_prior + self.R
        K = (P_prior * x_t) / S if abs(S) > 1e-12 else 0.0

        # Step 4: Update
        beta_post = beta_prior + K * innovation
        P_post = (1.0 - K * x_t) * P_prior

        # Ensure variance stays positive (numerical stability)
        P_post = max(P_post, 1e-12)

        # Store
        self._beta = beta_post
        self._P = P_post

        state = KalmanState(
            beta_hat=beta_post,
            P=P_post,
            innovation=innovation,
            S=S,
            K=K,
        )
        self._history.append(state)
        return state

    def reset(self) -> None:
        """Reset the filter to its initial state."""
        self._beta = self.beta_0
        self._P = self.P_0
        self._history = []

    def get_history(self) -> pd.DataFrame:
        """
        Return the filter's full history as a DataFrame.

        Useful for diagnostics and for comparing against rolling-OLS.
        """
        if not self._history:
            return pd.DataFrame()
        return pd.DataFrame([
            {
                "beta_hat":   s.beta_hat,
                "uncertainty": s.P,
                "innovation": s.innovation,
                "kalman_gain": s.K,
            }
            for s in self._history
        ])


# ---------------------------------------------------------------------------
# Batch Kalman Smoothing (Offline, for Research)
# ---------------------------------------------------------------------------

def kalman_batch(
    signals: np.ndarray,
    pnl: np.ndarray,
    Q: float = 1e-4,
    R: float = 1e-2,
    beta_0: float = -1.0,
    P_0: float = 1.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Run the Kalman filter over an entire time series in one pass.

    This is equivalent to running KalmanHedgeFilter.update() in a loop,
    but faster for backtesting since it uses vectorised operations where
    possible.

    Parameters
    ----------
    signals : array of IV residual signals x_t, shape (T,)
    pnl     : array of delta-hedged P&L observations y_t, shape (T,)
    Q, R    : noise parameters
    beta_0  : initial beta estimate
    P_0     : initial uncertainty

    Returns
    -------
    beta_estimates : array of posterior beta estimates, shape (T,)
    uncertainties  : array of posterior variances, shape (T,)
    """
    T = len(signals)
    beta_estimates = np.zeros(T)
    uncertainties = np.zeros(T)

    beta = beta_0
    P = P_0

    for t in range(T):
        x_t = signals[t]
        y_t = pnl[t]

        # Predict
        P_prior = P + Q

        # Update
        S = x_t**2 * P_prior + R
        K = (P_prior * x_t) / S if abs(S) > 1e-12 else 0.0
        innovation = y_t - x_t * beta
        beta = beta + K * innovation
        P = max((1.0 - K * x_t) * P_prior, 1e-12)

        beta_estimates[t] = beta
        uncertainties[t] = P

    return beta_estimates, uncertainties


# ---------------------------------------------------------------------------
# Parameter Estimation via Maximum Likelihood
# ---------------------------------------------------------------------------

def estimate_noise_params(
    signals: np.ndarray,
    pnl: np.ndarray,
    beta_0: float = -1.0,
    P_0: float = 1.0,
) -> Tuple[float, float]:
    """
    Estimate Q and R via maximum likelihood (innovation covariance method).

    We run the Kalman filter for a grid of (Q, R) values and pick the pair
    that maximises the log-likelihood of the innovations.

    The log-likelihood of the Gaussian innovations is:
        LL = -0.5 * sum( log(2*pi*S_t) + innovation_t^2 / S_t )

    Parameters
    ----------
    signals, pnl : training data
    beta_0, P_0  : initial state values

    Returns
    -------
    (Q_opt, R_opt) : optimal noise parameters
    """
    Q_grid = np.logspace(-6, -1, 20)
    R_grid = np.logspace(-4, 0, 20)

    best_ll = -np.inf
    best_Q, best_R = 1e-4, 1e-2

    T = len(signals)

    for Q in Q_grid:
        for R in R_grid:
            beta = beta_0
            P = P_0
            ll = 0.0

            for t in range(T):
                x_t = signals[t]
                y_t = pnl[t]
                P_prior = P + Q
                S = x_t**2 * P_prior + R
                innovation = y_t - x_t * beta
                K = (P_prior * x_t) / S if abs(S) > 1e-12 else 0.0
                beta = beta + K * innovation
                P = max((1.0 - K * x_t) * P_prior, 1e-12)

                if S > 0:
                    ll += -0.5 * (np.log(2 * np.pi * S) + innovation**2 / S)

            if ll > best_ll:
                best_ll = ll
                best_Q, best_R = Q, R

    return best_Q, best_R


# ---------------------------------------------------------------------------
# Rolling OLS Baseline (for comparison)
# ---------------------------------------------------------------------------

def rolling_ols_beta(
    signals: pd.Series,
    pnl: pd.Series,
    window: int = 60,
) -> pd.Series:
    """
    Compute rolling OLS hedge ratio for comparison against Kalman filter.

    beta_t = Cov(signal_t, pnl_t) / Var(signal_t)
    estimated over a rolling window.

    Parameters
    ----------
    signals : IV residual signal series
    pnl     : delta-hedged P&L series
    window  : rolling window size in days

    Returns
    -------
    pd.Series of rolling OLS beta estimates
    """
    cov = signals.rolling(window).cov(pnl)
    var = signals.rolling(window).var()
    beta = cov / var.replace(0, np.nan)
    beta.name = "rolling_ols_beta"
    return beta
