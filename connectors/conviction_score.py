"""Composite Conviction Score (CCS) — the directional-bias framework engine.

This module synthesizes the three primary terminal panes (AI Panel, Options
Flow, MM Attribution) into a single numerical score that gates trade entries.

Architecture
============
Runs as a background thread on 5s cadence (matches zone_emit). Every cycle:

  1. Pulls live state from:
     - greek_surface          → hp_gamma_shares_1pct (forced hedge flow)
     - wall_signals           → regime, gamma_flip, walls, recent crosses
     - flow_accumulator       → cum_signed_0dte, cum_signed_all per ticker
     - mm_attribution         → refill_ratio + per-exchange γ posting
     - alert_engine           → recent flow_alert events for cross-asset confirm

  2. Computes seven CCS components (each 0–100):
        a) amplification_factor — |hp_γ_shares_1pct| / median_5min_volume
                                  → the MASTER variable. < 1 = regime is
                                  noise, > 3 = regime is dominant.
        b) regime_alignment     — flow direction vs regime mechanic (long-γ
                                  damps, short-γ amplifies)
        c) distance_to_flip     — pts/% from gamma_flip (closer = unstable)
        d) flow_quality         — aggressor% × concentration × magnitude_ratio
        e) mm_signature         — refill_ratio + per-exchange concentration
        f) time_of_day          — windowed multiplier (open/chop/lunch/close)
        g) cross_asset_confirm  — count of same-direction tickers in alerts

  3. Detects anti-setups (binary skip flags):
     - spot within 0.2% of gamma_flip (regime fragile)
     - amp_factor < 1.0 (regime noise)
     - wall just_crossed_age > 30min without fresh alert (late)
     - Friday post-14:30 (weekly expiration distortion)
     - Pinning regime (spot stuck at wall > 2hr)

  4. Outputs:
     - composite score 0–100
     - direction: 'BULL' | 'BEAR' | 'NEUTRAL'
     - position size recommendation: FULL / HALF / QUARTER / PASS / REVERSE
     - per-component breakdown for transparency
     - anti-setup flags array
     - regime transition watch (when spot < 0.5% from flip)

  5. Exposes via:
     - get_state(ticker) for REST consumption
     - emits 'conviction_update' socketio event

Sign convention
---------------
hp_gamma_shares_1pct > 0  →  dealers must BUY shares on +1% spot move
                              In long-γ regime: dampening (BUY when price up,
                              SELL when price down — counter-trend).
                              In short-γ regime: amplifying (BUY when up,
                              SELL when down — same direction = bigger swings).

Direction recommendation logic
------------------------------
  Long-γ regime + premium flow bullish + spot inside band  → FADE  (dampening
  fights the flow)
  Long-γ regime + premium flow bullish + spot above call_wall  →  FOLLOW
  (above the dampening band, dealers turn into chasers)
  Short-γ regime + premium flow bullish  →  FOLLOW  (amplification)
  Short-γ regime + premium flow bearish  →  FOLLOW  (amplification)

This is the structural inversion the AI Panel alone cannot provide.

CONFIGURED constants (no magic-number tuning)
---------------------------------------------
AMP_FACTOR_REGIME_DOMINANT   = 3.0    (dealer flow > 3× organic = structural)
AMP_FACTOR_REGIME_BACKGROUND = 1.0    (dealer flow < organic = noise)
DISTANCE_TO_FLIP_FRAGILE_PCT = 0.002  (within 0.2% = anti-setup)
DISTANCE_TO_FLIP_ROBUST_PCT  = 0.015  (50pts on QQQ ≈ 1.5%)
PINNING_DURATION_HRS         = 2.0    (stuck-at-wall threshold)
QQQ_BASELINE_5MIN_VOLUME     = 5_000_000  (CONFIGURED — typical RTH 5-min QQQ
                                          share volume = ADV / 78 bars/day.
                                          rolling 5-min measurement supersedes
                                          this when sufficient samples landed.)
SCORE_FULL_SIZE              = 75
SCORE_HALF_SIZE              = 60
SCORE_QUARTER_SIZE           = 45
SCORE_REVERSE_TRADE          = 25
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# DST-aware ET clock. Falls back to a hardcoded -4h offset only if the
# system zoneinfo db isn't available (e.g. minimal Alpine container).
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo('America/New_York')
except Exception:
    _ET_TZ = None

def _now_et_minute_of_day() -> int:
    """Returns minutes-since-ET-midnight, DST-aware.

    The previous implementation used `datetime.utcnow().hour - 4` which
    silently shifts every time-of-day window by one hour for the ~5 months
    EST is active (Nov–Mar) — open scoring fired before market open, close
    scoring fired before the actual close.
    """
    if _ET_TZ is not None:
        n = datetime.now(_ET_TZ)
        return n.hour * 60 + n.minute
    # Fallback: assume EDT — same bug as before but only on systems that
    # can't load zoneinfo. Surfaces in logs so the operator notices.
    log.warning("[CCS] zoneinfo unavailable — falling back to hardcoded EDT offset")
    n = datetime.utcnow()
    return ((n.hour - 4) % 24) * 60 + n.minute

def _now_et_weekday() -> int:
    if _ET_TZ is not None:
        return datetime.now(_ET_TZ).weekday()
    return datetime.utcnow().weekday()

log = logging.getLogger(__name__)

# ── Constants (CONFIGURED — see module docstring for derivations) ───────────
AMP_FACTOR_REGIME_DOMINANT   = 3.0
AMP_FACTOR_REGIME_STRUCTURAL = 2.0
AMP_FACTOR_REGIME_CONTRIB    = 1.0
DISTANCE_TO_FLIP_FRAGILE_PCT = 0.002
DISTANCE_TO_FLIP_ROBUST_PCT  = 0.015
PINNING_DURATION_HRS         = 2.0

QQQ_BASELINE_5MIN_VOLUME     = 5_000_000   # ~typical RTH 5-min QQQ volume (1×ADV/78)

# Smoothing for hp_gamma — Schwab streams produce volatile dn_gamma readings.
# A 30-second EMA prevents amp_factor from flickering across regime thresholds
# every compute cycle.
HP_GAMMA_SMOOTHING_HALFLIFE_S = 30.0

SCORE_FULL_SIZE              = 75
SCORE_HALF_SIZE              = 60
SCORE_QUARTER_SIZE           = 45
SCORE_REVERSE_TRADE          = 25

# Component weights (sum need not = 100; we normalize by sum)
# Phase 9 (2026-04-30): rebalanced to make room for 6 intel signals from
# the new panel suite (Sweep / Pin / Hedge FC / SPX-vs-QQQ Div / Vol Regime /
# Wing Tracker). Original weights in comments. Total weight unchanged at 110.
W_REGIME_ALIGN     = 22   # was 30 — partially superseded by VOL_REGIME alignment
W_DISTANCE_TO_FLIP = 10   # was 15 — augmented by warehouse_quality multiplier
W_FLOW_QUALITY     = 15   # was 20 — sweep + wing now provide finer-grain signal
W_MM_SIGNATURE     = 10   # was 15 — warehouse quality covers most of this
W_TIME_OF_DAY      = 8    # was 10
W_CROSS_ASSET      = 5    # was 10 — replaced by W_INTEL_SPXQQQ for QQQ-specific
W_MISPRICING       = 8    # was 10 — Improvement #1
# ── Intel signal components (Phase 9) ──
# 2026-05-05: hedge_forecaster directional weight ZEROED.
# Empirical audit (n=2,146 paired ledger entries) showed:
#   - sign_match: 53.3% vs majority-class baseline 62.7% = -9.4% (WORSE than dumb)
#   - calibration_ratio median: 0.002 (forecast 500x too large)
#   - Pearson(forecast, observed): r = -0.01 (zero correlation)
# The model has NO predictive edge over base rate. Keeping its weight at 8
# (the "strongest forward signal" slot) was poisoning every conviction call.
# Both the socket emit (schwab_bridge.py) and REST endpoint (server.py) were
# stripped 2026-05-04, but this conviction-score consumer was missed.
# When/if a real edge model replaces the dealer-hedging hypothesis, restore
# this weight to the validated value.
W_INTEL_HEDGE_FC   = 0    # was 8 — hedge_forecaster has zero edge, audit failed
W_INTEL_PIN        = 6    # pin_convergence pin pull strength + alignment
W_INTEL_VOL_REGIME = 5    # vix_term_structure regime alignment with trade direction
W_INTEL_SPXQQQ     = 5    # spx_qqq_divergence verdict
# 2026-05-05: sweep direction weight ZEROED for the same reason — directional
# prediction had 41.7% aggregate hit rate (audit n=15,902 cleaned sweeps,
# 24% fake detections, 100% VIX fail). Sweep events stay descriptive but
# stop influencing conviction directional bias.
W_INTEL_SWEEP      = 0    # was 4 — sweep direction has zero edge over base rate
W_INTEL_WING       = 4    # wing_tracker regime + wing-side alignment
TOTAL_W = (W_REGIME_ALIGN + W_DISTANCE_TO_FLIP + W_FLOW_QUALITY
           + W_MM_SIGNATURE + W_TIME_OF_DAY + W_CROSS_ASSET
           + W_MISPRICING
           + W_INTEL_HEDGE_FC + W_INTEL_PIN + W_INTEL_VOL_REGIME
           + W_INTEL_SPXQQQ + W_INTEL_SWEEP + W_INTEL_WING)

# Warehouse-quality multiplier on distance_to_flip + pin scores.
# When dealer commitment at the key strike (flip / pin / wall) is COMMITTED
# (high posted_time × catch_rate), the regime is more reliable → multiplier
# boosts dist_score. If PHANTOM, regime is paper-thin → multiplier dampens.
WAREHOUSE_BOOST_MAX     = 1.20   # CONFIGURED — strongest commitment → +20% dist score
WAREHOUSE_PENALTY_MIN   = 0.80   # CONFIGURED — phantom-only nearby walls → -20% dist score


@dataclass
class _CCSState:
    """Per-ticker conviction snapshot."""
    ticker: str
    ts: float
    spot: float = 0.0
    score: float = 0.0
    direction: str = 'NEUTRAL'      # 'BULL' | 'BEAR' | 'NEUTRAL'
    size_recommendation: str = 'PASS'  # 'FULL' | 'HALF' | 'QUARTER' | 'PASS' | 'REVERSE'
    amp_factor: float = 0.0
    regime: str = 'unknown'
    regime_class: str = 'background'  # 'dominant'|'structural'|'contributory'|'background'
    components: dict = field(default_factory=dict)
    anti_setups: list = field(default_factory=list)
    regime_transition_watch: bool = False
    distance_to_flip_pct: float = 0.0
    rationale: str = ''
    # Smoothing state (per-ticker EMA)
    hp_gamma_smoothed: float = 0.0
    hp_gamma_last_ts: float = 0.0


class ConvictionScorer:
    """Background scorer — runs every 5s for QQQ (extend later for SPY)."""

    def __init__(self, socketio, tickers: Optional[list] = None,
                 cadence_sec: float = 5.0):
        self._sio = socketio
        self._tickers = tickers or ['QQQ']
        self._cadence = cadence_sec
        self._state: dict[str, _CCSState] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # 5-min rolling volume tracker per ticker (deque of (ts, lots))
        self._vol_history: dict[str, deque] = {t: deque(maxlen=600)
                                                for t in self._tickers}

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='conviction-scorer')
        self._thread.start()
        log.info(f"[CCS] started — tickers={self._tickers} cadence={self._cadence}s")

    def stop(self):
        self._running = False

    # ── Public state getters ────────────────────────────────────────────────
    def get_state(self, ticker: str) -> dict:
        with self._lock:
            st = self._state.get(ticker)
            if st is None:
                return {'ticker': ticker, 'ts': 0, 'score': 0,
                        'direction': 'NEUTRAL', 'rationale': 'no_state'}
            return self._serialize(st)

    def get_all_states(self) -> dict:
        with self._lock:
            return {t: self._serialize(st) for t, st in self._state.items()}

    @staticmethod
    def _serialize(st: _CCSState) -> dict:
        return {
            'ticker': st.ticker, 'ts': st.ts, 'spot': st.spot,
            'score': round(st.score, 1),
            'direction': st.direction,
            'size_recommendation': st.size_recommendation,
            'amp_factor': round(st.amp_factor, 3),
            'regime': st.regime, 'regime_class': st.regime_class,
            'distance_to_flip_pct': round(st.distance_to_flip_pct, 4),
            'components': st.components,
            'anti_setups': list(st.anti_setups),
            'regime_transition_watch': st.regime_transition_watch,
            'rationale': st.rationale,
        }

    # ── Equity volume ingestion (call from schwab_bridge equity quote handler)
    def feed_equity_print(self, ticker: str, last_size: int, ts_sec: float):
        """Track per-print volume to compute rolling 5-min totals.
        Called from _on_equity_quote whenever last_size > 0.
        """
        if ticker not in self._vol_history:
            return
        try:
            self._vol_history[ticker].append((float(ts_sec), int(last_size)))
        except Exception as _e:
            log.debug(f"[CCS] feed_equity_print err for {ticker}: {_e}")

    def _rolling_5min_volume(self, ticker: str) -> int:
        """Sum of last_size in the last 5 minutes. 0 if we have no data —
        caller falls back to the configured baseline."""
        dq = self._vol_history.get(ticker)
        if not dq:
            return 0
        cutoff = time.time() - 300.0
        # Clean stale + sum
        total = 0
        for ts, sz in dq:
            if ts >= cutoff:
                total += sz
        return total

    # ── Main loop ───────────────────────────────────────────────────────────
    def _loop(self):
        while self._running:
            try:
                for t in self._tickers:
                    self._compute(t)
            except Exception as e:
                log.warning(f"[CCS] compute err: {e}", exc_info=True)
            time.sleep(self._cadence)

    def _compute(self, ticker: str):
        """Compute CCS for one ticker. Stores + emits if changed materially."""
        # Lazy imports keep startup safe even if modules aren't loaded yet
        try:
            from background_engine import schwab_bridge as _sb
            from connectors import wall_signals as _ws
            from connectors.flow_accumulator import get_accumulator
            from connectors import mm_attribution as _mma
        except Exception as e:
            log.debug(f"[CCS] import deferred: {e}")
            return

        # ── 1. Hedge pressure → amplification factor (master variable) ─────
        gs = getattr(_sb, '_greek_surface', None)
        spot = float(getattr(_sb, '_latest_qqq', 0.0) or 0.0)
        if gs is None or spot <= 0:
            return

        try:
            hp_state = gs.export_hedge_pressure(spot) or {}
        except Exception as e:
            log.debug(f"[CCS] hp export err: {e}")
            return

        totals = hp_state.get('totals', {}) or {}
        hp_gamma_shares_raw = float(totals.get('hp_gamma_shares_1pct', 0.0) or 0.0)

        # ── EMA smoothing on hp_gamma_shares (BUG 3 fix) ──
        # Schwab streams cause dn_gamma to flicker as OI repopulates. Without
        # smoothing, amp_factor oscillates across regime thresholds within a
        # single 5s tick. 30s halflife = α ≈ 0.115 per 5s tick.
        # Lock-protected read so concurrent get_state() callers see a
        # consistent snapshot and a future second writer can't tear the
        # dataclass fields we're snapping into local vars below.
        with self._lock:
            prev = self._state.get(ticker)
            prev_hp_last_ts = prev.hp_gamma_last_ts if prev else 0.0
            prev_hp_smoothed = prev.hp_gamma_smoothed if prev else 0.0
        now_ts = time.time()
        if prev_hp_last_ts > 0:
            dt = now_ts - prev_hp_last_ts
            alpha = 1.0 - 0.5 ** (dt / HP_GAMMA_SMOOTHING_HALFLIFE_S)
            hp_gamma_shares = alpha * hp_gamma_shares_raw + (1.0 - alpha) * prev_hp_smoothed
        else:
            hp_gamma_shares = hp_gamma_shares_raw

        # Volume baseline for the ratio (BUG 2 fix)
        # Use larger of (rolling 5-min, configured RTH baseline). Schwab
        # LEVELONE_EQUITIES doesn't publish every print so rolling_vol
        # systematically undercounts; the 5M baseline is the real RTH floor.
        rolling_vol = self._rolling_5min_volume(ticker)
        baseline_vol = max(rolling_vol, QQQ_BASELINE_5MIN_VOLUME)
        amp_factor = abs(hp_gamma_shares) / max(baseline_vol, 1.0)

        # Regime class from amp_factor
        if amp_factor >= AMP_FACTOR_REGIME_DOMINANT:
            regime_class = 'dominant'
        elif amp_factor >= AMP_FACTOR_REGIME_STRUCTURAL:
            regime_class = 'structural'
        elif amp_factor >= AMP_FACTOR_REGIME_CONTRIB:
            regime_class = 'contributory'
        else:
            regime_class = 'background'

        # ── 2. Wall signals → regime + flip + walls ──────────────────────
        try:
            ws_state = _ws.get_state(ticker) or {}
        except Exception as e:
            log.debug(f"[CCS] wall_signals err: {e}")
            ws_state = {}
        regime_ws = ws_state.get('regime', 'unknown')
        gamma_flip = float(ws_state.get('gamma_flip') or 0.0)
        walls = ws_state.get('walls', []) or []
        # Find call_wall and put_wall by name
        call_wall = next((float(w.get('strike', 0))
                          for w in walls if (w.get('name') or '').lower() == 'call_wall'), 0.0)
        put_wall = next((float(w.get('strike', 0))
                         for w in walls if (w.get('name') or '').lower() == 'put_wall'), 0.0)

        # ── REGIME OVERRIDE (Phase 10D fix) ───────────────────────────────
        # wall_signals.regime is derived from spot-vs-gamma_flip, but for
        # chains with multiple sign-crossings (typical when OI clusters in
        # both deep ITM and OTM), the reported gamma_flip is the FIRST
        # crossing going up — often a low-strike artifact, not the actionable
        # transition zone. Trust the SIGN of hp_gamma_shares_1pct from
        # greek_surface — that's the actual dealer flow direction at spot.
        #
        # Sign convention (greek_surface.export_hedge_pressure docstring):
        #   hp_gamma_shares_1pct > 0  →  dealers must BUY on +1% rise
        #                                = SHORT-γ regime at spot
        #   hp_gamma_shares_1pct < 0  →  dealers must SELL on +1% rise
        #                                = LONG-γ regime at spot
        # When the two disagree, we surface BOTH labels in the rationale so
        # the operator can see the structural conflict.
        if abs(hp_gamma_shares_raw) > 1.0:
            regime_hp = 'short_gamma' if hp_gamma_shares_raw > 0 else 'long_gamma'
        else:
            regime_hp = regime_ws
        regime = regime_hp                              # use hp-sign as authoritative
        regime_disagrees = (regime_ws != regime_hp and regime_ws != 'unknown')

        # Distance to flip
        dist_pct = (abs(spot - gamma_flip) / spot) if (spot > 0 and gamma_flip > 0) else 1.0

        # ── 3. Flow direction + #2 setup mode + #3 cohort split ────────
        acc = get_accumulator()
        flow_signed_0dte = 0.0
        flow_signed_all = 0.0
        # Improvement #2 — atomic-action breakdown
        cum_call_buy = cum_call_sell = cum_put_buy = cum_put_sell = 0.0
        setup_mode = 'IDLE'
        setup_confidence = 0
        # Improvement #3 — exp_type / DTE 6-cohort split + institutional share
        cohort_0dte_am_M = cohort_0dte_pm_M = 0.0
        cohort_weekly_M  = cohort_monthly_M = 0.0
        cohort_quarterly_M = cohort_leaps_M = 0.0
        institutional_share = 0.0
        # Improvement #4 — venue concentration score
        venue_concentration = 0.0
        top_venue_mic = None
        top_venue_share = 0.0
        # Initialize the flow_accumulator's by-exchange snapshot so the
        # single_venue_artifact guard below has a defined sentinel even
        # when `acc` is None or the get_by_exchange call fails. Previously
        # the code used `'be' in dir()` which also worked but obscured
        # the dependency and broke if the variable was reassigned later.
        be_flow = None
        # Improvement #5 — OI-classified opening/closing flow split
        opening_signed_M = 0.0
        closing_signed_M = 0.0
        oi_classified_share = 0.0
        if acc:
            try:
                # Use _ticker_dict via get_all_states to get the enriched
                # view including setup_mode classification.
                all_states = acc.get_all_states() or {}
                flow_state = all_states.get(ticker) or {}
                flow_signed_0dte = float(flow_state.get('cum_signed_0dte', 0) or 0)
                flow_signed_all = float(flow_state.get('cum_signed_all', 0) or 0)
                cum_call_buy   = float(flow_state.get('cum_call_buy', 0) or 0)
                cum_call_sell  = float(flow_state.get('cum_call_sell', 0) or 0)
                cum_put_buy    = float(flow_state.get('cum_put_buy', 0) or 0)
                cum_put_sell   = float(flow_state.get('cum_put_sell', 0) or 0)
                setup_mode      = flow_state.get('setup_mode') or 'IDLE'
                setup_confidence = int(flow_state.get('setup_confidence') or 0)
                # Cohorts (signed, in dollars; CCS surfaces as $M)
                cohort_0dte_am_M    = float(flow_state.get('cohort_0dte_am_signed', 0) or 0) / 1e6
                cohort_0dte_pm_M    = float(flow_state.get('cohort_0dte_pm_signed', 0) or 0) / 1e6
                cohort_weekly_M     = float(flow_state.get('cohort_weekly_signed', 0) or 0) / 1e6
                cohort_monthly_M    = float(flow_state.get('cohort_monthly_signed', 0) or 0) / 1e6
                cohort_quarterly_M  = float(flow_state.get('cohort_quarterly_signed', 0) or 0) / 1e6
                cohort_leaps_M      = float(flow_state.get('cohort_leaps_signed', 0) or 0) / 1e6
                institutional_share = float(flow_state.get('institutional_share', 0) or 0)
                # Improvement #4 — venue concentration
                be_flow = acc.get_by_exchange(ticker, top_n=5) if hasattr(acc, 'get_by_exchange') else {}
                venue_concentration = float(be_flow.get('concentration_score', 0) or 0)
                top_venue_mic = be_flow.get('top1_mic')
                vens = be_flow.get('venues') or []
                if vens:
                    top_venue_share = float(vens[0].get('share_signed_pct', 0) or 0)
                # Improvement #5 — opening/closing split (Pan & Poteshman)
                opening_signed_M    = float(flow_state.get('opening_signed_flow', 0) or 0) / 1e6
                closing_signed_M    = float(flow_state.get('closing_signed_flow', 0) or 0) / 1e6
                oi_classified_share = float(flow_state.get('oi_classified_share', 0) or 0)
            except Exception as _e:
                log.debug(f"[CCS] flow_state read err for {ticker}: {_e}", exc_info=True)
        # Direction sign: use cum_signed_all alone — it ALREADY includes 0DTE
        # (flow_accumulator.py:384-386 → 0DTE component is added to BOTH
        # cum_signed_0dte AND cum_signed_all). The earlier sum
        # `flow_signed_0dte + flow_signed_all` double-counted the 0DTE leg
        # and inverted direction whenever non-0DTE opposed 0DTE in magnitude.
        flow_dir = 0
        if abs(flow_signed_all) > 1_000_000:
            flow_dir = 1 if flow_signed_all > 0 else -1

        # ── 4. Regime alignment scoring ──────────────────────────────────
        # Long-γ above flip: dampening — flow direction will be ABSORBED
        #   → flow up + long-γ + below call_wall = fade favored
        #   → flow up + long-γ + above call_wall = follow (out of damp zone)
        # Short-γ: amplifying — flow direction will be REINFORCED
        #   → flow up + short-γ = follow strongly
        regime_align_score = 0   # 0–100, signed direction handled separately
        recommended_dir = 'NEUTRAL'

        # ── BUG 1 fix ──
        # wall_signals' "call_wall" is the dominant call-OI cluster, NOT
        # necessarily above spot. Don't use wall position to decide damp_band;
        # use distance-to-flip instead. In long-γ regime, dampening is active
        # when spot is meaningfully above gamma_flip — that's the gamma-positive
        # zone where dealer hedges fight price moves. The exact bandwidth
        # depends on charm, but for a structural read, distance > 1% from
        # flip places us deep in the dampening zone.
        if flow_dir == 0:
            regime_align_score = 30  # no clear flow, neutral
        elif regime == 'long_gamma':
            # Long-γ damps moves (dealers absorb) when spot is comfortably
            # above flip. Within 0.5% of flip, dampening is weak (regime is
            # transitional). > 1% from flip is the deep dampening zone.
            in_damp_band = (gamma_flip > 0 and dist_pct > 0.010)
            near_flip = (gamma_flip > 0 and dist_pct <= 0.010)
            below_flip = (spot > 0 and gamma_flip > 0 and spot < gamma_flip)

            if below_flip:
                # If somehow long_gamma label but spot below flip, dealers
                # already amplifying — follow flow.
                regime_align_score = 75
                recommended_dir = 'BULL' if flow_dir > 0 else 'BEAR'
            elif in_damp_band:
                # Spot deep in dampening zone — fade flow (dealer hedge
                # absorbs). Recommended direction is OPPOSITE to flow.
                regime_align_score = 90
                recommended_dir = 'BEAR' if flow_dir > 0 else 'BULL'
            elif near_flip:
                # Near regime transition — uncertainty. Reduce conviction
                # and require stronger confirmation. Default to flow direction.
                regime_align_score = 50
                recommended_dir = 'BULL' if flow_dir > 0 else 'BEAR'
            else:
                regime_align_score = 40
                recommended_dir = 'BULL' if flow_dir > 0 else 'BEAR'
        elif regime == 'short_gamma':
            # Short-γ amplifies — follow flow.
            regime_align_score = 95
            recommended_dir = 'BULL' if flow_dir > 0 else 'BEAR'
        else:
            regime_align_score = 20  # unknown regime
            recommended_dir = 'NEUTRAL'

        # ── 5. Distance-to-flip scoring ──────────────────────────────────
        if dist_pct >= DISTANCE_TO_FLIP_ROBUST_PCT:
            dist_score = 100
        elif dist_pct >= 0.010:    # 1.0% buffer
            dist_score = 80
        elif dist_pct >= 0.005:
            dist_score = 60
        elif dist_pct >= DISTANCE_TO_FLIP_FRAGILE_PCT:
            dist_score = 30
        else:
            dist_score = 10  # within 0.2% of flip — fragile

        # ── 6. Flow quality scoring ──────────────────────────────────────
        # 0dte vs all-exp signal: aligned (both same sign) = institutional
        # confirms retail. Diverged (opposite signs) = institutional bets
        # AGAINST retail FOMO — fade-the-retail setup.
        flow_q_score = 30  # default
        ir_signs_aligned = ((flow_signed_0dte > 0) == (flow_signed_all > 0))
        ir_divergence = (abs(flow_signed_0dte) > 5_000_000
                         and abs(flow_signed_all) > 5_000_000
                         and not ir_signs_aligned)
        if abs(flow_signed_all) > 5_000_000:
            mag_mult = min(2.0, abs(flow_signed_all) / 25_000_000)  # caps at 50M
            inst_ratio = abs(flow_signed_all) / max(abs(flow_signed_0dte), 1.0)
            inst_score_raw = min(1.5, inst_ratio) / 1.5  # 0..1
            flow_q_score = int(round(50 + 30 * inst_score_raw + 20 * (mag_mult / 2.0)))
            # BUG 4 fix: when 0dte diverges from all-exp, all-exp is the
            # institutional signal — boost score because divergence is a
            # high-quality contrarian setup.
            if ir_divergence:
                flow_q_score = min(100, flow_q_score + 10)
            # Improvement #2 — refine by setup_mode classification:
            #   AGG_LONG / AGG_SHORT  → cleanest directional, +10 bonus
            #   HEDGED_LONG/SHORT     → less conviction (insurance bought), -5
            #   VOL_HARVEST           → not directional flow, decay-based, -15
            #   MIXED                 → low conviction signal, -10
            if setup_mode in ('AGG_LONG', 'AGG_SHORT'):
                flow_q_score = min(100, flow_q_score + 10)
            elif setup_mode in ('HEDGED_LONG', 'HEDGED_SHORT'):
                flow_q_score = max(0, flow_q_score - 5)
            elif setup_mode in ('VOL_HARVEST', 'VOL_LONG'):
                flow_q_score = max(0, flow_q_score - 15)
            elif setup_mode == 'MIXED':
                flow_q_score = max(0, flow_q_score - 10)
            # Improvement #3 — institutional share refinement.
            # Empirical (CBOE participant data, Bryzgalova 2023):
            #   institutional-skewed cohorts (0dte_am + monthly + quarterly +
            #   leaps) carry information; retail-skewed (0dte_pm + weekly)
            #   underperform on 1-day forward-return basis.
            # When institutional_share > 60% AND mode is directional, the
            # signal is HIGHER quality: institutions are positioning, not
            # retail FOMO. When share < 30%, the signal is MOSTLY retail —
            # in long-γ regime that's a fade signal, not a follow signal.
            if institutional_share > 0.60 and setup_mode in ('AGG_LONG', 'AGG_SHORT'):
                flow_q_score = min(100, flow_q_score + 10)
            elif institutional_share < 0.30 and setup_mode in ('AGG_LONG', 'AGG_SHORT'):
                # Retail-dominated directional signal — penalize unless we're
                # explicitly looking to fade (which is the long-γ default).
                if regime != 'long_gamma':
                    flow_q_score = max(0, flow_q_score - 8)
            # Improvement #4 — venue concentration refinement.
            # Concentration ≥ 0.60 = single venue owns the flow → typically
            # institutional algo routing through one execution path.
            # GUARD: concentration=1.0 with NO secondary venue seen is a
            # data-source artifact (e.g. SPY where Tradier isn't subscribed
            # and Schwab's tape consolidates all prints to one MIC code) —
            # not an actionable institutional signal. Skip the bonus when
            # we only have 1 venue showing.
            _ven_count = len((be_flow or {}).get('venues') or [])
            single_venue_artifact = (venue_concentration >= 0.99 and _ven_count <= 1)
            if (venue_concentration >= 0.60 and setup_mode in ('AGG_LONG', 'AGG_SHORT')
                and not single_venue_artifact):
                flow_q_score = min(100, flow_q_score + 8)
            elif venue_concentration <= 0.20 and setup_mode in ('AGG_LONG', 'AGG_SHORT'):
                # Highly distributed = retail clicking buy from many brokers
                # in long-γ regime, this is fade-signal confirmation
                if regime == 'long_gamma':
                    pass  # fading retail is the play, no extra penalty
                else:
                    flow_q_score = max(0, flow_q_score - 5)
            # ── Improvement #5 — DATA LIMITATION ──
            # Pan & Poteshman opening/closing flow alpha requires intraday
            # OI updates. Schwab's open_interest field is a daily snapshot
            # (frozen at prior session close until next morning), so we
            # cannot compute oi_delta intraday. The opening/closing cohort
            # buckets exist in the data model but will always show 100%
            # 'unknown' with our current data source. CCS skips applying
            # this component to flow_quality. To activate, would need
            # Tradier intraday OI REST polling or alt data feed.
            flow_q_score = max(0, min(100, flow_q_score))

        # ── 7. MM signature scoring ──────────────────────────────────────
        # Pull mm_attribution module summary + per-strike active levels
        mm_sig_score = 50
        per_exch_concentration = 0.0
        try:
            # summary stats
            summary = _mma.module_summary() or {}
            # by-exchange γ posting from get_hedge_pressure_by_exchange.
            # Distinct from `be_flow` above (flow_accumulator's per-MIC
            # signed-flow rollup): this is the hedge-pressure rollup with
            # per-exchange `hp_gamma_posted` shares — different schema.
            be_hp = _sb.get_hedge_pressure_by_exchange(ticker) or {}
            exch_rows = be_hp.get('exchanges', []) or []
            if exch_rows:
                # Concentration: top 1 venue's |posted| / total |posted|
                posted_abs = [abs(float(r.get('hp_gamma_posted', 0) or 0))
                              for r in exch_rows]
                total_abs = sum(posted_abs)
                top1 = max(posted_abs) if posted_abs else 0.0
                per_exch_concentration = (top1 / total_abs) if total_abs > 0 else 0.0
                # Single-defender bonus (CBOE-style holding)
                if per_exch_concentration > 0.60:
                    mm_sig_score += 20
            # Refill ratio: average across active absorption levels
            # (data lives in mm_attribution summary or active_absorption_levels)
        except Exception as e:
            log.debug(f"[CCS] mm_sig err: {e}")
        mm_sig_score = max(0, min(100, mm_sig_score))

        # ── 8. Time-of-day scoring ───────────────────────────────────────
        # DST-aware via zoneinfo — see _now_et_minute_of_day at module top.
        et_minute_of_day = _now_et_minute_of_day()
        # Use the regime alignment direction sign to choose follow vs fade
        is_fade = (regime == 'long_gamma' and recommended_dir != 'NEUTRAL'
                   and ((flow_dir > 0 and recommended_dir == 'BEAR')
                        or (flow_dir < 0 and recommended_dir == 'BULL')))

        # 09:30 = 570; 10:00 = 600; 11:30 = 690; 13:30 = 810; 15:00 = 900;
        # 16:00 = 960
        if 570 <= et_minute_of_day < 600:        # 09:30–10:00
            tod_score = 90 if not is_fade else 30
        elif 600 <= et_minute_of_day < 690:      # 10:00–11:30 — chop fade
            tod_score = 95 if is_fade else 40
        elif 690 <= et_minute_of_day < 810:      # 11:30–13:30 — pinning
            tod_score = 70  # works for both pin and fade
        elif 810 <= et_minute_of_day < 900:      # 13:30–15:00 — trend
            tod_score = 95 if not is_fade else 20
        elif 900 <= et_minute_of_day < 960:      # 15:00–16:00 — close noise
            tod_score = 25
        else:
            tod_score = 30  # off-hours

        # ── 9. Cross-asset confirm scoring ───────────────────────────────
        # Count tickers in flow_accumulator with same direction
        cross_asset_score = 50
        try:
            all_states = acc.get_all_states() if acc else {}
            same_dir = 0; opp_dir = 0
            for tk, st in all_states.items():
                if tk == ticker or tk not in ('SPX', 'SPY', 'QQQ', 'NDX'):
                    continue
                ticker_signed = float(st.get('cum_signed_all', 0) or 0)
                if abs(ticker_signed) < 1_000_000:
                    continue
                tdir = 1 if ticker_signed > 0 else -1
                if tdir == flow_dir and flow_dir != 0:
                    same_dir += 1
                else:
                    opp_dir += 1
            total_other = same_dir + opp_dir
            if total_other >= 3 and same_dir == total_other:
                cross_asset_score = 100   # all confirm
            elif total_other >= 2 and same_dir > opp_dir:
                cross_asset_score = 75
            elif same_dir == opp_dir:
                cross_asset_score = 50
            elif opp_dir > same_dir:
                cross_asset_score = 25
        except Exception:
            pass

        # ── 9a. Intel signals (Phase 9 — 6 panels feed the CCS) ──────────
        # Scoring helpers below are defensive: each panel may be empty
        # (NO_DATA reason) early in the session or after-hours. In every case
        # the helper returns 50 (neutral) so the score doesn't get pinned to
        # 0 just because a panel hasn't woken up yet.
        intel_hedge_fc_score = _score_hedge_forecast(recommended_dir, flow_dir)
        intel_pin_score      = _score_pin(spot)
        intel_vol_regime_score = _score_vol_regime(regime, recommended_dir)
        intel_spxqqq_score   = _score_spxqqq_div(flow_dir, regime)
        intel_sweep_score    = _score_sweep(ticker, flow_dir)
        intel_wing_score     = _score_wing(flow_dir, regime)
        # Warehouse-quality multiplier on dist_score. Computed AFTER dist_score
        # is set so we can boost/penalize based on the strike we care about.
        warehouse_multiplier, warehouse_class_at_flip = _warehouse_multiplier(
            spot, gamma_flip, call_wall, put_wall)
        dist_score = max(0, min(100, int(round(dist_score * warehouse_multiplier))))

        # ── 9b. Mispricing signal (Improvement #1) ───────────────────────
        # Theoretical-vs-Mark divergence reveals when institutional money is
        # paying premium ABOVE Black-Scholes fair value. The signal is
        # ORTHOGONAL to direction:
        #   - When mispricing aligned with flow direction = high-conviction
        #     accumulation (boost score)
        #   - When mispricing OPPOSES flow direction = exit liquidity hitting
        #     dealer book (mark below theo as flow drives in)
        #   - Magnitude: ±3% mispricing × magnitude → ±60 score
        mispricing_score = 50           # neutral default
        misp_avg_pct = 0.0
        misp_premium_score = 0.0
        misp_top_count = 0
        try:
            misp = acc.get_mispricing(ticker, top_n=10) if acc else {}
            misp_avg_pct = float(misp.get('avg_mispricing_pct', 0.0) or 0.0)
            misp_premium_score = float(misp.get('institutional_premium_score', 0.0) or 0.0)
            misp_top_count = len(misp.get('top_strikes', []) or [])
            # Translate into a 0–100 score:
            #   Premium aligned with flow direction = institutional accumulation
            #   confirms (+ score). Premium against flow = distribution / fade.
            if flow_dir != 0 and abs(misp_premium_score) > 10:
                # If flow is BULLISH and premium > 0 (paying above theo) =
                # accumulation confirms bullish setup → +
                # If flow is BULLISH and premium < 0 (selling below theo) =
                # distribution against bullish flow → -
                aligned = (flow_dir > 0 and misp_premium_score > 0) or \
                          (flow_dir < 0 and misp_premium_score < 0)
                if aligned:
                    mispricing_score = int(round(50 + abs(misp_premium_score) * 0.5))
                else:
                    mispricing_score = int(round(50 - abs(misp_premium_score) * 0.5))
                mispricing_score = max(0, min(100, mispricing_score))
            elif misp_top_count == 0:
                mispricing_score = 30   # no data — discount default
        except Exception as _e:
            log.debug(f"[CCS] mispricing fetch err: {_e}")

        # ── 10. Composite ────────────────────────────────────────────────
        ccs = (regime_align_score    * W_REGIME_ALIGN
               + dist_score          * W_DISTANCE_TO_FLIP
               + flow_q_score        * W_FLOW_QUALITY
               + mm_sig_score        * W_MM_SIGNATURE
               + tod_score           * W_TIME_OF_DAY
               + cross_asset_score   * W_CROSS_ASSET
               + mispricing_score    * W_MISPRICING
               + intel_hedge_fc_score   * W_INTEL_HEDGE_FC
               + intel_pin_score        * W_INTEL_PIN
               + intel_vol_regime_score * W_INTEL_VOL_REGIME
               + intel_spxqqq_score     * W_INTEL_SPXQQQ
               + intel_sweep_score      * W_INTEL_SWEEP
               + intel_wing_score       * W_INTEL_WING
               ) / TOTAL_W

        # ── 11. Anti-setup detection ─────────────────────────────────────
        anti = []
        if dist_pct < DISTANCE_TO_FLIP_FRAGILE_PCT:
            anti.append('regime_fragile_at_flip')
        if amp_factor < AMP_FACTOR_REGIME_CONTRIB:
            anti.append('amp_factor_too_low')
        # Friday 14:30+ — DST-aware weekday so it doesn't slip into Thursday
        # late session in the EST → EDT/EDT → EST changeover windows.
        wd = _now_et_weekday()  # 0=Mon
        if wd == 4 and et_minute_of_day >= 870:    # Fri 14:30 ET
            anti.append('friday_late_session')
        # 15:00+ no-entry
        if et_minute_of_day >= 900:
            anti.append('post_15:00_no_new_entries')

        # Regime transition watch
        regime_transition_watch = (dist_pct < 0.005 and amp_factor >= 2.0)

        # ── 12. Position size recommendation ─────────────────────────────
        if anti:
            size_rec = 'PASS'
        elif ccs >= SCORE_FULL_SIZE:
            size_rec = 'FULL'
        elif ccs >= SCORE_HALF_SIZE:
            size_rec = 'HALF'
        elif ccs >= SCORE_QUARTER_SIZE:
            size_rec = 'QUARTER'
        elif ccs <= SCORE_REVERSE_TRADE:
            size_rec = 'REVERSE'
        else:
            size_rec = 'PASS'

        # If recommending REVERSE, flip direction
        if size_rec == 'REVERSE' and recommended_dir != 'NEUTRAL':
            recommended_dir = 'BEAR' if recommended_dir == 'BULL' else 'BULL'

        # ── 13. Build rationale string ───────────────────────────────────
        rationale_parts = []
        rationale_parts.append(f"amp={amp_factor:.2f}x ({regime_class})")
        if regime_disagrees:
            rationale_parts.append(
                f"regime={regime}(hp){' [ws→' + regime_ws + ']'}"
            )
        else:
            rationale_parts.append(f"regime={regime}")
        if dist_pct < 0.01:
            rationale_parts.append(f"flip-near({dist_pct*100:.2f}%)")
        if flow_dir != 0:
            rationale_parts.append(
                f"flow={'+' if flow_dir > 0 else '-'}${abs(flow_signed_all)/1e6:.1f}M")
        if ir_divergence:
            rationale_parts.append('⚡ inst≠retail divergence')
        if abs(misp_avg_pct) >= 1.5:    # surface only meaningful mispricing
            tag = '⊙' if misp_avg_pct > 0 else '⊖'
            rationale_parts.append(f"{tag} mispricing {misp_avg_pct:+.2f}%")
        if setup_mode and setup_mode not in ('IDLE', 'MIXED'):
            rationale_parts.append(f"setup={setup_mode}({setup_confidence})")
        if institutional_share >= 0.65:
            rationale_parts.append(f"inst {int(institutional_share*100)}%")
        elif institutional_share <= 0.25 and (cum_call_buy + cum_call_sell) > 5_000_000:
            rationale_parts.append(f"retail {int((1-institutional_share)*100)}%")
        if venue_concentration >= 0.50 and top_venue_mic:
            rationale_parts.append(f"venue→{top_venue_mic}({int(top_venue_share)}%)")
        # NOTE: opening_signed_M / closing_signed_M deliberately NOT in
        # rationale — Schwab data limitation prevents intraday OI tracking
        # (see #5 doc comment above). Re-enable when intraday OI feed wired.
        if anti:
            rationale_parts.append(f"⚠ {','.join(anti)}")
        rationale = ' · '.join(rationale_parts)

        # ── 14. Store + emit ─────────────────────────────────────────────
        new_state = _CCSState(
            ticker=ticker,
            ts=now_ts,
            spot=spot,
            score=ccs,
            direction=recommended_dir,
            size_recommendation=size_rec,
            amp_factor=amp_factor,
            regime=regime,
            regime_class=regime_class,
            distance_to_flip_pct=dist_pct,
            components={
                'regime_alignment':  regime_align_score,
                'distance_to_flip':  dist_score,
                'flow_quality':      flow_q_score,
                'mm_signature':      mm_sig_score,
                'time_of_day':       tod_score,
                'cross_asset':       cross_asset_score,
                'mispricing_signal': mispricing_score,
                'per_exch_concentration': round(per_exch_concentration, 3),
                'baseline_volume':   baseline_vol,
                'rolling_5min_volume': rolling_vol,
                'hp_gamma_shares_1pct': round(hp_gamma_shares, 0),
                'hp_gamma_shares_raw': round(hp_gamma_shares_raw, 0),
                'flow_signed_all_M':  round(flow_signed_all / 1e6, 2),
                'flow_signed_0dte_M': round(flow_signed_0dte / 1e6, 2),
                'ir_divergence':     ir_divergence,
                'mispricing_avg_pct':       round(misp_avg_pct, 3),
                'mispricing_premium_score': round(misp_premium_score, 1),
                'mispricing_strikes_count': misp_top_count,
                # Improvement #2 — atomic-action 4-bucket exposure
                'setup_mode':               setup_mode,
                'setup_confidence':         setup_confidence,
                'cum_call_buy_M':   round(cum_call_buy / 1e6, 2),
                'cum_call_sell_M':  round(cum_call_sell / 1e6, 2),
                'cum_put_buy_M':    round(cum_put_buy / 1e6, 2),
                'cum_put_sell_M':   round(cum_put_sell / 1e6, 2),
                # Improvement #3 — exp_type/DTE 6-cohort signed flow ($M)
                'cohort_0dte_am_M':    round(cohort_0dte_am_M, 2),
                'cohort_0dte_pm_M':    round(cohort_0dte_pm_M, 2),
                'cohort_weekly_M':     round(cohort_weekly_M, 2),
                'cohort_monthly_M':    round(cohort_monthly_M, 2),
                'cohort_quarterly_M':  round(cohort_quarterly_M, 2),
                'cohort_leaps_M':      round(cohort_leaps_M, 2),
                'institutional_share': round(institutional_share, 3),
                # Improvement #4 — venue concentration
                'venue_concentration': round(venue_concentration, 3),
                'top_venue_mic':       top_venue_mic,
                'top_venue_share_pct': round(top_venue_share, 2),
                # Improvement #5 — OI-classified opening/closing split
                'opening_signed_M':    round(opening_signed_M, 2),
                'closing_signed_M':    round(closing_signed_M, 2),
                'oi_classified_share': round(oi_classified_share, 3),
                # Phase 9 — intel signal components
                'intel_hedge_fc':      intel_hedge_fc_score,
                'intel_pin':           intel_pin_score,
                'intel_vol_regime':    intel_vol_regime_score,
                'intel_spxqqq':        intel_spxqqq_score,
                'intel_sweep':         intel_sweep_score,
                'intel_wing':          intel_wing_score,
                'warehouse_multiplier': round(warehouse_multiplier, 3),
                'warehouse_class_at_flip': warehouse_class_at_flip,
                # Phase 10D — regime sources for transparency
                'regime_hp':        regime_hp,
                'regime_wall_signals': regime_ws,
                'regime_disagrees':    regime_disagrees,
            },
            anti_setups=anti,
            regime_transition_watch=regime_transition_watch,
            rationale=rationale,
            hp_gamma_smoothed=hp_gamma_shares,
            hp_gamma_last_ts=now_ts,
        )

        # Determine if we should emit (significant change or 30s tick).
        # Snap fields under lock then test outside, so the writer below
        # doesn't race with concurrent get_state() callers.
        with self._lock:
            prev = self._state.get(ticker)
            if prev is not None:
                prev_size_rec = prev.size_recommendation
                prev_score    = prev.score
                prev_dir      = prev.direction
                prev_ts       = prev.ts
            else:
                prev_size_rec = prev_score = prev_dir = prev_ts = None
            self._state[ticker] = new_state
        emit_now = True
        if prev_size_rec is not None:
            if (prev_size_rec == new_state.size_recommendation
                and abs(prev_score - new_state.score) < 3.0
                and prev_dir == new_state.direction
                and (time.time() - prev_ts) < 30.0):
                emit_now = False

        if emit_now and self._sio is not None:
            try:
                self._sio.emit('conviction_update', self._serialize(new_state))
            except Exception as e:
                log.debug(f"[CCS] emit err: {e}")


# ── Phase 9 — Intel signal scoring helpers ─────────────────────────────────
# Each function returns 0..100. Defensive: returns 50 (neutral) on any
# missing-data path so an offline panel doesn't silently zero the score.

def _score_hedge_forecast(recommended_dir: str, flow_dir: int) -> int:
    """Match hedge_forecaster.5min forecast direction with recommended_dir.
    Boost if aligned; reduce if opposite. Multiplied by forecast confidence.
    """
    try:
        from connectors import hedge_forecaster as _hf
        st = _hf.get_state('QQQ') or {}
    except Exception:
        return 50
    if st.get('reason'):
        return 50
    fc5 = (st.get('forecasts') or {}).get('5min') or {}
    fc_side = fc5.get('side')               # 'BUY'|'SELL'|'FLAT'|None
    fc_conf = float(fc5.get('confidence', 0) or 0)
    if not fc_side or fc_side == 'FLAT' or fc_conf <= 0:
        return 50
    # Translate fc_side → directional sign (BUY=+1, SELL=-1)
    fc_sign = 1 if fc_side == 'BUY' else -1
    rec_sign = 1 if recommended_dir == 'BULL' else (-1 if recommended_dir == 'BEAR' else 0)
    if rec_sign == 0:
        # No direction — just measure forecast strength
        return int(round(50 + fc_conf * 30))
    aligned = (fc_sign == rec_sign)
    base = 90 if aligned else 10
    # Confidence-weighted blend toward neutral 50
    return int(round(50 + (base - 50) * fc_conf))


def _score_pin(spot: float) -> int:
    """Pin pull strength + confidence. Higher when pin is high-confidence and
    spot is approaching it (esp. last-30-min amplification window).
    """
    try:
        from connectors import pin_convergence as _pc
        st = _pc.get_state('QQQ') or {}
    except Exception:
        return 50
    if st.get('reason'):
        return 50
    pin_est  = st.get('pin_estimate')
    pin_conf = float(st.get('pin_confidence') or 0)
    tr_sec   = float(st.get('time_remaining_sec') or 0)
    if not pin_est or not (spot > 0):
        return 50
    # Distance from spot to pin in dollars
    dist = abs(float(pin_est) - spot)
    # Distance score: 0% (closer than $0.5) → 1.0, $5 away → 0.0
    dist_score_norm = max(0.0, 1.0 - dist / 5.0)
    # Confidence amplification — pin_confidence already saturates by design
    base = 50 + (pin_conf * dist_score_norm) * 50
    # Last-30-min boost: pin pull intensifies dramatically
    if tr_sec > 0 and tr_sec < 1800:
        boost = 1.0 + (1800 - tr_sec) / 1800 * 0.30        # +30% max
        base *= boost
    return max(0, min(100, int(round(base))))


def _score_vol_regime(regime: str, recommended_dir: str) -> int:
    """Vol regime alignment. Calm + dampening = aligned for fade trades;
    Stress + amplifying = aligned for follow trades.
    """
    try:
        from connectors import vix_term_structure as _vts
        st = _vts.get_state() or {}
    except Exception:
        return 50
    if st.get('reason') or st.get('regime') == 'NO_DATA':
        return 50
    vol_regime = st.get('regime') or 'NORMAL'
    strength   = float(st.get('regime_strength') or 0)
    # Map vol regime → market action expectation:
    #   CALM_CONTANGO        → mean-reverting (fade favored)
    #   STRESS_BACKWARDATION → momentum (follow favored)
    #   STRESS_CONTANGO      → momentum but fragile (follow w/ caution)
    #   ELEVATED             → unstable, lower conviction
    #   TECH_DIVERGENCE      → asymmetric, mid-conviction
    #   VVIX_DIVERGENCE      → tail-risk priced in (lower follow conviction)
    #   NORMAL               → neutral
    if vol_regime == 'NORMAL':
        return 50
    if vol_regime == 'CALM_CONTANGO':
        return int(round(50 + 30 * strength))           # boosts default
    if vol_regime == 'STRESS_BACKWARDATION':
        return int(round(50 + 35 * strength))           # strong follow signal
    if vol_regime == 'STRESS_CONTANGO':
        return int(round(50 + 20 * strength))           # mid-strong follow
    if vol_regime == 'ELEVATED':
        return int(round(50 - 10 * strength))           # lower conviction
    if vol_regime == 'TECH_DIVERGENCE':
        return int(round(50 + 10 * strength))
    if vol_regime == 'VVIX_DIVERGENCE':
        return int(round(50 - 15 * strength))           # tail bid = caution
    return 50


def _score_spxqqq_div(flow_dir: int, regime: str) -> int:
    """SPX-vs-QQQ divergence verdict scoring.

    DIVERGENT_REGIME = strongest signal (laggard expected to follow leader).
    DIVERGENT_MAGNITUDE = mid signal (one ticker dominates flow).
    ALIGNED_BULL/BEAR = baseline confirmation.
    """
    try:
        from connectors import spx_qqq_divergence as _sqd
        st = _sqd.get_state() or {}
    except Exception:
        return 50
    div = (st.get('divergence') or {})
    verdict = div.get('verdict')
    strength = float(div.get('strength', 0) or 0)
    if not verdict or verdict in ('NO_DATA', 'NEUTRAL'):
        return 50
    if verdict == 'DIVERGENT_REGIME':
        return int(round(50 + 35 * strength))           # 50..85
    if verdict == 'DIVERGENT_MAGNITUDE':
        return int(round(50 + 20 * strength))           # 50..70
    if verdict == 'ALIGNED_BULL':
        # Aligned bull boosts BULL flow, dampens BEAR
        if flow_dir > 0:
            return int(round(50 + 20 * strength))
        elif flow_dir < 0:
            return int(round(50 - 15 * strength))
        return 50
    if verdict == 'ALIGNED_BEAR':
        if flow_dir < 0:
            return int(round(50 + 20 * strength))
        elif flow_dir > 0:
            return int(round(50 - 15 * strength))
        return 50
    return 50


def _score_sweep(ticker: str, flow_dir: int) -> int:
    """Recent sweep alignment with flow direction. Recent sweeps in the same
    direction = institutional confirmation.

    Phase 10A — sweeps with `hf_aligned == True` (aligned with hedge_forecaster.5min)
    receive a size-weighting bonus, since dual confirmation = ~80% conviction.
    """
    try:
        from connectors import sweep_detector as _swd
        if not hasattr(_swd, 'get_recent_sweeps'):
            return 50
        sweeps = _swd.get_recent_sweeps(20) or []
    except Exception:
        return 50
    if not sweeps:
        return 50
    # Filter to last 5 minutes + matching ticker
    now_ms = time.time() * 1000.0
    cutoff_ms = now_ms - 5 * 60 * 1000.0
    recent = [s for s in sweeps
                if s.get('underlying') == ticker
                and float(s.get('last_print_ts', 0) or 0) >= cutoff_ms]
    if not recent:
        return 50
    # Count direction-aligned sweeps with HF-alignment bonus (Phase 10A)
    same_count = 0
    opp_count = 0
    total_size_aligned = 0
    total_size_opposed = 0
    hf_dual_aligned_count = 0          # sweeps where flow_dir AND hf_aligned both confirm
    hf_dual_aligned_size = 0
    for s in recent:
        direction = s.get('direction')                  # 'BUY'|'SELL'
        opt_side = s.get('option_side')                 # 'C'|'P'
        size = int(s.get('total_size', 0) or 0)
        hf_aligned = s.get('hf_aligned')                # True/False/None
        hf_conf = float(s.get('hf_confidence', 0) or 0)
        # Map sweep → underlying directional bias:
        #   BUY calls or SELL puts → BULLISH
        #   BUY puts or SELL calls → BEARISH
        bias = 0
        if direction == 'BUY' and opt_side == 'C':   bias = +1
        elif direction == 'SELL' and opt_side == 'P': bias = +1
        elif direction == 'BUY' and opt_side == 'P': bias = -1
        elif direction == 'SELL' and opt_side == 'C': bias = -1
        if bias == 0 or flow_dir == 0:
            continue
        if bias == flow_dir:
            same_count += 1
            total_size_aligned += size
            # HF dual-alignment bonus: sweep matches flow AND matches forecast
            if hf_aligned is True and hf_conf > 0.30:
                hf_dual_aligned_count += 1
                hf_dual_aligned_size += size
        else:
            opp_count += 1
            total_size_opposed += size
    if same_count == 0 and opp_count == 0:
        # Recent sweeps but flow_dir undefined
        return min(100, 60 + len(recent) * 2)
    # Score by signed size proportion
    total_size = total_size_aligned + total_size_opposed
    if total_size == 0:
        return 50
    align_share = total_size_aligned / total_size
    base = 20 + 70 * align_share                       # 20..90
    # Phase 10A — HF dual-alignment bonus: up to +10 when ≥ half the aligned
    # sweep size also has hf_aligned == True with confidence > 0.30
    if hf_dual_aligned_size > 0 and total_size_aligned > 0:
        dual_share = hf_dual_aligned_size / total_size_aligned
        base += 10 * dual_share                        # +0..+10
    return int(round(max(0, min(100, base))))


def _score_wing(flow_dir: int, regime: str) -> int:
    """Wing tracker regime + side-vs-flow alignment.

    EXTREME = high signal value. Whether wings align with flow_dir matters:
      Call wing buying + BULL flow_dir = aligned (institutions agree retail)
      Put wing buying + BEAR flow_dir = aligned (tail hedge into selling)
      Wing buying opposite to flow = institutions fading retail (high conviction
      contrarian signal in long_gamma regime)
    """
    try:
        from connectors import wing_tracker as _wt
        st = _wt.get_state() or {}
    except Exception:
        return 50
    if st.get('reason') or st.get('regime') == 'NO_DATA':
        return 50
    wing_regime = st.get('regime')
    strength = float(st.get('regime_strength', 0) or 0)
    # Compute net wing bias (calls vs puts in NEAR/DEEP/TAIL)
    zones = st.get('zones') or {}
    call_v = 0
    put_v  = 0
    for zone in ('NEAR_WING', 'DEEP_WING', 'TAIL'):
        z = zones.get(zone) or {}
        call_v += int(z.get('call_volume', 0) or 0)
        put_v  += int(z.get('put_volume', 0) or 0)
    if call_v == 0 and put_v == 0:
        return 50
    wing_bias = (call_v - put_v) / max(call_v + put_v, 1)
    # Default base from regime intensity
    if wing_regime == 'NORMAL':
        base = 50
    elif wing_regime == 'ACTIVE':
        base = int(round(50 + 25 * strength))
    elif wing_regime == 'EXTREME':
        base = int(round(50 + 35 * strength))
    else:
        base = 50
    # Direction alignment: wing_bias > 0 = call-heavy = bullish institutional
    # Aligned with flow_dir → boost; opposed in long_gamma → still informative
    if flow_dir == 0:
        return min(100, max(0, base))
    aligned = (wing_bias > 0 and flow_dir > 0) or (wing_bias < 0 and flow_dir < 0)
    if aligned:
        return min(100, base + int(round(10 * abs(wing_bias))))
    else:
        # Long-gamma: institutions fading retail = high contrarian signal
        if regime == 'long_gamma':
            return min(100, base + int(round(5 * abs(wing_bias))))
        # Short-gamma: institutions opposed to flow = friction = lower conviction
        return max(0, base - int(round(15 * abs(wing_bias))))


def _warehouse_multiplier(spot: float, gamma_flip: float,
                           call_wall: float, put_wall: float) -> tuple:
    """Return (multiplier ∈ [WAREHOUSE_PENALTY_MIN, WAREHOUSE_BOOST_MAX],
    classification_at_flip).

    Reads dealer_warehouse strength at the strike nearest spot's most relevant
    level (gamma_flip if close to it, else nearest wall). Strong commitment →
    boost dist_score; phantom depth → penalty.
    """
    try:
        from connectors import dealer_warehouse as _dw
    except Exception:
        return (1.0, None)
    # Choose the relevant strike: closest of (flip, call_wall, put_wall) to spot
    candidates = []
    for K, name in ((gamma_flip, 'flip'), (call_wall, 'call_wall'), (put_wall, 'put_wall')):
        if isinstance(K, (int, float)) and K > 0:
            candidates.append((abs(K - spot), K, name))
    if not candidates:
        return (1.0, None)
    candidates.sort()
    closest_K = candidates[0][1]
    # Read warehouse state to find classification at this strike
    try:
        wh_state = _dw.get_state() or {}
        strikes = wh_state.get('strikes') or []
        # Find the K closest to closest_K
        nearest = None
        nearest_d = float('inf')
        for r in strikes:
            try:
                K = float(r.get('K', 0) or 0)
                if K <= 0:
                    continue
                d = abs(K - closest_K)
                if d < nearest_d:
                    nearest_d = d
                    nearest = r
            except Exception:
                continue
        if nearest is None:
            return (1.0, None)
        cls = nearest.get('classification') or 'INACTIVE'
        if cls == 'COMMITTED':
            return (WAREHOUSE_BOOST_MAX, cls)
        elif cls == 'PHANTOM':
            return (WAREHOUSE_PENALTY_MIN, cls)
        elif cls == 'ACTIVE':
            return (1.05, cls)
        return (1.0, cls)
    except Exception:
        return (1.0, None)


# ── Module-level singleton ─────────────────────────────────────────────────
_scorer: Optional[ConvictionScorer] = None


def init_scorer(socketio, tickers: Optional[list] = None) -> ConvictionScorer:
    """Idempotent — call once from schwab_bridge startup."""
    global _scorer
    if _scorer is None:
        _scorer = ConvictionScorer(socketio, tickers=tickers)
        _scorer.start()
    return _scorer


def get_scorer() -> Optional[ConvictionScorer]:
    return _scorer
