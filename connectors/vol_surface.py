"""
Vol Surface Monitor — Live Volatility Surface State Machine

Maintains a real-time view of the volatility surface from the options chain
data already flowing through schwab_bridge and greek_surface.

Key metrics:
  - IV Skew:         put IV / call IV at ATM — fear/greed thermometer
  - Term Structure:  0DTE IV vs next-expiry IV — contango/backwardation
  - Vol Premium:     IV − RV (realized vol from NQ price history)
  - Vol Regime:      STRESSED / ELEVATED / NORMAL / COMPLACENT / COMPRESSED
  - IV Rank:         Where current ATM IV sits vs recent range (0-100)
  - Skew Velocity:   Rate of change of IV skew (sudden fear = front-run)

Regime classification logic (used by EdgeDetector to adapt thresholds):
  STRESSED:     backwardation + put_skew > 1.15 + vol_premium > 20%
  ELEVATED:     backwardation + put_skew > 1.08 + vol_premium > 10%
  NORMAL:       contango/flat + put_skew 0.95-1.08 + vol_premium 5-15%
  COMPLACENT:   contango + put_skew < 1.05 + vol_premium < 5%
  COMPRESSED:   IV at multi-day lows + vol_premium < 2% (spring-loaded)

Integration:
  schwab_bridge._maybe_emit_zones() → VolSurface.update(zone_data)
  edge_detector._check_greek_signals() → vol_regime from VolSurface
"""

import time
import math
from collections import deque


