"""
MMTracker — Venue-Intelligent Market Maker Withdrawal Detection

Schwab NASDAQ_BOOK gives us exchange venue IDs at each price level.
These are NOT individual firm MPIDs, but they ARE informative because
each venue has a known speed and participant profile:

  FAST (informed/HFT-heavy):
    memx  — Founded by Citadel, Virtu, Morgan Stanley. When MEMX pulls, smart money exits.
    arcx  — NYSE Arca. Primary ETF venue, heavy HFT presence.
    batx  — Cboe BZX. Electronic/algorithmic market makers.
    edgx  — Cboe EDGX. Maker-taker, attracts active liquidity providers.

  MEDIUM:
    NSDQ  — NASDAQ. Listing exchange for QQQ. Large, sticky institutional quotes.
    edga  — Cboe EDGA. Inverted pricing (pays takers). Different strategy class.
    baty  — Cboe BYX. Inverted pricing.
    phlx  — PHLX. Options-focused. Presence = options dealer hedging equity exposure.

  SLOW (passive/retail):
    amex  — NYSE American. Often retail flow.
    cinn  — Cincinnati/NSX. Smaller exchange.
    mwse  — Chicago Stock Exchange. Often odd-lot routing.

The gold mine: When FAST venues pull but SLOW venues stay, the informed
participants are out and only uninformed liquidity remains. That's the signal.
"""

import time
import math
import logging
from collections import deque, defaultdict

log = logging.getLogger("mm_tracker")


# ═══════════════════════════════════════════════════════════
#  VENUE CLASSIFICATION
# ═══════════════════════════════════════════════════════════

# Speed tier: higher = faster/more informed
VENUE_TIER = {
    # Fast tier (HFT/informed) — weight 3
    'memx': 3,    # Citadel/Virtu/Morgan Stanley founded
    'arcx': 3,    # NYSE Arca — primary ETF arb venue
    'batx': 3,    # Cboe BZX — electronic MMs
    'edgx': 3,    # Cboe EDGX — maker-taker, active LPs

    # Medium tier — weight 2
    'NSDQ': 2,    # NASDAQ listing exchange — sticky, institutional
    'edga': 2,    # Cboe EDGA — inverted, different strategy class
    'baty': 2,    # Cboe BYX — inverted
    'phlx': 2,    # PHLX — options dealer hedging

    # Slow tier (passive/retail) — weight 1
    'amex': 1,    # NYSE American
    'cinn': 1,    # Cincinnati/NSX
    'mwse': 1,    # Chicago Stock Exchange
    'iexg': 1,    # IEX — speed bump, intentionally slow
}

FAST_VENUES = {v for v, t in VENUE_TIER.items() if t == 3}
SLOW_VENUES = {v for v, t in VENUE_TIER.items() if t == 1}


class EmpiricalDist:
    """Lightweight non-parametric distribution for MMTracker."""

    def __init__(self, half_life=50, reservoir_size=500):
        self._alpha = 1.0 - math.exp(-math.log(2) / half_life)
        self._ewma_mean = 0.0
        self._initialized = False
        self._reservoir = deque(maxlen=reservoir_size)
        self._sorted_cache = []
        self._cache_dirty = False
        self._count = 0

    def update(self, value):
        if not self._initialized:
            self._ewma_mean = value
            self._initialized = True
        else:
            self._ewma_mean += self._alpha * (value - self._ewma_mean)
        self._reservoir.append(value)
        self._cache_dirty = True
        self._count += 1

    @property
    def mean(self):
        return self._ewma_mean

    @property
    def count(self):
        return self._count

    @property
    def warm(self):
        return len(self._reservoir) >= 30

    def percentile_of(self, value):
        if not self.warm:
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


# ═══════════════════════════════════════════════════════════
#  MMTRACKER — VENUE-INTELLIGENT WITHDRAWAL DETECTION
# ═══════════════════════════════════════════════════════════

