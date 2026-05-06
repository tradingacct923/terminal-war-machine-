#!/usr/bin/env python3
"""
Tradier 3-Session Concurrent WS Test (standalone)

Question to answer empirically with PROOF:

  Q. Does Tradier allow 3 concurrent WebSocket connections from the same
     OAuth token? We've already verified 2 work (tradier_multi_session_test.py).

The test:
  1. Request session ID #1 → Open WS Conn A with 5 symbols
  2. Wait 10s, verify Conn A receives events
  3. Request session ID #2 → Open WS Conn B with 5 different symbols
  4. Wait 10s, verify both Conn A + Conn B alive and flowing
  5. Request session ID #3 → Open WS Conn C with 5 different symbols
  6. Monitor for 60 seconds — verify ALL THREE stay alive AND receive events
  7. Report: SUCCESS (all 3 alive + flowing) or FAILURE (which one died)
"""
import json
import os
import threading
import time
import sys
import urllib.request

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
    print('❌ No Tradier token')
    sys.exit(1)

try:
    import websocket
except ImportError:
    print('❌ pip install websocket-client')
    sys.exit(1)


# ── Three distinct test symbol sets (different equities, all liquid) ────────
SYMS_A = ['QQQ',  'SPY',  'IWM',  'DIA',  'XLK']
SYMS_B = ['NVDA', 'AAPL', 'MSFT', 'AMZN', 'META']
SYMS_C = ['GOOGL','TSLA', 'AVGO', 'AMD',  'NFLX']


class ConnState:
    def __init__(self, name, symbols):
        self.name = name
        self.symbols = symbols
        self.session_id = None
        self.ws = None
        self.events = 0
        self.last_event_ts = 0
        self.symbols_seen = set()
        self.connect_time = None
        self.disconnect_time = None
        self.disconnect_code = None
        self.disconnect_msg = None
        self.error = None


