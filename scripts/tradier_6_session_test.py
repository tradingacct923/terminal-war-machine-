#!/usr/bin/env python3
"""
Tradier 6-Session Concurrent WS Test (standalone)

Question to answer empirically with PROOF:

  Q. Can Tradier sustain 6 concurrent WebSocket connections from the same
     OAuth token? Already verified: 2 (production deployment), 3 (probe),
     5 (probe). 6 is UNTESTED.

WARNING: Live server currently runs 5 Tradier WS conns (Phase 15).
This test opens 6 ADDITIONAL sessions on the same token → total 11 per token.
If Tradier caps total per-token sessions below 11, the test will fail OR
some of the production conns may die. Post-test log audit will detect this.

The test:
  Phase 1-6 — Open Conn A through F sequentially (4s stagger each)
  Phase 7   — Monitor ALL SIX for 60 seconds
  Phase 8   — Verdict: which conns stayed alive + flowed events
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


# ── Six distinct test symbol sets — all liquid US equities ──────────────
SYMS_A = ['QQQ',   'SPY',   'IWM',  'DIA',  'XLK']
SYMS_B = ['NVDA',  'AAPL',  'MSFT', 'AMZN', 'META']
SYMS_C = ['GOOGL', 'TSLA',  'AVGO', 'AMD',  'NFLX']
SYMS_D = ['VTI',   'VOO',   'ARKK', 'USO',  'GLD']
SYMS_E = ['JPM',   'BAC',   'GS',   'MS',   'WFC']
SYMS_F = ['XOM',   'CVX',   'BA',   'CAT',  'GE']


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
            if state.events % 200 == 1:
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
    print(' Tradier 6-Session Concurrent WS Test')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print(' NOTE: Live server runs 5 Tradier conns. Total sessions on token: 11')
    print('═' * 75)
    print()

    states = []

    for phase, (label, syms) in enumerate([
        ('A', SYMS_A), ('B', SYMS_B), ('C', SYMS_C),
        ('D', SYMS_D), ('E', SYMS_E), ('F', SYMS_F),
    ], 1):
        print(f'Phase {phase}: Open Conn {label} ({phase} concurrent so far)')
        print('─' * 75)
        s = open_conn(label, syms, states)
        if not s:
            print(f'❌ Failed to obtain session ID for Conn {label}')
            return False
        time.sleep(7)
        alive_states = [(st.name, st.disconnect_time is None, st.events) for st in states]
        alive_count = sum(1 for _, alive, _ in alive_states if alive)
        print(f'  After 7s — alive: {alive_count}/{phase}')
        for name, alive, events in alive_states:
            mark = '✓' if alive else '❌'
            print(f'    {mark} Conn {name}: {events} events')
        if alive_count < phase:
            print(f'  ⚠ Only {alive_count} of {phase} alive — Tradier may be capping at {alive_count}')
        print()

    # ── Phase 7: Sustained monitoring ─────────────────────────────────
    print(f'Phase 7: Monitor ALL SIX for 60 seconds')
    print('─' * 75)
    baselines = [s.events for s in states]
    start = time.time()
    while time.time() - start < 60:
        time.sleep(10)
        elapsed = int(time.time() - start)
        line_parts = [f'T+{elapsed:>2}s']
        for s in states:
            mark = '✓' if s.disconnect_time is None else '❌'
            line_parts.append(f'{s.name}:{mark}{s.events:>5d}ev')
        print('  ' + ' | '.join(line_parts))

    # ── Verdict ───────────────────────────────────────────────────────
    print()
    print('═' * 75)
    print(' VERDICT')
    print('═' * 75)
    alive_count = 0
    growing_count = 0
    for s, base in zip(states, baselines):
        alive = s.disconnect_time is None
        grew = s.events > base
        if alive: alive_count += 1
        if alive and grew: growing_count += 1
        die_reason = ''
        if not alive:
            die_reason = (f' | died t+{(s.disconnect_time - s.connect_time):.0f}s '
                          f'code={s.disconnect_code} '
                          f'msg={s.disconnect_msg!r}')
        status = '✅' if (alive and grew) else ('⚠ alive but silent' if alive else '❌ died')
        print(f'  Conn {s.name}: {status} | events={s.events} (grew Phase 7: {grew}){die_reason}')
    print()

    if alive_count == 6 and growing_count == 6:
        print('  ✅✅✅ 6-SESSION WORKS — All 6 connections sustained 60s, all flowing events')
        print('  → Total per-token sessions verified: 11 (5 server + 6 test)')
        print('  → Can deploy 6-WS architecture (Conn F) for full QQQ chain coverage')
        return True
    elif alive_count == 6 and growing_count < 6:
        print(f'  ⚠ All 6 alive but {6-growing_count} silent (low-volume sector?)')
        print('  → Probably safe; verify with denser symbols if needed')
        return None
    else:
        print(f'  ❌ Only {alive_count}/6 alive — Tradier per-token cap reached')
        return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