class MMTracker:
    """Track market maker venue behavior from NASDAQ_BOOK data.

    Detects:
      1. Venue count drops at TOB (empirical percentile)
      2. Asymmetric withdrawal (bid-side pull vs ask-side pull)
      3. FAST venue dropout — informed venues exit, passive stay
      4. Venue-weighted size change — fast venue size matters more
      5. Smart/dumb divergence — fast venues SHORT, slow venues still quoting
    """

    def __init__(self, edge_detector=None):
        self._edge = edge_detector

        # ── Per-symbol state ──
        self._prev_snapshot = {}

        # ── Empirical distributions ──
        self._mm_count_change_dist = {}
        self._size_change_dist = {}
        self._fast_count_dist = {}   # fast venue count at TOB
        self._smart_dumb_ratio_dist = {}  # ratio of fast/total venue count

        # ── Per-venue tracking ──
        self._venue_history = defaultdict(lambda: deque(maxlen=1000))
        # venue -> deque of (timestamp, action, side, size_at_tob)

        self._venue_presence = {}  # symbol -> {venue: {bid: set(prices), ask: set(prices)}}

        # ── Withdrawal event buffer ──
        self._withdrawal_events = deque(maxlen=200)

        # ── Throttle ──
        self._last_log_time = 0
        self._update_count = 0

    def _get_dist(self, dist_dict, symbol):
        if symbol not in dist_dict:
            dist_dict[symbol] = EmpiricalDist(half_life=100, reservoir_size=500)
        return dist_dict[symbol]

    def update(self, book_data):
        """Process a NASDAQ_BOOK update. Called on every book snapshot."""
        symbol = book_data.get('symbol', '')
        bids = book_data.get('bids', [])
        asks = book_data.get('asks', [])
        now = time.time()

        if not bids and not asks:
            return

        self._update_count += 1

        # ── Extract current venue state ──
        curr = {
            'bids': self._extract_venue_state(bids),
            'asks': self._extract_venue_state(asks),
            'timestamp': now,
        }

        # ── Venue metrics at TOB ──
        bid_tob = curr['bids'].get('tob', {})
        ask_tob = curr['asks'].get('tob', {})

        bid_mm_count = bid_tob.get('mm_count', 0)
        ask_mm_count = ask_tob.get('mm_count', 0)
        bid_fast_count = len(bid_tob.get('fast_venues', set()))
        ask_fast_count = len(ask_tob.get('fast_venues', set()))
        bid_slow_count = len(bid_tob.get('slow_venues', set()))
        ask_slow_count = len(ask_tob.get('slow_venues', set()))

        # Smart/dumb ratio: what fraction of TOB venues are fast?
        bid_smart_ratio = bid_fast_count / max(bid_mm_count, 1)
        ask_smart_ratio = ask_fast_count / max(ask_mm_count, 1)

        self._get_dist(self._fast_count_dist, f'{symbol}_bid').update(bid_fast_count)
        self._get_dist(self._fast_count_dist, f'{symbol}_ask').update(ask_fast_count)
        self._get_dist(self._smart_dumb_ratio_dist, f'{symbol}_bid').update(bid_smart_ratio)
        self._get_dist(self._smart_dumb_ratio_dist, f'{symbol}_ask').update(ask_smart_ratio)

        # ── Compare to previous snapshot ──
        prev = self._prev_snapshot.get(symbol)
        if prev:
            self._detect_withdrawals(symbol, prev, curr, now)
            self._track_venue_changes(symbol, prev, curr, now)

        # ── Update venue presence ──
        self._update_venue_presence(symbol, curr)

        # ── Store for next comparison ──
        self._prev_snapshot[symbol] = curr

    def _extract_venue_state(self, levels):
        """Extract venue-classified state from book levels."""
        state = {'levels': {}, 'tob': {}}
        all_venues = set()
        fast_venues_all = set()

        for i, lvl in enumerate(levels[:10]):
            price = lvl.get('price', 0)
            if price <= 0:
                continue

            venues = {}
            fast_at_level = set()
            slow_at_level = set()

            for mm in lvl.get('market_makers', []):
                venue = mm.get('id', '').lower()
                if not venue:
                    continue
                size = mm.get('size', 0)
                tier = VENUE_TIER.get(venue, VENUE_TIER.get(venue.upper(), 1))
                venues[venue] = {'size': size, 'tier': tier}
                all_venues.add(venue)

                if tier == 3:
                    fast_at_level.add(venue)
                    fast_venues_all.add(venue)
                elif tier == 1:
                    slow_at_level.add(venue)

            state['levels'][price] = {
                'size': lvl.get('size', 0),
                'mm_count': lvl.get('mm_count', len(venues)),
                'venues': venues,
                'fast_venues': fast_at_level,
                'slow_venues': slow_at_level,
            }

            # TOB = first level
            if i == 0:
                state['tob'] = {
                    'price': price,
                    'size': lvl.get('size', 0),
                    'mm_count': lvl.get('mm_count', len(venues)),
                    'venues': venues,
                    'fast_venues': fast_at_level,
                    'slow_venues': slow_at_level,
                    'all_venues': set(venues.keys()),
                }

        state['all_venues'] = all_venues
        state['fast_venues_all'] = fast_venues_all
        return state

    def _detect_withdrawals(self, symbol, prev, curr, now):
        """Detect venue withdrawals with smart/dumb classification."""
        prev_bid_tob = prev['bids'].get('tob', {})
        curr_bid_tob = curr['bids'].get('tob', {})
        prev_ask_tob = prev['asks'].get('tob', {})
        curr_ask_tob = curr['asks'].get('tob', {})

        # ── MM count changes ──
        prev_bid_mmc = prev_bid_tob.get('mm_count', 0)
        curr_bid_mmc = curr_bid_tob.get('mm_count', 0)
        prev_ask_mmc = prev_ask_tob.get('mm_count', 0)
        curr_ask_mmc = curr_ask_tob.get('mm_count', 0)

        bid_mm_delta = curr_bid_mmc - prev_bid_mmc
        ask_mm_delta = curr_ask_mmc - prev_ask_mmc

        # Ignore if price level changed (tick up/down is not a withdrawal)
        bid_price_stable = prev_bid_tob.get('price', 0) == curr_bid_tob.get('price', -1)
        ask_price_stable = prev_ask_tob.get('price', 0) == curr_ask_tob.get('price', -1)

        if not bid_price_stable:
            bid_mm_delta = 0
        if not ask_price_stable:
            ask_mm_delta = 0

        mm_change_dist = self._get_dist(self._mm_count_change_dist, symbol)
        mm_change_dist.update(bid_mm_delta)
        mm_change_dist.update(ask_mm_delta)

        # ── Fast venue dropouts ──
        prev_bid_fast = prev_bid_tob.get('fast_venues', set()) if bid_price_stable else set()
        curr_bid_fast = curr_bid_tob.get('fast_venues', set())
        prev_ask_fast = prev_ask_tob.get('fast_venues', set()) if ask_price_stable else set()
        curr_ask_fast = curr_ask_tob.get('fast_venues', set())

        bid_fast_withdrew = prev_bid_fast - curr_bid_fast
        ask_fast_withdrew = prev_ask_fast - curr_ask_fast
        bid_fast_added = curr_bid_fast - prev_bid_fast
        ask_fast_added = curr_ask_fast - prev_ask_fast

        # ── Slow venue status (are they still there?) ──
        curr_bid_slow = curr_bid_tob.get('slow_venues', set())
        curr_ask_slow = curr_ask_tob.get('slow_venues', set())

        # ── Size changes with venue weighting ──
        bid_fast_size_delta = self._venue_weighted_size_delta(
            prev_bid_tob.get('venues', {}), curr_bid_tob.get('venues', {}), tier_filter=3)
        ask_fast_size_delta = self._venue_weighted_size_delta(
            prev_ask_tob.get('venues', {}), curr_ask_tob.get('venues', {}), tier_filter=3)

        if not mm_change_dist.warm:
            return

        # ── Detect significant withdrawal ──
        bid_mm_pctl = mm_change_dist.percentile_of(bid_mm_delta)
        ask_mm_pctl = mm_change_dist.percentile_of(ask_mm_delta)

        bid_pulling = bid_mm_pctl <= 5.0 and bid_mm_delta < 0
        ask_pulling = ask_mm_pctl <= 5.0 and ask_mm_delta < 0

        # ── Fast venue dropout (even if total count didn't drop much) ──
        bid_fast_dropout = bid_price_stable and len(bid_fast_withdrew) >= 3  # 3+ fast venues pulled simultaneously
        ask_fast_dropout = ask_price_stable and len(ask_fast_withdrew) >= 3

        # ── Smart/dumb divergence ──
        # Fast pulled from bid but slow still on bid → informed expect DOWN
        bid_smart_dumb_div = (bid_fast_dropout and len(curr_bid_slow) > 0)
        ask_smart_dumb_div = (ask_fast_dropout and len(curr_ask_slow) > 0)

        should_signal = bid_pulling or ask_pulling or bid_fast_dropout or ask_fast_dropout

        if not should_signal:
            return

        # ── Direction determination ──
        if (bid_pulling or bid_fast_dropout) and not (ask_pulling or ask_fast_dropout):
            direction = 'SHORT'
            pull_type = 'BID_PULL'
        elif (ask_pulling or ask_fast_dropout) and not (bid_pulling or bid_fast_dropout):
            direction = 'LONG'
            pull_type = 'ASK_PULL'
        else:
            direction = 'UNCERTAIN'
            pull_type = 'BOTH_PULL'

        # ── Confidence based on smart/dumb divergence ──
        # Fast venues out + slow venues still there = highest confidence
        smart_dumb_flag = bid_smart_dumb_div or ask_smart_dumb_div

        withdrawal = {
            'symbol': symbol,
            'direction': direction,
            'pull_type': pull_type,
            'bid_mm_delta': bid_mm_delta,
            'ask_mm_delta': ask_mm_delta,
            'bid_mm_pctl': round(bid_mm_pctl, 1),
            'ask_mm_pctl': round(ask_mm_pctl, 1),
            # Fast venue detail
            'bid_fast_withdrew': list(bid_fast_withdrew),
            'ask_fast_withdrew': list(ask_fast_withdrew),
            'bid_fast_remaining': list(curr_bid_fast),
            'ask_fast_remaining': list(curr_ask_fast),
            'bid_slow_remaining': list(curr_bid_slow),
            'ask_slow_remaining': list(curr_ask_slow),
            # Size deltas
            'bid_fast_size_delta': bid_fast_size_delta,
            'ask_fast_size_delta': ask_fast_size_delta,
            # Smart/dumb divergence
            'smart_dumb_divergence': smart_dumb_flag,
            'timestamp': now,
            'curr_bid_mm_count': curr_bid_mmc,
            'curr_ask_mm_count': curr_ask_mmc,
        }

        self._withdrawal_events.append(withdrawal)

        # Log with venue detail
        if now - self._last_log_time > 3:
            self._last_log_time = now
            arrow = '🔴' if direction == 'SHORT' else '🟢' if direction == 'LONG' else '⚪'
            div_tag = ' SMART/DUMB↕' if smart_dumb_flag else ''
            fast_pulled = bid_fast_withdrew | ask_fast_withdrew
            fast_str = ','.join(fast_pulled) if fast_pulled else 'none'
            print(f"[MM-TRACKER] {arrow} {pull_type} {direction}{div_tag} | "
                  f"bid_mm: {prev_bid_mmc}→{curr_bid_mmc} (P{bid_mm_pctl:.0f}) | "
                  f"ask_mm: {prev_ask_mmc}→{curr_ask_mmc} (P{ask_mm_pctl:.0f}) | "
                  f"fast_out: [{fast_str}]")

        # Forward to EdgeDetector
        if self._edge and direction in ('LONG', 'SHORT'):
            try:
                self._edge.on_mm_withdrawal(withdrawal)
            except Exception:
                pass

    @staticmethod
    def _venue_weighted_size_delta(prev_venues, curr_venues, tier_filter=None):
        """Compute size change for venues of a specific tier."""
        prev_size = 0
        curr_size = 0
        for venue, info in prev_venues.items():
            tier = info.get('tier', 1)
            if tier_filter is None or tier == tier_filter:
                prev_size += info.get('size', 0)
        for venue, info in curr_venues.items():
            tier = info.get('tier', 1)
            if tier_filter is None or tier == tier_filter:
                curr_size += info.get('size', 0)
        return curr_size - prev_size

    def _track_venue_changes(self, symbol, prev, curr, now):
        """Track per-venue additions and withdrawals."""
        for side_key in ['bids', 'asks']:
            side_label = 'bid' if side_key == 'bids' else 'ask'
            prev_venues = prev[side_key].get('all_venues', set())
            curr_venues = curr[side_key].get('all_venues', set())

            withdrew = prev_venues - curr_venues
            added = curr_venues - prev_venues

            for venue in withdrew:
                tier = VENUE_TIER.get(venue, VENUE_TIER.get(venue.upper(), 1))
                self._venue_history[venue].append((now, 'withdrew', side_label, tier))
            for venue in added:
                tier = VENUE_TIER.get(venue, VENUE_TIER.get(venue.upper(), 1))
                self._venue_history[venue].append((now, 'added', side_label, tier))

    def _update_venue_presence(self, symbol, curr):
        """Update venue presence map."""
        presence = defaultdict(lambda: {'bid': set(), 'ask': set()})
        for side_key in ['bids', 'asks']:
            side_label = 'bid' if side_key == 'bids' else 'ask'
            for price, level in curr[side_key].get('levels', {}).items():
                for venue in level.get('venues', {}):
                    presence[venue][side_label].add(price)
        self._venue_presence[symbol] = dict(presence)

    # ═══════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════

    def get_pull_bias(self, lookback_sec=30):
        """Get aggregate MM pull bias direction.
        Returns: -1 (SHORT), +1 (LONG), 0 (balanced)
        """
        now = time.time()
        cutoff = now - lookback_sec
        recent = [w for w in self._withdrawal_events if w['timestamp'] >= cutoff]
        if not recent:
            return 0

        short_votes = sum(1 for w in recent if w['direction'] == 'SHORT')
        long_votes = sum(1 for w in recent if w['direction'] == 'LONG')

        # Weight smart/dumb divergence events higher
        short_weight = sum(
            2 if w.get('smart_dumb_divergence') else 1
            for w in recent if w['direction'] == 'SHORT'
        )
        long_weight = sum(
            2 if w.get('smart_dumb_divergence') else 1
            for w in recent if w['direction'] == 'LONG'
        )

        if short_weight > long_weight * 1.5:
            return -1
        elif long_weight > short_weight * 1.5:
            return 1
        return 0

    def get_recent_withdrawals(self, lookback_sec=60):
        """Get recent withdrawal events."""
        now = time.time()
        return [w for w in self._withdrawal_events if now - w['timestamp'] < lookback_sec]

    def get_venue_report(self, symbol):
        """Get current venue presence report with tier classification."""
        presence = self._venue_presence.get(symbol, {})
        report = {}
        for venue, sides in presence.items():
            tier = VENUE_TIER.get(venue, VENUE_TIER.get(venue.upper(), 1))
            tier_label = 'FAST' if tier == 3 else 'MEDIUM' if tier == 2 else 'SLOW'
            report[venue] = {
                'bid_levels': len(sides.get('bid', set())),
                'ask_levels': len(sides.get('ask', set())),
                'tier': tier_label,
            }
        return report

    def get_stats_report(self):
        """Return diagnostic stats."""
        recent_30 = self.get_recent_withdrawals(30)
        smart_dumb_count = sum(1 for w in recent_30 if w.get('smart_dumb_divergence'))
        return {
            'update_count': self._update_count,
            'withdrawal_events_total': len(self._withdrawal_events),
            'recent_30s': len(recent_30),
            'smart_dumb_divergences_30s': smart_dumb_count,
            'pull_bias': self.get_pull_bias(),
            'tracked_venues': len(self._venue_history),
        }
