"""
0DTE Squeeze Detector — Options Dealer Delta Hedge Flow Predictor

When massive 0DTE (zero-days-to-expiry) option blocks trade, the dealer
who WRITES the option must immediately delta-hedge in the underlying futures.
This creates mandatory, predictable flow:

  - Large 0DTE PUT purchase → Dealer is now long delta → Must SELL futures to hedge
  - Large 0DTE CALL purchase → Dealer is now short delta → Must BUY futures to hedge

The squeeze happens when cumulative unhedged dealer delta exceeds a threshold,
creating a forced flow event that moves the underlying predictably.

Architecture:
  1. Track all options quote updates for contracts expiring TODAY (DTE=0)
  2. Detect block-sized volume spikes (empirical P90+ for that strike)
  3. Compute cumulative unhedged dealer delta (position × delta × multiplier)
  4. When net dealer delta exceeds threshold → SQUEEZE alert
  5. Forward to EdgeDetector for cascade confirmation with NQ order flow

Integration:
  - schwab_bridge._on_options_quote() feeds data into this engine
  - Alerts forwarded to edge_detector.on_dte0_squeeze()
  - SQUEEZE + NQ sweep/absorption alignment = highest-conviction signal
"""

import time
import math
import logging
from datetime import datetime, date
from collections import deque, defaultdict

log = logging.getLogger("dte0_squeeze")