def request_session():
    req = urllib.request.Request(
        'https://api.tradier.com/v1/markets/events/session',
        method='POST', data=b'',
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
    def on_open(ws):
        state.connect_time = time.time()
        print(f'  [{state.name}] ✓ WS connected at {time.strftime("%H:%M:%S")}')
        payload = {
            'symbols': state.symbols,
            'sessionid': state.session_id,
            'linebreak': True,
            'filter': ['timesale', 'trade', 'quote', 'tradex'],
        }
        try:
            ws.send(json.dumps(payload))
            print(f'  [{state.name}] 📡 Subscribed: {state.symbols}')
        except Exception as e:
            print(f'  [{state.name}] ❌ Subscribe failed: {e}')
            state.error = f'subscribe_fail: {e}'

    def on_message(ws, raw):
        try:
            data = json.loads(raw)
            symbol = data.get('symbol', '')
            event_type = data.get('type', '?')
            state.events += 1
            state.last_event_ts = time.time()
            if symbol:
                state.symbols_seen.add(symbol)
            if state.events % 20 == 1:
                print(f'  [{state.name}] received {state.events} events '
                      f'(latest: {event_type} {symbol})')
        except Exception:
            pass

    def on_close(ws, code, msg):
        state.disconnect_time = time.time()
        state.disconnect_code = code
        state.disconnect_msg = msg
        uptime = state.disconnect_time - (state.connect_time or state.disconnect_time)
        print(f'  [{state.name}] ❌ WS CLOSED uptime={uptime:.1f}s '
              f'code={code} msg={msg!r}')

    def on_error(ws, err):
        state.error = str(err)
        print(f'  [{state.name}] ⚠ WS error: {err!r}')

    return on_open, on_message, on_close, on_error


def run_connection(state: ConnState):
    on_open, on_message, on_close, on_error = make_callbacks(state)
    state.ws = websocket.WebSocketApp(
        'wss://ws.tradier.com/v1/markets/events',
        on_open=on_open, on_message=on_message,
        on_close=on_close, on_error=on_error,
    )
    try:
        state.ws.run_forever(ping_interval=30, ping_timeout=10)
    except Exception as e:
        state.error = str(e)


def main():
    print('═' * 70)
    print(' Tradier 3-Session Concurrent WS Test')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 70)
    print()

    states = []

    # ── Phase 1: Open Conn A ─────────────────────────────────────────────
    print('Phase 1: Open Conn A')
    print('─' * 70)
    state_a = ConnState('A', SYMS_A)
    state_a.session_id = request_session()
    if not state_a.session_id:
        print('❌ Session ID #1 failed')
        return False
    print(f'  [A] sessionid: {state_a.session_id}')
    threading.Thread(target=run_connection, args=(state_a,), daemon=True).start()
    states.append(state_a)
    time.sleep(10)
    if state_a.disconnect_time:
        print('  [A] ❌ Disconnected during Phase 1 baseline — aborting')
        return False
    print(f'  [A] Phase 1 OK: {state_a.events} events, {len(state_a.symbols_seen)} symbols')
    print()

    # ── Phase 2: Open Conn B ─────────────────────────────────────────────
    print('Phase 2: Open Conn B (parallel with A)')
    print('─' * 70)
    state_b = ConnState('B', SYMS_B)
    state_b.session_id = request_session()
    if not state_b.session_id:
        print('❌ Session ID #2 failed')
        return False
    print(f'  [B] sessionid: {state_b.session_id}  (different from A)')
    threading.Thread(target=run_connection, args=(state_b,), daemon=True).start()
    states.append(state_b)
    time.sleep(10)
    a_alive = (state_a.disconnect_time is None)
    b_alive = (state_b.disconnect_time is None)
    print(f'  After 10s — A alive: {a_alive} ({state_a.events} ev), '
          f'B alive: {b_alive} ({state_b.events} ev)')
    if not a_alive or not b_alive:
        print('  ❌ One of A/B died — abort 3rd-conn test')
        return False
    print()

    # ── Phase 3: Open Conn C (THE TEST) ──────────────────────────────────
    print('Phase 3: Open Conn C (3rd concurrent session — THE TEST)')
    print('─' * 70)
    state_c = ConnState('C', SYMS_C)
    state_c.session_id = request_session()
    if not state_c.session_id:
        print('❌ Session ID #3 failed (Tradier may rate-limit session creation)')
        return False
    print(f'  [C] sessionid: {state_c.session_id}  (different from A and B)')
    a_baseline_events = state_a.events
    b_baseline_events = state_b.events
    threading.Thread(target=run_connection, args=(state_c,), daemon=True).start()
    states.append(state_c)
    print()

    # ── Phase 4: Monitor all three for 60 seconds ────────────────────────
    print('Phase 4: Monitor ALL THREE for 60 seconds')
    print('─' * 70)
    start = time.time()
    while time.time() - start < 60:
        time.sleep(5)
        elapsed = int(time.time() - start)
        a_st = '✓' if state_a.disconnect_time is None else '❌'
        b_st = '✓' if state_b.disconnect_time is None else '❌'
        c_st = '✓' if state_c.disconnect_time is None else '❌'
        print(f'  T+{elapsed:>2}s  | A: {a_st} {state_a.events:>4d}ev '
              f'| B: {b_st} {state_b.events:>4d}ev '
              f'| C: {c_st} {state_c.events:>4d}ev')

    # ── Phase 5: Verdict ─────────────────────────────────────────────────
    print()
    print('═' * 70)
    print(' VERDICT')
    print('═' * 70)
    a_alive = state_a.disconnect_time is None
    b_alive = state_b.disconnect_time is None
    c_alive = state_c.disconnect_time is None
    a_grew = state_a.events > a_baseline_events
    b_grew = state_b.events > b_baseline_events
    c_grew = state_c.events > 0

    print(f'  Conn A: alive={a_alive}, events={state_a.events} (grew during Phase 4: {a_grew})')
    print(f'  Conn B: alive={b_alive}, events={state_b.events} (grew during Phase 4: {b_grew})')
    print(f'  Conn C: alive={c_alive}, events={state_c.events} (received events: {c_grew})')
    print()

    if a_alive and b_alive and c_alive:
        if a_grew and b_grew and c_grew:
            print('  ✅✅ 3-SESSION WORKS')
            print('  → All 3 connections alive AND receiving events')
            print('  → Can expand to dual+1 deployment for full chain coverage')
            return True
        else:
            print('  ⚠ All 3 alive but one or more silent (after-hours / low-volume?)')
            print('  → Likely safe; verify during RTH for definitive proof')
            return None
    elif a_alive and b_alive and not c_alive:
        print('  ❌ Conn C REJECTED')
        print(f'  → Tradier closed C: code={state_c.disconnect_code} msg={state_c.disconnect_msg!r}')
        print('  → 3rd concurrent session NOT supported (cap = 2)')
        return False
    elif (not a_alive or not b_alive) and c_alive:
        print('  ❌ TRADIER KICKED A or B when C opened')
        print(f'  → A: alive={a_alive}, B: alive={b_alive}')
        print('  → 3rd session forces an existing one to close')
        return False
    else:
        print('  ❌ MULTIPLE FAILURES')
        return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
