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
from connectors.hmm_regime import (
    OnlineHMM,
    make_vix_term_hmm,
    build_vix_term_features,
    VIX_RV_WINDOW_OBS,
)
from connectors.kv_estimator import TICKER_ALPHA, ALPHA_DEFAULT, ALPHA_DECAY_C


def _memory_regime_from_alpha(alpha: float) -> str:
    """Yatawara (2026) memory regime classification.

    α < 0.15: LONG_MEMORY  — shocks persist days; pin levels sticky; VIX-like
    α 0.15-0.40: NORMAL    — standard equity behavior (QQQ ≈0.30)
    α > 0.40: SHORT_MEMORY — mean-reverts fast; fade extremes
    """
    if alpha < 0.15:
        return 'LONG_MEMORY'
    if alpha > 0.40:
        return 'SHORT_MEMORY'
    return 'NORMAL'


def _half_life_days_from_alpha(alpha: float) -> float:
    """Solve g(j) = exp[-c·(j^α - 1)] = 0.5 for j.
    j^α = 1 + ln(2)/c  →  j = (1 + ln(2)/c)^(1/α)
    """
    if alpha <= 0:
        return float('inf')
    try:
        return (1.0 + math.log(2.0) / ALPHA_DECAY_C) ** (1.0 / alpha)
    except (OverflowError, ValueError, ZeroDivisionError):
        return float('inf')


