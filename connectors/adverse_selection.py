"""
Adverse Selection Engine — Institutional-grade informed flow measurement.

Combines 4 microstructure models:
  1. Kyle lambda: permanent price impact per unit signed flow (rolling OLS)
  2. Glosten-Harris: decompose impact into permanent vs transitory
  3. Huang-Stoll: decompose spread into adverse selection / inventory / order processing
  4. Realized spread: measure MM profitability (effective - adverse selection)

All estimators are O(1) per trade using rolling sufficient statistics.

References:
  Kyle (1985): Continuous auctions and insider trading
  Glosten & Harris (1988): Estimating the components of the bid-ask spread
  Hasbrouck (1991): Measuring the information content of stock trades
  Huang & Stoll (1997): The components of the bid-ask spread

Fed by l2_worker.on_trade() for every NQ tick.
Emits state dict via get_state() for edge_detector signal gating.
"""

import math
from collections import deque


class KyleLambdaEstimator:
    """
    Kyle (1985) lambda — permanent price impact coefficient.

    OLS: dP = alpha + lambda * Q + epsilon
    Where dP = mid change, Q = signed volume.

    Uses running sufficient statistics for O(1) per trade.
    Maintains fast (300 trades) and slow (2000 trades) windows.
    When fast lambda diverges above slow by >2 sigma, informed flow detected.
    """

    def __init__(self, window=500):
        self.window = window
        self._buf_dp = deque(maxlen=window)
        self._buf_q = deque(maxlen=window)
        self._n = 0
        self._sum_q = 0.0
        self._sum_dp = 0.0
        self._sum_qq = 0.0
        self._sum_qdp = 0.0
        self._sum_dpdp = 0.0

        self.lambda_ = 0.0
        self.alpha = 0.0
        self.t_stat = 0.0
        self.r_squared = 0.0
        self._last_mid = None

    def on_trade(self, mid_price, volume, side):
        """
        mid_price: current mid after trade
        volume: unsigned trade size
        side: 'b' or 's' (buyer/seller initiated)
        """
        if self._last_mid is None:
            self._last_mid = mid_price
            return

        dp = mid_price - self._last_mid
        q = volume if side == 'b' else (-volume if side == 's' else 0)
        self._last_mid = mid_price

        if q == 0:
            return

        # Evict oldest if at capacity
        if len(self._buf_dp) == self.window:
            old_dp = self._buf_dp[0]
            old_q = self._buf_q[0]
            self._sum_q -= old_q
            self._sum_dp -= old_dp
            self._sum_qq -= old_q * old_q
            self._sum_qdp -= old_q * old_dp
            self._sum_dpdp -= old_dp * old_dp
            self._n -= 1

        self._buf_dp.append(dp)
        self._buf_q.append(q)
        self._sum_q += q
        self._sum_dp += dp
        self._sum_qq += q * q
        self._sum_qdp += q * dp
        self._sum_dpdp += dp * dp
        self._n += 1

        if self._n >= 30:
            denom = self._n * self._sum_qq - self._sum_q ** 2
            if abs(denom) > 1e-12:
                self.lambda_ = (self._n * self._sum_qdp - self._sum_q * self._sum_dp) / denom
                self.alpha = (self._sum_dp - self.lambda_ * self._sum_q) / self._n

                # R-squared
                ss_tot = self._sum_dpdp - self._sum_dp ** 2 / self._n
                mean_q = self._sum_q / self._n
                var_q = self._sum_qq / self._n - mean_q ** 2

                if ss_tot > 1e-12 and var_q > 1e-12:
                    ss_reg = self.lambda_ ** 2 * var_q * self._n
                    self.r_squared = max(0, min(1, ss_reg / ss_tot))

                    # t-statistic
                    resid_var = max(0, (ss_tot - ss_reg) / max(self._n - 2, 1))
                    se = math.sqrt(resid_var / (var_q * self._n)) if var_q * self._n > 0 else 1e-6
                    self.t_stat = self.lambda_ / max(se, 1e-10)

    @property
    def warm(self):
        return self._n >= 30


