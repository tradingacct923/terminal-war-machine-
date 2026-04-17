"""
IV Calibrator — Tradier ORATS IV Surface Integration
Polls Tradier every 5-15 minutes for bid_iv/ask_iv/mid_iv/smv_vol
to calibrate our real-time BSM solver against the ORATS institutional model.

Key metrics computed:
  - IV Spread (ask_iv - bid_iv): Market maker uncertainty gauge
  - IV Model Discrepancy (Schwab IV vs ORATS smv_vol): Edge detection
  - IV Surface Freshness: Stale detection for ORATS data

The calibrator runs as a background thread and exposes its latest
results via get_iv_calibration() for other engines to consume.
"""

import time
import logging
import threading
from datetime import datetime

log = logging.getLogger(__name__)


class IVCalibrator:
    """Polls Tradier for ORATS IV data and computes calibration metrics."""

    def __init__(self, ticker='QQQ', poll_interval=300):
        """
        Args:
            ticker: Underlying symbol to calibrate (default QQQ)
            poll_interval: Seconds between Tradier polls (default 300 = 5 min)
        """
        self._ticker = ticker
        self._poll_interval = poll_interval
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Latest calibration results
        self._latest = {
            'ticker': ticker,
            'timestamp': None,
            'atm_strike': 0,
            'bid_iv': 0,
            'ask_iv': 0,
            'mid_iv': 0,
            'smv_vol': 0,            # ORATS smoothed vol
            'iv_spread': 0,          # ask_iv - bid_iv (MM uncertainty)
            'iv_spread_label': 'TIGHT',
            'mm_uncertainty': 0,     # normalized spread
            'strikes': {},           # {strike: {bid_iv, ask_iv, mid_iv, smv_vol, spread}}
            'skew_25d': 0,           # 25-delta put IV - 25-delta call IV
            'freshness': 'STALE',
        }

    def start(self):
        """Start background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name='iv-calibrator')
        self._thread.start()
        log.info(f"[IV-CAL] Started — polling {self._ticker} every {self._poll_interval}s")

    def stop(self):
        """Stop the polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def get_calibration(self):
        """Get latest IV calibration data with freshness check (thread-safe)."""
        with self._lock:
            data = dict(self._latest)
        ts = data.get('timestamp')
        if not ts:
            data['freshness'] = 'UNINIT'
            return data
        try:
            age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
            if age > self._poll_interval * 4:
                data['freshness'] = 'DEAD'
            elif age > self._poll_interval * 2:
                data['freshness'] = 'STALE'
            else:
                data['freshness'] = 'FRESH'
            data['age_seconds'] = int(age)
        except Exception:
            data['freshness'] = 'UNKNOWN'
        return data

    def _poll_loop(self):
        """Background polling loop with exponential backoff on failure."""
        time.sleep(10)
        fail_count = 0
        while self._running:
            try:
                self._poll_tradier()
                fail_count = 0
                time.sleep(self._poll_interval)
            except Exception as e:
                fail_count += 1
                backoff = min(self._poll_interval * (2 ** min(fail_count, 4)), 3600)
                log.info(f"[IV-CAL] ⚠️ Poll failed ({fail_count}x): {e} — backing off {backoff}s")
                time.sleep(backoff)

    def _poll_tradier(self):
        """Fetch Tradier chain with greeks=true, extract IV surface."""
        from data_provider import _tradier_get, _fetch_quote, _fetch_expirations

        # Get spot + nearest expiration
        spot = _fetch_quote(self._ticker)
        exps = _fetch_expirations(self._ticker)
        if not exps:
            return

        # Use nearest expiration
        exp_date = exps[0]['date']
        exp_dte = exps[0]['dte']

        # Fetch chain with greeks=true (includes ORATS IV)
        chain = _tradier_get("/markets/options/chains", {
            "symbol": self._ticker,
            "expiration": exp_date,
            "greeks": "true",
        })
        opts = chain.get("options", {}).get("option", [])
        if isinstance(opts, dict):
            opts = [opts]
        if not opts:
            return

        # Parse IV data per strike
        atm_strike = round(spot)
        strike_data = {}
        atm_bid_iv = 0
        atm_ask_iv = 0
        atm_mid_iv = 0
        atm_smv = 0

        # Track 25-delta strikes for skew
        call_25d_iv = 0
        put_25d_iv = 0

        for opt in opts:
            strike = float(opt.get('strike', 0))
            otype = opt.get('option_type', '')
            greeks = opt.get('greeks') or {}

            bid_iv = float(greeks.get('bid_iv') or 0)
            ask_iv = float(greeks.get('ask_iv') or 0)
            mid_iv = float(greeks.get('mid_iv') or 0)
            smv_vol = float(greeks.get('smv_vol') or 0)
            delta_val = float(greeks.get('delta') or 0)

            if bid_iv <= 0 and ask_iv <= 0:
                continue

            spread = ask_iv - bid_iv if ask_iv > 0 and bid_iv > 0 else 0

            if strike not in strike_data:
                strike_data[strike] = {}
            strike_data[strike][otype] = {
                'bid_iv': round(bid_iv, 4),
                'ask_iv': round(ask_iv, 4),
                'mid_iv': round(mid_iv, 4),
                'smv_vol': round(smv_vol, 4),
                'spread': round(spread, 4),
                'delta': round(delta_val, 4),
            }

            # Track ATM
            if abs(strike - spot) < 2.5 and mid_iv > 0:
                if otype == 'call':
                    atm_bid_iv = bid_iv
                    atm_ask_iv = ask_iv
                    atm_mid_iv = mid_iv
                    atm_smv = smv_vol
                    atm_strike = strike

            # Track 25-delta for skew
            if otype == 'call' and 0.20 < abs(delta_val) < 0.30:
                call_25d_iv = mid_iv
            if otype == 'put' and 0.20 < abs(delta_val) < 0.30:
                put_25d_iv = mid_iv

        # Compute aggregate metrics
        iv_spread = atm_ask_iv - atm_bid_iv if atm_ask_iv > 0 and atm_bid_iv > 0 else 0

        # Classify MM uncertainty
        if iv_spread < 0.02:
            spread_label = 'TIGHT'
            mm_uncertainty = 0
        elif iv_spread < 0.04:
            spread_label = 'NORMAL'
            mm_uncertainty = 1
        elif iv_spread < 0.08:
            spread_label = 'WIDE'
            mm_uncertainty = 2
        else:
            spread_label = 'EXTREME'
            mm_uncertainty = 3

        # 25-delta skew
        skew_25d = put_25d_iv - call_25d_iv if put_25d_iv > 0 and call_25d_iv > 0 else 0

        with self._lock:
            self._latest = {
                'ticker': self._ticker,
                'timestamp': datetime.now().isoformat(),
                'dte': exp_dte,
                'expiry': exp_date,
                'atm_strike': atm_strike,
                'spot': spot,
                'bid_iv': round(atm_bid_iv, 4),
                'ask_iv': round(atm_ask_iv, 4),
                'mid_iv': round(atm_mid_iv, 4),
                'smv_vol': round(atm_smv, 4),
                'iv_spread': round(iv_spread, 4),
                'iv_spread_label': spread_label,
                'mm_uncertainty': mm_uncertainty,
                'strikes': strike_data,
                'skew_25d': round(skew_25d, 4),
                'freshness': 'FRESH',
                'strike_count': len(strike_data),
            }

        log.info(f"[IV-CAL] {self._ticker} ATM={atm_strike} mid_iv={atm_mid_iv:.4f} "
              f"spread={iv_spread:.4f} ({spread_label}) smv={atm_smv:.4f} "
              f"skew25d={skew_25d:+.4f} | {len(strike_data)} strikes")