class VolSurface:
    """Live volatility surface state machine.

    Fed by zone_update data from schwab_bridge every 5s.
    Provides vol regime classification to EdgeDetector.
    """

    def __init__(self):
        # ── IV history for ranking ──
        self._atm_iv_history = deque(maxlen=500)    # ~42 min at 5s intervals
        self._iv_skew_history = deque(maxlen=500)
        self._vol_premium_history = deque(maxlen=500)

        # ── Rate of change tracking ──
        self._prev_iv_skew = None
        self._prev_atm_iv = None
        self._prev_update_ts = 0

        # ── Current state ──
        self._regime = 'NORMAL'
        self._regime_confidence = 0.0
        self._state = {
            'atm_iv': 0,
            'iv_skew': 1.0,
            'iv_skew_label': 'FLAT',
            'term_structure': 'FLAT',
            'vol_premium': 0,
            'iv_rank': 50,
            'skew_velocity': 0,
            'iv_velocity': 0,
            'regime': 'NORMAL',
            'regime_confidence': 0,
            'regime_duration_s': 0,
        }
        self._regime_start_ts = time.time()
        self._last_regime_change = 0

        # ── Regime transition smoothing (prevent flapping) ──
        self._regime_votes = deque(maxlen=12)  # Last 12 updates (~60s)

    def update(self, zone_data, realized_vol=0.0):
        """Called every zone emission cycle with Greek surface data.

        Args:
            zone_data: dict from schwab_bridge _maybe_emit_zones()
            realized_vol: intraday realized volatility (annualized %) from NQ
        """
        now = time.time()
        dt = now - self._prev_update_ts if self._prev_update_ts > 0 else 5.0
        self._prev_update_ts = now

        # Extract IV surface data
        atm_iv = zone_data.get('atm_iv', 0)       # % (e.g., 22.5)
        iv_skew = zone_data.get('iv_skew', 1.0)   # ratio (put IV / call IV)
        iv_skew_label = zone_data.get('iv_skew_label', 'FLAT')
        term_structure = zone_data.get('term_structure', 'FLAT')
        iv_0dte = zone_data.get('iv_0dte', 0)      # near-term IV
        iv_next = zone_data.get('iv_next_dte', 0)   # next-expiry IV
        mean_iv = zone_data.get('mean_iv', 0)       # OI-weighted mean IV

        # Use whichever IV measure is available
        if atm_iv <= 0:
            atm_iv = mean_iv

        if atm_iv <= 0:
            return  # No IV data yet

        # ── Vol Premium: IV − RV ──
        vol_premium = atm_iv - realized_vol if realized_vol > 0 else 0

        # ── IV Rank: where current IV sits in recent range ──
        self._atm_iv_history.append(atm_iv)
        iv_rank = 50
        if len(self._atm_iv_history) >= 20:
            iv_min = min(self._atm_iv_history)
            iv_max = max(self._atm_iv_history)
            iv_range = iv_max - iv_min
            if iv_range > 0.1:
                iv_rank = ((atm_iv - iv_min) / iv_range) * 100
                iv_rank = max(0, min(100, iv_rank))

        # ── Skew Velocity: rate of change of IV skew ──
        skew_velocity = 0
        if self._prev_iv_skew is not None and dt > 0:
            skew_velocity = (iv_skew - self._prev_iv_skew) / dt * 60  # per minute
        self._prev_iv_skew = iv_skew
        self._iv_skew_history.append(iv_skew)

        # ── IV Velocity: rate of change of ATM IV ──
        iv_velocity = 0
        if self._prev_atm_iv is not None and dt > 0:
            iv_velocity = (atm_iv - self._prev_atm_iv) / dt * 60  # % per minute
        self._prev_atm_iv = atm_iv

        # ── Vol Premium history for compression detection ──
        if vol_premium != 0:
            self._vol_premium_history.append(vol_premium)

        # ── Regime Classification ──
        new_regime = self._classify_regime(
            iv_skew, term_structure, vol_premium, iv_rank,
            skew_velocity, iv_velocity
        )

        # Smoothed regime: majority vote over last 12 updates
        self._regime_votes.append(new_regime)
        regime_counts = {}
        for v in self._regime_votes:
            regime_counts[v] = regime_counts.get(v, 0) + 1
        smoothed_regime = max(regime_counts, key=regime_counts.get)
        regime_confidence = regime_counts[smoothed_regime] / len(self._regime_votes) * 100

        # Only transition if confidence > 50% (majority)
        if smoothed_regime != self._regime and regime_confidence >= 50:
            self._regime = smoothed_regime
            self._regime_start_ts = now
            self._last_regime_change = now

        regime_duration = now - self._regime_start_ts

        # ── Update state ──
        self._state = {
            'atm_iv': round(atm_iv, 2),
            'iv_skew': round(iv_skew, 4),
            'iv_skew_label': iv_skew_label,
            'term_structure': term_structure,
            'vol_premium': round(vol_premium, 2),
            'iv_rank': round(iv_rank, 1),
            'skew_velocity': round(skew_velocity, 4),
            'iv_velocity': round(iv_velocity, 4),
            'regime': self._regime,
            'regime_confidence': round(regime_confidence, 1),
            'regime_duration_s': round(regime_duration, 0),
            'iv_0dte': round(iv_0dte, 2),
            'iv_next_dte': round(iv_next, 2),
        }

        return self._state

    def _classify_regime(self, iv_skew, term_structure, vol_premium,
                         iv_rank, skew_velocity, iv_velocity):
        """Pure regime classification from vol surface features.

        Returns one of: STRESSED, ELEVATED, NORMAL, COMPLACENT, COMPRESSED
        """
        # Score each dimension (higher = more stressed)
        skew_score = 0
        if iv_skew > 1.15:
            skew_score = 4
        elif iv_skew > 1.10:
            skew_score = 3
        elif iv_skew > 1.05:
            skew_score = 2
        elif iv_skew > 0.98:
            skew_score = 1
        else:
            skew_score = 0  # Call skew = extreme complacency

        term_score = 0
        if term_structure == 'BACKWARDATION':
            term_score = 3  # Stressed: near-term IV > far-term
        elif term_structure == 'FLAT':
            term_score = 1
        else:  # CONTANGO
            term_score = 0  # Calm

        premium_score = 0
        if vol_premium > 20:
            premium_score = 4
        elif vol_premium > 10:
            premium_score = 3
        elif vol_premium > 5:
            premium_score = 2
        elif vol_premium > 2:
            premium_score = 1
        else:
            premium_score = 0

        # Skew velocity bonus (sudden fear spike)
        velocity_bonus = 0
        if skew_velocity > 0.02:  # Skew surging rapidly
            velocity_bonus = 2
        elif skew_velocity > 0.01:  # Skew increasing
            velocity_bonus = 1

        total_score = skew_score + term_score + premium_score + velocity_bonus

        # ── IV compression detection (spring-loaded) ──
        # When IV is at multi-update lows AND vol premium is near zero,
        # this is a vol compression → spring-loaded for explosive move
        is_compressed = (iv_rank < 15 and vol_premium < 3 and
                         len(self._atm_iv_history) >= 30)

        # ── Classification ──
        if total_score >= 9:
            return 'STRESSED'
        elif total_score >= 6:
            return 'ELEVATED'
        elif is_compressed:
            return 'COMPRESSED'
        elif total_score <= 2:
            return 'COMPLACENT'
        else:
            return 'NORMAL'

    def get_state(self):
        """Return current vol surface state dict."""
        return dict(self._state)

    def get_regime(self):
        """Return current vol regime string."""
        return self._regime

    def get_regime_adjustments(self):
        """Return regime-specific signal adjustments for EdgeDetector.

        These are LOGGED as features — not used as multipliers.
        The outcome logger will determine if they're predictive.
        """
        r = self._regime
        if r == 'STRESSED':
            return {
                'vol_regime': r,
                'vol_signal_note': 'Widen detection thresholds — stressed regime = noisier signals',
                'persistence_adjust': -1,  # Lower persistence requirement
                'cooldown_adjust': -0.5,   # Faster cooldowns (more signals allowed)
            }
        elif r == 'ELEVATED':
            return {
                'vol_regime': r,
                'vol_signal_note': 'Slightly widen thresholds — elevated uncertainty',
                'persistence_adjust': 0,
                'cooldown_adjust': -0.3,
            }
        elif r == 'COMPRESSED':
            return {
                'vol_regime': r,
                'vol_signal_note': 'Spring-loaded — next breakout may be explosive',
                'persistence_adjust': 1,   # Higher persistence (wait for confirmation)
                'cooldown_adjust': 0,
            }
        elif r == 'COMPLACENT':
            return {
                'vol_regime': r,
                'vol_signal_note': 'Tight range — only fire on extreme outliers',
                'persistence_adjust': 2,   # Much higher persistence
                'cooldown_adjust': 1.0,    # Slower cooldowns (fewer signals)
            }
        else:  # NORMAL
            return {
                'vol_regime': r,
                'vol_signal_note': 'Normal regime — use standard thresholds',
                'persistence_adjust': 0,
                'cooldown_adjust': 0,
            }

    def get_skew_alert(self):
        """Check for rapid skew changes (fear indicator).

        Returns (alert_type, severity) or (None, 0)
        """
        velocity = self._state.get('skew_velocity', 0)
        iv_velocity = self._state.get('iv_velocity', 0)

        # Rapid put skew increase = sudden fear
        if velocity > 0.015 and self._state.get('iv_skew', 1.0) > 1.05:
            return 'SKEW_SPIKE', min(3, int(velocity / 0.01))

        # Rapid IV spike with already-stressed surface
        if iv_velocity > 1.0 and self._regime in ('STRESSED', 'ELEVATED'):
            return 'IV_SURGE', min(3, int(iv_velocity / 0.5))

        # IV compression breaking (vol expanding from compressed state)
        if (self._regime == 'COMPRESSED' and iv_velocity > 0.5 and
                self._state.get('iv_rank', 50) > 30):
            return 'COMPRESSION_BREAK', 2

        return None, 0