class GlostenHarrisEstimator:
    """
    Glosten-Harris (1988) — permanent vs transitory price impact.

    dP = c1*Q_t + c2*Q_{t-1} + eps
    permanent = c1 + c2 (information component)
    transitory = -c2 (inventory/bounce component)
    adverse_selection_ratio = |permanent| / (|permanent| + |transitory|)

    O(1) per trade via 2-regressor running OLS sufficient statistics.
    """

    def __init__(self, window=500):
        self.window = window
        self._buf = deque(maxlen=window)
        self._s11 = 0.0  # sum(q_t^2)
        self._s22 = 0.0  # sum(q_prev^2)
        self._s12 = 0.0  # sum(q_t * q_prev)
        self._s1y = 0.0  # sum(q_t * dp)
        self._s2y = 0.0  # sum(q_prev * dp)
        self._n = 0
        self._last_q = 0.0

        self.lambda_permanent = 0.0
        self.theta_transitory = 0.0
        self.adverse_selection_ratio = 0.0

    def on_trade(self, dp, volume, side):
        """dp: mid-price change, volume: unsigned, side: 'b'/'s'."""
        q = volume if side == 'b' else (-volume if side == 's' else 0)
        if q == 0:
            self._last_q = q
            return

        q_prev = self._last_q
        self._last_q = q

        # Evict oldest
        if len(self._buf) == self.window:
            old = self._buf[0]
            self._s11 -= old[1] ** 2
            self._s22 -= old[2] ** 2
            self._s12 -= old[1] * old[2]
            self._s1y -= old[1] * old[0]
            self._s2y -= old[2] * old[0]
            self._n -= 1

        self._buf.append((dp, q, q_prev))
        self._s11 += q * q
        self._s22 += q_prev * q_prev
        self._s12 += q * q_prev
        self._s1y += q * dp
        self._s2y += q_prev * dp
        self._n += 1

        if self._n >= 50:
            det = self._s11 * self._s22 - self._s12 ** 2
            if abs(det) > 1e-12:
                c1 = (self._s22 * self._s1y - self._s12 * self._s2y) / det
                c2 = (self._s11 * self._s2y - self._s12 * self._s1y) / det
                self.theta_transitory = -c2
                self.lambda_permanent = c1 + c2
                total = abs(self.lambda_permanent) + abs(self.theta_transitory)
                if total > 1e-10:
                    self.adverse_selection_ratio = abs(self.lambda_permanent) / total

    @property
    def warm(self):
        return self._n >= 50


class HuangStollDecomposition:
    """
    Huang-Stoll (1997) — spread component decomposition.

    Decomposes bid-ask spread into:
      alpha: adverse selection (information asymmetry fraction)
      beta: inventory holding cost
      gamma: order processing cost (residual)

    Model: dM = alpha * (S/2) * Q + eps (midpoint revision equation)
           Q_t = rho * Q_{t-1} + eta (trade persistence)

    O(1) per trade via running regression statistics.
    """

    def __init__(self, window=500):
        self.window = window
        self._buf = deque(maxlen=window)
        self._q_buf = deque(maxlen=window)
        # Midpoint revision regression: dM = alpha * (S/2 * Q)
        self._sum_xx = 0.0
        self._sum_xy = 0.0
        self._n = 0
        # Trade persistence: Q_t = rho * Q_{t-1}
        self._sum_qq = 0.0
        self._sum_qq1 = 0.0
        self._nq = 0

        self.alpha = 0.0   # adverse selection
        self.beta = 0.0    # inventory
        self.gamma = 0.0   # order processing
        self.rho = 0.0     # trade continuation
        self._last_mid = None
        self._last_q = 0

    def on_trade(self, mid_price, spread, side):
        """
        mid_price: (best_bid + best_ask) / 2
        spread: best_ask - best_bid (in points)
        side: 'b' or 's'
        """
        q = 1 if side == 'b' else (-1 if side == 's' else 0)
        if q == 0 or spread <= 0:
            return

        if self._last_mid is not None:
            dm = mid_price - self._last_mid
            x = (spread / 2.0) * q

            if len(self._buf) == self.window:
                old = self._buf[0]
                old_x = (old[1] / 2.0) * old[2]
                self._sum_xx -= old_x ** 2
                self._sum_xy -= old_x * old[0]
                self._n -= 1

            self._buf.append((dm, spread, q))
            self._sum_xx += x * x
            self._sum_xy += x * dm
            self._n += 1

            # Trade persistence
            if self._last_q != 0:
                if len(self._q_buf) == self.window:
                    oq = self._q_buf[0]
                    self._sum_qq -= oq[0] ** 2
                    self._sum_qq1 -= oq[0] * oq[1]
                    self._nq -= 1
                self._q_buf.append((self._last_q, q))
                self._sum_qq += self._last_q ** 2
                self._sum_qq1 += self._last_q * q
                self._nq += 1

        self._last_mid = mid_price
        self._last_q = q

        if self._n >= 50:
            # alpha from regression through origin: dM = alpha*(S/2*Q)
            if self._sum_xx > 1e-12:
                self.alpha = max(0, min(1, self._sum_xy / self._sum_xx))

            # rho from trade persistence
            if self._nq >= 30 and self._sum_qq > 1e-12:
                self.rho = self._sum_qq1 / self._sum_qq

            # Decomposition: beta from serial correlation
            self.beta = max(0, min(1 - self.alpha, abs(self.rho) * (1 - self.alpha)))
            self.gamma = max(0, 1.0 - self.alpha - self.beta)

    @property
    def warm(self):
        return self._n >= 50


