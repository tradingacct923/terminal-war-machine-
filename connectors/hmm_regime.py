"""
HMM Regime Detection — Hidden Markov Model for volatility regime classification.

Replaces majority voting in vol_surface.py with a proper probabilistic model.

3 hidden states mapped to 5 regime labels:
  State 0 → COMPRESSED / COMPLACENT  (low vol, low skew)
  State 1 → NORMAL / ELEVATED        (mid vol, moderate skew)
  State 2 → STRESSED                 (high vol, high skew, backwardation)

Feature vector (4D, fed every 5s from zone_update):
  [iv_rank, iv_skew, vol_premium, vpin]

Uses online forward filtering (α recursion) for O(K²) per observation.
Parameters updated via incremental sufficient statistics (soft EM).

References:
  Hamilton (1989): A new approach to the economic analysis of
    nonstationary time series and the business cycle
  Rabiner (1989): A tutorial on hidden Markov models and
    selected applications in speech recognition
  Baum-Welch via incremental EM: Cappé (2011)

Min dwell time prevents rapid switching (configurable, default 3 obs = 15s).
"""

import math
import numpy as np
from collections import deque


class OnlineHMM:
    """
    Online Hidden Markov Model with forward filtering and incremental EM.

    K = number of hidden states (default 3).
    D = feature dimension (default 4: iv_rank, iv_skew, vol_premium, vpin).

    Emission model: diagonal Gaussian per state.
    Transition matrix: row-stochastic, initialized with strong self-transitions.
    """

    def __init__(self, n_states=3, n_features=4, ema_halflife=100):
        self.K = n_states
        self.D = n_features

        # ── Transition matrix (row-stochastic) ──
        # Strong diagonal = regime persistence (0.92 self-transition)
        off_diag = 0.08 / (self.K - 1)
        self.A = np.full((self.K, self.K), off_diag)
        np.fill_diagonal(self.A, 0.92)

        # ── Initial state distribution ──
        self.pi = np.ones(self.K) / self.K

        # ── Emission parameters (diagonal Gaussian per state) ──
        # Priors: informed by typical vol surface values
        # State 0: low vol (COMPRESSED/COMPLACENT)
        # State 1: normal vol (NORMAL/ELEVATED)
        # State 2: high vol (STRESSED)
        self.mu = np.array([
            [15.0, 0.98, 2.0, 0.35],   # State 0: low IV rank, low skew, low premium, low VPIN
            [45.0, 1.05, 8.0, 0.50],   # State 1: mid IV rank, mild skew, moderate premium
            [80.0, 1.15, 20.0, 0.70],  # State 2: high IV rank, strong skew, high premium, high VPIN
        ])

        # Variance per state per feature (diagonal covariance)
        self.var = np.array([
            [200.0, 0.01, 10.0, 0.02],
            [300.0, 0.02, 25.0, 0.03],
            [200.0, 0.03, 40.0, 0.03],
        ])

        # Floor variance to prevent numerical collapse
        self._var_floor = np.array([50.0, 0.005, 5.0, 0.01])

        # ── Forward filter state ──
        self.alpha = np.ones(self.K) / self.K  # filtered distribution P(z_t | x_1:t)
        self._obs_count = 0

        # ── Incremental EM sufficient statistics (EMA-based) ──
        # Decay factor from half-life
        self._ema_alpha = 1.0 - math.exp(-math.log(2) / max(ema_halflife, 1))
        # Running sums for parameter updates
        self._ss_gamma = np.ones(self.K) / self.K          # state occupancy P(z_t=k)
        self._ss_mu = self.mu.copy()                        # weighted mean
        self._ss_var = self.var.copy()                      # weighted variance
        self._ss_xi = self.A.copy() * (1.0 / self.K)       # transition counts

        # ── Regime output ──
        self._regime_history = deque(maxlen=20)
        self._current_state = 1       # start in NORMAL
        self._dwell_count = 0         # ticks in current state
        self._min_dwell = 3           # min observations before regime switch (15s at 5s rate)

        # ── State → regime label mapping ──
        # Each state maps to a primary label; sub-classification via features
        self._REGIME_MAP = {
            0: 'COMPRESSED',   # refined to COMPLACENT if iv_rank > 10
            1: 'NORMAL',       # refined to ELEVATED if features warrant
            2: 'STRESSED',
        }

    def _emission_prob(self, x):
        """Diagonal Gaussian emission: P(x | z=k) for all k.

        Returns (K,) array of likelihoods.
        """
        # x: (D,), mu: (K, D), var: (K, D)
        diff = x[np.newaxis, :] - self.mu   # (K, D)
        exponent = -0.5 * np.sum(diff ** 2 / self.var, axis=1)  # (K,)
        normalizer = np.prod(np.sqrt(2 * math.pi * self.var), axis=1)  # (K,)
        probs = np.exp(exponent) / normalizer
        # Floor to prevent underflow
        return np.maximum(probs, 1e-300)

    def observe(self, features):
        """
        Process one observation (4D feature vector).

        Args:
            features: dict with keys iv_rank, iv_skew, vol_premium, vpin
                      OR np.array of shape (4,)

        Returns:
            dict with regime, state_probs, most_likely_state
        """
        if isinstance(features, dict):
            x = np.array([
                features.get('iv_rank', 50.0),
                features.get('iv_skew', 1.0),
                features.get('vol_premium', 8.0),
                features.get('vpin', 0.5),
            ])
        else:
            x = np.asarray(features, dtype=float)

        # ── Forward step: α_t ∝ B(x_t) * (A^T α_{t-1}) ──
        emission = self._emission_prob(x)               # (K,)
        predicted = self.A.T @ self.alpha                # (K,)  — prediction step
        alpha_raw = emission * predicted                 # (K,)  — update step
        alpha_sum = alpha_raw.sum()
        if alpha_sum > 0:
            self.alpha = alpha_raw / alpha_sum           # normalize
        else:
            # Numerical underflow — reset to uniform
            self.alpha = np.ones(self.K) / self.K

        self._obs_count += 1

        # ── Incremental EM parameter update (after warmup) ──
        if self._obs_count > 20:
            self._update_params(x, emission, predicted)

        # ── MAP state with dwell time ──
        map_state = int(np.argmax(self.alpha))
        if map_state != self._current_state:
            self._dwell_count += 1
            if self._dwell_count >= self._min_dwell:
                self._current_state = map_state
                self._dwell_count = 0
        else:
            self._dwell_count = 0

        # ── Refine regime label ──
        regime = self._refine_regime(self._current_state, x)
        self._regime_history.append(regime)

        return {
            'regime': regime,
            'state': self._current_state,
            'state_probs': self.alpha.tolist(),
            'map_state': map_state,
            'obs_count': self._obs_count,
        }

    def _refine_regime(self, state, x):
        """Map hidden state to regime label with feature-based refinement."""
        iv_rank = x[0]
        iv_skew = x[1]
        vol_premium = x[2]
        vpin = x[3]

        if state == 0:
            # Low-vol state: COMPRESSED if IV at lows, COMPLACENT if just low
            if iv_rank < 15 and vol_premium < 3:
                return 'COMPRESSED'
            return 'COMPLACENT'
        elif state == 1:
            # Mid-vol state: ELEVATED if skew rising or premium > 12
            if iv_skew > 1.08 and vol_premium > 12:
                return 'ELEVATED'
            if vpin > 0.65:
                return 'ELEVATED'
            return 'NORMAL'
        else:  # state == 2
            return 'STRESSED'

    def _update_params(self, x, emission, predicted):
        """Incremental EM: update mu, var, A using EMA of sufficient statistics."""
        a = self._ema_alpha
        gamma = self.alpha  # posterior P(z_t=k | x_1:t)

        # Update state occupancy
        self._ss_gamma = (1 - a) * self._ss_gamma + a * gamma

        # Update emission means and variances per state
        for k in range(self.K):
            w = gamma[k]
            if w < 1e-10:
                continue
            # Running mean: mu_k ← (1-a)*mu_k + a*x  (weighted by gamma_k)
            self._ss_mu[k] = (1 - a) * self._ss_mu[k] + a * x
            # Running variance: var_k ← (1-a)*var_k + a*(x-mu_k)²
            diff = x - self._ss_mu[k]
            self._ss_var[k] = (1 - a) * self._ss_var[k] + a * (diff ** 2)

        # Apply to emission parameters (with floor)
        self.mu = self._ss_mu.copy()
        self.var = np.maximum(self._ss_var, self._var_floor)

        # Update transition matrix (EMA of ξ_{t-1,t})
        if self._obs_count > 21:
            # ξ(i,j) ∝ α_{t-1}(i) * A(i,j) * B_j(x_t)
            xi = np.outer(self.alpha, emission * predicted)
            xi_sum = xi.sum()
            if xi_sum > 0:
                xi /= xi_sum
                self._ss_xi = (1 - a) * self._ss_xi + a * xi
                # Normalize rows
                row_sums = self._ss_xi.sum(axis=1, keepdims=True)
                row_sums = np.maximum(row_sums, 1e-10)
                self.A = self._ss_xi / row_sums

    def get_regime(self):
        """Return current regime string."""
        if self._obs_count < 3:
            return 'NORMAL'
        return self._refine_regime(self._current_state,
                                    np.array([50.0, 1.0, 8.0, 0.5]))

    def get_state_probs(self):
        """Return filtered state probabilities."""
        return {
            'prob_low_vol': round(float(self.alpha[0]), 4),
            'prob_normal': round(float(self.alpha[1]), 4),
            'prob_stressed': round(float(self.alpha[2]), 4),
        }

    def get_transition_matrix(self):
        """Return current learned transition matrix."""
        return self.A.tolist()

    @property
    def warm(self):
        return self._obs_count >= 10
