#!/usr/bin/env python3
"""
Tradier 5-Session Concurrent WS Test (standalone)

Question to answer empirically with PROOF:

  Q. Can Tradier sustain 5 concurrent WebSocket connections from the same
     OAuth token? We've already verified 2 work (Phase 14 deployment) and
     3 work (tradier_3rd_session_test.py). 4 and 5 are UNTESTED.

The test:
  Phase 1 — Open Conn A with 5 distinct symbols, verify events flow
  Phase 2 — Open Conn B with different symbols, both alive + flowing
  Phase 3 — Open Conn C, all 3 alive + flowing
  Phase 4 — Open Conn D, all 4 alive + flowing
  Phase 5 — Open Conn E, all 5 alive + flowing
  Phase 6 — Monitor ALL FIVE for 60 seconds, verify no kicks

Each connection: 5 different liquid US equities (guaranteed RTH event flow)
Sessions are unique per connection (different sessionid each)
Stagger 5s between conn opens (matches production deployment pattern)
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


# ── Five distinct test symbol sets — all liquid US equities ─────────────
SYMS_A = ['QQQ',   'SPY',   'IWM',  'DIA',  'XLK']
SYMS_B = ['NVDA',  'AAPL',  'MSFT', 'AMZN', 'META']
SYMS_C = ['GOOGL', 'TSLA',  'AVGO', 'AMD',  'NFLX']
SYMS_D = ['VTI',   'VOO',   'ARKK', 'USO',  'GLD']
SYMS_E = ['JPM',   'BAC',   'GS',   'MS',   'WFC']


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
            if state.events % 100 == 1:
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


def open_conn(name, symbols, states):
    """Open one Tradier WS conn, append to states list."""
    s = ConnState(name, symbols)
    s.session_id = request_session()
    if not s.session_id:
        print(f'❌ Session ID for Conn {name} failed')
        return None
    print(f'  [{name}] sessionid: {s.session_id}')
    threading.Thread(target=run_connection, args=(s,), daemon=True).start()
    states.append(s)
    return s


def main():
    print('═' * 75)
    print(' Tradier 5-Session Concurrent WS Test')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 75)
    print()

    states = []

    # ── Phase 1: Conn A ─────────────────────────────────────────────────
    print('Phase 1: Open Conn A')
    print('─' * 75)
    state_a = open_conn('A', SYMS_A, states)
    if not state_a: return False
    time.sleep(8)
    if state_a.disconnect_time:
        print('  [A] ❌ Disconnected during Phase 1 baseline — abort')
        return False
    print(f'  [A] Phase 1 OK: {state_a.events} events, '
          f'{len(state_a.symbols_seen)} symbols')
    print()

    # ── Phase 2: Conn B ─────────────────────────────────────────────────
    print('Phase 2: Open Conn B (parallel with A)')
    print('─' * 75)
    state_b = open_conn('B', SYMS_B, states)
    if not state_b: return False
    time.sleep(8)
    a_alive = state_a.disconnect_time is None
    b_alive = state_b.disconnect_time is None
    print(f'  After 8s — A:{a_alive} ({state_a.events}ev), B:{b_alive} ({state_b.events}ev)')
    if not (a_alive and b_alive):
        print('  ❌ A or B died — abort')
        return False
    print()

    # ── Phase 3: Conn C ─────────────────────────────────────────────────
    print('Phase 3: Open Conn C (3 concurrent)')
    print('─' * 75)
    state_c = open_conn('C', SYMS_C, states)
    if not state_c: return False
    time.sleep(8)
    a_alive = state_a.disconnect_time is None
    b_alive = state_b.disconnect_time is None
    c_alive = state_c.disconnect_time is None
    print(f'  After 8s — A:{a_alive} ({state_a.events}ev), '
          f'B:{b_alive} ({state_b.events}ev), C:{c_alive} ({state_c.events}ev)')
    if not (a_alive and b_alive and c_alive):
        print('  ❌ A, B, or C died — abort')
        return False
    print()

    # ── Phase 4: Conn D ─────────────────────────────────────────────────
    print('Phase 4: Open Conn D (4 concurrent — UNTESTED before)')
    print('─' * 75)
    state_d = open_conn('D', SYMS_D, states)
    if not state_d: return False
    time.sleep(8)
    a_alive = state_a.disconnect_time is None
    b_alive = state_b.disconnect_time is None
    c_alive = state_c.disconnect_time is None
    d_alive = state_d.disconnect_time is None
    print(f'  After 8s — A:{a_alive} B:{b_alive} C:{c_alive} D:{d_alive}')
    if not all([a_alive, b_alive, c_alive, d_alive]):
        print('  ❌ At least one of A-D died at 4-conn — Tradier session cap = 3')
        # Don't abort yet; collect more diagnostic info on which died
    print()

    # ── Phase 5: Conn E ─────────────────────────────────────────────────
    print('Phase 5: Open Conn E (5 concurrent — UNTESTED before)')
    print('─' * 75)
    state_e = open_conn('E', SYMS_E, states)
    if not state_e: return False
    time.sleep(8)
    a_alive = state_a.disconnect_time is None
    b_alive = state_b.disconnect_time is None
    c_alive = state_c.disconnect_time is None
    d_alive = state_d.disconnect_time is None
    e_alive = state_e.disconnect_time is None
    print(f'  After 8s — A:{a_alive} B:{b_alive} C:{c_alive} D:{d_alive} E:{e_alive}')
    print()

    # ── Phase 6: Sustained monitoring ──────────────────────────────────
    print('Phase 6: Monitor ALL FIVE for 60 seconds')
    print('─' * 75)
    a_baseline = state_a.events
    b_baseline = state_b.events
    c_baseline = state_c.events
    d_baseline = state_d.events
    e_baseline = state_e.events

    start = time.time()
    while time.time() - start < 60:
        time.sleep(10)
        elapsed = int(time.time() - start)
        a_st = '✓' if state_a.disconnect_time is None else '❌'
        b_st = '✓' if state_b.disconnect_time is None else '❌'
        c_st = '✓' if state_c.disconnect_time is None else '❌'
        d_st = '✓' if state_d.disconnect_time is None else '❌'
        e_st = '✓' if state_e.disconnect_time is None else '❌'
        print(f'  T+{elapsed:>2}s | A:{a_st} {state_a.events:>5d}ev '
              f'| B:{b_st} {state_b.events:>5d}ev '
              f'| C:{c_st} {state_c.events:>5d}ev '
              f'| D:{d_st} {state_d.events:>5d}ev '
              f'| E:{e_st} {state_e.events:>5d}ev')

    # ── Verdict ─────────────────────────────────────────────────────────
    print()
    print('═' * 75)
    print(' VERDICT')
    print('═' * 75)
    final = {
        'A': (state_a, a_baseline),
        'B': (state_b, b_baseline),
        'C': (state_c, c_baseline),
        'D': (state_d, d_baseline),
        'E': (state_e, e_baseline),
    }
    alive_count = 0
    growing_count = 0
    for n, (st, base) in final.items():
        alive = st.disconnect_time is None
        grew = st.events > base
        if alive: alive_count += 1
        if alive and grew: growing_count += 1
        status = '✅' if (alive and grew) else ('⚠ alive but silent' if alive else '❌ died')
        die_reason = ''
        if not alive:
            die_reason = (f' | died t+{(st.disconnect_time - st.connect_time):.0f}s '
                          f'code={st.disconnect_code} '
                          f'msg={st.disconnect_msg!r}')
        print(f'  Conn {n}: {status} | events={st.events} '
              f'(grew during Phase 6: {grew}){die_reason}')
    print()

    if alive_count == 5 and growing_count == 5:
        print('  ✅✅ 5-SESSION WORKS — All 5 connections sustained 60s, all flowing events')
        print('  → Can deploy 5-WS architecture for full QQQ chain coverage')
        return True
    elif alive_count == 5 and growing_count < 5:
        print(f'  ⚠ All 5 alive but {5-growing_count} silent (low-volume symbols?)')
        print('  → Probably safe; verify with denser symbols if needed')
        return None
    else:
        print(f'  ❌ Only {alive_count}/5 connections alive — Tradier rejects beyond {alive_count}')
        return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