class RealizedSpreadTracker:
    """
    Effective vs realized spread decomposition.

    Effective spread = 2 * D * (P_trade - M_prevailing)
    Realized spread = 2 * D * (P_trade - M_{t+tau})
    Adverse selection = Effective - Realized = 2 * D * (M_{t+tau} - M_prevailing)

    Uses EWMA for rolling estimates. Resolves pending trades when
    the horizon (tau seconds) has elapsed.
    """

    def __init__(self, horizon_seconds=5.0, max_pending=2000, ema_alpha=0.005):
        self.horizon = horizon_seconds
        self._pending = deque(maxlen=max_pending)
        self._alpha = ema_alpha

        self.avg_effective_spread = 0.0
        self.avg_realized_spread = 0.0
        self.avg_adverse_selection = 0.0
        self._count = 0

    def on_trade(self, trade_price, mid, side, timestamp):
        direction = 1 if side == 'b' else (-1 if side == 's' else 0)
        if direction == 0 or mid <= 0:
            return
        eff = 2.0 * direction * (trade_price - mid)
        self._pending.append((timestamp, mid, direction, eff))

    def resolve(self, current_mid, current_time):
        """Call every trade or every 100ms to resolve matured pending trades."""
        resolved = 0
        while self._pending and (current_time - self._pending[0][0]) >= self.horizon:
            ts, mid_at_trade, direction, eff = self._pending.popleft()
            adv_sel = 2.0 * direction * (current_mid - mid_at_trade)
            realized = eff - adv_sel

            a = self._alpha
            self.avg_effective_spread = (1 - a) * self.avg_effective_spread + a * eff
            self.avg_realized_spread = (1 - a) * self.avg_realized_spread + a * realized
            self.avg_adverse_selection = (1 - a) * self.avg_adverse_selection + a * adv_sel
            self._count += 1
            resolved += 1
        return resolved

    @property
    def warm(self):
        return self._count >= 30


