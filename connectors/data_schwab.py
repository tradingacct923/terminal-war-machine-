"""
Schwab Market Data Module
Fetches NDX options chains, NQ quotes, VXN, and historical bars.
"""

import os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta


class SchwabData:
    def __init__(self, auth):
        """
        Args:
            auth: SchwabAuth instance (authenticated)
        """
        self.auth = auth
        self.base = '/marketdata/v1'

    def get_ndx_options_chain(self, dte=1):
        """
        Pull full NDX options chain for specific DTE.
        Returns dict with calls and puts DataFrames.

        For 1DTE: gets tomorrow's expiration (or today if intraday 0DTE)
        """
        today = datetime.now()

        # Calculate target expiration date
        target = today + timedelta(days=dte)
        # Skip weekends
        while target.weekday() >= 5:
            target += timedelta(days=1)

        exp_date = target.strftime('%Y-%m-%d')

        params = {
            'symbol': '$NDX',
            'contractType': 'ALL',
            'strikeCount': 200,  # Wide range of strikes
            'includeUnderlyingQuote': 'true',
            'fromDate': exp_date,
            'toDate': exp_date,
        }

        data = self.auth.get(f'{self.base}/chains', params=params)

        # Parse the response into structured DataFrames
        result = {
            'underlying_price': data.get('underlyingPrice', 0),
            'underlying_symbol': data.get('symbol', '$NDX'),
            'expiration': exp_date,
            'calls': self._parse_chain_leg(data.get('callExpDateMap', {})),
            'puts': self._parse_chain_leg(data.get('putExpDateMap', {})),
            'raw': data
        }

        calls_count = len(result['calls']) if result['calls'] is not None else 0
        puts_count = len(result['puts']) if result['puts'] is not None else 0
        print(f"[DATA] NDX chain: {calls_count} calls, {puts_count} puts, "
              f"underlying={result['underlying_price']:.2f}, exp={exp_date}")

        return result

    def _parse_chain_leg(self, exp_date_map):
        """Parse Schwab's nested expDateMap into a flat DataFrame"""
        rows = []
        for exp_date, strikes in exp_date_map.items():
            for strike_str, contracts in strikes.items():
                for contract in contracts:
                    rows.append({
                        'symbol': contract.get('symbol', ''),
                        'description': contract.get('description', ''),
                        'strike': float(strike_str),
                        'bid': contract.get('bid', 0),
                        'ask': contract.get('ask', 0),
                        'last': contract.get('last', 0),
                        'mark': contract.get('mark', 0),
                        'mid': (contract.get('bid', 0) + contract.get('ask', 0)) / 2,
                        'volume': contract.get('totalVolume', 0),
                        'open_interest': contract.get('openInterest', 0),
                        'implied_volatility': contract.get('volatility', 0),
                        'delta': contract.get('delta', 0),
                        'gamma': contract.get('gamma', 0),
                        'theta': contract.get('theta', 0),
                        'vega': contract.get('vega', 0),
                        'rho': contract.get('rho', 0),
                        'dte': contract.get('daysToExpiration', 0),
                        'in_the_money': contract.get('inTheMoney', False),
                        'expiration': exp_date.split(':')[0],
                        'put_call': contract.get('putCall', ''),
                        'multiplier': contract.get('multiplier', 100),
                    })

        if not rows:
            return None
        return pd.DataFrame(rows)

    def get_nq_quote(self):
        """Get real-time NQ futures quote"""
        data = self.auth.get(f'{self.base}/quotes', params={'symbols': '/NQ'})

        # Schwab returns quotes keyed by symbol
        nq = data.get('/NQ', data.get('NQ', {}))
        quote = nq.get('quote', nq)

        result = {
            'last': quote.get('lastPrice', quote.get('mark', 0)),
            'bid': quote.get('bidPrice', 0),
            'ask': quote.get('askPrice', 0),
            'high': quote.get('highPrice', 0),
            'low': quote.get('lowPrice', 0),
            'open': quote.get('openPrice', 0),
            'close': quote.get('closePrice', 0),
            'volume': quote.get('totalVolume', 0),
            'net_change': quote.get('netChange', 0),
            'pct_change': quote.get('netPercentChange', 0),
        }

        print(f"[DATA] NQ: {result['last']:.2f} ({result['net_change']:+.2f})")
        return result

    def get_vxn_quote(self):
        """Get VXN (CBOE Nasdaq Volatility Index) quote"""
        # Try different symbol formats
        for symbol in ['$VXN.X', '$VXN', 'VXN']:
            try:
                data = self.auth.get(f'{self.base}/quotes', params={'symbols': symbol})
                if data:
                    vxn = data.get(symbol, {})
                    quote = vxn.get('quote', vxn)
                    result = {
                        'last': quote.get('lastPrice', quote.get('mark', 0)),
                        'close': quote.get('closePrice', 0),
                        'high': quote.get('highPrice', 0),
                        'low': quote.get('lowPrice', 0),
                        'net_change': quote.get('netChange', 0),
                    }
                    if result['last'] > 0:
                        print(f"[DATA] VXN: {result['last']:.2f} ({result['net_change']:+.2f})")
                        return result
            except Exception:
                continue

        print("[DATA] ⚠️  VXN not available via Schwab, falling back to yfinance")
        return None

    def get_nq_history(self, days=22, freq_minutes=5):
        """
        Get NQ historical bars for realized volatility calculation.

        Args:
            days: number of days of history
            freq_minutes: bar frequency in minutes (1, 5, 10, 15, 30)
        """
        params = {
            'symbol': '/NQ',
            'periodType': 'day',
            'period': days,
            'frequencyType': 'minute',
            'frequency': freq_minutes,
        }

        data = self.auth.get(f'{self.base}/pricehistory', params=params)
        candles = data.get('candles', [])

        if not candles:
            print(f"[DATA] ⚠️  No NQ historical bars returned")
            return None

        df = pd.DataFrame(candles)
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df = df.rename(columns={'open': 'open', 'high': 'high', 'low': 'low',
                                 'close': 'close', 'volume': 'volume'})
        df = df.set_index('datetime')
        df = df.sort_index()

        print(f"[DATA] NQ {freq_minutes}min bars: {len(df)} candles, "
              f"{df.index[0].strftime('%m/%d')} to {df.index[-1].strftime('%m/%d')}")
        return df

    def get_nq_daily(self, days=60):
        """Get NQ daily OHLC bars for Yang-Zhang estimator"""
        params = {
            'symbol': '/NQ',
            'periodType': 'month',
            'period': max(1, days // 30 + 1),
            'frequencyType': 'daily',
            'frequency': 1,
        }

        data = self.auth.get(f'{self.base}/pricehistory', params=params)
        candles = data.get('candles', [])

        if not candles:
            print(f"[DATA] ⚠️  No NQ daily bars returned")
            return None

        df = pd.DataFrame(candles)
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df = df.rename(columns={'open': 'open', 'high': 'high', 'low': 'low',
                                 'close': 'close', 'volume': 'volume'})
        df = df.set_index('datetime')
        df = df.sort_index()
        # Keep only last N days
        df = df.tail(days)

        print(f"[DATA] NQ daily bars: {len(df)} days, "
              f"{df.index[0].strftime('%m/%d')} to {df.index[-1].strftime('%m/%d')}")
        return df

    def get_multiple_quotes(self, symbols):
        """Get quotes for multiple symbols at once"""
        symbol_str = ','.join(symbols)
        data = self.auth.get(f'{self.base}/quotes', params={'symbols': symbol_str})
        return data
