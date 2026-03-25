"""
Schwab WebSocket Streaming Client
Real-time Level 1, Level 2 Book, Options, Futures, Charts, and Account Activity.

Usage:
    from schwab_auth import SchwabAuth
    from schwab_streamer import SchwabStreamer

    auth = SchwabAuth()
    streamer = SchwabStreamer(auth)
    streamer.start()

    # Subscribe to NQ futures
    streamer.subscribe_futures(['/NQ', '/ES'])

    # Subscribe to Level 2 book
    streamer.subscribe_nasdaq_book(['AAPL', 'QQQ'])

    # Register callbacks
    streamer.on('LEVELONE_FUTURES', my_handler)
    streamer.on('NASDAQ_BOOK', my_book_handler)

    # ... later
    streamer.stop()
"""

import json
import time
import asyncio
import threading
import traceback
from datetime import datetime
from collections import defaultdict

import websockets
import requests


# ═══════════════════════════════════════════════════════════
#  FIELD MAPS — number → human-readable name
# ═══════════════════════════════════════════════════════════

FUTURES_FIELDS = {
    0: 'symbol', 1: 'bid', 2: 'ask', 3: 'last', 4: 'bid_size', 5: 'ask_size',
    6: 'bid_id', 7: 'ask_id', 8: 'volume', 9: 'last_size',
    10: 'quote_time', 11: 'trade_time', 12: 'high', 13: 'low', 14: 'close',
    15: 'exchange_id', 16: 'description', 17: 'last_id', 18: 'open',
    19: 'net_change', 20: 'pct_change', 21: 'exchange_name', 22: 'security_status',
    23: 'open_interest', 24: 'mark', 25: 'tick', 26: 'tick_amount',
    27: 'product', 28: 'price_format', 29: 'trading_hours', 30: 'is_tradable',
    31: 'multiplier', 32: 'is_active', 33: 'settlement_price',
    34: 'active_symbol', 35: 'expiration_date', 36: 'expiration_style',
    37: 'ask_time', 38: 'bid_time', 39: 'quoted_in_session', 40: 'settlement_date',
}

EQUITY_FIELDS = {
    0: 'symbol', 1: 'bid', 2: 'ask', 3: 'last', 4: 'bid_size', 5: 'ask_size',
    6: 'ask_id', 7: 'bid_id', 8: 'volume', 9: 'last_size',
    10: 'high', 11: 'low', 12: 'close', 13: 'exchange_id', 14: 'marginable',
    15: 'description', 16: 'last_id', 17: 'open', 18: 'net_change',
    19: '52wk_high', 20: '52wk_low', 21: 'pe_ratio', 22: 'div_amount',
    23: 'div_yield', 24: 'nav', 25: 'exchange_name', 26: 'div_date',
    27: 'is_regular_quote', 28: 'is_regular_trade', 29: 'regular_last',
    30: 'regular_last_size', 31: 'regular_net_change', 32: 'security_status',
    33: 'mark', 34: 'quote_time', 35: 'trade_time', 36: 'regular_trade_time',
    37: 'bid_time', 38: 'ask_time', 39: 'ask_mic', 40: 'bid_mic', 41: 'last_mic',
    42: 'net_pct_change', 43: 'regular_pct_change', 44: 'mark_change',
    45: 'mark_pct_change', 46: 'htb_quantity', 47: 'htb_rate', 48: 'htb',
    49: 'shortable', 50: 'post_market_change', 51: 'post_market_pct_change',
}

