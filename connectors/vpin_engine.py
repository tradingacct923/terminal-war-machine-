"""
VPIN Engine — Volume-Synchronized Probability of Informed Trading

VPIN measures order flow toxicity in real-time by analyzing the imbalance
between buy and sell volume within fixed-volume buckets. When VPIN spikes,
it means informed (toxic) flow has entered the market, and a volatility
event is imminent.

Architecture:
  1. Volume Clock bucketing: Each bucket accumulates exactly V_bucket volume.
  2. Trade classification: Uses the CME native aggressor flag (buy/sell).
  3. Order imbalance: |V_buy - V_sell| / V_bucket per bucket.
  4. VPIN = rolling mean of order imbalance over N buckets.
  5. When VPIN crosses a percentile threshold → TOXIC_FLOW alert.

Reference: Easley, López de Prado, O'Hara (2012)
  "Flow Toxicity and Liquidity in a High-Frequency World"

Integration:
  - l2_worker.on_trade() calls vpin.on_trade(symbol, volume, side, timestamp)
  - VPIN state is forwarded to EdgeDetector for regime conditioning
  - High VPIN flips the engine from mean-reversion → momentum
"""

import time
import math
import logging
from collections import deque

log = logging.getLogger("vpin_engine")


class VPINEngine:
    """Volume-Synchronized Probability of Informed Trading.

    Computes real-time order flow toxicity for a single symbol.
    Uses volume-clock bucketing (not time-clock) so that VPIN
    adapts naturally to fast/slow markets.
    """

    def __init__(self, bucket_size=50, n_buckets=50, half_life=30):
        """
        Args:
            bucket_size: Volume per bucket. Calibrate to ~1 minute of avg volume.
                         For NQ: ~50 contracts/bucket during RTH.
            n_buckets:   Rolling window of buckets for VPIN calculation.
            half_life:   EWMA half-life for the VPIN trend tracker.
        """
        self._bucket_size = bucket_size
        self._n_buckets = n_buckets

        # ── Current bucket accumulator ──
        self._curr_buy_vol = 0
        self._curr_sell_vol = 0
        self._curr_total_vol = 0

        # ── Completed bucket history ──
        self._bucket_imbalances = deque(maxlen=n_buckets)
        self._bucket_timestamps = deque(maxlen=n_buckets)
        self._buckets_completed = 0

        # ── VPIN state ──
        self._vpin = 0.0
        self._vpin_history = deque(maxlen=500)

        # ── EWMA trend tracker ──
        self._alpha = 1.0 - math.exp(-math.log(2) / half_life)
        self._ewma_vpin = 0.0
        self._ewma_var = 0.0
        self._ewma_initialized = False

        # ── Empirical percentile reservoir ──
        self._reservoir = deque(maxlen=500)
        self._sorted_cache = []
        self._cache_dirty = False

        # ── Alert state ──
        self._last_alert_time = 0
        self._alert_cooldown = 15.0  # seconds between alerts

        # ── Calibration ──
        self._trade_count = 0
        self._session_volume = 0
        self._session_start = time.time()
        self._calibrated = False

        # ── Callback ──
        self._on_toxic_flow = None  # callback(vpin_data_dict)

    def set_callback(self, callback):
        """Set callback for toxic flow alerts."""
        self._on_toxic_flow = callback

    @property
    def vpin(self):
        return self._vpin

    @property
    def warm(self):
        return self._buckets_completed >= self._n_buckets

    @property
    def ewma_vpin(self):
        return self._ewma_vpin

    def on_trade(self, symbol, volume, side, timestamp):
        """Process a single trade tick.

        Args:
            symbol: e.g. 'NQ'
            volume: trade size (contracts)
            side:   'b' (aggressive buy) or 's' (aggressive sell)
            timestamp: epoch seconds
        """
        self._trade_count += 1
        self._session_volume += volume

        # ── Auto-calibrate bucket size after 5 minutes ──
        if not self._calibrated and self._trade_count >= 500:
            elapsed = timestamp - self._session_start
            if elapsed > 300:  # 5 minutes
                avg_vol_per_min = self._session_volume / (elapsed / 60)
                # Target: ~1 minute of volume per bucket
                self._bucket_size = max(10, int(avg_vol_per_min))
                self._calibrated = True
                log.info(f"[VPIN] Auto-calibrated bucket_size={self._bucket_size} "
                         f"(avg {avg_vol_per_min:.0f} vol/min)")

        # ── Classify and accumulate ──
        if side == 'b':
            self._curr_buy_vol += volume
        elif side == 's':
            self._curr_sell_vol += volume
        else:
            # Neutral: split 50/50 (standard VPIN convention)
            self._curr_buy_vol += volume / 2
            self._curr_sell_vol += volume / 2

        self._curr_total_vol += volume

        # ── Check if bucket is full ──
        while self._curr_total_vol >= self._bucket_size:
            overflow = self._curr_total_vol - self._bucket_size

            # Proportionally split overflow back
            if self._curr_total_vol > 0:
                buy_ratio = self._curr_buy_vol / self._curr_total_vol
            else:
                buy_ratio = 0.5

            # Finalize this bucket
            bucket_buy = self._curr_buy_vol - (overflow * buy_ratio)
            bucket_sell = self._curr_sell_vol - (overflow * (1 - buy_ratio))

            # Order imbalance for this bucket: |V_buy - V_sell| / V_bucket
            imbalance = abs(bucket_buy - bucket_sell) / self._bucket_size
            self._bucket_imbalances.append(imbalance)
            self._bucket_timestamps.append(timestamp)
            self._buckets_completed += 1

            # ── Compute VPIN ──
            if len(self._bucket_imbalances) >= 10:
                self._vpin = sum(self._bucket_imbalances) / len(self._bucket_imbalances)
                self._vpin_history.append((timestamp, self._vpin))

                # Update EWMA
                if not self._ewma_initialized:
                    self._ewma_vpin = self._vpin
                    self._ewma_var = 0.0
                    self._ewma_initialized = True
                else:
                    delta = self._vpin - self._ewma_vpin
                    self._ewma_vpin += self._alpha * delta
                    self._ewma_var = (1 - self._alpha) * (self._ewma_var + self._alpha * delta * delta)

                # Update percentile reservoir
                self._reservoir.append(self._vpin)
                self._cache_dirty = True

                # ── Check for toxic flow alert ──
                self._check_toxicity(symbol, timestamp)

            # Carry overflow into next bucket
            self._curr_buy_vol = overflow * buy_ratio
            self._curr_sell_vol = overflow * (1 - buy_ratio)
            self._curr_total_vol = overflow

    def _check_toxicity(self, symbol, timestamp):
        """Check if VPIN has crossed into toxic territory."""
        if not self.warm:
            return

        pctl = self.percentile_of(self._vpin)
        ewma_std = math.sqrt(max(self._ewma_var, 1e-10))

        # Z-score relative to EWMA trend
        z_score = (self._vpin - self._ewma_vpin) / ewma_std if ewma_std > 0.001 else 0

        # Toxic = VPIN above P85 AND z-score > 1.5 (sudden spike, not gradual drift)
        is_toxic = pctl >= 85.0 and z_score > 1.5
        # Extreme toxic = P95 or z > 2.5
        is_extreme = pctl >= 95.0 or z_score > 2.5

        if not (is_toxic or is_extreme):
            return

        if timestamp - self._last_alert_time < self._alert_cooldown:
            return

        self._last_alert_time = timestamp

        severity = 'EXTREME' if is_extreme else 'ELEVATED'
        regime_recommendation = 'MOMENTUM' if is_extreme else 'CAUTION'

        alert = {
            'symbol': symbol,
            'vpin': round(self._vpin, 4),
            'vpin_pctl': round(pctl, 1),
            'vpin_z': round(z_score, 2),
            'ewma_vpin': round(self._ewma_vpin, 4),
            'severity': severity,
            'regime_recommendation': regime_recommendation,
            'bucket_size': self._bucket_size,
            'buckets_completed': self._buckets_completed,
            'timestamp': timestamp,
        }

        arrow = '🟣' if is_extreme else '🟡'
        log.info(f"[VPIN] {arrow} {severity} TOXIC FLOW | "
                 f"VPIN={self._vpin:.4f} (P{pctl:.0f}, z={z_score:.1f}) | "
                 f"regime→{regime_recommendation}")
        # Also print to stdout for visibility
        print(f"[VPIN] {arrow} {severity} TOXIC FLOW | "
              f"VPIN={self._vpin:.4f} (P{pctl:.0f}, z={z_score:.1f}) | "
              f"regime→{regime_recommendation}")

        if self._on_toxic_flow:
            try:
                self._on_toxic_flow(alert)
            except Exception:
                pass

    def percentile_of(self, value):
        """Exact empirical percentile rank."""
        if len(self._reservoir) < 30:
            return 50.0
        if self._cache_dirty:
            self._sorted_cache = sorted(self._reservoir)
            self._cache_dirty = False
        n = len(self._sorted_cache)
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if self._sorted_cache[mid] < value:
                lo = mid + 1
            else:
                hi = mid
        return (lo / n) * 100.0

    def get_state(self):
        """Return current VPIN state for state-vector logging."""
        return {
            'vpin': round(self._vpin, 4),
            'vpin_ewma': round(self._ewma_vpin, 4),
            'vpin_pctl': round(self.percentile_of(self._vpin), 1) if self.warm else 0,
            'vpin_bucket_size': self._bucket_size,
            'vpin_buckets': self._buckets_completed,
            'vpin_warm': self.warm,
        }

    def get_regime_modifier(self):
        """Return a regime modifier based on VPIN state.

        Returns:
            'MOMENTUM' if toxic flow detected (trend-follow, don't fade)
            'REVERSION' if flow is clean (safe to fade extremes)
            'NEUTRAL' if insufficient data
        """
        if not self.warm:
            return 'NEUTRAL'

        pctl = self.percentile_of(self._vpin)
        if pctl >= 85:
            return 'MOMENTUM'
        elif pctl <= 25:
            return 'REVERSION'
        return 'NEUTRAL'
