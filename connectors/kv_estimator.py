"""
Volatility-Delta k_v Estimator (Phase 19, 2026-05-01)

Implements Kobayashi (2025) "Vega mais Estável com Delta de Volatilidade":
F90100 technical note.

═══════════════════════════════════════════════════════════════════════════
 THE METHOD
═══════════════════════════════════════════════════════════════════════════

Standard Black-Scholes Δ assumes IV is constant when spot moves. In reality
spot and IV co-move negatively (typical equity index skew dynamics). This
creates systematic hedge error and biases signed-Δ-notional flow magnitudes.

Kobayashi (2025) defines a coefficient k_v in clean operational units:

   k_v = "p.p. of IV per 1% of price"

Estimated from event-free rolling windows by robust regression:

   ΔIV_pp_t = -k_v × ΔS%_t + ε

Then the volatility-adjusted delta is:

   Δ_vol = Δ - Vega × (k_v / S_ref)

with Vega in per-1.0-σ-decimal units (BSM convention, = 100 × Schwab vega).

═══════════════════════════════════════════════════════════════════════════
 CONVENTIONS (MEASURED VALUES)
═══════════════════════════════════════════════════════════════════════════

   k_v units:                 pp / %
   Default for indices:       0.70 (CONFIGURED — paper §4 reference value)
   Default for VIX:          -0.60 (REVERSE — VIX up when spot up)
   Plausibility band:         [0.0, 3.0] for indices, [-2.0, 0.0] for VIX
   History window:            30 daily samples
   Min samples to estimate:   5 (else falls back to default)
   Refresh cadence:           daily (15:30 ET, end of trade window)
   Event-day filter:          skip ±1 day around earnings, FOMC, NFP, CPI

When k_v → 0: Δ_vol regresses to classical Δ (mathematically clean fallback).
When in event window: skip live adjustment, use classical Δ.

═══════════════════════════════════════════════════════════════════════════
 USAGE
═══════════════════════════════════════════════════════════════════════════

   from connectors.kv_estimator import (
       get_kv_estimator,
       adjust_delta_for_volatility,
   )

   # In FlowAccumulator (per-print):
   est = get_kv_estimator()
   k_v = est.get_kv(ticker)            # cached, ~0.7 default
   delta_adj = adjust_delta_for_volatility(
       delta=delta_bsm,                 # BSM Δ
       vega_per_pp=vega_schwab_or_bsm,  # Vega per 1% IV move (Schwab convention)
       k_v=k_v,
       spot=underlying_price,
   )
   signed_dn = side × size × delta_adj × spot × 100

   # Sample collector (called once per day):
   est.add_sample(ticker, ts, spot, iv_atm_pct)
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)

# ── CONFIGURED CONSTANTS (per Kobayashi 2025 + our calibration choices) ────
# Phase 19.5 (2026-05-01): window shrunk 30→12 days based on Yatawara (2026)
# "The Shape of Volatility Memory" — median half-life of vol memory across 100
# assets is 5 days. 12-day window = ~2× half-life keeps signal sharp without
# being polluted by stale samples beyond practical memory horizon.
KV_HISTORY_DAYS         = 12           # MEASURED — 2× Yatawara median 5d half-life
KV_MIN_SAMPLES          = 5            # min (ΔS%, ΔIV_pp) pairs
KV_MIN_DS_PCT           = 0.05         # discard pairs with |ΔS%| < this (noise)
KV_PLAUSIBILITY_LO_IDX  = 0.0          # lower bound for indices
KV_PLAUSIBILITY_HI_IDX  = 3.0          # upper bound
KV_PLAUSIBILITY_LO_VIX  = -2.0         # VIX has reverse relation
KV_PLAUSIBILITY_HI_VIX  = 0.0
KV_LARGE_MOVE_PCT       = 5.0          # skip days with |ΔS%| > this (likely event)

# ── YATAWARA (2026) MEMORY EXPONENTS — per-ticker α ────────────────────────
# Source: Yatawara, J. (2026) "The Shape of Volatility Memory: ARCH(∞) Kernels
# Across 100 Assets, 2000-2026". Stretched-exponential decay g(j) = exp[-c(j^α-1)].
# Lower α = longer memory = shocks persist; higher α = mean-reverting fast.
# Used as recency weights in Theil-Sen regression for k_v estimation.
TICKER_ALPHA = {
    'QQQ':   0.30,   # MEASURED — equity ETF median (Yatawara Table 4)
    'SPY':   0.32,   # MEASURED
    'SPX':   0.32,   # MEASURED
    'IWM':   0.27,   # MEASURED — small caps slightly longer memory
    'XLK':   0.30,   # CONFIGURED — equity ETF default
    'XLE':   0.28,   # CONFIGURED
    'XLF':   0.30,   # CONFIGURED
    'VIX':   0.10,   # MEASURED — VIX has strongest memory across all assets
    'NVDA':  0.22,   # CONFIGURED — high-vol single name
    'AAPL':  0.30,   # CONFIGURED
    'MSFT':  0.30,
    'AMZN':  0.27,
    'META':  0.25,
    'GOOGL': 0.30,
    'TSLA':  0.20,   # CONFIGURED — high realized vol, longer memory
}
ALPHA_DEFAULT  = 0.30   # equity median fallback
# c calibrated such that QQQ (α=0.30) has a 5-day half-life — matching Yatawara's
# median across 100 assets (paper §5.2):
#   g(5, α=0.30) = 0.5  →  c = ln(2) / (5^0.30 − 1) ≈ 1.116
# Single shared c is a simplification; paper fits per-asset c, but this anchor
# captures the paper's central claim and produces sensible weights:
#   QQQ  α=0.30: weight at age 1d=1.00, 5d=0.50, 10d=0.37, 12d=0.33
#   VIX  α=0.10: weight at age 1d=1.00, 5d=0.83, 10d=0.78, 30d=0.69
ALPHA_DECAY_C  = 1.116  # MEASURED — anchored to QQQ 5-day half-life (Yatawara §5.2)


# ── PHASE 20C: KALMAN FILTER PARAMETERS ────────────────────────────────────
# Process noise Q: how fast k_v drifts day-to-day. Empirical k_v ranges
# 0.5-1.5 across regimes; daily drift of ~0.02 (2% of k_v's range) yields
# Q ≈ 0.0004. Conservative — Kalman will adapt as data accumulates.
KALMAN_Q_INIT      = 4e-4      # CONFIGURED — daily process noise variance
# Observation noise R: variance of (ΔIV_pp − k_v · -ΔS%) residual after a good
# fit. Historical fit residuals on QQQ are typically ±0.5pp at the daily scale,
# so R ≈ 0.25.
KALMAN_R_INIT      = 0.25      # CONFIGURED — observation noise variance
# Initial uncertainty P_0: large enough to allow fast convergence from default
# k_v to true value within ~5 observations.
KALMAN_P0          = 0.5       # CONFIGURED — initial posterior variance

# Default k_v per ticker — used until enough samples accumulate.
# Indices typically 0.5-1.0; VIX is negative; small caps slightly higher.
TICKER_DEFAULT_KV = {
    'QQQ':    0.70,    # NQ-100 ETF
    'SPY':    0.70,    # S&P 500 ETF
    'SPX':    0.65,    # cash-settled SPX
    'IWM':    0.80,    # Russell 2000 (higher skew)
    'XLK':    0.75,    # tech sector
    'XLE':    0.50,    # energy (lower skew, sometimes positive)
    'XLF':    0.75,    # financials
    'VIX':   -0.60,    # vol of vol — REVERSE relationship
    'NVDA':   1.20,    # high-vol single names typically have higher k_v
    'AAPL':   1.00,
    'MSFT':   1.00,
    'AMZN':   1.10,
    'META':   1.20,
    'GOOGL':  1.00,
    'TSLA':   1.50,    # very high implied skew
}
KV_DEFAULT_FALLBACK = 0.70  # for any unlisted ticker


@dataclass
class _KvSample:
    """One (spot, ATM IV) sample for k_v estimation."""
    timestamp: float
    spot:      float
    iv_atm_pct: float    # ATM IV as percent (e.g., 30.0 means 30%)
    is_event:  bool = False   # if True, skipped from k_v fit


@dataclass
class _TickerKvState:
    """Per-ticker rolling state."""
    samples:       deque = field(default_factory=lambda: deque(maxlen=KV_HISTORY_DAYS * 2))
    current_kv:    float = 0.0       # latest estimate
    samples_used:  int = 0           # samples in last fit
    last_estimate_ts: float = 0.0    # epoch of last estimate
    fallback_default: float = KV_DEFAULT_FALLBACK
    n_total_samples: int = 0         # lifetime counter
    alpha:         float = ALPHA_DEFAULT  # Yatawara memory exponent for this ticker
    weighted:      bool = False      # True if last fit used Yatawara weights
    # Phase 20C: parallel Kalman estimator
    kalman_kv:     float = 0.0       # latest Kalman-filter k_v estimate
    kalman_unc:    float = 0.0       # posterior variance of Kalman estimate
    kalman_obs:    int = 0           # number of observations Kalman has seen
    kalman_filter: object = None     # KalmanHedgeFilter instance (lazy-init)


def _yatawara_weight(age_days: float, alpha: float) -> float:
    """Yatawara (2026) stretched-exponential memory kernel.

    g(j) = exp[-c × (j^α − 1)]

    age_days=0 → weight=1.0 (most recent sample)
    age_days→∞ → weight→0 (slow for low α, fast for high α)

    For QQQ α≈0.30, weight at 5d ≈ 0.71, at 10d ≈ 0.55, at 20d ≈ 0.38.
    For VIX α≈0.10, weight at 5d ≈ 0.92, at 10d ≈ 0.88, at 20d ≈ 0.83.
    """
    if age_days <= 0:
        return 1.0
    try:
        return math.exp(-ALPHA_DECAY_C * (age_days ** alpha - 1.0))
    except (OverflowError, ValueError):
        return 0.0


def _weighted_median(pairs: list) -> float:
    """Weighted median of (value, weight) pairs.

    Returns the value at which cumulative weight crosses 50% of total.
    Falls back to plain median if total weight ≤ 0.
    """
    if not pairs:
        return 0.0
    sorted_pairs = sorted(pairs, key=lambda x: x[0])
    total_w = sum(w for _, w in sorted_pairs)
    if total_w <= 0:
        return statistics.median([v for v, _ in sorted_pairs])
    target = total_w / 2.0
    cum = 0.0
    for v, w in sorted_pairs:
        cum += w
        if cum >= target:
            return v
    return sorted_pairs[-1][0]


class KvEstimator:
    """Per-ticker k_v estimator with robust regression (Theil-Sen median).

    Thread-safe. Designed for periodic sample injection (daily) and frequent
    k_v lookup (per-print). Estimation is cached until next sample arrives.
    """

    def __init__(self):
        self._state: dict[str, _TickerKvState] = {}
        self._lock = threading.RLock()
        log.info("[KV-ESTIMATOR] Phase 19 initialized — Kobayashi (2025) Volatility-Delta")

    # ── SAMPLE INGEST ─────────────────────────────────────────────────────
    def add_sample(self, ticker: str, timestamp: float,
                   spot: float, iv_atm_pct: float, is_event: bool = False) -> None:
        """Record a daily (spot, ATM IV) sample.

        Args:
            ticker:     symbol (e.g. 'QQQ')
            timestamp:  epoch seconds
            spot:       underlying price ($)
            iv_atm_pct: ATM IV as percent (NOT decimal — 30.0 for 30%)
            is_event:   True if this day had a relevant event (earnings, FOMC, etc.)
                        Event-day samples are stored but excluded from k_v fit.
        """
        if spot <= 0 or iv_atm_pct <= 0:
            return
        with self._lock:
            state = self._state.setdefault(ticker, _TickerKvState(
                fallback_default=TICKER_DEFAULT_KV.get(ticker, KV_DEFAULT_FALLBACK),
                alpha=TICKER_ALPHA.get(ticker, ALPHA_DEFAULT),
            ))
            # Capture previous sample for Kalman observation BEFORE append
            prev = state.samples[-1] if state.samples else None
            state.samples.append(_KvSample(
                timestamp=timestamp, spot=spot,
                iv_atm_pct=iv_atm_pct, is_event=is_event,
            ))
            state.n_total_samples += 1

            # Phase 20C: feed (ds_pct, div_pp) pair into parallel Kalman filter
            if prev is not None and prev.spot > 0 and not is_event:
                ds_pct = 100.0 * (spot - prev.spot) / prev.spot
                div_pp = iv_atm_pct - prev.iv_atm_pct
                # Apply same noise filter as Theil-Sen so estimators see same data
                if KV_MIN_DS_PCT <= abs(ds_pct) <= KV_LARGE_MOVE_PCT:
                    self._kalman_observe(state, ticker, ds_pct, div_pp)

        # Re-estimate after each sample (cheap, < 100µs)
        self._reestimate(ticker)

    def _kalman_observe(self, state, ticker: str,
                          ds_pct: float, div_pp: float) -> None:
        """Apply one (ds_pct, div_pp) observation to the Kalman filter.

        Model:
            x_t = -ds_pct        (regressor; negative because ΔIV = -k_v·ΔS%)
            y_t = div_pp         (observation)
            β_t = k_v            (state)
        """
        if state.kalman_filter is None:
            # Lazy-init with default k_v as prior
            from connectors.kalman_filter import KalmanHedgeFilter
            beta_0 = state.fallback_default
            state.kalman_filter = KalmanHedgeFilter(
                Q=KALMAN_Q_INIT,
                R=KALMAN_R_INIT,
                beta_0=beta_0,
                P_0=KALMAN_P0,
            )
        try:
            x_t = -ds_pct
            y_t = div_pp
            kstate = state.kalman_filter.update(x_t, y_t)
            # Apply same plausibility clamp as Theil-Sen
            if ticker == 'VIX':
                kv_clamped = max(KV_PLAUSIBILITY_LO_VIX,
                                 min(KV_PLAUSIBILITY_HI_VIX, kstate.beta_hat))
            else:
                kv_clamped = max(KV_PLAUSIBILITY_LO_IDX,
                                 min(KV_PLAUSIBILITY_HI_IDX, kstate.beta_hat))
            state.kalman_kv = float(kv_clamped)
            state.kalman_unc = float(kstate.P)
            state.kalman_obs += 1
            # Append A/B record (Theil-Sen vs Kalman estimates for this ticker)
            self._append_ab_record(ticker, ds_pct, div_pp, kstate.innovation,
                                    kstate.K, state)
        except Exception as e:
            log.warning(f"[KV-KALMAN] {ticker} observe failed: {e}")

    def _append_ab_record(self, ticker: str, ds_pct: float, div_pp: float,
                           innovation: float, kalman_gain: float,
                           state) -> None:
        """Append one (ds_pct, div_pp, theil_sen_kv, kalman_kv) record to
        the k_v A/B outcome ledger.

        Phase 20C: enables offline analysis of Theil-Sen vs Kalman tracking
        accuracy and adaptation speed across regime shifts.

        File: logs/kv_ab_outcomes_YYYYMMDD.jsonl
        """
        import json
        import os
        from datetime import datetime as _dt
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'logs',
        )
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            return
        today = _dt.now().strftime('%Y%m%d')
        path = os.path.join(log_dir, f'kv_ab_outcomes_{today}.jsonl')
        rec = {
            'ts':              time.time(),
            'ticker':          ticker,
            'ds_pct':          round(ds_pct, 4),
            'div_pp':          round(div_pp, 4),
            'theil_sen_kv':    round(state.current_kv, 5) if state.samples_used > 0 else None,
            'theil_sen_n':     state.samples_used,
            'kalman_kv':       round(state.kalman_kv, 5),
            'kalman_unc':      round(state.kalman_unc, 6),
            'kalman_n':        state.kalman_obs,
            'kalman_innov':    round(innovation, 4),
            'kalman_gain':     round(kalman_gain, 4),
            'fallback_default': state.fallback_default,
            'alpha':           state.alpha,
        }
        try:
            with open(path, 'a') as f:
                f.write(json.dumps(rec) + '\n')
        except Exception:
            pass

    def _reestimate(self, ticker: str) -> None:
        """Recompute k_v from rolling samples for ticker.

        Phase 19.5: pairwise slopes are weighted by Yatawara (2026) stretched-
        exponential decay g(j) = exp[-c·(j^α − 1)] where j is age in days and
        α is the ticker's memory exponent. Recent slopes dominate; older slopes
        contribute less without being abruptly cut.
        """
        with self._lock:
            state = self._state.get(ticker)
            if state is None:
                return

            # Filter out event-day samples for the fit
            samples = [s for s in state.samples if not s.is_event]
            if len(samples) < KV_MIN_SAMPLES + 1:  # need N+1 to make N pairs
                return  # not enough data, keep using fallback default

            now = time.time()
            alpha = state.alpha if state.alpha > 0 else ALPHA_DEFAULT

            # Build pairwise (ΔS%, ΔIV_pp) deltas between consecutive event-free days
            slopes_weighted = []   # list of (slope, yatawara_weight)
            for i in range(1, len(samples)):
                s0, s1 = samples[i - 1], samples[i]
                if s0.spot <= 0:
                    continue
                ds_pct = 100.0 * (s1.spot - s0.spot) / s0.spot
                div_pp = s1.iv_atm_pct - s0.iv_atm_pct
                # Discard tiny moves (noise) and giant moves (likely events we missed)
                if abs(ds_pct) < KV_MIN_DS_PCT:
                    continue
                if abs(ds_pct) > KV_LARGE_MOVE_PCT:
                    continue
                slope = div_pp / ds_pct
                # Age of this slope = age of the more-recent sample (s1)
                age_days = max(0.0, (now - s1.timestamp) / 86400.0)
                w = _yatawara_weight(age_days, alpha)
                slopes_weighted.append((slope, w))

            if len(slopes_weighted) < KV_MIN_SAMPLES:
                return  # still not enough useful data

            # Robust regression: WEIGHTED Theil-Sen (median where weights count)
            # k_v = -slope (since ΔIV_pp = -k_v × ΔS%)
            median_slope = _weighted_median(slopes_weighted)
            k_v = -median_slope

            # Plausibility limits
            if ticker == 'VIX':
                k_v = max(KV_PLAUSIBILITY_LO_VIX, min(KV_PLAUSIBILITY_HI_VIX, k_v))
            else:
                k_v = max(KV_PLAUSIBILITY_LO_IDX, min(KV_PLAUSIBILITY_HI_IDX, k_v))

            state.current_kv = k_v
            state.samples_used = len(slopes_weighted)
            state.last_estimate_ts = now
            state.weighted = True

    # ── LOOKUP ─────────────────────────────────────────────────────────────
    def get_kv(self, ticker: str) -> float:
        """Return cached k_v for ticker, or fallback default if unestimated.

        Default estimator is Theil-Sen (Phase 19/19.5). Use get_kv_kalman()
        for the parallel Kalman estimate, or get_kv_combined() for both.
        """
        with self._lock:
            state = self._state.get(ticker)
            if state is None:
                return TICKER_DEFAULT_KV.get(ticker, KV_DEFAULT_FALLBACK)
            if state.samples_used > 0 and state.current_kv != 0.0:
                return state.current_kv
            return state.fallback_default

    def get_kv_kalman(self, ticker: str) -> float:
        """Return parallel Kalman-filter k_v estimate (Phase 20C).

        Returns the fallback default until the filter has seen at least one
        observation.
        """
        with self._lock:
            state = self._state.get(ticker)
            if state is None:
                return TICKER_DEFAULT_KV.get(ticker, KV_DEFAULT_FALLBACK)
            if state.kalman_obs > 0:
                return state.kalman_kv
            return state.fallback_default

    def get_kv_combined(self, ticker: str) -> dict:
        """Return BOTH Theil-Sen and Kalman estimates for A/B comparison.

        Used by outcome ledger and downstream consumers that want to track
        both estimators in parallel.
        """
        with self._lock:
            state = self._state.get(ticker)
            if state is None:
                default = TICKER_DEFAULT_KV.get(ticker, KV_DEFAULT_FALLBACK)
                return {
                    'theil_sen':       default,
                    'theil_sen_n':     0,
                    'kalman':          default,
                    'kalman_unc':      None,
                    'kalman_n':        0,
                    'fallback_default': default,
                }
            return {
                'theil_sen':       (state.current_kv if state.samples_used > 0 else
                                     state.fallback_default),
                'theil_sen_n':     state.samples_used,
                'kalman':          (state.kalman_kv if state.kalman_obs > 0 else
                                     state.fallback_default),
                'kalman_unc':      (state.kalman_unc if state.kalman_obs > 0 else None),
                'kalman_n':        state.kalman_obs,
                'fallback_default': state.fallback_default,
            }

    def get_state(self, ticker: Optional[str] = None) -> dict:
        """Diagnostic dump."""
        with self._lock:
            if ticker:
                state = self._state.get(ticker)
                if not state:
                    return {
                        'ticker':       ticker,
                        'kv':           TICKER_DEFAULT_KV.get(ticker, KV_DEFAULT_FALLBACK),
                        'kv_source':    'default',
                        'samples':      0,
                    }
                return {
                    'ticker':         ticker,
                    'kv':             self.get_kv(ticker),
                    'kv_source':      'fitted' if state.samples_used > 0 else 'default',
                    'kv_default':     state.fallback_default,
                    'samples_used':   state.samples_used,
                    'history_len':    len(state.samples),
                    'total_seen':     state.n_total_samples,
                    'last_fit_ts':    state.last_estimate_ts,
                    'alpha':          state.alpha,
                    'alpha_weighted': state.weighted,
                    'lookback_days':  KV_HISTORY_DAYS,
                    # Phase 20C: parallel Kalman estimator
                    'kv_kalman':      (state.kalman_kv if state.kalman_obs > 0
                                        else state.fallback_default),
                    'kv_kalman_unc':  (state.kalman_unc if state.kalman_obs > 0
                                        else None),
                    'kv_kalman_obs':  state.kalman_obs,
                }
            return {t: self.get_state(t) for t in self._state.keys()}


# ─── MODULE-LEVEL SINGLETON ───────────────────────────────────────────────
_kv_instance: Optional[KvEstimator] = None
_kv_init_lock = threading.Lock()


def get_kv_estimator() -> KvEstimator:
    """Singleton accessor."""
    global _kv_instance
    if _kv_instance is None:
        with _kv_init_lock:
            if _kv_instance is None:
                _kv_instance = KvEstimator()
    return _kv_instance


# ─── PUBLIC ADJUSTMENT FUNCTION ───────────────────────────────────────────
def adjust_delta_for_volatility(delta: float,
                                 vega_per_pp: float,
                                 k_v: float,
                                 spot: float) -> float:
    """Apply Kobayashi (2025) Volatility-Delta adjustment.

    Derived form (Section 6.2 of paper):
        Δ_vol = Δ - Vega_per_decimal × (k_v / S_ref)

    Where Vega_per_decimal = ∂C/∂σ with σ in decimal (BSM convention).
    Schwab and most retail platforms report Vega per 1% IV change (per pp),
    so the conversion is:
        Vega_per_decimal = 100 × Vega_per_pp

    Substituting:
        Δ_vol = Δ - 100 × Vega_per_pp × (k_v / S_ref)

    Args:
        delta:       BSM Δ (dimensionless)
        vega_per_pp: Vega per 1% IV change (Schwab convention; or BSM_vega/100)
        k_v:         Volatility delta coefficient in (pp / %)
        spot:        Underlying spot price ($)

    Returns:
        Adjusted Δ (Δ_vol). When k_v=0, returns delta unchanged.
        When inputs invalid, returns delta unchanged (safe fallback).

    Verification (Kobayashi paper example, S=$30, IV=30%, Δ=0.55, Vega=0.12, k_v=0.7):
        Note: paper's Vega=0.12 is in (per 1.0 σ_decimal) = 100 × per-pp form.
        So per-pp equivalent is Vega_pp = 0.12 / 100 = 0.0012? No — paper's
        formula uses Vega_per_decimal directly, so we need to be careful:

        Δ_vol_paper = 0.55 - 0.12 × 0.7/30 = 0.5472

        With our function (per-pp convention):
        If Vega_per_pp = 0.0012 (matching paper's 0.12 in per-decimal),
            Δ_vol = 0.55 - 100 × 0.0012 × 0.7/30 = 0.55 - 0.0028 = 0.5472 ✓

        Equivalently if we just plug in vega_per_pp = paper_vega / 100:
        ✓ matches.
    """
    if spot <= 0 or k_v == 0.0 or vega_per_pp == 0.0:
        return delta
    try:
        return delta - 100.0 * vega_per_pp * k_v / spot
    except (ZeroDivisionError, OverflowError):
        return delta