class VolSurface:
    """Live volatility surface state machine.

    Fed by zone_update data from schwab_bridge every 5s.
    Provides vol regime classification to EdgeDetector.
    """

    def __init__(self, ticker='QQQ'):
        # Phase 19.5: ticker-bound for memory regime classification.
        # Default QQQ (current single-underlying deployment); pass ticker explicitly
        # if instantiated for SPX/IWM/etc.
        self._ticker = ticker
        self._memory_alpha = TICKER_ALPHA.get(ticker, ALPHA_DEFAULT)
        self._memory_regime = _memory_regime_from_alpha(self._memory_alpha)
        self._memory_half_life_days = _half_life_days_from_alpha(self._memory_alpha)

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
        self._regime_votes = deque(maxlen=12)  # Last 12 updates (~60s) [legacy fallback]

        # ── HMM regime detector (replaces majority voting) ──
        # Profile A: legacy IV-features [iv_rank, iv_skew, vol_premium, vpin]
        self._hmm = OnlineHMM(n_states=3, n_features=4, ema_halflife=100)
        self._vpin_value = 0.5  # updated externally by schwab_bridge

        # ── Phase 20B: parallel VIX-term-structure HMM ──
        # Profile B: Nguyen (2025) §4.2 features [log(VIX), VIX/VVIX,
        # VIX/VIX1D, rv_VIX]. Separated from Profile A to avoid the
        # circularity Nguyen warns about (regime conditioning a signal
        # built from same features). Both HMMs run every observe() call;
        # winner determined offline via outcome ledger.
        self._hmm_vix = make_vix_term_hmm(ema_halflife=100)
        self._vix_history = deque(maxlen=VIX_RV_WINDOW_OBS)
        # External feeders set these (schwab_bridge writes in equity quote handler)
        self._vix_value = None
        self._vvix_value = None
        self._vix1d_value = None
        # Track most recent VIX-HMM state for state emission
        self._vix_hmm_state = None
        self._vix_hmm_probs = [1/3.0, 1/3.0, 1/3.0]
        self._vix_hmm_warm = False

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

        # ── Regime Classification via HMM forward filtering ──
        hmm_result = self._hmm.observe({
            'iv_rank': iv_rank,
            'iv_skew': iv_skew,
            'vol_premium': vol_premium,
            'vpin': self._vpin_value,
        })
        hmm_regime = hmm_result['regime']
        hmm_probs = hmm_result['state_probs']
        regime_confidence = max(hmm_probs) * 100

        # ── Phase 20B: parallel VIX-term-structure HMM ──
        # Only observe when all three inputs available; else carry forward
        # the previous state (or stay at uniform prior on cold start).
        vix_features = build_vix_term_features(
            vix=self._vix_value,
            vvix=self._vvix_value,
            vix1d=self._vix1d_value,
            vix_history=self._vix_history,
        )
        vix_regime = None
        vix_state = None
        if vix_features is not None:
            vix_result = self._hmm_vix.observe(vix_features)
            self._vix_hmm_state = int(vix_result['state'])
            self._vix_hmm_probs = vix_result['state_probs']
            self._vix_hmm_warm = self._hmm_vix.warm
            # Map VIX-HMM state to label (uses same 3-state ordering: low/normal/stress)
            VIX_HMM_LABELS = {0: 'CONTANGO', 1: 'TRANSITION', 2: 'BACKWARDATION'}
            vix_regime = VIX_HMM_LABELS.get(self._vix_hmm_state, 'TRANSITION')
            vix_state = self._vix_hmm_state

        # Legacy fallback: also track votes for comparison logging
        legacy_regime = self._classify_regime(
            iv_skew, term_structure, vol_premium, iv_rank,
            skew_velocity, iv_velocity
        )
        self._regime_votes.append(legacy_regime)

        # Use HMM regime as authoritative
        if hmm_regime != self._regime:
            self._regime = hmm_regime
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
            # HMM state probabilities
            'hmm_prob_low_vol': round(hmm_probs[0], 4),
            'hmm_prob_normal': round(hmm_probs[1], 4),
            'hmm_prob_stressed': round(hmm_probs[2], 4),
            'hmm_state': hmm_result['state'],
            'legacy_regime': legacy_regime,
            # Phase 19.5: Yatawara memory regime (per-ticker, static)
            'memory_regime':         self._memory_regime,
            'memory_alpha':          round(self._memory_alpha, 3),
            'memory_half_life_days': round(self._memory_half_life_days, 1),
            # Phase 20B: VIX-term-structure HMM (parallel; A/B vs IV-features HMM)
            'vix_hmm_regime':           vix_regime,                            # 'CONTANGO'/'TRANSITION'/'BACKWARDATION' or None
            'vix_hmm_state':            vix_state,                             # 0/1/2 or None
            'vix_hmm_warm':             bool(self._vix_hmm_warm),
            'vix_hmm_prob_contango':    round(self._vix_hmm_probs[0], 4),
            'vix_hmm_prob_transition':  round(self._vix_hmm_probs[1], 4),
            'vix_hmm_prob_backwardation': round(self._vix_hmm_probs[2], 4),
            'vix_hmm_inputs_present':   bool(vix_features is not None),
            'vix_value':                self._vix_value,
            'vvix_value':               self._vvix_value,
            'vix1d_value':              self._vix1d_value,
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

    def set_vpin(self, vpin_value):
        """Update VPIN input for HMM. Called by schwab_bridge or l2_worker."""
        self._vpin_value = max(0.0, min(1.0, vpin_value))

    def set_vix_family(self, vix=None, vvix=None, vix1d=None):
        """Update VIX-family spot inputs for the parallel VIX-term HMM.

        Phase 20B (2026-05-01): wired in schwab_bridge._on_equity_quote
        every time $VIX, $VVIX, or $VIX1D ticks. None values are ignored
        so callers can update one symbol at a time.
        """
        if vix is not None and vix > 0:
            self._vix_value = float(vix)
            # Append to rolling history (used for realised-vol-of-VIX feature)
            self._vix_history.append(float(vix))
        if vvix is not None and vvix > 0:
            self._vvix_value = float(vvix)
        if vix1d is not None and vix1d > 0:
            self._vix1d_value = float(vix1d)

    def append_hmm_ab_record(self, log_dir=None):
        """Append one HMM-A/B comparison record to outcome ledger.

        Phase 20B: per-emission record of (IV-HMM regime, VIX-HMM regime,
        future-vol target). Future-vol target is filled offline by a
        post-processor that joins this ledger with the next-day realised
        VIX move.

        File: logs/hmm_ab_outcomes_YYYYMMDD.jsonl
        """
        import json, os
        from datetime import datetime as _dt
        if log_dir is None:
            log_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'logs',
            )
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            return
        today = _dt.now().strftime('%Y%m%d')
        path = os.path.join(log_dir, f'hmm_ab_outcomes_{today}.jsonl')
        rec = {
            'ts': time.time(),
            'iv_hmm_regime': self._state.get('regime'),
            'iv_hmm_state':  self._state.get('hmm_state'),
            'iv_hmm_probs':  [
                self._state.get('hmm_prob_low_vol'),
                self._state.get('hmm_prob_normal'),
                self._state.get('hmm_prob_stressed'),
            ],
            'vix_hmm_regime': self._state.get('vix_hmm_regime'),
            'vix_hmm_state':  self._state.get('vix_hmm_state'),
            'vix_hmm_warm':   self._state.get('vix_hmm_warm', False),
            'vix_hmm_probs':  [
                self._state.get('vix_hmm_prob_contango'),
                self._state.get('vix_hmm_prob_transition'),
                self._state.get('vix_hmm_prob_backwardation'),
            ],
            'vix_inputs_present': self._state.get('vix_hmm_inputs_present', False),
            'vix':   self._vix_value,
            'vvix':  self._vvix_value,
            'vix1d': self._vix1d_value,
            'spot_iv': self._state.get('atm_iv'),
        }
        try:
            with open(path, 'a') as f:
                f.write(json.dumps(rec) + '\n')
        except Exception:
            pass

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
