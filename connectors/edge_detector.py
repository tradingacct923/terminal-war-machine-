"""
Edge Detector v4 — Non-Parametric, Regime-Aware, Cross-Asset Signal Engine

Architecture:
  1. EmpiricalDist: Non-parametric percentile ranks (EWMA + reservoir)
  2. EdgeDetector: Regime-conditional thresholds, per-underlying screener
  3. GEX-weighted signals: amplify/discount based on proximity to gamma walls
  4. NQ futures detection forwarding: iceberg/sweep from l2_worker → edge votes
  5. Outcome feedback loop: tracks PnL at 10s/30s/60s, reports win rate
  6. Joint probability confidence: not naive averaging

Cross-asset data consumed:
  Stream 1: QQQ L2 (NASDAQ_BOOK) — book microstructure
  Stream 2: Options Screener     — unusual volume spikes
  Stream 3: Live GEX zones       — gamma walls, flip point
  Stream 4: NQ futures detections — iceberg/sweep from l2_worker
"""

import time
import math
import threading
import re
from collections import deque, defaultdict


# ═══════════════════════════════════════════════════════════
#  EMPIRICAL DISTRIBUTION TRACKER
# ═══════════════════════════════════════════════════════════

class EmpiricalDist:
    """Non-parametric distribution tracker with EWMA + empirical percentile ranks.

    Percentile rank is exact: "higher than X% of observed values."
    No distributional assumption — works on Gaussian, Cauchy, bimodal, whatever.
    """

    def __init__(self, half_life=50, reservoir_size=500):
        self._alpha = 1.0 - math.exp(-math.log(2) / half_life)
        self._ewma_mean = 0.0
        self._ewma_var = 0.0
        self._initialized = False
        self._reservoir = deque(maxlen=reservoir_size)
        self._sorted_cache = []
        self._cache_dirty = False
        self._count = 0

    def update(self, value):
        if not self._initialized:
            self._ewma_mean = value
            self._ewma_var = 0.0
            self._initialized = True
        else:
            delta = value - self._ewma_mean
            self._ewma_mean += self._alpha * delta
            self._ewma_var = (1 - self._alpha) * (self._ewma_var + self._alpha * delta * delta)
        self._reservoir.append(value)
        self._cache_dirty = True
        self._count += 1

    def _rebuild_cache(self):
        if self._cache_dirty:
            self._sorted_cache = sorted(self._reservoir)
            self._cache_dirty = False

    @property
    def mean(self):
        return self._ewma_mean

    @property
    def std(self):
        return math.sqrt(max(0, self._ewma_var))

    @property
    def count(self):
        return self._count

    @property
    def warm(self):
        return len(self._reservoir) >= 30

    def percentile_of(self, value):
        """What percentile is this value at? Returns 0-100. Non-parametric."""
        if not self.warm:
            return 50.0
        self._rebuild_cache()
        n = len(self._sorted_cache)
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if self._sorted_cache[mid] < value:
                lo = mid + 1
            else:
                hi = mid
        return (lo / n) * 100.0

    def is_extreme_high(self, value, pctl_threshold=95.0):
        if not self.warm:
            return False
        return self.percentile_of(value) >= pctl_threshold

    def is_extreme_low(self, value, pctl_threshold=5.0):
        if not self.warm:
            return False
        return self.percentile_of(value) <= pctl_threshold

    def value_at_percentile(self, pctl):
        if not self.warm:
            return self.mean
        self._rebuild_cache()
        idx = int(pctl / 100.0 * (len(self._sorted_cache) - 1))
        idx = max(0, min(idx, len(self._sorted_cache) - 1))
        return self._sorted_cache[idx]


# ═══════════════════════════════════════════════════════════
#  REGIME-CONDITIONAL THRESHOLDS
# ═══════════════════════════════════════════════════════════

REGIME_PCTL_THRESHOLDS = {
    # regime: (extreme_high, extreme_low, persist_absorption, persist_exhaustion)
    'pin_mean_revert':      (98, 2, 5, 8),
    'long_gamma_stable':    (96, 4, 4, 6),
    'transition':           (95, 5, 3, 5),
    'short_gamma_volatile': (92, 8, 3, 4),
    'crash_tail_risk':      (90, 10, 2, 3),
}


# ═══════════════════════════════════════════════════════════
#  EDGE DETECTOR v4 — CROSS-ASSET ENGINE
# ═══════════════════════════════════════════════════════════

