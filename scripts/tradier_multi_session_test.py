#!/usr/bin/env python3
"""
Tradier Multi-Session WS Test (standalone — does NOT touch live server)

Verifies whether Tradier allows 2 concurrent WebSocket connections from
the SAME OAuth token. If yes, we can double our per-print coverage by
splitting the symbol budget across two WS connections.

The test:
  1. Request session ID #1 from Tradier REST
  2. Open WS Conn A with 5 known-active symbols (QQQ + Mag-8)
  3. Verify Conn A receives at least 1 event in 30 seconds
  4. Request session ID #2 from Tradier REST
  5. Open WS Conn B with 5 different known-active symbols
  6. Verify BOTH connections receive events for the next 60 seconds
  7. Report: SUCCESS (both alive) or FAILURE (Conn A kicked / silent)

Run anytime — works during RTH or after-hours. Doesn't subscribe any
overlapping symbols with the live server (uses different test symbols).
"""
import json
import os
import threading
import time
import sys
import urllib.request
from collections import defaultdict

# Load Tradier token
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'config.json'
)
try:
    with open(CONFIG_PATH) as f:
        TOKEN = json.load(f).get('options_api_key', '')
except Exception:
    TOKEN = os.getenv('TRADIER_TOKEN', '')

if not TOKEN:
    print('❌ No Tradier token found. Set TRADIER_TOKEN env or config.json')
    sys.exit(1)

try:
    import websocket  # websocket-client
except ImportError:
    print('❌ Missing dependency: pip install websocket-client')
    sys.exit(1)


# ── Test symbol sets (NOT in live server's subscribed set) ────────────────
# Use deep-OTM strikes that the live server isn't currently watching to
# avoid any state conflicts. These are likely silent during AH but should
# get at least 1-2 prints during RTH.
CONN_A_SYMBOLS = [
    'QQQ',                            # equity (always trades)
    'SPY',
    'NVDA',
    'AAPL',
    'TSLA',
]

CONN_B_SYMBOLS = [
    'MSFT',                           # different equities
    'AMZN',
    'META',
    'GOOGL',
    'AVGO',
]


# ── Per-connection state ──────────────────────────────────────────────────
class ConnState:
    def __init__(self, name):
        self.name = name
        self.session_id = None
        self.ws = None
        self.events_received = 0
        self.last_event_ts = 0
        self.symbols_seen = set()
        self.connect_time = None
        self.disconnect_time = None
        self.error = None


def request_session():
    """Request a Tradier WS session — returns session_id."""
    req = urllib.request.Request(
        'https://api.tradier.com/v1/markets/events/session',
        method='POST',
        data=b'',
        headers={
            'Authorization': f'Bearer {TOKEN}',
            'Accept': 'application/json',
            'Content-Length': '0',
        }
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read())
    return d.get('stream', {}).get('sessionid')


def make_callbacks(state: ConnState):
    """Closure over connection state for WS callbacks."""

    def on_open(ws):
        state.connect_time = time.time()
        print(f'  [{state.name}] ✓ WS connected at {time.strftime("%H:%M:%S")}')
        # Send subscribe payload
        symbols = CONN_A_SYMBOLS if state.name == 'A' else CONN_B_SYMBOLS
        payload = {
            'symbols': symbols,
            'sessionid': state.session_id,
            'linebreak': True,
            'filter': ['timesale', 'trade', 'quote', 'tradex'],
        }
        try:
            ws.send(json.dumps(payload))
            print(f'  [{state.name}] 📡 Subscribed: {symbols}')
        except Exception as e:
            print(f'  [{state.name}] ❌ Subscribe failed: {e}')
            state.error = f'subscribe_fail: {e}'

    def on_message(ws, raw):
        try:
            data = json.loads(raw)
            event_type = data.get('type', '?')
            symbol = data.get('symbol', '')
            state.events_received += 1
            state.last_event_ts = time.time()
            if symbol:
                state.symbols_seen.add(symbol)
            # Print every 25th event to keep log readable
            if state.events_received % 25 == 1:
                print(f'  [{state.name}] received {state.events_received} events '
                      f'(latest: {event_type} {symbol})')
        except Exception:
            pass

    def on_close(ws, close_code, close_msg):
        state.disconnect_time = time.time()
        uptime = state.disconnect_time - (state.connect_time or state.disconnect_time)
        print(f'  [{state.name}] ❌ WS CLOSED at {time.strftime("%H:%M:%S")} '
              f'(uptime={uptime:.1f}s code={close_code} msg={close_msg!r})')

    def on_error(ws, err):
        state.error = str(err)
        print(f'  [{state.name}] ⚠ WS error: {err!r}')

    return on_open, on_message, on_close, on_error


