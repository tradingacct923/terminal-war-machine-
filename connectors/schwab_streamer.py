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

# TIMESALE_* fields are identical across equity/options/futures (per Schwab docs):
#   0: key (symbol), 1: trade_time (ms epoch), 2: last_price, 3: last_size, 4: last_sequence
TIMESALE_FIELDS = {
    0: 'symbol', 1: 'trade_time', 2: 'last_price', 3: 'last_size', 4: 'last_sequence',
}

FIELD_MAPS = {
    'LEVELONE_FUTURES': FUTURES_FIELDS,
    'LEVELONE_EQUITIES': EQUITY_FIELDS,
    'LEVELONE_OPTIONS': OPTIONS_FIELDS,
    'CHART_FUTURES': CHART_FUTURES_FIELDS,
    'CHART_EQUITY': CHART_EQUITY_FIELDS,
    'TIMESALE_OPTIONS': TIMESALE_FIELDS,
    'TIMESALE_EQUITY': TIMESALE_FIELDS,
    'TIMESALE_FUTURES': TIMESALE_FIELDS,
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

    def __init__(self, auth, n_connections=1):
        """
        Args:
            auth: Authenticated SchwabAuth instance
            n_connections: Number of concurrent WebSocket connections to
                           maintain. Each connection has its own 3,000-symbol
                           LEVELONE_OPTIONS quota from Schwab. Multiple
                           connections from the same OAuth token are accepted
                           by Schwab as long as their LOGINs are staggered
                           (we delay each by idx*4s to avoid the auth race).
                           All connections run as concurrent asyncio tasks
                           inside the SAME event loop / thread, so gevent
                           monkey-patching doesn't collide. Default 1 keeps
                           backward compatibility with single-WS callers.
        """
        self.auth = auth
        self._n = max(1, int(n_connections))
        self._thread = None
        self._loop = None
        self._running = False
        self._request_id = 0
        self._callbacks = defaultdict(list)

        # Per-connection state (lists indexed by conn_idx 0..N-1).
        # Each WebSocket has its own subscription dict so reconnect of
        # any one connection re-subscribes only its own symbols, and the
        # 3,000-symbol per-WebSocket Schwab cap is honored independently.
        self._wss = [None] * self._n
        self._subs_per_conn = [dict() for _ in range(self._n)]

        # Latest data store (thread-safe reads via GIL).
        # Shared across connections — _process_message merges by symbol so
        # a quote update on either WS lands in the same `latest` dict.
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

    # Legacy single-conn shims for any external accessors.
    @property
    def _ws(self):
        """Backward-compat: return the first connection's WebSocket."""
        return self._wss[0] if self._wss else None

    @_ws.setter
    def _ws(self, value):
        if self._wss:
            self._wss[0] = value

    @property
    def _subscriptions(self):
        """Backward-compat: return the first connection's subscription dict."""
        return self._subs_per_conn[0] if self._subs_per_conn else {}

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

    def subscribe_futures(self, symbols, fields=None, conn_idx=0):
        """Subscribe to Level 1 futures quotes on connection `conn_idx`."""
        if fields is None:
            fields = '0,1,2,3,4,5,8,9,10,11,12,13,14,18,19,20,22,23,24,31'
        self._subscribe('LEVELONE_FUTURES', symbols, fields, conn_idx)

    def subscribe_equities(self, symbols, fields=None, conn_idx=0):
        """Subscribe to Level 1 equity quotes on connection `conn_idx`."""
        if fields is None:
            # Fields 37-41: bid_time, ask_time, ask_mic, bid_mic, last_mic
            # — NBBO venue tracking for MM withdrawal detection at tick speed
            fields = '0,1,2,3,4,5,8,9,10,11,12,17,18,25,32,33,37,38,39,40,41,42'
        self._subscribe('LEVELONE_EQUITIES', symbols, fields, conn_idx)

    def subscribe_options(self, symbols, fields=None, conn_idx=0):
        """Subscribe to Level 1 options on connection `conn_idx`.

        Each connection has its own 3,000-symbol Schwab cap, so distribute
        large option universes across multiple connections via conn_idx.
        """
        if fields is None:
            # 2026-05-04: bumped to FULL Schwab field set (0-55 inclusive).
            # Critical missing fields previously: 42 (last_trading_day),
            # 43 (settlement_type — A/P for AM/PM), 44 (net_pct_change),
            # 47 (implied_yield), 48 (is_penny_pilot), 49 (option_root —
            # explicit root extraction), 50/51 (52wk hi/lo), 54 (indicative_quote_time).
            # `settlement_type` (43) is the AUTHORITATIVE AM/PM indicator —
            # see flow_accumulator._classify_setup_mode for usage.
            fields = ','.join(str(i) for i in range(0, 56))
        self._subscribe('LEVELONE_OPTIONS', symbols, fields, conn_idx)

    def subscribe_nyse_book(self, symbols, conn_idx=0):
        """Subscribe to NYSE Level 2 book on connection `conn_idx`."""
        self._subscribe('NYSE_BOOK', symbols, '0,1,2,3', conn_idx)

    def subscribe_nasdaq_book(self, symbols, conn_idx=0):
        """Subscribe to NASDAQ Level 2 book on connection `conn_idx`."""
        self._subscribe('NASDAQ_BOOK', symbols, '0,1,2,3', conn_idx)

    def subscribe_options_book(self, symbols, conn_idx=0):
        """Subscribe to OPTIONS Level 2 book on connection `conn_idx`.

        Pin to the same conn_idx as the underlying LEVELONE_OPTIONS sub
        for the same symbols — keeps quote + book on one WS for consistency.
        """
        self._subscribe('OPTIONS_BOOK', symbols, '0,1,2,3', conn_idx)

    def subscribe_timesale_options(self, symbols, conn_idx=0):
        """Subscribe to trade-by-trade option prints (TIMESALE_OPTIONS)."""
        self._subscribe('TIMESALE_OPTIONS', symbols, '0,1,2,3,4', conn_idx)

    def subscribe_timesale_equity(self, symbols, conn_idx=0):
        """Subscribe to trade-by-trade equity prints (TIMESALE_EQUITY)."""
        self._subscribe('TIMESALE_EQUITY', symbols, '0,1,2,3,4', conn_idx)

    def subscribe_timesale_futures(self, symbols, conn_idx=0):
        """Subscribe to trade-by-trade futures prints (TIMESALE_FUTURES)."""
        self._subscribe('TIMESALE_FUTURES', symbols, '0,1,2,3,4', conn_idx)

    def subscribe_chart_futures(self, symbols, conn_idx=0):
        """Subscribe to real-time futures chart candles on connection `conn_idx`."""
        self._subscribe('CHART_FUTURES', symbols, '0,1,2,3,4,5,6', conn_idx)

    def subscribe_chart_equity(self, symbols, conn_idx=0):
        """Subscribe to real-time equity chart candles on connection `conn_idx`."""
        self._subscribe('CHART_EQUITY', symbols, '0,1,2,3,4,5,6,7,8', conn_idx)

    def subscribe_account_activity(self, conn_idx=0):
        """Subscribe to account activity (order fills, etc.)."""
        self._subscribe('ACCT_ACTIVITY', ['Account Activity'], '0,1,2,3', conn_idx)

    def subscribe_screener_option(self, sort='VOLUME', frequency='0', conn_idx=0):
        """Subscribe to real-time options screener stream."""
        self._subscribe('SCREENER_OPTION', [f'OPTION_ALL_{sort}_{frequency}'], '0,1,2,3,4', conn_idx)

    def subscribe_screener_equity(self, exchange='NYSE', sort='VOLUME', frequency='0', conn_idx=0):
        """Subscribe to real-time equity screener stream."""
        self._subscribe('SCREENER_EQUITY', [f'{exchange}_{sort}_{frequency}'], '0,1,2,3,4', conn_idx)

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
        """Main event loop — runs N concurrent WebSocket connections inside ONE asyncio loop.

        Critical for gevent compat: server.py monkey-patches threading, so
        spawning a SECOND threading.Thread with its OWN asyncio loop collides.
        Solution: ONE thread, ONE loop, but inside that loop run N concurrent
        WebSocket coroutines via asyncio.gather. Each coroutine maintains its
        own WebSocket session with its own Schwab session ID and its own
        3,000-symbol LEVELONE_OPTIONS quota.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        while self._running:
            try:
                self._loop.run_until_complete(self._run_all_connections())
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

    async def _run_all_connections(self):
        """Spawn _connect_one(idx) for each WebSocket and wait for all.

        Each _connect_one is now an infinite while-loop that self-heals on
        WS close — connections recover INDEPENDENTLY. We use
        return_exceptions=True so a fatal error in one connection doesn't
        cancel the others (each is responsible for its own reconnect logic).
        """
        return await asyncio.gather(
            *[self._connect_one(idx, login_delay=idx * 4)
              for idx in range(self._n)],
            return_exceptions=True,
        )

    async def _connect_one(self, idx, login_delay=0):
        """Maintain ONE WebSocket connection at index `idx` — SELF-HEALING.

        Wraps connect/login/message-loop in `while self._running` so a
        WebSocket close on this connection ONLY affects this connection;
        other connections in the asyncio.gather group keep running. Without
        this loop, a single WS close would cause _connect_one to return
        normally, asyncio.gather would keep waiting for the other coroutine
        forever, and the dead connection would never recover.

        Per-connection reconnect counter so back-off on conn 0 doesn't
        affect conn 1.

        login_delay staggers concurrent LOGINs to avoid Schwab's auth race
        (two LOGINs <~1s apart on the same token → code=3 'token invalid').
        Applied only on the FIRST connect; reconnects don't re-stagger
        because by then the other connection is established.
        """
        per_conn_reconnect_attempts = 0
        first_connect = True

        while self._running:
            try:
                if first_connect and login_delay > 0:
                    print(f"[STREAM-{idx}] waiting {login_delay}s before LOGIN (auth-race avoidance)...")
                    await asyncio.sleep(login_delay)
                first_connect = False

                print(f"[STREAM-{idx}] Connecting to {self._streamer_url}...")
                async with websockets.connect(
                    self._streamer_url,
                    max_size=16 * 2**20,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._wss[idx] = ws
                    per_conn_reconnect_attempts = 0  # reset on successful connect

                    # Login on this WS
                    login_success = await self._login(idx)
                    if not login_success:
                        raise Exception(f"Login failed on connection {idx}")
                    print(f"[STREAM-{idx}] ✅ Connected and logged in")

                    # Re-subscribe per-connection state (for reconnects)
                    for service, sub in self._subs_per_conn[idx].items():
                        await self._send_subscribe(idx, service, sub['keys'], sub['fields'])

                    # Message loop
                    last_heartbeat = time.time()
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            self._process_message(data, conn_idx=idx)
                            if 'notify' in data or 'data' in data or 'response' in data:
                                last_heartbeat = time.time()
                            if time.time() - last_heartbeat > 90:
                                print(f"[STREAM-{idx}] ⚠️  No messages in 90s, reconnecting...")
                                break
                        except json.JSONDecodeError:
                            print(f"[STREAM-{idx}] ⚠️  Invalid JSON: {message[:100]}")
                        except Exception as e:
                            print(f"[STREAM-{idx}] ⚠️  Message processing error: {e}")
                # `async with` exit — WS closed cleanly OR loop broke.
                # Fall through to outer while + reconnect.
                self._wss[idx] = None
                if self._running:
                    print(f"[STREAM-{idx}] WS closed — will reconnect")

            except Exception as e:
                self._wss[idx] = None
                if not self._running:
                    return  # graceful shutdown
                per_conn_reconnect_attempts += 1
                delay = min(2 ** per_conn_reconnect_attempts, self._max_reconnect_delay)
                print(f"[STREAM-{idx}] ❌ Connection error: {e}")
                print(f"[STREAM-{idx}] Reconnecting in {delay}s (attempt {per_conn_reconnect_attempts})...")
                # Token refresh — same access_token across both conns,
                # but if it expired this brings it current
                try:
                    self.auth._refresh()
                except Exception:
                    pass
                await asyncio.sleep(delay)

    # Backward-compat alias — older code may call this directly
    async def _connect_and_stream(self):
        """Single-connection legacy path (calls _connect_one(0))."""
        return await self._connect_one(0, login_delay=0)

    async def _login(self, conn_idx=0):
        """Send LOGIN command on the WebSocket at index `conn_idx`."""
        ws = self._wss[conn_idx]
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

        await ws.send(json.dumps(login_req))

        # Wait for login response
        try:
            resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(resp_raw)

            if 'response' in resp:
                for r in resp['response']:
                    content = r.get('content', {})
                    code = content.get('code', -1)
                    msg = content.get('msg', '')
                    if code == 0:
                        print(f"[STREAM-{conn_idx}] ✅ LOGIN successful: {msg}")
                        return True
                    else:
                        print(f"[STREAM-{conn_idx}] ❌ LOGIN failed: code={code}, msg={msg}")
                        return False
        except asyncio.TimeoutError:
            print(f"[STREAM-{conn_idx}] ❌ LOGIN timeout")
            return False

        return False

    async def _disconnect(self):
        """Send LOGOUT and close ALL WebSockets."""
        for idx, ws in enumerate(self._wss):
            if not ws:
                continue
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
                await ws.send(json.dumps(logout_req))
                await ws.close()
            except Exception:
                pass
            self._wss[idx] = None

    # ─── INTERNAL: SUBSCRIPTIONS ────────────────────────

    def _subscribe(self, service, symbols, fields, conn_idx=0):
        """Subscribe to symbols on a service via the WebSocket at `conn_idx`.

        First call for a (conn_idx, service) pair sends SUBS (establishes
        subscription). Subsequent calls send ADD with only NEW symbols so
        Schwab's SUBS-replace semantics don't evict earlier subscriptions.
        Per-connection subscription state is tracked in
        `_subs_per_conn[conn_idx][service]` so each WebSocket re-subscribes
        only its own symbols on reconnect.
        """
        if isinstance(symbols, str):
            symbols = [symbols]
        if conn_idx < 0 or conn_idx >= self._n:
            print(f"[STREAM] ⚠️  invalid conn_idx={conn_idx} (n_connections={self._n})")
            return

        # Split into existing (already subscribed) vs new (need to add)
        subs = self._subs_per_conn[conn_idx]
        prior = subs.get(service, {'keys_set': set(), 'fields': fields})
        prior_keys = prior.get('keys_set', set())
        new_keys = [s for s in symbols if s not in prior_keys]
        if not new_keys:
            return  # Nothing to do — already subscribed on this connection

        # Update local subscription state
        updated_keys = prior_keys | set(symbols)
        subs[service] = {
            'keys_set': updated_keys,
            'keys': ','.join(updated_keys),
            'fields': fields,
        }

        ws = self._wss[conn_idx] if conn_idx < len(self._wss) else None
        if ws and self._loop and self._loop.is_running():
            command = 'SUBS' if not prior_keys else 'ADD'
            asyncio.run_coroutine_threadsafe(
                self._send_subscribe(conn_idx, service, ','.join(new_keys), fields, command),
                self._loop
            )

    async def _send_subscribe(self, conn_idx, service, keys, fields, command='SUBS'):
        """Send a SUBS or ADD command on the WebSocket at `conn_idx`.

        Chunks payload to stay under Schwab's 65,535-byte WS frame limit.
        First chunk uses the caller-supplied command; subsequent chunks use
        ADD so they extend the active subscription instead of replacing it.
        """
        # 2,000 keys per chunk ≈ 24 KB payload — safe margin under 65,535.
        # Empirically, 6,000 LEVELONE_OPTIONS keys = 71,521 bytes (1009 close).
        CHUNK_SIZE = 2000

        key_list = keys.split(',') if isinstance(keys, str) and keys else list(keys or [])
        if not key_list:
            return

        ws = self._wss[conn_idx] if conn_idx < len(self._wss) else None
        if not ws:
            print(f"[STREAM-{conn_idx}] ⚠️  no WS for conn_idx={conn_idx} — dropping {service} SUBS")
            return

        chunks = [key_list[i:i + CHUNK_SIZE] for i in range(0, len(key_list), CHUNK_SIZE)]

        for i, chunk in enumerate(chunks):
            cmd = command if i == 0 else 'ADD'
            req = {
                "requests": [{
                    "requestid": str(self._next_id()),
                    "service": service,
                    "command": cmd,
                    "SchwabClientCustomerId": self._customer_id,
                    "SchwabClientCorrelId": self._correl_id,
                    "parameters": {
                        "keys": ','.join(chunk),
                        "fields": fields,
                    }
                }]
            }
            try:
                await ws.send(json.dumps(req))
                suffix = f" (chunk {i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
                print(f"[STREAM-{conn_idx}] 📡 {cmd} → {service}: {len(chunk)} keys{suffix}")
            except Exception as e:
                print(f"[STREAM-{conn_idx}] ⚠️  {cmd} failed for {service}: {e}")
                return

    # ─── INTERNAL: MESSAGE PROCESSING ───────────────────

    def _process_message(self, data, conn_idx=0):
        """Route incoming WebSocket messages.

        conn_idx is the connection that delivered the message — used only
        for logging; data handlers (set via .on()) are shared across all
        connections so the upstream consumer doesn't need to know which
        WS produced the event.
        """
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
                    print(f"[STREAM-{conn_idx}] ✅ {service} {command}: {msg}")
                else:
                    print(f"[STREAM-{conn_idx}] ⚠️  {service} {command} code={code}: {msg}")

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
                elif service in ('SCREENER_OPTION', 'SCREENER_EQUITY'):
                    self._process_screener(service, content, timestamp)
                else:
                    self._process_level1(service, content, timestamp)

    def _process_screener(self, service, content, timestamp):
        """Process screener data (SCREENER_OPTION / SCREENER_EQUITY).
        
        Schwab screener structure per entry:
          key: 'OPTION_ALL_VOLUME_0'
          0: key (same)
          1: timestamp (ms since epoch)
          2: sort field name
          3: frequency
          4: Items array - list of dicts with named keys:
              {symbol, description, lastPrice, netChange, 
               netPercentChange, totalVolume, ...}
        """
        for entry in content:
            screener_key = entry.get('key', entry.get('0', 'UNKNOWN'))
            items = entry.get('4', [])  # Field 4 = Items array
            
            if not isinstance(items, list):
                items = []

            decoded = {
                'symbol': screener_key,
                '_timestamp': timestamp,
                'sort_field': entry.get('2', ''),
                'frequency': entry.get('3', 0),
                'items': items,
                '_raw': entry,
            }

            # Store latest
            self.latest[service][screener_key] = decoded

            # Fire callbacks — each callback gets the full screener result
            for cb in self._callbacks.get(service, []):
                try:
                    cb(decoded)
                except Exception as e:
                    print(f"[STREAM] ⚠️  Screener callback error: {e}")

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