class EdgeDetector:
    """Non-parametric, regime-aware, GEX-weighted, cross-asset signal engine.

    v4 upgrades over v3:
      1. GEX-weighted signals — proximity to gamma walls amplifies/discounts
      2. NQ futures detection forwarding — iceberg/sweep from l2_worker
      3. Per-underlying screener distributions
      4. Joint probability confidence
      5. Signal outcome feedback loop
      6. Regime label + GEX zone on every signal
    v5: Multi-Engine Confluence Gate (CONVICTION signals)
    """

    # ─── Engine Family Map ─── Each signal type belongs to one independent
    # engine family. The Confluence Gate requires 3+ DIFFERENT families
    # to agree on direction before emitting a CONVICTION signal.
    ENGINE_FAMILIES = {
        'DOMINANCE_LONG': 'TAPE',       'DOMINANCE_SHORT': 'TAPE',
        'ABSORPTION_LONG': 'BOOK',      'ABSORPTION_SHORT': 'BOOK',
        'EXHAUSTION_LONG': 'BOOK',      'EXHAUSTION_SHORT': 'BOOK',
        'CASCADE_LONG': 'STRUCTURE',    'CASCADE_SHORT': 'STRUCTURE',
        'SPEED_BREAKOUT_LONG': 'GREEKS','SPEED_BREAKOUT_SHORT': 'GREEKS',
        'VANNA_SQUEEZE_LONG': 'GREEKS', 'VANNA_SQUEEZE_SHORT': 'GREEKS',
        'CHARM_PIN_LONG': 'GREEKS',     'CHARM_PIN_SHORT': 'GREEKS',
        'DELTA_HEDGE_LONG': 'FLOW',     'DELTA_HEDGE_SHORT': 'FLOW',
        'FLOW_DIVERGENCE_LONG': 'FLOW', 'FLOW_ALIGNMENT_SHORT': 'FLOW',
        'CONFLUENCE_NUCLEAR_LONG': 'OPTIONS', 'CONFLUENCE_NUCLEAR_SHORT': 'OPTIONS',
    }

    def __init__(self, streamer, flow_classifier=None, socketio=None):
        self._streamer = streamer
        self._flow = flow_classifier
        self._sio = socketio
        self._lock = threading.Lock()
        self._greek_surface = None  # Set by schwab_bridge after GreekSurface init
        self._vol_surface = None    # Set by schwab_bridge after VolSurface init

        # ─── Book Microstructure Stats (per symbol) ───────
        self._book_stats = {}

        # ─── Screener Stats (PER UNDERLYING) ─────────────
        self._screener_vol_by_underlying = {}
        self._screener_buffer = deque(maxlen=50)

        # ─── Flow Stats (per symbol) ─────────────────────
        self._flow_stats = {}

        # ─── Timing/Latency Calibration ──────────────────
        self._hedge_latency_stats = EmpiricalDist(half_life=20, reservoir_size=100)
        self._last_delta_hedge_ts = 0
        self._last_delta_hedge_dir = None

        # ─── Signal Spam Prevention ──────────────────────
        self._last_signal = {}
        # Regime-adaptive cooldown (was static 10.0)
        self._COOLDOWN_BY_REGIME = {
            'crash_tail_risk': 3.0,
            'short_gamma_volatile': 5.0,
            'transition': 10.0,
            'long_gamma_stable': 15.0,
            'pin_mean_revert': 20.0,
        }

        # ─── NQ Trade Volume Distribution (for tape_alert percentile) ──
        self._nq_trade_vol_dist = EmpiricalDist(half_life=50, reservoir_size=500)

        # ─── Last regime broadcast ────────────────────────
        self._last_regime_broadcast = 0
        self._last_broadcast_regime = ''
        self._last_eq_context_emit = 0

        # ─── Consecutive tick counters ───────────────────
        self._absorption_ticks = {}
        self._exhaustion_ticks = {}

        # ─── Signal Outcome Tracker ──────────────────────
        self._pending_outcomes = deque(maxlen=200)
        self._completed_outcomes = deque(maxlen=500)
        self._last_prices = {}

        # ─── GEX Zone Data (from zone_update events) ─────
        self._live_zones = {}  # {call_wall, put_wall, gamma_flip, max_pain, ...}
        self._zones_updated_at = 0

        # ─── NQ Futures Detection Buffer ─────────────────
        self._nq_detections = deque(maxlen=100)  # recent NQ iceberg/sweep events
        self._nq_detection_stats = EmpiricalDist(half_life=30, reservoir_size=200)
        self._nq_lead_lag_stats = EmpiricalDist(half_life=20, reservoir_size=100)

        # ─── MM Withdrawal Buffer ─────────────────────────
        self._mm_withdrawals = deque(maxlen=100)
        self._mm_tracker_ref = None  # set by schwab_bridge

        # ─── Stop Cluster Map ─────────────────────────────
        self._stop_clusters = []  # ranked list of {price, side, density, reasons}
        self._session_high = 0.0
        self._session_low = 999999.0

        # ─── Cascade Detection ────────────────────────────
        self._cascade_cooldown = 30.0
        self._last_cascade_signal = 0

        # ─── Phase 1a: MPID Velocity Divergence ──────────
        self._mpid_sizes = {}       # {mpid: {'bid': total_size, 'ask': total_size}}
        self._mvd_bid_dist = EmpiricalDist(half_life=60, reservoir_size=200)
        self._mvd_ask_dist = EmpiricalDist(half_life=60, reservoir_size=200)
        self._last_mvd_signal = 0
        self._last_mvd_context = None  # populated on sweep detection

        # ─── Phase 1c: Hawkes Intensity ──────────────────
        # No hardcoded parameters — mu is self-calibrated from
        # running mean of event rate, alpha/beta derived from
        # the empirical inter-arrival distribution.
        self._hawkes_lambda = 0.0   # current intensity (starts cold)
        self._hawkes_last_t = 0.0   # last event time
        self._hawkes_dist = EmpiricalDist(half_life=100, reservoir_size=300)
        self._hawkes_rate_dist = EmpiricalDist(half_life=50, reservoir_size=200)  # inter-event rate tracker
        self._hawkes_event_count = 0

        # ─── Greek Surface State (from zone_update) ───────
        self._prev_iv_skew = None     # For IV skew change detection
        self._prev_atm_iv = None      # For IV change direction (vanna trigger)
        self._iv_change_dist = EmpiricalDist(half_life=30, reservoir_size=200)
        self._charm_magnitude_dist = EmpiricalDist(half_life=40, reservoir_size=200)
        self._vanna_exposure_dist = EmpiricalDist(half_life=40, reservoir_size=200)
        self._speed_dist = EmpiricalDist(half_life=40, reservoir_size=200)

        # ─── Confluence State (anti-spam) ─────────────────
        self._prev_confluence_count = 0     # Track changes in count
        self._prev_confluence_strike = 0    # Track if strongest zone moved
        self._prev_spot_side = None         # Track price crossing zone boundary
        self._last_confluence_signal = 0    # Dedicated cooldown (60s min)

        # ─── Trade Volume Distribution (for tape glow scoring) ───
        # Tracks empirical distribution of trade sizes per symbol.
        # Used by score_trade() to compute regime-adaptive percentile ranks.
        self._trade_vol_dists = {}  # {symbol: EmpiricalDist}

        # ─── NBBO Venue Tracking (tick-speed MM detection) ────
        self._nbbo_bid_mic = {}     # {symbol: last bid_mic venue}
        self._nbbo_ask_mic = {}     # {symbol: last ask_mic venue}
        self._nbbo_bid_time = {}    # {symbol: last bid_time (ms epoch)}
        self._nbbo_ask_time = {}    # {symbol: last ask_time (ms epoch)}
        self._nbbo_venue_flips = deque(maxlen=200)  # venue flip events
        self._last_staleness_alert = 0

        # ─── Cross-Asset Divergence (SPY/VIX) ────────────────
        self._spy_price = 0.0
        self._spy_pct_change = 0.0
        self._vix_price = 0.0
        self._vix_pct_change = 0.0
        self._qqq_pct_change = 0.0
        self._cross_divergence_score = 0.0  # -1 (bearish) to +1 (bullish)
        self._last_cross_divergence_check = 0

        # ─── Microstructure Price Dominance (CVD Tape) ───────
        self._price_levels = {}           # {0.10_price_band: {'buy_vol': int, 'sell_vol': int, 'ticks': int, 'first_seen': float, 'last_seen': float}}
        self._current_price_band = 0.0    # The active $0.10 price band
        self._band_vol_dist = EmpiricalDist(half_life=60, reservoir_size=500)  # Tracks relative volume density per band
        self._last_dominance_signal = 0.0 # Cooldown for dominance alerts

        # ─── Multi-Engine Confluence Gate (CONVICTION) ────
        self._conviction_window = deque(maxlen=100)  # {type, family, is_long, confidence, timestamp}
        self._last_conviction_signal = 0.0  # 120s cooldown

        self._running = False

    def start(self):
        if self._streamer:
            self._streamer.on('NASDAQ_BOOK', self._on_book)
            self._streamer.on('SCREENER_OPTION', self._on_screener)
            self._running = True
            print("[EDGE] ✅ Edge detector v4 — regime+GEX+NQ+MM cross-asset engine active")

    def stop(self):
        self._running = False

    def get_cross_asset_context(self, symbol='NQ'):
        """Provide cross-asset context back to l2_worker.

        Called by l2_worker during iceberg detection to enrich NQ detections
        with QQQ-side intelligence (GEX zones, stop clusters, MM withdrawals).

        Returns dict with context or None if no data available.
        """
        # Map NQ → QQQ for GEX context
        qqq_sym = 'QQQ'

        # GEX context (use QQQ price, not NQ)
        gex_factor = 1.0
        gex_zone = 'NO_DATA'
        mid = self._last_prices.get(qqq_sym, 0)
        if mid > 0 and self._live_zones:
            # Determine direction from iceberg side (will be applied by l2_worker)
            # Return neutral context — l2_worker applies direction-specific logic
            gex_factor_long, gex_zone_long = self._gex_context(qqq_sym, True)
            gex_factor_short, gex_zone_short = self._gex_context(qqq_sym, False)
            # Return the more extreme factor
            if abs(gex_factor_long - 1.0) >= abs(gex_factor_short - 1.0):
                gex_factor = gex_factor_long
                gex_zone = gex_zone_long
            else:
                gex_factor = gex_factor_short
                gex_zone = gex_zone_short

        # Stop cluster proximity
        self._update_stop_clusters(qqq_sym)
        near_cluster = False
        cluster_price = 0
        cluster_side = ''
        if self._stop_clusters:
            nearest = self._stop_clusters[0]
            if nearest.get('distance_pct', 999) < 0.5:
                near_cluster = True
                cluster_price = nearest['price']
                cluster_side = nearest['side']

        # MM withdrawal bias
        mm_dir, mm_count = self._get_mm_consensus(lookback_sec=30)
        mm_bias = -1 if mm_dir == 'SHORT' else 1 if mm_dir == 'LONG' else 0

        return {
            'gex_zone': gex_zone,
            'gex_factor': round(gex_factor, 2),
            'near_stop_cluster': near_cluster,
            'stop_cluster_price': cluster_price,
            'stop_cluster_side': cluster_side,
            'mm_pull_bias': mm_bias,
            'mm_withdrawal_count': mm_count,
        }

    # ═══════════════════════════════════════════════════════
    #  GEX ZONE INTEGRATION
    # ═══════════════════════════════════════════════════════

    def update_zones(self, zone_data):
        """Called externally when zone_update fires (from schwab_bridge).

        zone_data = {
            'call_wall': float, 'put_wall': float,
            'gamma_flip': float, 'max_pain': float, ...
        }
        """
        with self._lock:
            self._live_zones = zone_data
            self._zones_updated_at = time.time()

        # Check Greek surface signals if data is present
        if zone_data.get('vanna_wall'):
            self._check_greek_signals(zone_data)

    def _gex_context(self, symbol, direction_is_long):
        """Classify GEX zone for feature logging. Factor is always 1.0 (neutral).

        SENIOR QUANT RULE: We do NOT amplify or discount confidence based on
        guessed multipliers. The zone LABEL is logged to edge_outcomes.jsonl
        as a feature. After 30+ sessions, we derive the actual win-rate-per-zone
        from the data and only THEN apply data-driven multipliers.

        Returns (factor, zone_label):
          factor = 1.0 always (neutral until proven otherwise)
          zone_label = classification for logging/display
        """
        mid = self._last_prices.get(symbol, 0)
        zones = self._live_zones

        if not zones or mid <= 0:
            return 1.0, 'NO_GEX'

        if time.time() - self._zones_updated_at > 60:
            return 1.0, 'GEX_STALE'

        call_wall = zones.get('call_wall', 0)
        put_wall = zones.get('put_wall', 0)
        gamma_flip = zones.get('gamma_flip', 0)

        if call_wall <= 0 or put_wall <= 0:
            return 1.0, 'GEX_INCOMPLETE'

        proximity_pct = 0.003  # 0.3% = "near" the wall

        if direction_is_long:
            if call_wall > 0 and abs(mid - call_wall) / mid < proximity_pct:
                return 1.0, 'AT_CALL_WALL_BREAKOUT'
            if put_wall > 0 and abs(mid - put_wall) / mid < proximity_pct:
                return 1.0, 'AT_PUT_WALL_SUPPORT'
            if gamma_flip > 0 and mid > gamma_flip:
                return 1.0, 'LONG_GAMMA_ZONE'
            if gamma_flip > 0 and mid < gamma_flip:
                return 1.0, 'SHORT_GAMMA_ZONE'
        else:
            if put_wall > 0 and abs(mid - put_wall) / mid < proximity_pct:
                return 1.0, 'AT_PUT_WALL_BREAKOUT'
            if call_wall > 0 and abs(mid - call_wall) / mid < proximity_pct:
                return 1.0, 'AT_CALL_WALL_RESIST'
            if gamma_flip > 0 and mid < gamma_flip:
                return 1.0, 'SHORT_GAMMA_ZONE'
            if gamma_flip > 0 and mid > gamma_flip:
                return 1.0, 'LONG_GAMMA_ZONE'

        if gamma_flip > 0 and abs(mid - gamma_flip) / mid < proximity_pct:
            return 1.0, 'AT_GAMMA_FLIP'

        return 1.0, 'OPEN_SPACE'

    # ═══════════════════════════════════════════════════════
    #  NQ FUTURES DETECTION FORWARDING
    # ═══════════════════════════════════════════════════════

    def on_nq_detection(self, detection_type, detection_data, symbol='NQ'):
        """Called by l2_worker when NQ iceberg/sweep/spoof fires.

        detection_type: 'iceberg' | 'drifting_iceberg' | 'sweep' | 'ignition' | 'spoof' | 'wall_gone'
        detection_data: dict from l2_worker (side, volume, cv, etc.)
        """
        if not self._running:
            return

        now = time.time()
        side = detection_data.get('side', '')
        # Normalize direction
        if detection_type in ('iceberg', 'drifting_iceberg'):
            # side is the AGGRESSOR — who is hitting the iceberg.
            # side='s' (sell aggressor hitting bid) → BID iceberg → buyer wall → BULLISH
            # side='b' (buy aggressor lifting ask) → ASK iceberg → seller wall → BEARISH
            # Trade WITH the iceberg (against the aggressor)
            is_long = side not in ('b', 'BID', 'bid', 'buy')  # FIXED: was inverted
        elif detection_type == 'sweep':
            # Sweep BUY = aggressive buyer = bullish
            is_long = side in ('b', 'BUY', 'buy')
        elif detection_type == 'wall_gone':
            is_long = side in ('ask', 'ASK')  # ask wall gone = resistance removed = bullish
        else:
            return  # ignition/spoof are noise, not directional

        with self._lock:
            self._nq_detections.append({
                'type': detection_type,
                'is_long': is_long,
                'data': detection_data,
                'timestamp': now,
                'qqq_price_at_detection': self._last_prices.get('QQQ', 0),
            })

        # ── Emit tape_alert to frontend for EQ Book glow ──
        if self._sio and detection_type in ('iceberg', 'drifting_iceberg', 'sweep', 'wall_gone'):
            vol = detection_data.get('total_vol', detection_data.get('volume', 0))
            price = detection_data.get('price', detection_data.get('mid', 0))
            # Compute percentile if distribution is warm
            pctl = 95.0  # default high
            if vol > 0:
                self._nq_trade_vol_dist.update(vol)
                if self._nq_trade_vol_dist.warm:
                    pctl = self._nq_trade_vol_dist.percentile_of(vol)
            # Determine tier from percentile
            if pctl >= 99:
                tier = 'whale'
            elif pctl >= 95:
                tier = 'inst'
            else:
                tier = 'sig'
            trade_side = 'buy' if is_long else 'sell'
            self._sio.emit('tape_alert', {
                'price': price,
                'timestamp': int(now * 1000),
                'tier': tier,
                'pctl': round(pctl, 0),
                'regime': self._get_regime(),
                'source': 'nq_detection',
                'detection_type': detection_type,
                'side': trade_side,
                'volume': vol,
            })

        # Log NQ detection
        direction = '🟢 LONG' if is_long else '🔴 SHORT'
        vol = detection_data.get('total_vol', detection_data.get('volume', '?'))
        print(f"[EDGE-NQ] {direction} {detection_type} @ NQ | vol={vol} | side={side}")

        # ── Emit tape_alert for verified NQ detection (force_glow) ──
        # These are pre-verified institutional events — the Python engine already
        # mathematically proved this was an iceberg/sweep. Always push to tape.
        if self._sio:
            price = detection_data.get('price', detection_data.get('at_price', 0))
            try:
                tape_side = 'b' if is_long else 's'
                self._sio.emit('tape_alert', {
                    'symbol': symbol,
                    'price': float(price) if price else 0,
                    'volume': int(vol) if isinstance(vol, (int, float)) else 0,
                    'side': tape_side,
                    'pctl': 99.9,  # verified — treat as extreme
                    'regime': self._get_regime(),
                    'regime_threshold': 90,
                    'tier': 'whale',
                    'timestamp': int(now * 1000),
                    'source': 'nq_detection',
                    'detection_type': detection_type,
                    'force_glow': True,
                })
            except Exception:
                pass  # never let emit errors break the detection engine

    def _get_nq_consensus(self, lookback_sec=30):
        """Get NQ detection consensus from recent detections.

        Returns (direction, strength_pctl, count):
          direction: 'LONG', 'SHORT', or None
          strength_pctl: how extreme the consensus is (0-100)
          count: number of detections in window
        """
        now = time.time()
        cutoff = now - lookback_sec

        recent = [d for d in self._nq_detections if d['timestamp'] >= cutoff]
        if not recent:
            return None, 0, 0

        long_count = sum(1 for d in recent if d['is_long'])
        short_count = sum(1 for d in recent if not d['is_long'])
        total = len(recent)

        # Need at least 2 detections for consensus
        if total < 2:
            return None, 0, total

        # Weighted by recency
        long_weight = sum(
            math.exp(-(now - d['timestamp']) / 15)  # 15s half-life
            for d in recent if d['is_long']
        )
        short_weight = sum(
            math.exp(-(now - d['timestamp']) / 15)
            for d in recent if not d['is_long']
        )

        if long_weight > short_weight * 1.5:
            # Update lead-lag tracker
            self._nq_detection_stats.update(total)
            pctl = self._nq_detection_stats.percentile_of(total) if self._nq_detection_stats.warm else 70.0
            return 'LONG', pctl, total
        elif short_weight > long_weight * 1.5:
            self._nq_detection_stats.update(total)
            pctl = self._nq_detection_stats.percentile_of(total) if self._nq_detection_stats.warm else 70.0
            return 'SHORT', pctl, total

        return None, 0, total  # mixed, no consensus

    # ═══════════════════════════════════════════════════════
    #  REGIME ACCESS
    # ═══════════════════════════════════════════════════════

    def _get_regime(self):
        try:
            from background_engine.l2_worker import _CURRENT_REGIME
            return _CURRENT_REGIME
        except (ImportError, AttributeError):
            return 'transition'

    def _get_regime_thresholds(self):
        regime = self._get_regime()
        return REGIME_PCTL_THRESHOLDS.get(regime, REGIME_PCTL_THRESHOLDS['transition'])

    # ═══════════════════════════════════════════════════════
    #  BOOK MICROSTRUCTURE
    # ═══════════════════════════════════════════════════════

    def _get_book_stats(self, symbol):
        if symbol not in self._book_stats:
            self._book_stats[symbol] = {
                'hit_rate': EmpiricalDist(half_life=50),
                'replenish_rate': EmpiricalDist(half_life=50),
                'price_var': EmpiricalDist(half_life=50),
                'spread': EmpiricalDist(half_life=80),
                'tob_size': EmpiricalDist(half_life=80),
                'net_delta': EmpiricalDist(half_life=80),
                'delta_change': EmpiricalDist(half_life=40),
                'total_bid': EmpiricalDist(half_life=80),
                'total_ask': EmpiricalDist(half_life=80),
                'size_change': EmpiricalDist(half_life=50),
                '_prev_bids': {},
                '_prev_asks': {},
                '_prev_best_bid': 0.0,
                '_recent_best_bids': deque(maxlen=30),
            }
        return self._book_stats[symbol]

    def _on_book(self, book_data):
        if not self._running:
            return

        symbol = book_data.get('symbol', '')
        bids = book_data.get('bids', [])
        asks = book_data.get('asks', [])
        if not bids and not asks:
            return

        with self._lock:
            stats = self._get_book_stats(symbol)

            best_bid = bids[0]['price'] if bids else 0
            best_ask = asks[0]['price'] if asks else 0
            spread = (best_ask - best_bid) if (best_bid > 0 and best_ask > 0) else 0

            tob_bid_size = bids[0]['size'] if bids else 0
            tob_ask_size = asks[0]['size'] if asks else 0
            tob_size = tob_bid_size + tob_ask_size

            total_bid = sum(b['size'] for b in bids)
            total_ask = sum(a['size'] for a in asks)
            net_delta = total_bid - total_ask

            prev_delta_mean = stats['net_delta'].mean if stats['net_delta'].warm else net_delta
            delta_change = net_delta - prev_delta_mean

            if best_bid > 0 and best_ask > 0:
                self._last_prices[symbol] = (best_bid + best_ask) / 2

            # Hit/replenish via book diff
            curr_bids = {b['price']: b['size'] for b in bids}
            curr_asks = {a['price']: a['size'] for a in asks}
            prev_bids = stats['_prev_bids']
            prev_asks = stats['_prev_asks']

            hits = 0
            replenishes = 0
            size_changes = []

            for book_prev, book_curr in [(prev_bids, curr_bids), (prev_asks, curr_asks)]:
                if not book_prev:
                    continue
                for price, prev_size in book_prev.items():
                    if prev_size <= 0:
                        continue
                    curr_size = book_curr.get(price, 0)
                    abs_change = abs(curr_size - prev_size)
                    if abs_change > 0:
                        size_changes.append(abs_change)

                    if stats['size_change'].warm:
                        change_pctl = stats['size_change'].percentile_of(abs_change)
                        if curr_size < prev_size and change_pctl >= 70:
                            hits += 1
                        elif curr_size > prev_size and change_pctl >= 70:
                            replenishes += 1
                    else:
                        change_ratio = curr_size / prev_size if prev_size > 0 else 1
                        if change_ratio < 0.7:
                            hits += 1
                        elif change_ratio > 1.3:
                            replenishes += 1

            for sc in size_changes:
                stats['size_change'].update(sc)

            stats['_prev_bids'] = curr_bids
            stats['_prev_asks'] = curr_asks

            if best_bid > 0:
                stats['_recent_best_bids'].append(best_bid)

            price_var = 0.0
            if len(stats['_recent_best_bids']) >= 5:
                prices = list(stats['_recent_best_bids'])
                p_mean = sum(prices) / len(prices)
                price_var = sum((p - p_mean) ** 2 for p in prices) / len(prices)

            stats['hit_rate'].update(hits)
            stats['replenish_rate'].update(replenishes)
            stats['price_var'].update(price_var)
            stats['spread'].update(spread)
            stats['tob_size'].update(tob_size)
            stats['net_delta'].update(net_delta)
            stats['delta_change'].update(delta_change)
            stats['total_bid'].update(total_bid)
            stats['total_ask'].update(total_ask)

            self._detect_absorption(symbol, stats, hits, replenishes, price_var)
            self._detect_exhaustion(symbol, stats, hits, spread, tob_size)
            self._check_hedge_confirmation(symbol, stats, net_delta, delta_change)
            self._resolve_outcomes(symbol)

            # Phase 1a: MPID sweep detection (QQQ equity book)
            self._update_mvd(symbol, bids, asks)

            # Phase 1c: Hawkes intensity (aggressive flow)
            if hits > 0:
                self._update_hawkes(is_aggressive=True)
            else:
                self._update_hawkes(is_aggressive=False)

    # ═══════════════════════════════════════════════════════
    #  ABSORPTION DETECTION
    # ═══════════════════════════════════════════════════════

    def _detect_absorption(self, symbol, stats, hits, replenishes, price_var):
        if not stats['hit_rate'].warm:
            return

        high_pctl, low_pctl, persist_abs, _ = self._get_regime_thresholds()

        hit_pctl = stats['hit_rate'].percentile_of(hits)
        replenish_pctl = stats['replenish_rate'].percentile_of(replenishes)
        price_pctl = stats['price_var'].percentile_of(price_var)

        hit_elevated = hit_pctl >= high_pctl
        replenish_elevated = replenish_pctl >= high_pctl
        price_suppressed = price_pctl <= (100 - high_pctl + 10)

        if hit_elevated and replenish_elevated and price_suppressed:
            self._absorption_ticks[symbol] = self._absorption_ticks.get(symbol, 0) + 1
        else:
            self._absorption_ticks[symbol] = 0

        if self._absorption_ticks.get(symbol, 0) >= persist_abs:
            net_delta = stats['net_delta'].mean if stats['net_delta'].warm else 0
            direction = 'LONG' if net_delta > 0 else 'SHORT'
            is_long = direction == 'LONG'

            raw_confidence = self._joint_confidence([hit_pctl, replenish_pctl, 100.0 - price_pctl])

            # GEX amplification
            gex_factor, gex_zone = self._gex_context(symbol, is_long)
            confidence = min(99.9, raw_confidence * gex_factor)

            # NQ consensus
            nq_dir, nq_pctl, nq_count = self._get_nq_consensus()
            nq_agrees = nq_dir == direction if nq_dir else None

            components = {
                'hit_rate_pctl': round(hit_pctl, 1),
                'replenish_rate_pctl': round(replenish_pctl, 1),
                'price_var_pctl': round(price_pctl, 1),
                'net_delta': round(net_delta, 1),
                'consecutive_ticks': self._absorption_ticks[symbol],
                'regime': self._get_regime(),
                'gex_zone': gex_zone,
                'gex_factor': round(gex_factor, 2),
            }
            if nq_dir:
                components['nq_consensus'] = nq_dir
                components['nq_agrees'] = nq_agrees
                components['nq_detections'] = nq_count
                # NQ agreement logged as feature only — no multiplier bias
                # Data will prove if NQ confirmation adds alpha

            self._emit_signal(f'ABSORPTION_{direction}', confidence, components, symbol)
            self._absorption_ticks[symbol] = 0

    # ═══════════════════════════════════════════════════════
    #  EXHAUSTION DETECTION
    # ═══════════════════════════════════════════════════════

    def _detect_exhaustion(self, symbol, stats, hits, spread, tob_size):
        if not stats['hit_rate'].warm:
            return

        high_pctl, low_pctl, _, persist_exh = self._get_regime_thresholds()

        hit_pctl = stats['hit_rate'].percentile_of(hits)
        spread_pctl = stats['spread'].percentile_of(spread)
        tob_pctl = stats['tob_size'].percentile_of(tob_size)

        hit_collapsed = hit_pctl <= low_pctl
        spread_widening = spread_pctl >= (high_pctl - 5)
        book_thin = tob_pctl <= (low_pctl + 10)

        if hit_collapsed and spread_widening and book_thin:
            self._exhaustion_ticks[symbol] = self._exhaustion_ticks.get(symbol, 0) + 1
        else:
            self._exhaustion_ticks[symbol] = 0

        if self._exhaustion_ticks.get(symbol, 0) >= persist_exh:
            total_bid = stats['total_bid'].mean if stats['total_bid'].warm else 0
            total_ask = stats['total_ask'].mean if stats['total_ask'].warm else 0
            direction = 'LONG' if total_bid > total_ask else 'SHORT'
            is_long = direction == 'LONG'

            raw_confidence = self._joint_confidence([100.0 - hit_pctl, spread_pctl, 100.0 - tob_pctl])
            gex_factor, gex_zone = self._gex_context(symbol, is_long)
            confidence = min(99.9, raw_confidence * gex_factor)

            nq_dir, nq_pctl, nq_count = self._get_nq_consensus()
            nq_agrees = nq_dir == direction if nq_dir else None

            components = {
                'hit_rate_pctl': round(hit_pctl, 1),
                'spread_pctl': round(spread_pctl, 1),
                'tob_size_pctl': round(tob_pctl, 1),
                'consecutive_ticks': self._exhaustion_ticks[symbol],
                'regime': self._get_regime(),
                'gex_zone': gex_zone,
                'gex_factor': round(gex_factor, 2),
            }
            if nq_dir:
                components['nq_consensus'] = nq_dir
                components['nq_agrees'] = nq_agrees
                components['nq_detections'] = nq_count
                # NQ agreement logged as feature only — no multiplier bias

            self._emit_signal(f'EXHAUSTION_{direction}', confidence, components, symbol)
            self._exhaustion_ticks[symbol] = 0

    # ═══════════════════════════════════════════════════════
    #  SCREENER ANOMALY (PER-UNDERLYING)
    # ═══════════════════════════════════════════════════════

    def _get_screener_dist(self, underlying):
        if underlying not in self._screener_vol_by_underlying:
            self._screener_vol_by_underlying[underlying] = EmpiricalDist(half_life=40, reservoir_size=300)
        return self._screener_vol_by_underlying[underlying]

    def _extract_underlying(self, option_symbol):
        sym = option_symbol.strip()
        match = re.match(r'^([A-Z]{1,6})\s', sym)
        if match:
            return match.group(1)
        match = re.match(r'^([A-Z]{1,6})\d{6}[CP]', sym)
        if match:
            return match.group(1)
        return sym[:3]

    def _on_screener(self, data):
        if not self._running:
            return
        items = data.get('items', [])
        if not items:
            return

        now = time.time()
        high_pctl = self._get_regime_thresholds()[0]

        with self._lock:
            for item in items:
                vol = item.get('totalVolume', 0)
                if vol <= 0:
                    continue

                sym = item.get('symbol', '')
                underlying = self._extract_underlying(sym)
                dist = self._get_screener_dist(underlying)
                dist.update(vol)

                if not dist.warm:
                    continue

                vol_pctl = dist.percentile_of(vol)
                if vol_pctl >= high_pctl:
                    cp_match = re.search(r'\d{6}([CP])', sym)
                    if not cp_match:
                        continue

                    option_type = 'CALL' if cp_match.group(1) == 'C' else 'PUT'

                    self._screener_buffer.append({
                        'symbol': sym,
                        'underlying': underlying,
                        'option_type': option_type,
                        'totalVolume': vol,
                        'vol_pctl': vol_pctl,
                        'lastPrice': item.get('lastPrice', 0),
                        'netPercentChange': item.get('netPercentChange', 0),
                        'timestamp': now,
                    })

    # ═══════════════════════════════════════════════════════
    #  DELTA HEDGE CONFIRMATION
    # ═══════════════════════════════════════════════════════

    def _check_hedge_confirmation(self, symbol, stats, net_delta, delta_change):
        if not stats['delta_change'].warm:
            return

        now = time.time()
        regime = self._get_regime()
        high_pctl, low_pctl, _, _ = self._get_regime_thresholds()

        if self._hedge_latency_stats.warm:
            tau = self._hedge_latency_stats.value_at_percentile(75)
        else:
            tau = 10.0

        # Get MM consensus for validation
        mm_dir, mm_count = self._get_mm_consensus(lookback_sec=30)

        confirmed = []
        for alert in list(self._screener_buffer):
            age = now - alert['timestamp']
            if age > tau * 2:
                continue

            delta_pctl = stats['delta_change'].percentile_of(delta_change)
            is_long = alert['option_type'] == 'CALL'
            alert_dir = 'LONG' if is_long else 'SHORT'

            if is_long and delta_pctl >= high_pctl:
                signal_type = 'DELTA_HEDGE_LONG'
            elif not is_long and delta_pctl <= low_pctl:
                signal_type = 'DELTA_HEDGE_SHORT'
            else:
                continue

            # 1. P90+ Paradox Filter: High win rate up to P90, terrible above it (exhaustion)
            if is_long and delta_pctl >= 90.0:
                continue
            if not is_long and delta_pctl <= 10.0:
                continue

            # 2. Market Maker validation
            if mm_dir:
                if mm_dir != alert_dir:
                    continue  # Abort if MM explicitly pulling opposite side
            elif regime == 'short_gamma_volatile':
                continue  # In volatile regimes, REQUIRE MM agreement to fire

            # 3. Anti-spam & Whipsaw Flow Control
            if self._last_delta_hedge_ts > 0:
                time_since = now - self._last_delta_hedge_ts
                if self._last_delta_hedge_dir == alert_dir:
                    if time_since < 30.0:
                        continue  # 30s same-direction spam cooldown
                else:
                    if time_since < 45.0:
                        continue  # 45s whipsaw cooldown

            self._last_delta_hedge_ts = now
            self._last_delta_hedge_dir = alert_dir

            confirmed.append((signal_type, alert, delta_pctl))
            self._hedge_latency_stats.update(age)


        for signal_type, alert, delta_pctl in confirmed:
            flow_confirmed = True
            flow_components = {}
            if self._flow:
                report = self._flow.get_report(symbol)
                if report:
                    flow_components = {
                        'flow_size_score': report.get('size_score', 50),
                        'flow_sweep_score': report.get('sweep_score', 50),
                        'flow_overall': report.get('overall_score', 50),
                    }
                    flow_stats = self._get_flow_stats(symbol)
                    size_score = report.get('size_score', 50)
                    flow_stats['size_score'].update(size_score)
                    if flow_stats['size_score'].warm:
                        flow_confirmed = flow_stats['size_score'].percentile_of(size_score) >= 60.0

            if flow_confirmed:
                is_long = signal_type.endswith('LONG')
                vol_pctl = alert.get('vol_pctl', 95.0)
                delta_extremity = delta_pctl if is_long else (100.0 - delta_pctl)
                raw_confidence = self._joint_confidence([delta_extremity, vol_pctl])

                gex_factor, gex_zone = self._gex_context(symbol, is_long)
                confidence = min(99.9, raw_confidence * gex_factor)

                nq_dir, nq_pctl, nq_count = self._get_nq_consensus()
                nq_agrees = (nq_dir == ('LONG' if is_long else 'SHORT')) if nq_dir else None

                components = {
                    'screener_vol_pctl': round(vol_pctl, 1),
                    'delta_change_pctl': round(delta_pctl, 1),
                    'underlying': alert.get('underlying', ''),
                    'option_type': alert['option_type'],
                    'option_symbol': alert['symbol'],
                    'option_volume': alert['totalVolume'],
                    'hedge_latency_sec': round(now - alert['timestamp'], 2),
                    'regime': self._get_regime(),
                    'gex_zone': gex_zone,
                    'gex_factor': round(gex_factor, 2),
                    **flow_components,
                }
                if nq_dir:
                    components['nq_consensus'] = nq_dir
                    components['nq_agrees'] = nq_agrees
                    components['nq_detections'] = nq_count

                # ── Vanna/Charm confluence enrichment (logged as features) ──
                if self._greek_surface:
                    try:
                        qqq_price = self._last_prices.get(symbol, 0)
                        if qqq_price > 0:
                            # Vanna direction: does IV-driven delta shift agree?
                            atm_iv = self._live_zones.get('atm_iv', 0)
                            iv_dir = 1 if (self._prev_atm_iv and atm_iv > self._prev_atm_iv) else -1
                            vanna_dir = self._greek_surface.get_vanna_direction(qqq_price, iv_dir)
                            components['vanna_agrees'] = (vanna_dir == 'LONG') == is_long
                            components['vanna_direction'] = vanna_dir

                            # Charm bias: time decay pulling which way?
                            from datetime import datetime
                            et_hour = datetime.now().hour + datetime.now().minute / 60.0
                            charm_dir, charm_str = self._greek_surface.get_charm_bias(et_hour)
                            components['charm_agrees'] = (charm_dir == 'UP') == is_long
                            components['charm_direction'] = charm_dir
                            components['charm_strength'] = round(charm_str, 3)

                            # Speed context: is gamma accelerating?
                            speed_sign, speed_label = self._greek_surface.get_speed_context(qqq_price)
                            components['speed_label'] = speed_label

                            # Confluence check: is this price on a multi-Greek wall?
                            is_conf, conf_signals, conf_types = self._greek_surface.get_confluence_at_price(qqq_price)
                            if is_conf:
                                components['at_confluence'] = True
                                components['confluence_signals'] = conf_signals
                                components['confluence_types'] = conf_types
                    except Exception:
                        pass

                # ── Vol Surface regime enrichment ──
                if self._vol_surface:
                    try:
                        vol_adj = self._vol_surface.get_regime_adjustments()
                        components['vol_regime'] = vol_adj.get('vol_regime', 'UNKNOWN')
                        vol_state = self._vol_surface.get_state()
                        components['vol_premium'] = vol_state.get('vol_premium', 0)
                        components['iv_rank'] = vol_state.get('iv_rank', 50)
                        components['skew_velocity'] = vol_state.get('skew_velocity', 0)
                    except Exception:
                        pass

                self._emit_signal(signal_type, confidence, components, symbol)

                try:
                    self._screener_buffer.remove(alert)
                except ValueError:
                    pass

    # ═══════════════════════════════════════════════════════
    #  FLOW DIVERGENCE
    # ═══════════════════════════════════════════════════════

    def _get_flow_stats(self, symbol):
        if symbol not in self._flow_stats:
            self._flow_stats[symbol] = {
                'size_score': EmpiricalDist(half_life=40),
                'venue_score': EmpiricalDist(half_life=40),
                'iceberg_score': EmpiricalDist(half_life=40),
                'sweep_score': EmpiricalDist(half_life=40),
                'pressure_score': EmpiricalDist(half_life=40),
            }
        return self._flow_stats[symbol]

    def check_flow_divergence(self, symbol):
        if not self._flow or not self._running:
            return

        report = self._flow.get_report(symbol)
        if not report or report.get('total_updates', 0) < 10:
            return

        flow_stats = self._get_flow_stats(symbol)
        for key in ['size_score', 'venue_score', 'iceberg_score', 'sweep_score', 'pressure_score']:
            flow_stats[key].update(report.get(key, 50))

        if not flow_stats['size_score'].warm:
            return

        high_pctl, low_pctl, _, _ = self._get_regime_thresholds()
        size_score = report['size_score']
        iceberg_score = report['iceberg_score']
        sweep_score = report['sweep_score']
        pressure = report['pressure_score']

        size_pctl = flow_stats['size_score'].percentile_of(size_score)
        iceberg_pctl = flow_stats['iceberg_score'].percentile_of(iceberg_score)
        sweep_pctl = flow_stats['sweep_score'].percentile_of(sweep_score)

        # Divergence
        if size_pctl <= (100 - high_pctl + 15) and iceberg_pctl >= (high_pctl - 15) and pressure < 45:
            raw_confidence = self._joint_confidence([100.0 - size_pctl, iceberg_pctl])
            gex_factor, gex_zone = self._gex_context(symbol, True)
            confidence = min(99.9, raw_confidence * gex_factor)

            self._emit_signal('FLOW_DIVERGENCE_LONG', confidence, {
                'size_score_pctl': round(size_pctl, 1),
                'iceberg_score_pctl': round(iceberg_pctl, 1),
                'sweep_score_pctl': round(sweep_pctl, 1),
                'pressure': round(pressure, 1),
                'raw_size_score': round(size_score, 1),
                'raw_iceberg_score': round(iceberg_score, 1),
                'regime': self._get_regime(),
                'gex_zone': gex_zone,
            }, symbol)

        # Alignment
        elif size_pctl >= (high_pctl - 15) and sweep_pctl >= (high_pctl - 15) and iceberg_pctl <= 40 and pressure < 45:
            raw_confidence = self._joint_confidence([size_pctl, sweep_pctl])
            gex_factor, gex_zone = self._gex_context(symbol, False)
            confidence = min(99.9, raw_confidence * gex_factor)

            self._emit_signal('FLOW_ALIGNMENT_SHORT', confidence, {
                'size_score_pctl': round(size_pctl, 1),
                'sweep_score_pctl': round(sweep_pctl, 1),
                'iceberg_score_pctl': round(iceberg_pctl, 1),
                'pressure': round(pressure, 1),
                'raw_size_score': round(size_score, 1),
                'raw_sweep_score': round(sweep_score, 1),
                'regime': self._get_regime(),
                'gex_zone': gex_zone,
            }, symbol)

    # ═══════════════════════════════════════════════════════
    #  JOINT PROBABILITY CONFIDENCE
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _joint_confidence(pctl_list):
        """Joint confidence from independent percentile observations.
        Two P95 events = P99.75, not (P95+P95)/2 = P95."""
        if not pctl_list:
            return 50.0
        tail_probs = []
        for p in pctl_list:
            p = max(50.1, min(99.99, p))
            tail_probs.append(1.0 - p / 100.0)
        joint_tail = 1.0
        for tp in tail_probs:
            joint_tail *= tp
        return round(min(99.99, max(50.0, (1.0 - joint_tail) * 100.0)), 1)

    # ═══════════════════════════════════════════════════════
    #  SIGNAL OUTCOME FEEDBACK
    # ═══════════════════════════════════════════════════════

    def _record_outcome(self, signal_type, symbol, confidence, direction_is_long):
        now = time.time()
        mid = self._last_prices.get(symbol, 0)
        if mid <= 0:
            return
        # Phase 1 feature snapshot at signal time
        cross_score = self.get_cross_asset_score()
        hawkes_pctl = self.get_hawkes_pctl()
        _, gex_zone = self._gex_context(symbol, direction_is_long)
        regime = self._get_regime()
        nbbo_dir, nbbo_count = self.get_nbbo_venue_consensus(lookback_sec=15)
        cross_div = self.get_cross_divergence_score()
        self._pending_outcomes.append({
            'signal_type': signal_type, 'symbol': symbol,
            'confidence': confidence, 'is_long': direction_is_long,
            'entry_price': mid, 'entry_ts': now,
            'check_10s': now + 10, 'check_30s': now + 30, 'check_60s': now + 60,
            'outcome_10s': None, 'outcome_30s': None, 'outcome_60s': None,
            # Phase 1 features for model training
            'cross_asset_score': round(cross_score, 3),
            'hawkes_pctl': round(hawkes_pctl, 1),
            'gex_zone': gex_zone,
            'regime': regime,
            # Phase 3 features: NBBO venue + cross-asset divergence
            'nbbo_venue_dir': nbbo_dir,
            'nbbo_venue_count': nbbo_count,
            'cross_divergence': cross_div,
            'spy_price': round(self._spy_price, 2),
            'vix_price': round(self._vix_price, 2),
            'vix_pct': round(self._vix_pct_change, 2),
        })

    def _resolve_outcomes(self, symbol):
        mid = self._last_prices.get(symbol, 0)
        if mid <= 0:
            return
        now = time.time()
        still_pending = deque(maxlen=200)
        for p in self._pending_outcomes:
            if p['symbol'] != symbol:
                still_pending.append(p)
                continue
            direction = 1 if p['is_long'] else -1
            if p['outcome_10s'] is None and now >= p['check_10s']:
                p['outcome_10s'] = round((mid - p['entry_price']) * direction, 4)
            if p['outcome_30s'] is None and now >= p['check_30s']:
                p['outcome_30s'] = round((mid - p['entry_price']) * direction, 4)
            if p['outcome_60s'] is None and now >= p['check_60s']:
                p['outcome_60s'] = round((mid - p['entry_price']) * direction, 4)
                self._completed_outcomes.append(p)
                # ── Persist to JSONL ──
                self._persist_edge_outcome(p)
                continue
            still_pending.append(p)
        self._pending_outcomes = still_pending

    def _persist_edge_outcome(self, outcome):
        """Append completed edge signal outcome to JSONL log file."""
        try:
            import os, json
            log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "edge_outcomes.jsonl")
            record = {
                "signal_type": outcome["signal_type"],
                "symbol": outcome["symbol"],
                "confidence": outcome["confidence"],
                "is_long": outcome["is_long"],
                "entry_price": outcome["entry_price"],
                "entry_ts": outcome["entry_ts"],
                "ts_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(outcome["entry_ts"])),
                "outcome_10s": outcome["outcome_10s"],
                "outcome_30s": outcome["outcome_30s"],
                "outcome_60s": outcome["outcome_60s"],
                "win_10s": outcome["outcome_10s"] is not None and outcome["outcome_10s"] > 0,
                "win_30s": outcome["outcome_30s"] is not None and outcome["outcome_30s"] > 0,
                "win_60s": outcome["outcome_60s"] is not None and outcome["outcome_60s"] > 0,
                # Phase 1 features at signal time
                "cross_asset_score": outcome.get("cross_asset_score"),
                "hawkes_pctl": outcome.get("hawkes_pctl"),
                "gex_zone": outcome.get("gex_zone"),
                "regime": outcome.get("regime"),
                # Phase 3 features: NBBO venue + cross-asset divergence
                "nbbo_venue_dir": outcome.get("nbbo_venue_dir"),
                "nbbo_venue_count": outcome.get("nbbo_venue_count"),
                "cross_divergence": outcome.get("cross_divergence"),
                "spy_price": outcome.get("spy_price"),
                "vix_price": outcome.get("vix_price"),
                "vix_pct": outcome.get("vix_pct"),
            }
            # ── Phase 2: Greek surface features for model training ──
            # Snapshot the full Greek state at signal time.
            # These are the features that will drive win-rate-per-Greek analysis.
            zones = self._live_zones
            if zones:
                record.update({
                    "vanna_wall": zones.get("vanna_wall", 0),
                    "charm_direction": zones.get("charm_direction", ""),
                    "charm_magnitude": zones.get("charm_magnitude", 0),
                    "speed_at_spot": zones.get("speed_at_spot", 0),
                    "speed_sign": zones.get("speed_sign", ""),
                    "zomma_at_spot": zones.get("zomma_at_spot", 0),
                    "iv_skew": zones.get("iv_skew", 1.0),
                    "iv_skew_label": zones.get("iv_skew_label", ""),
                    "term_structure": zones.get("term_structure", ""),
                    "confluence_count": zones.get("confluence_count", 0),
                    "vol_regime": zones.get("vol_regime", ""),
                    "vol_premium": zones.get("vol_premium", 0),
                    "iv_rank": zones.get("iv_rank", 50),
                    "avg_mispricing_pct": zones.get("avg_mispricing_pct", 0),
                    "mark_flow_direction": zones.get("mark_flow_direction", ""),
                    "mm_uncertainty": zones.get("mm_uncertainty", 0),
                    "iv_spread": zones.get("iv_spread", 0),
                    "mean_iv": zones.get("mean_iv", 0),
                    "net_premium_m": zones.get("net_premium_m", 0),
                    "net_theta_m": zones.get("net_theta_m", 0),
                })
            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            print(f"[EDGE] Outcome persist error: {e}")

    # ═══════════════════════════════════════════════════════
    #  SIGNAL EMISSION
    # ═══════════════════════════════════════════════════════

    def _emit_signal(self, signal_type, confidence, components, symbol='QQQ'):
        now = time.time()
        last = self._last_signal.get(signal_type, 0)
        # Regime-adaptive cooldown
        regime = self._get_regime()
        cooldown = self._COOLDOWN_BY_REGIME.get(regime, 10.0)
        if now - last < cooldown:
            return
        self._last_signal[signal_type] = now

        # Broadcast regime to frontend (throttled to once per regime change or 10s)
        if self._sio and (regime != self._last_broadcast_regime or now - self._last_regime_broadcast > 10.0):
            self._sio.emit('regime_update', {'regime': regime, 'timestamp': int(now * 1000)})
            self._last_broadcast_regime = regime
            self._last_regime_broadcast = now

        # AUDIT 2026-04-01: P90-100 confidence paradox — signals above P90
        # underperform P80-90 (overfit indicator). Dampen to P85 max for
        # outcome tracking to avoid recording noise as high-confidence.
        effective_confidence = min(confidence, 89.9)

        accuracy = self._get_signal_accuracy(signal_type)

        signal = {
            'type': signal_type,
            'symbol': symbol,
            'confidence_pctl': round(confidence, 1),
            'components': components,
            'timestamp': int(now * 1000),
        }
        if accuracy:
            signal['track_record'] = accuracy

        # Phase 1 enrichment: cross-asset score, Hawkes, MVD, DEX
        cross_score = self.get_cross_asset_score()
        hawkes_pctl = self.get_hawkes_pctl()
        components['cross_asset_score'] = round(cross_score, 3)
        components['hawkes_pctl'] = round(hawkes_pctl, 1)
        if self._last_mvd_context and (now - self._last_mvd_signal) < 10:
            components['mvd'] = self._last_mvd_context
        # DEX (Delta Exposure) — first-order dealer directional positioning
        if self._live_zones:
            components['total_dex'] = self._live_zones.get('total_dex', 0)
            components['dex_wall_long'] = self._live_zones.get('dex_wall_long', 0)
            components['dex_wall_short'] = self._live_zones.get('dex_wall_short', 0)
            # Net Premium — dollar commitment bias
            components['net_premium_m'] = self._live_zones.get('net_premium_m', 0)
            # Mean IV — OI-weighted implied volatility (%)
            components['mean_iv'] = self._live_zones.get('mean_iv', 0)
            # Net Theta — daily time decay ($M)
            components['net_theta_m'] = self._live_zones.get('net_theta_m', 0)
            # ── Higher-order Greek features (for outcome analysis) ──
            components['vanna_wall'] = self._live_zones.get('vanna_wall', 0)
            components['charm_direction'] = self._live_zones.get('charm_direction', '')
            components['charm_magnitude'] = self._live_zones.get('charm_magnitude', 0)
            components['speed_at_spot'] = self._live_zones.get('speed_at_spot', 0)
            components['speed_sign'] = self._live_zones.get('speed_sign', '')
            components['zomma_at_spot'] = self._live_zones.get('zomma_at_spot', 0)
            components['iv_skew'] = self._live_zones.get('iv_skew', 1.0)
            components['iv_skew_label'] = self._live_zones.get('iv_skew_label', '')
            components['term_structure'] = self._live_zones.get('term_structure', '')
            components['confluence_count'] = self._live_zones.get('confluence_count', 0)

        # ── NBBO venue consensus (tick-speed MM detection) ──
        nbbo_dir, nbbo_count = self.get_nbbo_venue_consensus(lookback_sec=15)
        if nbbo_dir:
            components['nbbo_venue_consensus'] = nbbo_dir
            components['nbbo_venue_count'] = nbbo_count

        # ── Cross-asset divergence (SPY/VIX) ──
        components['cross_divergence'] = self.get_cross_divergence_score()
        if self._spy_price > 0:
            components['spy_price'] = round(self._spy_price, 2)
            components['spy_pct'] = round(self._spy_pct_change, 2)
        if self._vix_price > 0:
            components['vix_price'] = round(self._vix_price, 2)
            components['vix_pct'] = round(self._vix_pct_change, 2)

        direction = '🟢' if 'LONG' in signal_type else '🔴'
        regime = components.get('regime', self._get_regime())
        gex_zone = components.get('gex_zone', '')
        nq_tag = ' NQ✓' if components.get('nq_agrees') else (' NQ✗' if components.get('nq_agrees') is False else '')
        acc_str = f" WR={accuracy['win_rate_30s']}%" if accuracy else ""
        hawkes_str = f" λ={hawkes_pctl:.0f}"
        cross_str = f" X={cross_score:+.2f}"
        mvd_str = f" MVD={components['mvd']['side']}" if 'mvd' in components else ""
        print(f"[EDGE] {direction} {signal_type} | P{confidence:.0f} | {symbol} | {regime} | {gex_zone}{nq_tag}{acc_str}{hawkes_str}{cross_str}{mvd_str}")

        if self._sio:
            self._sio.emit('edge_signal', signal)

        is_long = 'LONG' in signal_type
        self._record_outcome(signal_type, symbol, effective_confidence, is_long)

        # ── Multi-Engine Confluence Gate ──
        # Log this signal into the conviction window (skip CONVICTION signals to avoid recursion)
        family = self.ENGINE_FAMILIES.get(signal_type)
        if family:
            self._conviction_window.append({
                'type': signal_type,
                'family': family,
                'is_long': is_long,
                'confidence': confidence,
                'timestamp': now,
                'symbol': symbol,
            })
            self._check_conviction(symbol)

    def _get_signal_accuracy(self, signal_type):
        outcomes = [o for o in self._completed_outcomes if o['signal_type'] == signal_type]
        if len(outcomes) < 5:
            return None
        wins_30s = sum(1 for o in outcomes if o['outcome_30s'] is not None and o['outcome_30s'] > 0)
        avg_30s = sum(o['outcome_30s'] for o in outcomes if o['outcome_30s'] is not None) / len(outcomes)
        return {
            'win_rate_30s': round(wins_30s / len(outcomes) * 100, 1),
            'avg_move_30s': round(avg_30s, 4),
            'sample_size': len(outcomes),
        }

    # ═══════════════════════════════════════════════════════
    #  MULTI-ENGINE CONFLUENCE GATE (CONVICTION SIGNALS)
    # ═══════════════════════════════════════════════════════

    def _check_conviction(self, symbol='QQQ'):
        """Check if 3+ independent engine families agree on direction within 60s.

        When multiple independent detection engines all confirm the same
        directional bias simultaneously, the probability of a large move
        (20+ points / $0.20+) increases dramatically.
        """
        now = time.time()

        # 120s cooldown on CONVICTION signals
        if now - self._last_conviction_signal < 120.0:
            return

        # Collect signals from the last 60 seconds
        cutoff = now - 60.0
        recent_long = {}   # {family: best_confidence}
        recent_short = {}  # {family: best_confidence}
        long_sources = []
        short_sources = []

        for sig in self._conviction_window:
            if sig['timestamp'] < cutoff:
                continue
            family = sig['family']
            conf = sig['confidence']
            if sig['is_long']:
                if family not in recent_long or conf > recent_long[family]:
                    recent_long[family] = conf
                long_sources.append(sig['type'])
            else:
                if family not in recent_short or conf > recent_short[family]:
                    recent_short[family] = conf
                short_sources.append(sig['type'])

        # Check LONG conviction
        if len(recent_long) >= 3:
            self._fire_conviction('LONG', recent_long, long_sources, symbol, now)
            return

        # Check SHORT conviction
        if len(recent_short) >= 3:
            self._fire_conviction('SHORT', recent_short, short_sources, symbol, now)
            return

    def _fire_conviction(self, direction, family_confidences, source_types, symbol, now):
        """Emit a CONVICTION signal with joint probability confidence."""
        self._last_conviction_signal = now

        # Joint confidence from independent families
        conf_values = list(family_confidences.values())
        confidence = self._joint_confidence(conf_values)

        families = sorted(family_confidences.keys())
        is_long = direction == 'LONG'
        gex_factor, gex_zone = self._gex_context(symbol, is_long)
        confidence = min(99.9, confidence * gex_factor)

        # Deduplicate source types for logging
        unique_sources = sorted(set(source_types))

        components = {
            'families': families,
            'family_count': len(families),
            'sources': unique_sources,
            'family_confidences': {f: round(c, 1) for f, c in family_confidences.items()},
            'regime': self._get_regime(),
            'gex_zone': gex_zone,
            'gex_factor': round(gex_factor, 2),
        }

        signal_type = f'CONVICTION_{direction}'
        print(f"[CONVICTION] 🔥 {signal_type} | P{confidence:.0f} | {len(families)} families: {', '.join(families)} | sources: {', '.join(unique_sources)}")

        # Emit through the standard pipeline but bypass _check_conviction recursion
        # because CONVICTION is not in ENGINE_FAMILIES
        now_ts = time.time()
        signal = {
            'type': signal_type,
            'symbol': symbol,
            'confidence_pctl': round(confidence, 1),
            'components': components,
            'timestamp': int(now_ts * 1000),
        }

        if self._sio:
            self._sio.emit('edge_signal', signal)

        self._record_outcome(signal_type, symbol, min(confidence, 89.9), is_long)

    # ═══════════════════════════════════════════════════════
    #  MM WITHDRAWAL HANDLING
    # ═══════════════════════════════════════════════════════

    def on_mm_withdrawal(self, withdrawal):
        """Called by MMTracker when significant MM quote-pulling detected.

        withdrawal = {
            'direction': 'LONG' | 'SHORT' | 'UNCERTAIN',
            'pull_type': 'BID_PULL' | 'ASK_PULL' | 'BOTH_PULL',
            'bid_mm_delta': int, 'ask_mm_delta': int,
            'bid_mm_pctl': float, 'ask_mm_pctl': float,
            ...
        }
        """
        if not self._running:
            return

        with self._lock:
            self._mm_withdrawals.append(withdrawal)

        # Check for cascade conditions immediately
        self._check_cascade(withdrawal.get('symbol', 'QQQ'))

    def on_dte0_squeeze(self, squeeze):
        """Called by DTE0SqueezeDetector when dealer delta squeeze conditions met.

        squeeze = {
            'direction': 'LONG' | 'SHORT',
            'squeeze_type': 'CALL_SQUEEZE' | 'PUT_SQUEEZE',
            'dealer_delta': float,
            'confidence': float,
            ...
        }
        """
        if not self._running:
            return

        with self._lock:
            if not hasattr(self, '_dte0_squeezes'):
                self._dte0_squeezes = deque(maxlen=100)
            self._dte0_squeezes.append(squeeze)

        # Check for cascade conditions — squeeze + NQ structural = highest conviction
        self._check_cascade('QQQ')

    def _get_mm_consensus(self, lookback_sec=30):
        """Get MM withdrawal consensus direction."""
        now = time.time()
        recent = [w for w in self._mm_withdrawals if now - w['timestamp'] < lookback_sec]
        if not recent:
            return None, 0

        short_votes = sum(1 for w in recent if w['direction'] == 'SHORT')
        long_votes = sum(1 for w in recent if w['direction'] == 'LONG')

        if short_votes > long_votes * 1.5 and short_votes >= 2:
            return 'SHORT', short_votes
        elif long_votes > short_votes * 1.5 and long_votes >= 2:
            return 'LONG', long_votes
        return None, 0

    # ═══════════════════════════════════════════════════════
    #  MICROSTRUCTURE DOMINANCE (Order Flow & Absorption)
    # ═══════════════════════════════════════════════════════

    def on_equity_trade(self, symbol, price, size, bid, ask):
        """Processes Level 1 trades to build a CVD and Volume Profile map."""
        if not self._running or symbol != 'QQQ' or size <= 0:
            return

        now = time.time()
        
        # Round to $0.10 bands (e.g. 580.46 -> 580.50)
        band = round(price * 10) / 10.0

        with self._lock:
            # Handle band changes
            if band != self._current_price_band:
                self._current_price_band = band
                # When moving to a new price band, we reset the timer for that band's dominant evaluation
                if band not in self._price_levels:
                    self._price_levels[band] = {'buy_vol': 0, 'sell_vol': 0, 'ticks': 0, 'first_seen': now, 'last_seen': now}
                else:
                    self._price_levels[band]['first_seen'] = now  # Reset the "active duration" check
                    self._price_levels[band]['ticks'] = 0         # Reset tick count for this active burst

                # GC: Purge stale bands (older than 5 min) to prevent unbounded memory growth
                stale_cutoff = now - 300.0
                stale_bands = [b for b, lv in self._price_levels.items() if lv['last_seen'] < stale_cutoff]
                for b in stale_bands:
                    del self._price_levels[b]

            level = self._price_levels[band]
            level['last_seen'] = now

            # Only count this tick if we can classify it (bid and ask must be valid)
            # Schwab sends partial updates with bid=0 or ask=0 — skip those
            if bid > 0 and ask > 0:
                level['ticks'] += 1
                if price >= ask:
                    level['buy_vol'] += size
                elif price <= bid:
                    level['sell_vol'] += size
                else:
                    # Inside the spread — classify by proximity to bid/ask
                    mid = (bid + ask) / 2.0
                    if price > mid:
                        level['buy_vol'] += size
                    elif price < mid:
                        level['sell_vol'] += size
                    # exact mid price: ignore for delta

            # Snapshot level state for detection outside lock
            level_snap = dict(level)

        # Fire detection OUTSIDE the lock to avoid deadlock:
        # _detect_price_dominance → _emit_signal → _check_conviction → _fire_conviction
        # all must run without holding self._lock
        self._detect_price_dominance(symbol, band, level_snap)

    def _detect_price_dominance(self, symbol, band, level):
        """Analyzes a price band for overwhelming Volume Delta + Absorption using Z-scores."""
        now = time.time()
        
        # Throttle signals to avoid spam on huge flurries (30 sec cooldown)
        if now - self._last_dominance_signal < 30.0:
            return

        buy_vol = level['buy_vol']
        sell_vol = level['sell_vol']
        total_vol = buy_vol + sell_vol
        ticks = level.get('ticks', 0)

        # Feed the Empirical Distribution
        if total_vol > 0:
            self._band_vol_dist.update(total_vol)

        # Evaluate the Relative Volume Density (Percentile)
        vol_pctl = self._band_vol_dist.percentile_of(total_vol)

        # 3. Calculate Binomial Z-Score to measure absolute directional aggression
        eff_n = max(1.0, total_vol / 100.0)
        p_hat = buy_vol / total_vol if total_vol > 0 else 0.5
        std_err = math.sqrt(0.25 / eff_n)
        z_score = (p_hat - 0.5) / std_err if std_err > 0 else 0

        duration = now - level['first_seen']

        if not self._band_vol_dist.warm:
            return  # Need baseline distribution

        # 1. Require statistically significant total volume (Top 15% of current regime)
        if vol_pctl < 85.0:
            return

        # 2. Require sufficient tick density (25+ trades absorbed at this exact band)
        if ticks < 25:
            return

        is_long = None
        direction = None
        
        # Scenario A: Massive Buy Aggression (Z > 2.58 = 99% confidence), but price sits still.
        # This means an Iceberg Seller is absorbing everything. Seller dominates. Bearish.
        if z_score >= 2.58:
            is_long = False
            direction = 'SHORT'
            
        # Scenario B: Massive Sell Aggression (Z < -2.58 = 99% confidence), but price sits still.
        # This means an Iceberg Buyer is absorbing everything. Buyer dominates. Bullish.
        elif z_score <= -2.58:
            is_long = True
            direction = 'LONG'

        if direction:
            # We found a dominant absorption iceberg mathematically
            self._last_dominance_signal = now

            # Base confidence 55 + density bonus
            # Density bonus scales with Z-Score magnitude (capped at +30)
            z_bonus = min(30.0, (abs(z_score) - 2.58) * 10.0)

            # Vol bonus scales with percentile (capped at +10)
            vol_bonus = min(10.0, (vol_pctl - 85.0) * 0.66)

            confidence = min(99.0, 55.0 + z_bonus + vol_bonus)

            gex_factor, gex_zone = self._gex_context(symbol, is_long)
            confidence = min(99.9, confidence * gex_factor)
            
            components = {
                'price_band': band,
                'total_volume': total_vol,
                'vol_pctl': round(vol_pctl, 1),
                'z_score': round(z_score, 2),
                'absorbed_ticks': ticks,
                'gex_zone': gex_zone,
                'gex_factor': round(gex_factor, 2)
            }
            
            self._emit_signal(f'DOMINANCE_{direction}', confidence, components, symbol)

    # Fast venue MICs (same classification as MMTracker VENUE_TIER)
    _FAST_MICS = {'MEMX', 'ARCX', 'BATS', 'EDGX', 'memx', 'arcx', 'batx', 'edgx'}
    _SLOW_MICS = {'AMEX', 'XCIS', 'XCHI', 'IEXG', 'amex', 'cinn', 'mwse', 'iexg'}

    def on_nbbo_venue(self, symbol, price, bid_mic, ask_mic, last_mic, bid_time, ask_time):
        """Called on every QQQ equity tick with NBBO venue IDs.

        Detects:
          1. Venue flips — fast venue drops off NBBO, replaced by slow venue
          2. Quote staleness — one side hasn't updated in >500ms during trading
        """
        if not self._running:
            return

        now = time.time()

        # ── Venue flip detection ──
        prev_bid_mic = self._nbbo_bid_mic.get(symbol, '')
        prev_ask_mic = self._nbbo_ask_mic.get(symbol, '')

        # Bid venue flip: fast→slow = bid-side MM pulled (bearish)
        if prev_bid_mic and bid_mic and bid_mic != prev_bid_mic:
            was_fast = prev_bid_mic.upper() in self._FAST_MICS
            now_slow = bid_mic.upper() in self._SLOW_MICS
            if was_fast and now_slow:
                self._nbbo_venue_flips.append({
                    'symbol': symbol, 'side': 'bid', 'direction': 'SHORT',
                    'from_venue': prev_bid_mic, 'to_venue': bid_mic,
                    'price': price, 'timestamp': now,
                })

        # Ask venue flip: fast→slow = ask-side MM pulled (bullish)
        if prev_ask_mic and ask_mic and ask_mic != prev_ask_mic:
            was_fast = prev_ask_mic.upper() in self._FAST_MICS
            now_slow = ask_mic.upper() in self._SLOW_MICS
            if was_fast and now_slow:
                self._nbbo_venue_flips.append({
                    'symbol': symbol, 'side': 'ask', 'direction': 'LONG',
                    'from_venue': prev_ask_mic, 'to_venue': ask_mic,
                    'price': price, 'timestamp': now,
                })

        # Update cache
        if bid_mic:
            self._nbbo_bid_mic[symbol] = bid_mic
        if ask_mic:
            self._nbbo_ask_mic[symbol] = ask_mic
        if bid_time:
            self._nbbo_bid_time[symbol] = bid_time
        if ask_time:
            self._nbbo_ask_time[symbol] = ask_time

        # ── Quote staleness detection ──
        # If bid_time or ask_time is >500ms behind the other, that side is going stale
        if bid_time and ask_time and bid_time > 0 and ask_time > 0:
            staleness_ms = abs(bid_time - ask_time)
            if staleness_ms > 500 and now - self._last_staleness_alert > 5.0:
                self._last_staleness_alert = now
                stale_side = 'bid' if bid_time < ask_time else 'ask'
                # Stale bid = bid MMs about to pull → bearish leading indicator
                # Stale ask = ask MMs about to pull → bullish leading indicator
                flip_dir = 'SHORT' if stale_side == 'bid' else 'LONG'
                self._nbbo_venue_flips.append({
                    'symbol': symbol, 'side': stale_side,
                    'direction': flip_dir, 'from_venue': 'STALE',
                    'to_venue': f'{staleness_ms:.0f}ms',
                    'price': price, 'timestamp': now,
                })

    def get_nbbo_venue_consensus(self, lookback_sec=15):
        """Get NBBO-based venue flip consensus (faster than L2 MM consensus).
        Returns: (direction, count) — 'LONG'/'SHORT'/None, event count
        """
        now = time.time()
        recent = [f for f in self._nbbo_venue_flips if now - f['timestamp'] < lookback_sec]
        if not recent:
            return None, 0

        short_votes = sum(1 for f in recent if f['direction'] == 'SHORT')
        long_votes = sum(1 for f in recent if f['direction'] == 'LONG')

        if short_votes > long_votes and short_votes >= 2:
            return 'SHORT', short_votes
        elif long_votes > short_votes and long_votes >= 2:
            return 'LONG', long_votes
        return None, 0

    # ═══════════════════════════════════════════════════════
    #  CROSS-ASSET DIVERGENCE (SPY/VIX)
    # ═══════════════════════════════════════════════════════

    def on_cross_asset_price(self, symbol, price, pct_change):
        """Called on every SPY/VIX/QQQ tick to update cross-asset state."""
        if symbol == 'SPY':
            self._spy_price = price
            self._spy_pct_change = pct_change
        elif symbol == 'VIX':
            self._vix_price = price
            self._vix_pct_change = pct_change
        elif symbol == 'QQQ':
            self._qqq_pct_change = pct_change

        # Throttle divergence computation to every 2s
        now = time.time()
        if now - self._last_cross_divergence_check < 2.0:
            return
        self._last_cross_divergence_check = now
        self._compute_cross_divergence()

    def _compute_cross_divergence(self):
        """Compute cross-asset divergence score from QQQ vs SPY and VIX.

        Score range: -1.0 (extreme bearish divergence) to +1.0 (extreme bullish).
          - QQQ lagging SPY + VIX rising = bearish (-1.0)
          - QQQ leading SPY + VIX falling = bullish (+1.0)
          - Aligned moves = neutral (0.0)
        """
        score = 0.0

        # QQQ vs SPY divergence (rotation signal)
        if self._spy_pct_change != 0 and self._qqq_pct_change != 0:
            spread = self._qqq_pct_change - self._spy_pct_change
            # Normalize: >0.5% spread is significant
            qqq_spy_signal = max(-1.0, min(1.0, spread / 0.5))
            score += qqq_spy_signal * 0.4  # 40% weight

        # VIX signal (fear gauge)
        if self._vix_pct_change != 0:
            # VIX rising = bearish, VIX falling = bullish
            # >3% VIX move is very significant
            vix_signal = max(-1.0, min(1.0, -self._vix_pct_change / 3.0))
            score += vix_signal * 0.4  # 40% weight

        # VIX level context (absolute fear)
        if self._vix_price > 0:
            if self._vix_price > 30:
                score -= 0.2  # High absolute VIX = caution on longs
            elif self._vix_price < 15:
                score += 0.2  # Low VIX = complacency, longs favored

        self._cross_divergence_score = max(-1.0, min(1.0, score))

    def get_cross_divergence_score(self):
        """Get the current cross-asset divergence score.
        >0 = bullish divergence (QQQ outperforming, VIX dropping)
        <0 = bearish divergence (QQQ lagging, VIX spiking)
        """
        return round(self._cross_divergence_score, 3)

    # ═══════════════════════════════════════════════════════
    #  STOP CLUSTER MAPPING
    # ═══════════════════════════════════════════════════════

    def _update_stop_clusters(self, symbol):
        """Rebuild stop cluster map from available data.

        Stop clusters are inferred from:
          1. Round numbers (retail gravitates to round prices)
          2. GEX walls (options dealer hedging creates support/resistance → retail stops there)
          3. Session high/low (breakout traders set stops at prior session levels)
        """
        mid = self._last_prices.get(symbol, 0)
        if mid <= 0:
            return

        # Track session high/low
        if mid > self._session_high:
            self._session_high = mid
        if mid < self._session_low:
            self._session_low = mid

        clusters = []

        # ── Round numbers within 2% of current price ──
        price_range = mid * 0.02
        base = int(mid)
        for round_price in range(base - int(price_range), base + int(price_range) + 1):
            # Major rounds: $XX0.00, $X00.00
            if round_price % 100 == 0:
                distance = abs(round_price - mid)
                if distance < price_range and distance > 0:
                    clusters.append({
                        'price': float(round_price),
                        'side': 'SELL' if round_price < mid else 'BUY',
                        'density': 'HIGH',
                        'reasons': ['round_100'],
                        'distance_pct': round(distance / mid * 100, 2),
                    })
            elif round_price % 50 == 0:
                distance = abs(round_price - mid)
                if distance < price_range and distance > 0:
                    clusters.append({
                        'price': float(round_price),
                        'side': 'SELL' if round_price < mid else 'BUY',
                        'density': 'MEDIUM',
                        'reasons': ['round_50'],
                        'distance_pct': round(distance / mid * 100, 2),
                    })

        # ── GEX walls ──
        zones = self._live_zones
        if zones:
            for zone_key, density in [('put_wall', 'HIGH'), ('call_wall', 'HIGH'),
                                       ('gamma_flip', 'MEDIUM'), ('max_pain', 'MEDIUM')]:
                zone_price = zones.get(zone_key, 0)
                if zone_price > 0:
                    distance = abs(zone_price - mid)
                    if distance < price_range and distance > 0:
                        side = 'SELL' if zone_price < mid else 'BUY'
                        # Check if this price already in clusters
                        existing = next((c for c in clusters
                                        if abs(c['price'] - zone_price) < 1.0), None)
                        if existing:
                            existing['reasons'].append(zone_key)
                            existing['density'] = 'HIGH'  # multi-reason = high density
                        else:
                            clusters.append({
                                'price': zone_price,
                                'side': side,
                                'density': density,
                                'reasons': [zone_key],
                                'distance_pct': round(distance / mid * 100, 2),
                            })

        # ── Session high/low ──
        for level, label in [(self._session_high, 'session_high'),
                             (self._session_low, 'session_low')]:
            if level > 0 and level != 999999.0:
                distance = abs(level - mid)
                if distance < price_range and distance > 0:
                    existing = next((c for c in clusters
                                    if abs(c['price'] - level) < 1.0), None)
                    if existing:
                        existing['reasons'].append(label)
                    else:
                        clusters.append({
                            'price': level,
                            'side': 'SELL' if level < mid else 'BUY',
                            'density': 'MEDIUM',
                            'reasons': [label],
                            'distance_pct': round(distance / mid * 100, 2),
                        })

        # Sort by distance (closest first)
        clusters.sort(key=lambda c: c['distance_pct'])
        self._stop_clusters = clusters

    # ═══════════════════════════════════════════════════════
    #  CASCADE DETECTION
    # ═══════════════════════════════════════════════════════

    def _check_cascade(self, symbol='QQQ'):
        """Check for stop cascade conditions.

        Cascade = NQ structural signal + MM withdrawal + price near stop cluster.
        When all three align → CASCADE_IMMINENT signal.
        """
        now = time.time()
        if now - self._last_cascade_signal < self._cascade_cooldown:
            return

        mid = self._last_prices.get(symbol, 0)
        if mid <= 0:
            return

        # Rebuild stop cluster map
        self._update_stop_clusters(symbol)

        with self._lock:
            # ── Stage 1: NQ structural signal ──
            nq_dir, nq_pctl, nq_count = self._get_nq_consensus(lookback_sec=30)

            # Also check for exhausting icebergs specifically
            nq_exhausting = any(
                d.get('data', {}).get('decay') == 'exhausting'
                for d in self._nq_detections
                if d['timestamp'] > now - 30
            )

            # ── Stage 2: MM withdrawal ──
            mm_dir, mm_count = self._get_mm_consensus(lookback_sec=30)

            # ── Stage 3: Price near stop cluster ──
            nearest_cluster = None
            cascade_dir = None

            if nq_dir and mm_dir and nq_dir == mm_dir:
                cascade_dir = nq_dir
            elif nq_dir and mm_count == 0:
                cascade_dir = nq_dir  # NQ signal alone + no MM disagreement
            elif mm_dir and nq_count == 0:
                cascade_dir = mm_dir  # MM signal alone + no NQ disagreement

            if not cascade_dir:
                return

            # Find nearest stop cluster in the signal direction
            target_side = 'SELL' if cascade_dir == 'SHORT' else 'BUY'
            matching_clusters = [
                c for c in self._stop_clusters
                if c['side'] == target_side and c['distance_pct'] < 0.5  # within 0.5%
            ]

            if not matching_clusters:
                return

            nearest_cluster = matching_clusters[0]

            # ── All three stages aligned → CASCADE IMMINENT ──
            sources_agreeing = 0
            if nq_dir == cascade_dir:
                sources_agreeing += 1
            if mm_dir == cascade_dir:
                sources_agreeing += 1
            if nearest_cluster:
                sources_agreeing += 1

            if sources_agreeing < 2:
                return

            # Confidence from NQ consensus pctl only — no guessed formulas
            # for MM count or cluster proximity. Those are logged as features.
            confidence = nq_pctl if nq_pctl > 0 else 75.0

            # GEX context
            is_long = cascade_dir == 'LONG'
            gex_factor, gex_zone = self._gex_context(symbol, is_long)
            confidence = min(99.9, confidence * gex_factor)

            components = {
                'cascade_direction': cascade_dir,
                'nq_direction': nq_dir,
                'nq_count': nq_count,
                'nq_exhausting': nq_exhausting,
                'mm_direction': mm_dir,
                'mm_withdrawal_count': mm_count,
                'stop_cluster_price': nearest_cluster['price'],
                'stop_cluster_distance_pct': nearest_cluster['distance_pct'],
                'stop_cluster_reasons': nearest_cluster['reasons'],
                'stop_cluster_density': nearest_cluster['density'],
                'sources_agreeing': sources_agreeing,
                'regime': self._get_regime(),
                'gex_zone': gex_zone,
                'gex_factor': round(gex_factor, 2),
            }

            self._emit_signal(f'CASCADE_{cascade_dir}', confidence, components, symbol)
            self._last_cascade_signal = now

    # ═══════════════════════════════════════════════════════
    #  TRADE VOLUME SCORING (for EQ Book tape glow)
    # ═══════════════════════════════════════════════════════

    def _get_trade_vol_dist(self, symbol):
        """Get or create EmpiricalDist for trade volumes of a symbol."""
        if symbol not in self._trade_vol_dists:
            self._trade_vol_dists[symbol] = EmpiricalDist(half_life=50, reservoir_size=500)
        return self._trade_vol_dists[symbol]

    def score_trade(self, symbol, volume, side, price, timestamp):
        """Score a trade's volume against the empirical distribution.

        Returns None if the trade is noise (below regime threshold).
        Emits a tape_alert via Socket.IO if the trade is statistically significant.

        Called by l2_worker on every trade tick — O(log n) bisect, <0.1ms.
        """
        if not self._running or volume <= 0:
            return None

        dist = self._get_trade_vol_dist(symbol)
        dist.update(volume)

        # Always emit context (throttled internally) — independent of trade score
        self._emit_eq_context()

        if not dist.warm:
            return None  # need >30 samples before scoring

        pctl = dist.percentile_of(volume)
        regime = self._get_regime()
        high_pctl = REGIME_PCTL_THRESHOLDS.get(regime, REGIME_PCTL_THRESHOLDS['transition'])[0]

        if pctl < high_pctl:
            return None  # noise — below regime-adaptive threshold

        # ── Classify tier ──
        if pctl >= 99.0:
            tier = 'whale'
        elif pctl >= 97.0:
            tier = 'inst'
        else:
            tier = 'sig'

        alert = {
            'symbol': symbol,
            'price': float(price),
            'volume': int(volume),
            'side': side,
            'pctl': round(pctl, 1),
            'regime': regime,
            'regime_threshold': high_pctl,
            'tier': tier,
            'timestamp': int(timestamp * 1000) if timestamp < 1e12 else int(timestamp),
            'source': 'empirical',
        }

        # Emit to frontend via Socket.IO
        if self._sio:
            try:
                self._sio.emit('tape_alert', alert)
            except Exception:
                pass


        return alert

    # ═══════════════════════════════════════════════════════
    #  EQ CONTEXT — Raw Signal Broadcast (Zero Blended Scores)
    # ═══════════════════════════════════════════════════════

    def _emit_eq_context(self):
        """Emit raw, un-blended cross-asset signals to frontend.

        EVERY value here comes from either:
          - An EmpiricalDist percentile (self-calibrating)
          - A raw count / ratio (arithmetic fact)
          - A regime label (state classification)

        NO guessed weights. NO blended scores. The MM's eyes do the fusion.
        Throttled to 1 emit per 2 seconds.
        """
        if not self._sio:
            return

        now = time.time()
        if now - self._last_eq_context_emit < 2.0:
            return
        self._last_eq_context_emit = now

        # 1. Hawkes λ percentile (fully self-calibrating)
        hawkes_pctl = self.get_hawkes_pctl()

        # 2. Regime label (empirical state classification)
        regime = self._get_regime()

        # 3. QQQ Call/Put volume ratio (raw arithmetic from screener)
        recent_screen = [s for s in self._screener_buffer
                         if now - s.get('timestamp', 0) < 60]
        call_vol = sum(s.get('totalVolume', 0) for s in recent_screen
                       if s.get('option_type') == 'CALL')
        put_vol = sum(s.get('totalVolume', 0) for s in recent_screen
                      if s.get('option_type') == 'PUT')
        # Raw ratio — no percentile guess, no weighting
        cp_ratio = round(call_vol / max(put_vol, 1), 2) if (call_vol + put_vol) > 0 else None

        # 4. NQ Iceberg/Sweep raw counts (pure event counts, no consensus blending)
        recent_det = [d for d in self._nq_detections if now - d['timestamp'] < 60]
        ice_long = sum(1 for d in recent_det if d['is_long'])
        ice_short = sum(1 for d in recent_det if not d['is_long'])

        # 5. MM withdrawal raw counts (pure event counts from MMTracker, no 1.5x multiplier)
        recent_mm = [w for w in self._mm_withdrawals if now - w['timestamp'] < 30]
        mm_bid_pulls = sum(1 for w in recent_mm if w.get('pull_type') in ('BID_PULL', 'BOTH_PULL'))
        mm_ask_pulls = sum(1 for w in recent_mm if w.get('pull_type') in ('ASK_PULL', 'BOTH_PULL'))
        mm_smart_dumb = sum(1 for w in recent_mm if w.get('smart_dumb_divergence'))

        try:
            self._sio.emit('eq_context', {
                'hawkes_pctl': round(hawkes_pctl, 0),
                'regime': regime,
                'cp_ratio': cp_ratio,                # null if no data
                'cp_call_vol': call_vol,
                'cp_put_vol': put_vol,
                'ice_long': ice_long,                # raw count
                'ice_short': ice_short,              # raw count
                'mm_bid_pulls': mm_bid_pulls,        # raw count
                'mm_ask_pulls': mm_ask_pulls,        # raw count
                'mm_smart_dumb': mm_smart_dumb,      # raw count
                'ts': int(now * 1000),
            })
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════

    def get_stats_report(self):
        report = {}

        for symbol, stats in self._book_stats.items():
            report[symbol] = {}
            for key, ed in stats.items():
                if isinstance(ed, EmpiricalDist):
                    report[symbol][key] = {
                        'ewma_mean': round(ed.mean, 4),
                        'p50': round(ed.value_at_percentile(50), 4) if ed.warm else None,
                        'p95': round(ed.value_at_percentile(95), 4) if ed.warm else None,
                        'p99': round(ed.value_at_percentile(99), 4) if ed.warm else None,
                        'count': ed.count,
                        'warm': ed.warm,
                    }

        report['_screener'] = {}
        for underlying, dist in self._screener_vol_by_underlying.items():
            report['_screener'][underlying] = {
                'ewma_mean': round(dist.mean, 0),
                'p95': round(dist.value_at_percentile(95), 0) if dist.warm else None,
                'count': dist.count,
                'warm': dist.warm,
            }

        if self._hedge_latency_stats.warm:
            report['_hedge_latency'] = {
                'median_sec': round(self._hedge_latency_stats.value_at_percentile(50), 2),
                'p75_sec': round(self._hedge_latency_stats.value_at_percentile(75), 2),
            }

        report['_regime'] = self._get_regime()
        report['_gex_zones'] = self._live_zones if self._live_zones else 'NO_DATA'

        report['_nq_detections'] = {
            'buffer_size': len(self._nq_detections),
            'recent_30s': len([d for d in self._nq_detections if time.time() - d['timestamp'] < 30]),
        }

        report['_mm_tracker'] = {
            'withdrawal_count': len(self._mm_withdrawals),
            'recent_30s': len([w for w in self._mm_withdrawals if time.time() - w['timestamp'] < 30]),
            'consensus': self._get_mm_consensus()[0],
        }

        report['_stop_clusters'] = self._stop_clusters[:5] if self._stop_clusters else []

        report['_signal_accuracy'] = {}
        signal_types = set(o['signal_type'] for o in self._completed_outcomes)
        for st in signal_types:
            acc = self._get_signal_accuracy(st)
            if acc:
                report['_signal_accuracy'][st] = acc

        report['_pending_outcomes'] = len(self._pending_outcomes)
        report['_completed_outcomes'] = len(self._completed_outcomes)

        report['_mvd'] = {
            'bid_warm': self._mvd_bid_dist.warm,
            'ask_warm': self._mvd_ask_dist.warm,
            'mpid_count': len(self._mpid_sizes),
        }
        report['_hawkes'] = {
            'lambda': round(self._hawkes_lambda, 2),
            'events': self._hawkes_event_count,
            'warm': self._hawkes_dist.warm,
        }

        return report

    # ═══════════════════════════════════════════════════════
    #  PHASE 1a: MPID VELOCITY DIVERGENCE (Multi-Venue Sweep)
    # ═══════════════════════════════════════════════════════

    def _update_mvd(self, symbol, bids, asks):
        """Track per-MPID quoted size changes to detect multi-venue sweeps.

        When a large institution sweeps QQQ, liquidity depletes across
        multiple exchanges simultaneously. We detect this by measuring
        the aggregate negative size change across all MPIDs on one side.
        """
        if symbol != 'QQQ':
            return

        # Aggregate current size by MPID
        curr = {}  # {mpid: {'bid': size, 'ask': size}}
        for level in bids:
            for mm in level.get('market_makers', []):
                mid = mm.get('id', '').lower()
                if not mid:
                    continue
                if mid not in curr:
                    curr[mid] = {'bid': 0, 'ask': 0}
                curr[mid]['bid'] += mm.get('size', 0)

        for level in asks:
            for mm in level.get('market_makers', []):
                mid = mm.get('id', '').lower()
                if not mid:
                    continue
                if mid not in curr:
                    curr[mid] = {'bid': 0, 'ask': 0}
                curr[mid]['ask'] += mm.get('size', 0)

        if not self._mpid_sizes:
            self._mpid_sizes = curr
            return

        # Calculate velocity: negative = size pulled
        mvd_bid = 0.0
        mvd_ask = 0.0
        venues_bid_depleted = 0
        venues_ask_depleted = 0

        all_mpids = set(list(self._mpid_sizes.keys()) + list(curr.keys()))
        for m in all_mpids:
            prev_bid = self._mpid_sizes.get(m, {}).get('bid', 0)
            prev_ask = self._mpid_sizes.get(m, {}).get('ask', 0)
            curr_bid = curr.get(m, {}).get('bid', 0)
            curr_ask = curr.get(m, {}).get('ask', 0)

            d_bid = curr_bid - prev_bid
            d_ask = curr_ask - prev_ask

            if d_bid < 0:
                mvd_bid += d_bid
                venues_bid_depleted += 1
            if d_ask < 0:
                mvd_ask += d_ask
                venues_ask_depleted += 1

        self._mpid_sizes = curr

        # Update distributions
        self._mvd_bid_dist.update(abs(mvd_bid))
        self._mvd_ask_dist.update(abs(mvd_ask))

        if not self._mvd_bid_dist.warm:
            return

        now = time.time()
        if now - self._last_mvd_signal < self._COOLDOWN_BY_REGIME.get(self._get_regime(), 10.0):
            return

        # Sweep detection: depletion must be in P95+ tail AND
        # majority of active venues depleted (not a hardcoded count)
        total_venues = len(all_mpids)
        venue_majority = max(2, total_venues // 3)  # at least 1/3 of active venues

        bid_pctl = self._mvd_bid_dist.percentile_of(abs(mvd_bid))
        ask_pctl = self._mvd_ask_dist.percentile_of(abs(mvd_ask))

        if ask_pctl >= 95 and venues_ask_depleted >= venue_majority:
            # Ask-side sweep = large buy → dealer short → expect NQ UP
            self._last_mvd_signal = now
            print(f"[MVD] 🔥 ASK SWEEP DETECTED | P{ask_pctl:.0f} | venues={venues_ask_depleted}/{total_venues} | depletion={mvd_ask:.0f}")
            self._last_mvd_context = {'side': 'BUY_SWEEP', 'pctl': round(ask_pctl, 1), 'venues': venues_ask_depleted, 'magnitude': round(mvd_ask, 0)}

        elif bid_pctl >= 95 and venues_bid_depleted >= venue_majority:
            # Bid-side sweep = large sell → dealer long → expect NQ DOWN
            self._last_mvd_signal = now
            print(f"[MVD] 🔥 BID SWEEP DETECTED | P{bid_pctl:.0f} | venues={venues_bid_depleted}/{total_venues} | depletion={mvd_bid:.0f}")
            self._last_mvd_context = {'side': 'SELL_SWEEP', 'pctl': round(bid_pctl, 1), 'venues': venues_bid_depleted, 'magnitude': round(mvd_bid, 0)}

    # ═══════════════════════════════════════════════════════
    #  PHASE 1b: CROSS-ASSET ALIGNMENT SCORE
    # ═══════════════════════════════════════════════════════

    def get_cross_asset_score(self):
        """Compute continuous [-1, +1] cross-asset alignment score.

        Combines NQ depth imbalance with QQQ options flow skew.
        Positive = bullish alignment, Negative = bearish alignment.
        """
        # NQ depth imbalance from l2_worker
        nq_imbalance = 0.0
        nq_stats = self._book_stats.get('NQ')
        if nq_stats and nq_stats['total_bid'].warm and nq_stats['total_ask'].warm:
            tb = nq_stats['total_bid'].mean
            ta = nq_stats['total_ask'].mean
            if (tb + ta) > 0:
                nq_imbalance = (tb - ta) / (tb + ta)

        # QQQ options flow skew from screener buffer
        qqq_skew = 0.0
        recent = [s for s in self._screener_buffer if time.time() - s.get('timestamp', 0) < 30]
        if recent:
            call_vol = sum(s.get('totalVolume', 0) for s in recent if s.get('option_type') == 'CALL')
            put_vol = sum(s.get('totalVolume', 0) for s in recent if s.get('option_type') == 'PUT')
            if (call_vol + put_vol) > 0:
                qqq_skew = (call_vol - put_vol) / (call_vol + put_vol)

        # NQ iceberg consensus
        nq_dir, nq_pctl, nq_count = self._get_nq_consensus()
        nq_signal = 0.0
        if nq_dir == 'LONG':
            nq_signal = min(nq_pctl / 100.0, 1.0)
        elif nq_dir == 'SHORT':
            nq_signal = -min(nq_pctl / 100.0, 1.0)

        # Equal-weight blend — no opinion on which source matters more.
        # Future XGBoost model will learn the optimal non-linear combination.
        n_sources = 0
        total = 0.0
        if nq_stats and nq_stats['total_bid'].warm:
            total += nq_imbalance
            n_sources += 1
        if recent:
            total += qqq_skew
            n_sources += 1
        if nq_dir:
            total += nq_signal
            n_sources += 1
        score = total / max(n_sources, 1)
        return max(-1.0, min(1.0, score))

    # ═══════════════════════════════════════════════════════
    #  PHASE 1c: HAWKES INTENSITY (Momentum Ignition)
    # ═══════════════════════════════════════════════════════

    def _update_hawkes(self, is_aggressive=True):
        """Update Hawkes process intensity on each aggressive L2 event.

        Self-calibrating O(1) recursive formula:
            μ = running mean of event rate (from _hawkes_rate_dist)
            β = 1 / median inter-event time (auto-derived)
            α = 0.5 × μ (excitation = half of baseline, scaled automatically)
            λ(t_n) = μ + α + (λ(t_{n-1}) - μ) · e^{-β·dt}

        No hardcoded parameters — all derived from the empirical data stream.
        """
        now = time.time()
        dt = now - self._hawkes_last_t if self._hawkes_last_t > 0 else 0.0

        # Track inter-event rate for self-calibration
        if dt > 0:
            rate = 1.0 / max(dt, 0.001)
            self._hawkes_rate_dist.update(rate)

        # Self-calibrated parameters from empirical distribution
        if self._hawkes_rate_dist.warm:
            mu = self._hawkes_rate_dist.mean        # baseline = mean event rate
            median_rate = self._hawkes_rate_dist.value_at_percentile(50)
            beta = max(median_rate, 0.1)             # decay = median rate (faster events → faster decay)
            alpha = 0.5 * mu                         # excitation = half of baseline
        else:
            # Cold start: use raw rate, no excitation
            mu = rate if dt > 0 else 1.0
            beta = 1.0
            alpha = 0.0

        if dt > 0 and is_aggressive:
            decay = math.exp(-beta * min(dt, 10.0))  # cap dt to prevent underflow
            self._hawkes_lambda = mu + alpha + \
                (self._hawkes_lambda - mu) * decay
        elif dt > 0:
            decay = math.exp(-beta * min(dt, 10.0))
            self._hawkes_lambda = mu + \
                (self._hawkes_lambda - mu) * decay

        self._hawkes_last_t = now
        self._hawkes_event_count += 1
        self._hawkes_dist.update(self._hawkes_lambda)

        return self._hawkes_lambda

    def get_hawkes_pctl(self):
        """Get the current Hawkes intensity as a percentile of its own distribution."""
        if not self._hawkes_dist.warm:
            return 50.0
        return self._hawkes_dist.percentile_of(self._hawkes_lambda)

    # ═══════════════════════════════════════════════════════
    #  MULTI-GREEK SIGNAL ENGINE
    # ═══════════════════════════════════════════════════════

    def _check_greek_signals(self, zone_data):
        """Evaluate higher-order Greek signals from zone_update data.

        Called every zone emission cycle (every 5s) with the full Greek surface.
        """
        now = time.time()
        symbol = 'QQQ'
        mid = self._last_prices.get(symbol, 0)
        if mid <= 0:
            return

        # Extract Greek surface data
        atm_iv = zone_data.get('atm_iv', 0)  # as percentage
        iv_skew = zone_data.get('iv_skew', 1.0)
        vanna_wall_ex = zone_data.get('vanna_wall_ex', 0)
        charm_direction = zone_data.get('charm_direction', 'NEUTRAL')
        charm_magnitude = zone_data.get('charm_magnitude', 0)
        speed_at_spot = zone_data.get('speed_at_spot', 0)
        speed_sign = zone_data.get('speed_sign', 'NEUTRAL')
        term_structure = zone_data.get('term_structure', 'FLAT')
        confluence_zones = zone_data.get('confluence_zones', [])
        vanna_wall = zone_data.get('vanna_wall_qqq', 0)

        # Track IV changes for vanna signal
        iv_change = 0
        if self._prev_atm_iv is not None and atm_iv > 0:
            iv_change = atm_iv - self._prev_atm_iv
            self._iv_change_dist.update(abs(iv_change))
        self._prev_atm_iv = atm_iv

        # Track empirical distributions for threshold calibration
        if vanna_wall_ex > 0:
            self._vanna_exposure_dist.update(vanna_wall_ex)
        if charm_magnitude > 0:
            self._charm_magnitude_dist.update(charm_magnitude)
        if speed_at_spot != 0:
            self._speed_dist.update(abs(speed_at_spot))

        high_pctl, low_pctl, _, _ = self._get_regime_thresholds()

        # ───────────────────────────────────────────────────────
        #  VANNA_SQUEEZE: Vol move + concentrated vanna = forced delta hedge
        # ───────────────────────────────────────────────────────
        if (self._iv_change_dist.warm and self._vanna_exposure_dist.warm
                and vanna_wall_ex > 0 and abs(iv_change) > 0):
            iv_change_pctl = self._iv_change_dist.percentile_of(abs(iv_change))
            vanna_pctl = self._vanna_exposure_dist.percentile_of(vanna_wall_ex)

            # Trigger: IV moving rapidly (P80+) AND vanna exposure concentrated (P80+)
            if iv_change_pctl >= (high_pctl - 10) and vanna_pctl >= (high_pctl - 10):
                # Direction: IV rising + spot below vanna wall = LONG
                iv_dir = 1 if iv_change > 0 else -1
                if mid < vanna_wall:
                    is_long = iv_dir > 0  # Rising IV pushes delta up below vanna wall
                else:
                    is_long = iv_dir < 0  # Falling IV pushes delta up above vanna wall

                direction = 'LONG' if is_long else 'SHORT'
                raw_confidence = self._joint_confidence([iv_change_pctl, vanna_pctl])
                gex_factor, gex_zone = self._gex_context(symbol, is_long)
                confidence = min(99.9, raw_confidence * gex_factor)

                components = {
                    'iv_change': round(iv_change, 4),
                    'iv_change_pctl': round(iv_change_pctl, 1),
                    'vanna_wall_ex': round(vanna_wall_ex, 0),
                    'vanna_pctl': round(vanna_pctl, 1),
                    'vanna_wall_qqq': round(vanna_wall, 2),
                    'iv_direction': 'RISING' if iv_change > 0 else 'FALLING',
                    'term_structure': term_structure,
                    'regime': self._get_regime(),
                    'gex_zone': gex_zone,
                }
                self._emit_signal(f'VANNA_SQUEEZE_{direction}', confidence, components, symbol)

        # ───────────────────────────────────────────────────────
        #  CHARM_PIN: Time decay pulling price toward max charm strike
        # ───────────────────────────────────────────────────────
        if self._charm_magnitude_dist.warm and charm_magnitude > 0:
            charm_pctl = self._charm_magnitude_dist.percentile_of(charm_magnitude)

            # Charm is strongest in the last 90 minutes of trading
            # Get current ET hour
            from datetime import datetime
            try:
                et_hour = datetime.now().hour + datetime.now().minute / 60.0
                # Adjust for ET (assuming server is ET; robust version would use pytz)
            except Exception:
                et_hour = 14.0  # default mid-afternoon

            # Only fire CHARM_PIN after 2 PM ET when charm decay accelerates
            if et_hour >= 14.0 and charm_pctl >= (high_pctl - 15):
                charm_gravity = zone_data.get('charm_gravity_qqq', mid)
                is_long = charm_direction == 'UP'
                direction = 'LONG' if is_long else 'SHORT'

                # Time multiplier: stronger signal later in day
                time_mult = min(1.0, (et_hour - 14.0) / 2.0)  # 0.0 at 2PM, 1.0 at 4PM
                raw_confidence = self._joint_confidence([charm_pctl, 50 + time_mult * 45])
                gex_factor, gex_zone = self._gex_context(symbol, is_long)
                confidence = min(99.9, raw_confidence * gex_factor)

                components = {
                    'charm_direction': charm_direction,
                    'charm_magnitude': round(charm_magnitude, 0),
                    'charm_pctl': round(charm_pctl, 1),
                    'charm_gravity_qqq': round(charm_gravity, 2),
                    'time_multiplier': round(time_mult, 2),
                    'et_hour': round(et_hour, 2),
                    'regime': self._get_regime(),
                    'gex_zone': gex_zone,
                }
                self._emit_signal(f'CHARM_PIN_{direction}', confidence, components, symbol)

        # ───────────────────────────────────────────────────────
        #  SPEED_BREAKOUT: Gamma acceleration = explosive move imminent
        # ───────────────────────────────────────────────────────
        if self._speed_dist.warm and abs(speed_at_spot) > 0:
            speed_pctl = self._speed_dist.percentile_of(abs(speed_at_spot))

            # Trigger: speed is extreme (P85+) AND positive (gamma accelerating)
            if speed_pctl >= (high_pctl - 10) and speed_sign == 'ACCELERATING':
                # Speed > 0 means gamma increases as price rises = breakout fuel
                # Direction depends on which side of gamma flip we're on
                gamma_flip = zone_data.get('gamma_flip', 0)
                if gamma_flip > 0 and mid > gamma_flip:
                    is_long = True  # Above gamma flip, positive speed = bullish breakout
                elif gamma_flip > 0:
                    is_long = False  # Below gamma flip, positive speed = bearish acceleration
                else:
                    is_long = speed_at_spot > 0

                direction = 'LONG' if is_long else 'SHORT'
                raw_confidence = self._joint_confidence([speed_pctl])
                gex_factor, gex_zone = self._gex_context(symbol, is_long)
                confidence = min(99.9, raw_confidence * gex_factor)

                components = {
                    'speed_at_spot': round(speed_at_spot, 2),
                    'speed_pctl': round(speed_pctl, 1),
                    'speed_sign': speed_sign,
                    'gamma_flip': round(gamma_flip, 2) if gamma_flip else 0,
                    'regime': self._get_regime(),
                    'gex_zone': gex_zone,
                }
                self._emit_signal(f'SPEED_BREAKOUT_{direction}', confidence, components, symbol)

        # ───────────────────────────────────────────────────────
        #  CONFLUENCE_NUCLEAR: 3+ Greeks align at same strike = mandatory flow
        #
        #  AUDIT FIX: Static confluence zones were firing every 10s (721 shorts
        #  at 48% WR). Now requires a CHANGE to fire:
        #    1. Confluence count changed (new zone formed/dissolved)
        #    2. Price crossed a confluence zone boundary
        #    3. Strongest confluence strike shifted to a different strike
        #  Also: minimum 60s dedicated cooldown, gate on count >= 4
        # ───────────────────────────────────────────────────────
        if confluence_zones:
            best_zone = None
            for zone in confluence_zones:
                if zone.get('signals', 0) >= 3:
                    best_zone = zone
                    break  # First = strongest

            if best_zone:
                signals = best_zone.get('signals', 0)
                types = best_zone.get('types', [])
                nq_price = best_zone.get('nq_price', 0)
                strike_qqq = best_zone.get('strike', mid)

                # ── Change detection: only fire on genuine state transitions ──
                current_count = zone_data.get('confluence_count', 0)
                current_side = 'ABOVE' if mid > strike_qqq else 'BELOW'

                count_changed = (current_count != self._prev_confluence_count and
                                 self._prev_confluence_count > 0)  # Skip first emission
                strike_shifted = (abs(strike_qqq - self._prev_confluence_strike) > 0.5 and
                                  self._prev_confluence_strike > 0)
                side_crossed = (current_side != self._prev_spot_side and
                                self._prev_spot_side is not None)

                # Update state for next cycle
                self._prev_confluence_count = current_count
                self._prev_confluence_strike = strike_qqq
                self._prev_spot_side = current_side

                # Gate: require count >= 4 AND a genuine change
                has_change = count_changed or strike_shifted or side_crossed
                cooldown_ok = (now - self._last_confluence_signal) >= 60.0

                if signals >= 4 and has_change and cooldown_ok:
                    self._last_confluence_signal = now

                    # Direction: price crossing INTO the zone
                    if side_crossed:
                        # Just crossed — direction is the crossing direction
                        is_long = current_side == 'BELOW'  # Price fell into zone = bounce LONG
                    else:
                        # Zone changed around us — use proximity
                        is_long = mid <= strike_qqq

                    # Confidence scales with confluence count (empirical: count=6 → 59% WR)
                    type_pctl = min(95.0, 60 + signals * 5)  # 4=80, 5=85, 6=90, 7=95
                    direction = 'LONG' if is_long else 'SHORT'
                    raw_confidence = self._joint_confidence([type_pctl])
                    gex_factor, gex_zone = self._gex_context(symbol, is_long)
                    confidence = min(99.9, raw_confidence * gex_factor)

                    trigger = []
                    if count_changed: trigger.append(f'count:{self._prev_confluence_count}→{current_count}')
                    if strike_shifted: trigger.append(f'strike_shift')
                    if side_crossed: trigger.append(f'cross:{self._prev_spot_side}→{current_side}')

                    components = {
                        'confluence_signals': signals,
                        'confluence_types': types,
                        'confluence_nq_price': nq_price,
                        'confluence_qqq_strike': round(strike_qqq, 2),
                        'distance_from_spot_pct': round(abs(strike_qqq - mid) / mid * 100, 3) if mid > 0 else 0,
                        'trigger': trigger,
                        'regime': self._get_regime(),
                        'gex_zone': gex_zone,
                    }
                    self._emit_signal(f'CONFLUENCE_NUCLEAR_{direction}', confidence, components, symbol)