OPTIONS_FIELDS = {
    0: 'symbol', 1: 'description', 2: 'bid', 3: 'ask', 4: 'last',
    5: 'high', 6: 'low', 7: 'close', 8: 'volume', 9: 'open_interest',
    10: 'implied_vol', 11: 'intrinsic_value', 12: 'exp_year', 13: 'multiplier',
    14: 'digits', 15: 'open', 16: 'bid_size', 17: 'ask_size', 18: 'last_size',
    19: 'net_change', 20: 'strike', 21: 'contract_type', 22: 'underlying',
    23: 'exp_month', 24: 'deliverables', 25: 'time_value', 26: 'exp_day',
    27: 'dte', 28: 'delta', 29: 'gamma', 30: 'theta', 31: 'vega', 32: 'rho',
    33: 'security_status', 34: 'theoretical_value', 35: 'underlying_price',
    36: 'exp_type', 37: 'mark', 38: 'quote_time', 39: 'trade_time',
    40: 'exchange', 41: 'exchange_name', 42: 'last_trading_day',
    43: 'settlement_type', 44: 'net_pct_change', 45: 'mark_change',
    46: 'mark_pct_change', 47: 'implied_yield', 48: 'is_penny_pilot',
    49: 'option_root', 50: '52wk_high', 51: '52wk_low',
    52: 'indicative_ask', 53: 'indicative_bid', 54: 'indicative_quote_time',
    55: 'exercise_type',
}

CHART_FUTURES_FIELDS = {
    0: 'key', 1: 'chart_time', 2: 'open', 3: 'high', 4: 'low', 5: 'close', 6: 'volume',
}

CHART_EQUITY_FIELDS = {
    0: 'key', 1: 'open', 2: 'high', 3: 'low', 4: 'close',
    5: 'volume', 6: 'sequence', 7: 'chart_time', 8: 'chart_day',
}

FIELD_MAPS = {
    'LEVELONE_FUTURES': FUTURES_FIELDS,
    'LEVELONE_EQUITIES': EQUITY_FIELDS,
    'LEVELONE_OPTIONS': OPTIONS_FIELDS,
    'CHART_FUTURES': CHART_FUTURES_FIELDS,
    'CHART_EQUITY': CHART_EQUITY_FIELDS,
}


