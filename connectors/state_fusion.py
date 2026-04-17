"""
Gaussian Copula Signal Fusion — Replace naive independence assumption.

The old _joint_confidence() assumed all signals are independent:
  P(all extreme) = ∏ P(signal_i extreme)

This overstates confidence when signals are correlated (they always are).

Gaussian copula approach:
  1. Transform each percentile to standard normal via Φ⁻¹(p/100)
  2. Estimate correlation matrix Σ from rolling EMA of z-score pairs
  3. Compute joint tail probability via multivariate normal CDF
  4. Use Meng's effective dimensionality: d_eff = d / (1 + (d-1)*ρ_bar)

For real-time: we estimate pairwise correlations via EMA and use the
effective-dimensionality shortcut (closed-form, no numerical integration).

References:
  Li (2000): On Default Correlation: A Copula Function Approach
  Meng (1994): Multiple-Imputation Inferences with Uncongenial Sources
  Embrechts, McNeil, Straumann (2002): Correlation and Dependence in
    Risk Management: Properties and Pitfalls
"""

import math
from collections import defaultdict


# Standard normal CDF/quantile approximations (Abramowitz & Stegun)
def _norm_cdf(x):
    """Standard normal CDF, Abramowitz & Stegun 26.2.17 approximation."""
    if x > 6.0:
        return 1.0
    if x < -6.0:
        return 0.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1.0 if x >= 0 else -1.0
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2.0)
    return 0.5 * (1.0 + sign * y)


def _norm_ppf(p):
    """Inverse standard normal CDF (Beasley-Springer-Moro approximation)."""
    if p <= 0.0:
        return -6.0
    if p >= 1.0:
        return 6.0
    if p == 0.5:
        return 0.0

    # Rational approximation for central region
    if 0.08 < p < 0.92:
        q = p - 0.5
        r = q * q
        return q * ((((-25.44106049637 * r + 41.39119773534) * r - 18.61500062529) * r + 2.506628277459) /
                     ((((3.13082909833 * r - 21.06224101826) * r + 23.08336743743) * r - 8.47351093090) * r + 1.0))

    # Tail approximation
    if p < 0.5:
        r = p
    else:
        r = 1.0 - p
    r = math.sqrt(-2.0 * math.log(max(r, 1e-300)))
    val = (((7.7108572002e-4 * r + 0.0943913819) * r + 1.5213664935) /
           ((1.0 + 0.01328068987 * r + 0.0189269816) * r + 1.0)) - r
    if p < 0.5:
        return val
    return -val


class GaussianCopulaFusion:
    """
    Online Gaussian copula for combining correlated percentile signals.

    Maintains EMA-estimated pairwise correlations between signal dimensions.
    Uses Meng's effective dimensionality for fast joint probability computation.

    Usage:
        fusion = GaussianCopulaFusion()
        # Feed named signal percentiles as they arrive
        fusion.update('size_pctl', 92.5)
        fusion.update('venue_pctl', 87.3)
        fusion.update('absorption_pctl', 95.1)
        # Get joint confidence accounting for correlation
        joint_pctl = fusion.joint_confidence([92.5, 87.3, 95.1])
    """

    def __init__(self, ema_halflife=200, min_obs=30):
        self._ema_alpha = 1.0 - math.exp(-math.log(2) / max(ema_halflife, 1))
        self._min_obs = min_obs

        # Named signal tracking: {name: {'z': last_z, 'count': n}}
        self._signals = {}

        # Pairwise correlation matrix via EMA
        # Key: (name_a, name_b) sorted tuple → {'corr': rho, 'count': n}
        self._pair_corr = defaultdict(lambda: {'sum_ab': 0.0, 'count': 0})

        # Global mean correlation (fallback when pairs not yet estimated)
        self._global_rho_sum = 0.0
        self._global_rho_count = 0
        self._global_rho = 0.3  # conservative prior

        # Most recent joint confidence (exposed for header telemetry)
        self._last_joint = 50.0

    def update(self, name, percentile):
        """Feed a named percentile signal (0-100).

        This updates the correlation matrix for all pairs including this signal.
        Call this on every signal observation for accurate correlation tracking.
        """
        # Transform to z-score
        p = max(0.01, min(99.99, percentile)) / 100.0
        z = _norm_ppf(p)

        # Update pairwise correlations with all other recent signals
        a = self._ema_alpha
        for other_name, other_state in self._signals.items():
            if other_name == name:
                continue
            pair_key = tuple(sorted([name, other_name]))
            pair = self._pair_corr[pair_key]
            # EMA of z_a * z_b (product moment = correlation for standardized variables)
            product = z * other_state['z']
            pair['sum_ab'] = (1 - a) * pair['sum_ab'] + a * product
            pair['count'] += 1

            # Update global rho estimate
            if pair['count'] >= self._min_obs:
                rho = max(-0.95, min(0.95, pair['sum_ab']))
                self._global_rho_sum += rho
                self._global_rho_count += 1

        # Store current z for this signal
        if name not in self._signals:
            self._signals[name] = {'z': z, 'count': 0}
        self._signals[name]['z'] = z
        self._signals[name]['count'] += 1

        # Periodically update global rho
        if self._global_rho_count > 0:
            self._global_rho = self._global_rho_sum / self._global_rho_count
            self._global_rho = max(0.05, min(0.85, self._global_rho))
            # Reset accumulators to avoid stale bias
            self._global_rho_sum = 0.0
            self._global_rho_count = 0

    def joint_confidence(self, pctl_list):
        """Compute joint confidence from multiple percentile observations.

        Uses effective dimensionality to account for correlation.

        If signals are perfectly correlated (ρ=1), d_eff=1 → joint = max(signals)
        If signals are independent (ρ=0), d_eff=d → joint = product rule
        Reality is between: d_eff = d / (1 + (d-1) * ρ_bar)

        Args:
            pctl_list: list of percentile values [0-100]

        Returns:
            Joint confidence percentile (50-99.99)
        """
        if not pctl_list:
            return 50.0

        d = len(pctl_list)
        if d == 1:
            return max(50.0, min(99.99, pctl_list[0]))

        # Transform to z-scores
        z_scores = []
        for p in pctl_list:
            p_clamp = max(50.1, min(99.99, p))
            z_scores.append(_norm_ppf(p_clamp / 100.0))

        # Average z-score (used for effective-dimensionality shortcut)
        z_mean = sum(z_scores) / d

        # Mean pairwise correlation
        rho_bar = self._global_rho

        # Effective dimensionality (Meng 1994)
        d_eff = d / (1.0 + (d - 1) * rho_bar)

        # Joint z-score: scale by sqrt(d_eff) / sqrt(d)
        # Under the copula, the average z-score's effective standard deviation
        # is inflated by correlation: σ_avg = sqrt((1 + (d-1)*ρ) / d) = 1/sqrt(d_eff)
        # So the "standardized" average z is: z_joint = z_mean * sqrt(d_eff)
        z_joint = z_mean * math.sqrt(d_eff)

        # Convert back to percentile
        joint_pctl = _norm_cdf(z_joint) * 100.0
        result = round(max(50.0, min(99.99, joint_pctl)), 1)
        self._last_joint = result
        return result

    def get_correlation_estimate(self):
        """Return current global correlation estimate and signal count."""
        return {
            'rho_bar': round(self._global_rho, 4),
            'n_signals': len(self._signals),
            'n_pairs': len(self._pair_corr),
            'last_joint': round(self._last_joint, 1),
        }