class AdverseSelectionEngine:
    """
    Unified adverse selection measurement.

    Combines Kyle lambda, Glosten-Harris, Huang-Stoll, and realized spread
    into a single composite score. Fed by l2_worker on every trade.

    State dict emitted via get_state() for use by edge_detector signal gating.

    Interpretation of composite score:
      0-25: CLEAN — uninformed flow, safe to provide liquidity
      25-50: NORMAL — mixed flow, standard conditions
      50-75: ELEVATED — informed traders present, tighten risk
      75-100: TOXIC — high adverse selection, don't provide liquidity
    """

    def __init__(self):
        self.kyle_fast = KyleLambdaEstimator(window=300)
        self.kyle_slow = KyleLambdaEstimator(window=2000)
        self.glosten_harris = GlostenHarrisEstimator(window=500)
        self.huang_stoll = HuangStollDecomposition(window=500)
        self.realized_spread = RealizedSpreadTracker(horizon_seconds=5.0)
        self._trade_count = 0
        self._last_mid = None

    def on_trade(self, mid_price, trade_price, spread, volume, side, timestamp):
        """
        Called on every trade from l2_worker.

        Args:
            mid_price: current mid (best_bid + best_ask) / 2
            trade_price: actual trade execution price
            spread: best_ask - best_bid
            volume: unsigned trade size (contracts)
            side: 'b' or 's' (CME aggressor flag)
            timestamp: epoch seconds
        """
        # Kyle lambda (uses mid changes)
        self.kyle_fast.on_trade(mid_price, volume, side)
        self.kyle_slow.on_trade(mid_price, volume, side)

        # Glosten-Harris (uses mid change + signed volume)
        if self._last_mid is not None:
            dp = mid_price - self._last_mid
            self.glosten_harris.on_trade(dp, volume, side)

        # Huang-Stoll (uses mid, spread, trade direction)
        self.huang_stoll.on_trade(mid_price, spread, side)

        # Realized spread (uses trade price vs mid, resolves after horizon)
        self.realized_spread.on_trade(trade_price, mid_price, side, timestamp)
        self.realized_spread.resolve(mid_price, timestamp)

        self._last_mid = mid_price
        self._trade_count += 1

    def get_state(self):
        """
        Return composite adverse selection state.

        Returns dict with individual model outputs + composite score.
        Designed to be included in l2_update payloads and consumed
        by edge_detector for signal gating.
        """
        # Normalize each model's output to [0, 1]
        # Kyle: lambda ~ 0.001-0.005 normal, > 0.01 elevated, > 0.02 extreme
        kyle_fast_norm = min(1.0, abs(self.kyle_fast.lambda_) / 0.02) if self.kyle_fast.warm else 0
        kyle_slow_norm = min(1.0, abs(self.kyle_slow.lambda_) / 0.02) if self.kyle_slow.warm else 0

        # Divergence: fast lambda much higher than slow = new informed flow
        kyle_divergence = 0.0
        if self.kyle_fast.warm and self.kyle_slow.warm:
            kyle_divergence = self.kyle_fast.lambda_ - self.kyle_slow.lambda_

        # Glosten-Harris: adverse selection ratio already [0, 1]
        gh_signal = self.glosten_harris.adverse_selection_ratio if self.glosten_harris.warm else 0.5

        # Huang-Stoll: alpha already [0, 1]
        hs_signal = self.huang_stoll.alpha if self.huang_stoll.warm else 0.5

        # Realized spread: adverse selection component / typical spread
        # NQ typical spread = 0.25, so normalize by that
        spread_signal = min(1.0, abs(self.realized_spread.avg_adverse_selection) / 0.25) \
            if self.realized_spread.warm else 0.5

        # Composite score: weighted average of normalized signals
        composite = (
            0.30 * kyle_fast_norm +
            0.25 * gh_signal +
            0.25 * hs_signal +
            0.20 * spread_signal
        )

        # Classify regime
        if composite > 0.75:
            regime = 'TOXIC'
        elif composite > 0.50:
            regime = 'ELEVATED'
        elif composite > 0.25:
            regime = 'NORMAL'
        else:
            regime = 'CLEAN'

        return {
            # Kyle lambda (fast + slow)
            'kyle_lambda_fast': round(self.kyle_fast.lambda_, 6),
            'kyle_lambda_slow': round(self.kyle_slow.lambda_, 6),
            'kyle_t_stat': round(self.kyle_fast.t_stat, 2),
            'kyle_r_squared': round(self.kyle_fast.r_squared, 4),
            'kyle_divergence': round(kyle_divergence, 6),

            # Glosten-Harris decomposition
            'gh_permanent': round(self.glosten_harris.lambda_permanent, 6),
            'gh_transitory': round(self.glosten_harris.theta_transitory, 6),
            'gh_info_ratio': round(gh_signal, 4),

            # Huang-Stoll spread decomposition
            'hs_adverse_selection': round(self.huang_stoll.alpha, 4),
            'hs_inventory': round(self.huang_stoll.beta, 4),
            'hs_order_processing': round(self.huang_stoll.gamma, 4),
            'hs_trade_persistence': round(self.huang_stoll.rho, 4),

            # Realized spread
            'effective_spread': round(self.realized_spread.avg_effective_spread, 4),
            'realized_spread': round(self.realized_spread.avg_realized_spread, 4),
            'adverse_selection_cost': round(self.realized_spread.avg_adverse_selection, 4),

            # Composite
            'adverse_selection_score': round(composite * 100, 1),
            'as_regime': regime,
            'trade_count': self._trade_count,
        }

    @property
    def warm(self):
        return self._trade_count >= 300