class SchwabStreamer:
    """
    Real-time streaming client for the Schwab WebSocket API.

    Supports:
    - LEVELONE_FUTURES (NQ, ES, etc.)
    - LEVELONE_EQUITIES (SPY, QQQ, AAPL, etc.)
    - LEVELONE_OPTIONS (with full greeks)
    - NYSE_BOOK / NASDAQ_BOOK / OPTIONS_BOOK (Level 2 depth)
    - CHART_FUTURES / CHART_EQUITY (real-time candles)
    - ACCT_ACTIVITY (order fills)
    """

    def __init__(self, auth):
        """
        Args:
            auth: Authenticated SchwabAuth instance
        """
        self.auth = auth
        self._ws = None
        self._thread = None
        self._loop = None
        self._running = False
        self._request_id = 0
        self._callbacks = defaultdict(list)
        self._subscriptions = {}  # service -> {keys, fields}

        # Latest data store (thread-safe reads via GIL)
        self.latest = defaultdict(dict)  # service -> {symbol: data}
        self.book = defaultdict(dict)    # 'NYSE_BOOK'|'NASDAQ_BOOK' -> {symbol: book_data}

        # Streamer connection info (from user preferences)
        self._streamer_url = None
        self._customer_id = None
        self._correl_id = None
        self._channel = None
        self._function_id = None

        # Reconnect settings
        self._max_reconnect_delay = 30
        self._reconnect_attempts = 0

    # ─── PUBLIC API ─────────────────────────────────────

    def start(self):
        """Start the streamer in a background thread."""
        if self._running:
            print("[STREAM] Already running")
            return

        # Fetch streamer connection info
        self._fetch_user_preferences()

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # Wait for connection to establish
        time.sleep(2)

    def stop(self):
        """Stop the streamer."""
        self._running = False
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
        print("[STREAM] Stopped")

    def on(self, service, callback):
        """
        Register a callback for a streaming service.

        Args:
            service: e.g. 'LEVELONE_FUTURES', 'NASDAQ_BOOK', 'ACCT_ACTIVITY'
            callback: function(data_dict) called on each update
        """
        self._callbacks[service].append(callback)

    def subscribe_futures(self, symbols, fields=None):
        """Subscribe to Level 1 futures quotes."""
        if fields is None:
            fields = '0,1,2,3,4,5,8,9,10,11,12,13,14,18,19,20,22,23,24,31'
        self._subscribe('LEVELONE_FUTURES', symbols, fields)

    def subscribe_equities(self, symbols, fields=None):
        """Subscribe to Level 1 equity quotes."""
        if fields is None:
            fields = '0,1,2,3,4,5,8,9,10,11,12,17,18,25,32,33,42'
        self._subscribe('LEVELONE_EQUITIES', symbols, fields)

    def subscribe_options(self, symbols, fields=None):
        """Subscribe to Level 1 options with greeks."""
        if fields is None:
            # bid, ask, last, volume, OI, IV, strike, dte, delta, gamma, theta, vega, underlying
            fields = '0,1,2,3,4,5,6,7,8,9,10,16,17,18,19,20,21,22,25,27,28,29,30,31,32,34,35,37'
        self._subscribe('LEVELONE_OPTIONS', symbols, fields)

    def subscribe_nyse_book(self, symbols):
        """Subscribe to NYSE Level 2 book."""
        self._subscribe('NYSE_BOOK', symbols, '0,1,2,3')

    def subscribe_nasdaq_book(self, symbols):
        """Subscribe to NASDAQ Level 2 book."""
        self._subscribe('NASDAQ_BOOK', symbols, '0,1,2,3')

    def subscribe_options_book(self, symbols):
        """Subscribe to OPTIONS Level 2 book."""
        self._subscribe('OPTIONS_BOOK', symbols, '0,1,2,3')

    def subscribe_chart_futures(self, symbols):
        """Subscribe to real-time futures chart candles."""
        self._subscribe('CHART_FUTURES', symbols, '0,1,2,3,4,5,6')

    def subscribe_chart_equity(self, symbols):
        """Subscribe to real-time equity chart candles."""
        self._subscribe('CHART_EQUITY', symbols, '0,1,2,3,4,5,6,7,8')

    def subscribe_account_activity(self):
        """Subscribe to account activity (order fills, etc.)."""
        self._subscribe('ACCT_ACTIVITY', ['Account Activity'], '0,1,2,3')

    def get_book(self, symbol, exchange='NASDAQ_BOOK'):
        """Get latest Level 2 book snapshot for a symbol."""
        return self.book.get(exchange, {}).get(symbol)

    def get_latest(self, service, symbol):
        """Get latest data for a service/symbol."""
        return self.latest.get(service, {}).get(symbol)

    # ─── INTERNAL: CONNECTION ───────────────────────────

    def _fetch_user_preferences(self):
        """Fetch streamer URL and credentials from Schwab user preferences endpoint."""
        try:
            resp = requests.get(
                'https://api.schwabapi.com/trader/v1/userPreference',
                headers=self.auth.get_headers()
            )
            if resp.status_code != 200:
                raise Exception(f"User preferences failed: {resp.status_code} - {resp.text}")

            prefs = resp.json()

            # Extract streamer info
            streamer_info = prefs.get('streamerInfo', [prefs])[0] if isinstance(prefs.get('streamerInfo'), list) else prefs.get('streamerInfo', prefs)

            self._streamer_url = streamer_info.get('streamerSocketUrl', streamer_info.get('schwabClientUrl', ''))
            self._customer_id = streamer_info.get('schwabClientCustomerId', '')
            self._correl_id = streamer_info.get('schwabClientCorrelId', '')
            self._channel = streamer_info.get('schwabClientChannel', 'N9')
            self._function_id = streamer_info.get('schwabClientFunctionId', 'APIAPP')

            if not self._streamer_url:
                raise Exception("No streamer URL in user preferences")

            # Ensure wss:// prefix
            if not self._streamer_url.startswith('wss://'):
                self._streamer_url = 'wss://' + self._streamer_url.lstrip('https://').lstrip('http://')

            print(f"[STREAM] Streamer URL: {self._streamer_url}")
            print(f"[STREAM] Customer ID: {self._customer_id[:8]}...")
            print(f"[STREAM] Correl ID: {self._correl_id[:8]}...")

        except Exception as e:
            raise Exception(f"Failed to fetch user preferences: {e}")

    def _run_loop(self):
        """Main event loop for the WebSocket connection (runs in background thread)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        while self._running:
            try:
                self._loop.run_until_complete(self._connect_and_stream())
            except Exception as e:
                if self._running:
                    self._reconnect_attempts += 1
                    delay = min(2 ** self._reconnect_attempts, self._max_reconnect_delay)
                    print(f"[STREAM] ❌ Connection error: {e}")
                    print(f"[STREAM] Reconnecting in {delay}s (attempt {self._reconnect_attempts})...")
                    time.sleep(delay)
                    # Refresh token before reconnect
                    try:
                        self.auth._refresh()
                    except Exception:
                        pass

    async def _connect_and_stream(self):
        """Connect to WebSocket and process messages."""
        print(f"[STREAM] Connecting to {self._streamer_url}...")

        async with websockets.connect(
            self._streamer_url,
            max_size=2**20,  # 1MB max message size
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._reconnect_attempts = 0

            # Login
            login_success = await self._login()
            if not login_success:
                raise Exception("Login failed")

            print(f"[STREAM] ✅ Connected and logged in")

            # Re-subscribe to any existing subscriptions (for reconnects)
            for service, sub in self._subscriptions.items():
                await self._send_subscribe(service, sub['keys'], sub['fields'])

            # Message loop
            last_heartbeat = time.time()
            async for message in ws:
                try:
                    data = json.loads(message)
                    self._process_message(data)

                    # Track heartbeats
                    if 'notify' in data:
                        last_heartbeat = time.time()

                    # Check heartbeat timeout
                    if time.time() - last_heartbeat > 60:
                        print("[STREAM] ⚠️  No heartbeat in 60s, reconnecting...")
                        break

                except json.JSONDecodeError:
                    print(f"[STREAM] ⚠️  Invalid JSON: {message[:100]}")
                except Exception as e:
                    print(f"[STREAM] ⚠️  Message processing error: {e}")

    async def _login(self):
        """Send LOGIN command to the streamer."""
        login_req = {
            "requests": [{
                "requestid": str(self._next_id()),
                "service": "ADMIN",
                "command": "LOGIN",
                "SchwabClientCustomerId": self._customer_id,
                "SchwabClientCorrelId": self._correl_id,
                "parameters": {
                    "Authorization": self.auth.access_token,
                    "SchwabClientChannel": self._channel,
                    "SchwabClientFunctionId": self._function_id,
                }
            }]
        }

        await self._ws.send(json.dumps(login_req))

        # Wait for login response
        try:
            resp_raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
            resp = json.loads(resp_raw)

            if 'response' in resp:
                for r in resp['response']:
                    content = r.get('content', {})
                    code = content.get('code', -1)
                    msg = content.get('msg', '')
                    if code == 0:
                        print(f"[STREAM] ✅ LOGIN successful: {msg}")
                        return True
                    else:
                        print(f"[STREAM] ❌ LOGIN failed: code={code}, msg={msg}")
                        return False
        except asyncio.TimeoutError:
            print("[STREAM] ❌ LOGIN timeout")
            return False

        return False

    async def _disconnect(self):
        """Send LOGOUT and close WebSocket."""
        if self._ws:
            try:
                logout_req = {
                    "requests": [{
                        "requestid": str(self._next_id()),
                        "service": "ADMIN",
                        "command": "LOGOUT",
                        "SchwabClientCustomerId": self._customer_id,
                        "SchwabClientCorrelId": self._correl_id,
                    }]
                }
                await self._ws.send(json.dumps(logout_req))
                await self._ws.close()
            except Exception:
                pass

    # ─── INTERNAL: SUBSCRIPTIONS ────────────────────────

    def _subscribe(self, service, symbols, fields):
        """Queue a subscription (sends immediately if connected)."""
        if isinstance(symbols, str):
            symbols = [symbols]

        keys = ','.join(symbols)
        self._subscriptions[service] = {'keys': keys, 'fields': fields}

        # Send immediately if connected
        if self._ws and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_subscribe(service, keys, fields),
                self._loop
            )

    async def _send_subscribe(self, service, keys, fields):
        """Send a SUBS command to the WebSocket."""
        req = {
            "requests": [{
                "requestid": str(self._next_id()),
                "service": service,
                "command": "SUBS",
                "SchwabClientCustomerId": self._customer_id,
                "SchwabClientCorrelId": self._correl_id,
                "parameters": {
                    "keys": keys,
                    "fields": fields,
                }
            }]
        }
        try:
            await self._ws.send(json.dumps(req))
            print(f"[STREAM] 📡 Subscribed to {service}: {keys}")
        except Exception as e:
            print(f"[STREAM] ⚠️  Subscribe failed for {service}: {e}")

    # ─── INTERNAL: MESSAGE PROCESSING ───────────────────

    def _process_message(self, data):
        """Route incoming WebSocket messages."""
        # Heartbeat
        if 'notify' in data:
            return  # Silently handle heartbeats

        # Subscription responses
        if 'response' in data:
            for r in data['response']:
                service = r.get('service', '')
                command = r.get('command', '')
                content = r.get('content', {})
                code = content.get('code', -1)
                msg = content.get('msg', '')
                if code == 0:
                    print(f"[STREAM] ✅ {service} {command}: {msg}")
                else:
                    print(f"[STREAM] ⚠️  {service} {command} code={code}: {msg}")

        # Data updates
        if 'data' in data:
            for item in data['data']:
                service = item.get('service', '')
                timestamp = item.get('timestamp', 0)
                content = item.get('content', [])

                if service in ('NYSE_BOOK', 'NASDAQ_BOOK', 'OPTIONS_BOOK'):
                    self._process_book(service, content, timestamp)
                elif service == 'ACCT_ACTIVITY':
                    self._process_acct_activity(content, timestamp)
                else:
                    self._process_level1(service, content, timestamp)

    def _process_level1(self, service, content, timestamp):
        """Process Level 1 data (futures, equities, options)."""
        field_map = FIELD_MAPS.get(service, {})

        for entry in content:
            symbol = entry.get('key', entry.get('0', 'UNKNOWN'))
            decoded = {'symbol': symbol, '_timestamp': timestamp, '_raw': entry}

            for k, v in entry.items():
                if k in ('key', 'delayed', 'assetMainType', 'assetSubType', 'cusip'):
                    decoded[k] = v
                    continue
                try:
                    field_num = int(k)
                    field_name = field_map.get(field_num, f'field_{field_num}')
                    decoded[field_name] = v
                except (ValueError, KeyError):
                    decoded[k] = v

            # Store latest
            self.latest[service][symbol] = decoded

            # Fire callbacks
            for cb in self._callbacks.get(service, []):
                try:
                    cb(decoded)
                except Exception as e:
                    print(f"[STREAM] ⚠️  Callback error for {service}: {e}")

    def _process_book(self, service, content, timestamp):
        """Process Level 2 book data."""
        for entry in content:
            symbol = entry.get('key', 'UNKNOWN')
            snap_time = entry.get('1', timestamp)

            book_data = {
                'symbol': symbol,
                'timestamp': snap_time,
                'bids': self._parse_book_levels(entry.get('2', [])),
                'asks': self._parse_book_levels(entry.get('3', [])),
            }

            # Store
            self.book[service][symbol] = book_data

            # Fire callbacks
            for cb in self._callbacks.get(service, []):
                try:
                    cb(book_data)
                except Exception as e:
                    print(f"[STREAM] ⚠️  Book callback error: {e}")

    def _parse_book_levels(self, levels):
        """Parse book price levels into structured data."""
        parsed = []
        for level in levels:
            entry = {
                'price': level.get('0', 0),
                'size': level.get('1', 0),
                'mm_count': level.get('2', 0),
                'market_makers': [],
            }
            for mm in level.get('3', []):
                entry['market_makers'].append({
                    'id': mm.get('0', ''),
                    'size': mm.get('1', 0),
                    'time': mm.get('2', 0),
                })
            parsed.append(entry)
        return parsed

    def _process_acct_activity(self, content, timestamp):
        """Process account activity messages."""
        for entry in content:
            decoded = {
                'account': entry.get('1', ''),
                'message_type': entry.get('2', ''),
                'message_data': entry.get('3', ''),
                'timestamp': timestamp,
            }

            # Try to parse message_data as JSON
            try:
                if decoded['message_data']:
                    decoded['message_data'] = json.loads(decoded['message_data'])
            except (json.JSONDecodeError, TypeError):
                pass

            # Fire callbacks
            for cb in self._callbacks.get('ACCT_ACTIVITY', []):
                try:
                    cb(decoded)
                except Exception as e:
                    print(f"[STREAM] ⚠️  ACCT_ACTIVITY callback error: {e}")

    # ─── HELPERS ────────────────────────────────────────

    def _next_id(self):
        """Get next unique request ID."""
        self._request_id += 1
        return self._request_id

    @property
    def is_connected(self):
        return self._ws is not None and self._running


# ═══════════════════════════════════════════════════════════
#  UTILITY: Pretty-print helpers
# ═══════════════════════════════════════════════════════════

def format_book(book_data, max_levels=10):
    """Format a Level 2 book snapshot for display."""
    if not book_data:
        return "No book data"

    lines = []
    lines.append(f"\n{'─' * 60}")
    lines.append(f"  📖 {book_data['symbol']} ORDER BOOK")
    ts = book_data.get('timestamp', 0)
    if ts > 0:
        try:
            dt = datetime.fromtimestamp(ts / 1000)
            lines.append(f"  {dt.strftime('%H:%M:%S.%f')[:-3]}")
        except Exception:
            pass
    lines.append(f"{'─' * 60}")

    bids = book_data.get('bids', [])[:max_levels]
    asks = book_data.get('asks', [])[:max_levels]

    lines.append(f"  {'BID':^28s} | {'ASK':^28s}")
    lines.append(f"  {'Size':>8s}  {'Price':>10s}  {'MMs':>4s} | {'Price':>10s}  {'Size':>8s}  {'MMs':>4s}")
    lines.append("  " + "─" * 26 + " | " + "─" * 26)

    max_rows = max(len(bids), len(asks))
    for i in range(min(max_rows, max_levels)):
        bid_str = ""
        ask_str = ""
        if i < len(bids):
            b = bids[i]
            bid_str = f"  {b['size']:>8,d}  {b['price']:>10.2f}  {b['mm_count']:>4d}"
        else:
            bid_str = f"  {'':>8s}  {'':>10s}  {'':>4s}"

        if i < len(asks):
            a = asks[i]
            ask_str = f"{a['price']:>10.2f}  {a['size']:>8,d}  {a['mm_count']:>4d}"
        else:
            ask_str = f"{'':>10s}  {'':>8s}  {'':>4s}"

        lines.append(f"{bid_str} | {ask_str}")

    return '\n'.join(lines)


def format_futures_quote(data):
    """Format a futures Level 1 quote for display."""
    if not data:
        return "No data"
    sym = data.get('symbol', '???')
    bid = data.get('bid', 0)
    ask = data.get('ask', 0)
    last = data.get('last', 0)
    net = data.get('net_change', 0)
    vol = data.get('volume', 0)
    return f"{sym}: {last:.2f} ({net:+.2f}) | bid={bid:.2f} ask={ask:.2f} | vol={vol:,}"