class DTE0SqueezeDetector:
    """Detect and predict dealer delta-hedge squeeze events from 0DTE options flow.

    Monitors real-time options volume for today's expiry contracts.
    When block trades create large unhedged dealer delta positions,
    the resulting forced hedge flow is predictable and tradeable.
    """

    def __init__(self, edge_detector=None):
        self._edge = edge_detector

        # ── Per-strike state ──
        self._strike_vol = defaultdict(lambda: {
            'call_vol': 0,
            'put_vol': 0,
            'call_delta': 0.5,   # approximate, updated when available
            'put_delta': -0.5,
            'gamma': 0.0,
            'last_price': 0.0,
        })

        # ── Volume spike detection (empirical per-strike) ──
        self._vol_distributions = {}  # strike -> deque of volume observations

        # ── Cumulative dealer delta tracking ──
        self._dealer_delta = 0.0      # net dealer delta (positive = dealer long = must sell)
        self._dealer_delta_history = deque(maxlen=500)

        # ── Block trade buffer ──
        self._block_trades = deque(maxlen=200)  # recent block-sized 0DTE trades
        self._last_block_log = 0

        # ── Squeeze alert state ──
        self._squeeze_events = deque(maxlen=100)
        self._last_squeeze_alert = 0
        self._squeeze_cooldown = 20.0  # seconds

        # ── EWMA for dealer delta trend ──
        self._alpha = 0.05
        self._ewma_delta = 0.0
        self._ewma_initialized = False

        # ── Today's date for DTE=0 filtering ──
        self._today = date.today()

        # ── Threshold calibration ──
        # QQQ options: 100x multiplier. A 500-contract block at 0.5 delta = 25,000 delta.
        # Threshold must reflect institutional-size only, not every moderate block.
        self._delta_threshold = 75000.0  # initial; auto-calibrates from P90
        self._delta_reservoir = deque(maxlen=300)
        self._delta_sorted = []
        self._delta_cache_dirty = False

        self._update_count = 0

    def on_options_update(self, data):
        """Process a LEVELONE_OPTIONS update.

        Expected fields from schwab_bridge:
            strike, contract_type ('C'/'P'), gamma, delta,
            total_volume, open_interest, last, symbol
        """
        self._update_count += 1

        # ── Filter: only 0DTE contracts ──
        sym = data.get('symbol', '')
        if not self._is_0dte(sym):
            return

        strike = float(data.get('strike', 0))
        if strike <= 0:
            return

        contract_type = data.get('contract_type', '')
        vol = int(data.get('total_volume', data.get('volume', 0)) or 0)
        gamma = float(data.get('gamma', 0) or 0)
        delta = float(data.get('delta', 0) or 0)
        last_price = float(data.get('last', data.get('lastPrice', 0)) or 0)
        oi = int(data.get('open_interest', 0) or 0)

        now = time.time()

        state = self._strike_vol[strike]
        prev_call_vol = state['call_vol']
        prev_put_vol = state['put_vol']

        if contract_type in ('C', 'CALL', 'call'):
            if state['call_vol'] == 0:
                # First observation: seed with current cumulative volume, don't count as increment
                state['call_vol'] = vol
                state['call_delta'] = delta if delta != 0 else state['call_delta']
                return
            state['call_delta'] = delta if delta != 0 else state['call_delta']
            vol_increment = max(0, vol - state['call_vol'])
            state['call_vol'] = vol
            option_side = 'CALL'
        elif contract_type in ('P', 'PUT', 'put'):
            if state['put_vol'] == 0:
                # First observation: seed with current cumulative volume, don't count as increment
                state['put_vol'] = vol
                state['put_delta'] = delta if delta != 0 else state['put_delta']
                return
            state['put_delta'] = delta if delta != 0 else state['put_delta']
            vol_increment = max(0, vol - state['put_vol'])
            state['put_vol'] = vol
            option_side = 'PUT'
        else:
            return

        state['gamma'] = abs(gamma)
        state['last_price'] = last_price

        if vol_increment <= 0:
            return

        # ── Detect block-sized trades ──
        # Track volume increments empirically
        vol_key = f"{strike}_{option_side}"
        if vol_key not in self._vol_distributions:
            self._vol_distributions[vol_key] = deque(maxlen=200)
        self._vol_distributions[vol_key].append(vol_increment)

        # Is this increment a block? (P80+ of observed increments at this strike)
        is_block = False
        vol_hist = self._vol_distributions[vol_key]
        if len(vol_hist) >= 20:
            sorted_hist = sorted(vol_hist)
            p80_idx = int(0.80 * len(sorted_hist))
            p80_val = sorted_hist[min(p80_idx, len(sorted_hist) - 1)]
            is_block = vol_increment >= p80_val and vol_increment >= 100
        elif vol_increment >= 500:
            # Pre-warmup: flag any large absolute increment
            is_block = True

        if not is_block:
            return

        # ── Compute dealer delta impact ──
        # When someone BUYS a call, the dealer is SHORT delta → must BUY underlying
        # When someone BUYS a put, the dealer is LONG delta → must SELL underlying
        # Assumption: most block trades are PURCHASES (aggressive opening)
        multiplier = 100  # options multiplier

        if option_side == 'CALL':
            # Dealer wrote calls → short delta → must buy futures to hedge
            # Positive dealer_delta_impact = dealer needs to BUY
            dealer_delta_impact = vol_increment * abs(delta) * multiplier
            hedge_direction = 'BUY'
        else:
            # Dealer wrote puts → long delta → must sell futures to hedge
            # Negative dealer_delta_impact = dealer needs to SELL
            dealer_delta_impact = -vol_increment * abs(delta) * multiplier
            hedge_direction = 'SELL'

        self._dealer_delta += dealer_delta_impact
        self._dealer_delta_history.append((now, self._dealer_delta))

        # Update EWMA
        if not self._ewma_initialized:
            self._ewma_delta = self._dealer_delta
            self._ewma_initialized = True
        else:
            self._ewma_delta += self._alpha * (self._dealer_delta - self._ewma_delta)

        # Update percentile reservoir
        self._delta_reservoir.append(abs(self._dealer_delta))
        self._delta_cache_dirty = True

        block = {
            'strike': strike,
            'option_side': option_side,
            'vol_increment': vol_increment,
            'delta': round(delta, 3),
            'dealer_delta_impact': round(dealer_delta_impact, 1),
            'cumulative_dealer_delta': round(self._dealer_delta, 1),
            'hedge_direction': hedge_direction,
            'timestamp': now,
        }
        self._block_trades.append(block)

        # Log significant blocks
        if now - self._last_block_log > 5:
            self._last_block_log = now
            arrow = '📈' if hedge_direction == 'BUY' else '📉'
            print(f"[0DTE] {arrow} {option_side} block @ {strike} | "
                  f"+{vol_increment} contracts (Δ={delta:.2f}) | "
                  f"dealer Δ={self._dealer_delta:+.0f} → hedge={hedge_direction}")

        # ── Check for squeeze conditions ──
        self._check_squeeze(now)

    def _check_squeeze(self, now):
        """Check if cumulative dealer delta has reached squeeze levels."""
        if now - self._last_squeeze_alert < self._squeeze_cooldown:
            return

        # Dynamic threshold from empirical distribution
        threshold = self._get_dynamic_threshold()
        abs_delta = abs(self._dealer_delta)

        if abs_delta < threshold:
            return

        # Determine squeeze direction
        if self._dealer_delta > 0:
            direction = 'LONG'   # Dealer must BUY futures → price goes UP
            squeeze_type = 'CALL_SQUEEZE'
        else:
            direction = 'SHORT'  # Dealer must SELL futures → price goes DOWN
            squeeze_type = 'PUT_SQUEEZE'

        # Confidence based on how far above threshold
        excess_ratio = abs_delta / threshold
        confidence = min(1.0, 0.5 + (excess_ratio - 1.0) * 0.25)

        # Count recent blocks in the same direction
        recent_blocks = [b for b in self._block_trades
                        if now - b['timestamp'] < 60]
        block_count = len(recent_blocks)

        squeeze = {
            'direction': direction,
            'squeeze_type': squeeze_type,
            'dealer_delta': round(self._dealer_delta, 1),
            'threshold': round(threshold, 1),
            'excess_ratio': round(excess_ratio, 2),
            'confidence': round(confidence, 3),
            'block_count_60s': block_count,
            'ewma_delta': round(self._ewma_delta, 1),
            'timestamp': now,
        }

        self._squeeze_events.append(squeeze)
        self._last_squeeze_alert = now

        arrow = '🟢' if direction == 'LONG' else '🔴'
        print(f"[0DTE] {arrow} {squeeze_type} SQUEEZE | "
              f"dealer Δ={self._dealer_delta:+.0f} (threshold={threshold:.0f}) | "
              f"confidence={confidence:.1%} | blocks={block_count} | "
              f"→ Forced {direction} hedge flow imminent")

        # Forward to EdgeDetector
        if self._edge:
            try:
                self._edge.on_dte0_squeeze(squeeze)
            except Exception:
                pass

    def _get_dynamic_threshold(self):
        """Compute dynamic squeeze threshold from empirical data."""
        if len(self._delta_reservoir) < 30:
            return self._delta_threshold  # use initial default

        if self._delta_cache_dirty:
            self._delta_sorted = sorted(self._delta_reservoir)
            self._delta_cache_dirty = False

        # P90 of absolute dealer delta = squeeze threshold
        p90_idx = int(0.90 * len(self._delta_sorted))
        p90_val = self._delta_sorted[min(p90_idx, len(self._delta_sorted) - 1)]
        return max(25000.0, p90_val)  # Floor at 25k delta (equity options scale)

    def _is_0dte(self, symbol):
        """Check if an option symbol expires today (DTE=0).

        Schwab option symbols have format like:
            'SPY   260401C00550000'  → SPY Apr 1, 2026 550 Call
            Format: SYMBOL YYMMDD[CP]SSSSSSSS
        """
        if not symbol or len(symbol) < 15:
            return False

        try:
            # Find the date portion (6 digits before C/P)
            # Look for pattern: digits followed by C or P
            clean = symbol.strip()
            # Find the YYMMDD portion — it's typically positions after the ticker name
            for i in range(len(clean) - 8, 3, -1):
                chunk = clean[i:i+6]
                if chunk.isdigit():
                    next_char = clean[i+6] if i+6 < len(clean) else ''
                    if next_char in ('C', 'P'):
                        yy = int(chunk[0:2])
                        mm = int(chunk[2:4])
                        dd = int(chunk[4:6])
                        exp_date = date(2000 + yy, mm, dd)
                        return exp_date == self._today
        except (ValueError, IndexError):
            pass

        return False

    # ═══════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════

    def get_state(self):
        """Return current squeeze state for state-vector logging."""
        return {
            'dealer_delta': round(self._dealer_delta, 1),
            'dealer_delta_ewma': round(self._ewma_delta, 1),
            'dealer_direction': 'LONG' if self._dealer_delta > 0 else 'SHORT',
            'squeeze_active': len([s for s in self._squeeze_events
                                  if time.time() - s['timestamp'] < 60]) > 0,
            'recent_blocks': len([b for b in self._block_trades
                                 if time.time() - b['timestamp'] < 120]),
        }

    def get_squeeze_bias(self, lookback_sec=60):
        """Get directional bias from recent squeeze events.

        Returns: -1 (SHORT squeeze), +1 (LONG squeeze), 0 (no squeeze)
        """
        now = time.time()
        recent = [s for s in self._squeeze_events if now - s['timestamp'] < lookback_sec]
        if not recent:
            return 0

        long_votes = sum(1 for s in recent if s['direction'] == 'LONG')
        short_votes = sum(1 for s in recent if s['direction'] == 'SHORT')

        if long_votes > short_votes:
            return 1
        elif short_votes > long_votes:
            return -1
        return 0