def run_connection(state: ConnState):
    """Blocking — runs the WS event loop. Call in a thread."""
    on_open, on_message, on_close, on_error = make_callbacks(state)
    state.ws = websocket.WebSocketApp(
        'wss://ws.tradier.com/v1/markets/events',
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
        on_error=on_error,
    )
    try:
        state.ws.run_forever(ping_interval=30, ping_timeout=10)
    except Exception as e:
        state.error = str(e)


def main():
    print('═' * 64)
    print(' Tradier Multi-Session WS Test')
    print(f' Started at: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 64)
    print()

    # ── Phase 1: Single-session baseline (Connection A only) ───────────
    print('Phase 1: Open Conn A alone, verify it works')
    print('─' * 64)
    state_a = ConnState('A')
    state_a.session_id = request_session()
    if not state_a.session_id:
        print('❌ Failed to obtain session ID for Conn A')
        return False
    print(f'  [A] sessionid: {state_a.session_id}')

    thread_a = threading.Thread(target=run_connection, args=(state_a,), daemon=True)
    thread_a.start()

    # Wait 15 seconds for Conn A to receive events
    time.sleep(15)
    print()
    print(f'  [A] Phase 1 result: {state_a.events_received} events, '
          f'symbols seen: {sorted(state_a.symbols_seen)}')
    if state_a.disconnect_time:
        print('  [A] ❌ Disconnected during Phase 1 — Tradier rejected single session')
        return False
    print()

    # ── Phase 2: Open Connection B in parallel ─────────────────────────
    print('Phase 2: Open Conn B (DIFFERENT session ID), monitor BOTH')
    print('─' * 64)
    state_b = ConnState('B')
    state_b.session_id = request_session()
    if not state_b.session_id:
        print('❌ Failed to obtain session ID for Conn B')
        return False
    print(f'  [B] sessionid: {state_b.session_id} (different from A)')

    a_baseline_events = state_a.events_received
    a_baseline_alive = (state_a.disconnect_time is None)

    thread_b = threading.Thread(target=run_connection, args=(state_b,), daemon=True)
    thread_b.start()

    # Monitor for 60 seconds
    print()
    print('  Monitoring both connections for 60 seconds...')
    start_t = time.time()
    while time.time() - start_t < 60:
        time.sleep(5)
        elapsed = int(time.time() - start_t)
        a_status = '✓ ALIVE' if state_a.disconnect_time is None else '❌ DEAD'
        b_status = '✓ ALIVE' if state_b.disconnect_time is None else '❌ DEAD'
        print(f'  T+{elapsed:>2}s | A: {a_status} ({state_a.events_received} ev) '
              f'| B: {b_status} ({state_b.events_received} ev)')

    # ── Phase 3: Final verdict ─────────────────────────────────────────
    print()
    print('═' * 64)
    print(' VERDICT')
    print('═' * 64)
    a_final_alive = (state_a.disconnect_time is None)
    b_final_alive = (state_b.disconnect_time is None)
    a_increased = state_a.events_received > a_baseline_events

    print(f'  Conn A baseline events:   {a_baseline_events}')
    print(f'  Conn A final events:      {state_a.events_received}')
    print(f'  Conn A still alive:       {a_final_alive}')
    print(f'  Conn A still receiving:   {a_increased}  (events grew during Phase 2)')
    print()
    print(f'  Conn B final events:      {state_b.events_received}')
    print(f'  Conn B still alive:       {b_final_alive}')
    print()

    if a_final_alive and b_final_alive:
        if a_increased and state_b.events_received > 0:
            print('  ✅ MULTI-SESSION WORKS')
            print('  → Both WS connections stayed alive AND received events')
            print('  → Can deploy multi-session for production')
            return True
        else:
            print('  ⚠️ AMBIGUOUS')
            print('  → Both connections alive but one or both silent (low-volume?)')
            print('  → Safe to deploy but needs RTH verification')
            return None
    elif b_final_alive and not a_final_alive:
        print('  ❌ TRADIER KICKED CONN A')
        print('  → 2nd session forced 1st session to close')
        print('  → Multi-session NOT supported (similar to Schwab April 28 behavior)')
        print('  → DO NOT deploy multi-session')
        return False
    elif a_final_alive and not b_final_alive:
        print('  ❌ CONN B FAILED')
        print('  → 2nd session was rejected or disconnected')
        print(f'  → Error: {state_b.error}')
        return False
    else:
        print('  ❌ BOTH CONNECTIONS DIED')
        print(f'  → A: {state_a.error}')
        print(f'  → B: {state_b.error}')
        return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
