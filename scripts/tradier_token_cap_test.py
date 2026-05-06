#!/usr/bin/env python3
"""
Tradier per-token cap empirical test.

Approach: Open a SIXTH WS connection (separate from our 5 production conns)
on the SAME OAuth token, subscribe to ~1,688 Mag-8 OCC symbols, and watch
for cap rejection signals.

This tests EXACTLY the question: "Will adding 1,688 more symbols to our
already-7,117 token blow past the Tradier per-token cap?"

If the test conn stays open and receives messages → cap is at least 8,805.
If Tradier closes the test conn or we see WS errors → cap is below 8,805.

The test conn closes itself after 60 seconds regardless. Production conns
are NOT touched.

Run: source venv/bin/activate && python scripts/tradier_token_cap_test.py
"""
import os
import sys
import json
import time
import threading
import urllib.request
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    print("=" * 78)
    print(" TRADIER PER-TOKEN CAP TEST")
    print("=" * 78)
    print()

    # Load token — config.json uses 'options_api_key' (provider=tradier)
    cfg_path = os.path.join(ROOT, 'config.json')
    with open(cfg_path) as f:
        cfg = json.load(f)
    token = cfg.get('options_api_key') or os.getenv('TRADIER_TOKEN', '')
    if not token:
        print("  ERROR: No Tradier token in config.json options_api_key or env TRADIER_TOKEN")
        return 1

    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    # Step 1: Pull Mag-8 OCC symbols (same logic as Phase 17B used to)
    MAG8 = ['NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO']
    target_per_ticker = 220
    print(f"  Step 1: Collecting {target_per_ticker}/ticker × 8 = ~1,760 Mag-8 OCC symbols")
    print()
    test_symbols = []
    for ticker in MAG8:
        try:
            # Spot
            req = urllib.request.Request(
                f'https://api.tradier.com/v1/markets/quotes?symbols={ticker}', headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                qd = json.loads(r.read())
            spot = float(qd['quotes']['quote']['last'] or 0)
            if spot <= 0:
                continue

            # Expirations
            req = urllib.request.Request(
                f'https://api.tradier.com/v1/markets/options/expirations?symbol={ticker}',
                headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                ed = json.loads(r.read())
            exps = (ed.get('expirations') or {}).get('date') or []
            if isinstance(exps, str):
                exps = [exps]
            exps = exps[:6]

            # Chains for each expiration
            radius = max(2.0, spot * 0.15)
            ticker_syms = []
            for exp in exps:
                try:
                    req = urllib.request.Request(
                        f'https://api.tradier.com/v1/markets/options/chains?symbol={ticker}&expiration={exp}&greeks=false',
                        headers=headers)
                    with urllib.request.urlopen(req, timeout=8) as r:
                        cd = json.loads(r.read())
                    options = (cd.get('options') or {}).get('option') or []
                    if isinstance(options, dict):
                        options = [options]
                    for opt in options:
                        K = float(opt.get('strike') or 0)
                        if abs(K - spot) > radius:
                            continue
                        sym = opt.get('symbol') or ''
                        if sym:
                            ticker_syms.append(sym)
                except Exception:
                    continue
            ticker_syms = ticker_syms[:target_per_ticker]
            print(f"    {ticker:<6s} spot=${spot:>7.2f} → {len(ticker_syms):>4d} OCC syms")
            test_symbols.extend(ticker_syms)
        except Exception as e:
            print(f"    {ticker}: ERROR {e}")
    print()
    print(f"  Total test symbols: {len(test_symbols)}")
    print(f"  This is what we'd add to bring token from 7,117 → ~{7117 + len(test_symbols)}")
    print()

    if len(test_symbols) < 100:
        print("  Not enough symbols collected; aborting")
        return 1

    # Step 2: Create session via REST
    print("  Step 2: Creating Tradier session via REST...")
    try:
        req = urllib.request.Request(
            'https://api.tradier.com/v1/markets/events/session',
            data=b'',
            headers=headers,
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            session_data = json.loads(r.read())
        session_id = session_data.get('stream', {}).get('sessionid')
        if not session_id:
            print(f"    ERROR: no session id in response: {session_data}")
            return 1
        print(f"    Session created: {session_id[:16]}...")
    except Exception as e:
        print(f"    ERROR creating session: {e}")
        return 1
    print()

    # Step 3: Open WS conn + subscribe
    print(f"  Step 3: Opening test WS conn + subscribing {len(test_symbols)} symbols...")
    print()

    # Track events on the test conn
    state = {
        'connected': False,
        'closed': False,
        'close_code': None,
        'close_reason': None,
        'messages_received': 0,
        'errors': [],
        'last_msg_ts': 0,
        'subscribe_resp': None,
    }
    state_lock = threading.Lock()

    try:
        from websocket import WebSocketApp
    except ImportError:
        print("    ERROR: websocket-client not installed")
        return 1

    def on_open(ws):
        with state_lock:
            state['connected'] = True
        print(f"    [TEST-WS] ✅ Connected")
        # Send SUBS payload
        payload = {
            'symbols':    test_symbols,
            'sessionid':  session_id,
            'linebreak':  True,
            'filter':     ['trade', 'timesale'],
        }
        try:
            ws.send(json.dumps(payload))
            print(f"    [TEST-WS] 📡 Sent SUBS for {len(test_symbols)} symbols")
        except Exception as e:
            with state_lock:
                state['errors'].append(f'SUBS send: {e}')

    def on_message(ws, msg):
        with state_lock:
            state['messages_received'] += 1
            state['last_msg_ts'] = time.time()
            # First few messages — capture for analysis
            if state['messages_received'] <= 3:
                state['errors'].append(f'msg#{state["messages_received"]}: {msg[:200]}')

    def on_error(ws, err):
        with state_lock:
            state['errors'].append(f'WS error: {err}')

    def on_close(ws, code, reason):
        with state_lock:
            state['closed'] = True
            state['close_code'] = code
            state['close_reason'] = reason
        print(f"    [TEST-WS] ⚠️  CLOSED code={code} reason={reason!r}")

    ws_app = WebSocketApp(
        'wss://ws.tradier.com/v1/markets/events',
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    def run_ws():
        try:
            ws_app.run_forever(ping_interval=0)
        except Exception as e:
            with state_lock:
                state['errors'].append(f'run_forever: {e}')

    ws_thread = threading.Thread(target=run_ws, daemon=True)
    ws_thread.start()

    # Wait 60 sec
    print("    Watching for 60 sec...")
    for i in range(12):
        time.sleep(5)
        with state_lock:
            connected = state['connected']
            closed = state['closed']
            msgs = state['messages_received']
            close_code = state['close_code']
            close_reason = state['close_reason']
        elapsed = (i + 1) * 5
        flag = "✓" if (connected and not closed) else "❌"
        msg = f"    T+{elapsed:>3}s  WS={flag}  msgs={msgs}"
        if closed:
            msg += f"  CLOSED code={close_code} reason={close_reason!r}"
        print(msg)
        if closed:
            break

    # Cleanup
    try:
        ws_app.close()
    except Exception:
        pass
    print()

    # Step 4: Final analysis
    print("=" * 78)
    print(" RESULTS")
    print("=" * 78)
    print()
    with state_lock:
        connected = state['connected']
        closed = state['closed']
        msgs = state['messages_received']
        close_code = state['close_code']
        close_reason = state['close_reason']
        errors = list(state['errors'])

    print(f"  Test conn lifetime:    {'CONNECTED' if (connected and not closed) else 'CLOSED'}")
    if closed:
        print(f"  Close code:            {close_code}")
        print(f"  Close reason:          {close_reason!r}")
    print(f"  Messages received:     {msgs}")
    print(f"  Errors recorded:       {len(errors)}")
    if errors:
        print(f"  Sample errors/messages:")
        for e in errors[:5]:
            print(f"    {e[:150]}")

    print()
    print(f"  Token-level total during test: {7117 + len(test_symbols)} symbols")
    print(f"    7,117 production conns + {len(test_symbols)} test conn")

    print()
    if connected and not closed and msgs > 0:
        print(f"  ✅ ✅  PROVEN: Tradier accepted {7117 + len(test_symbols)} symbols on token")
        print(f"        Capacity for adding {len(test_symbols)} to Conn-B is real.")
        return 0
    elif closed and close_code == 1000:
        print(f"  ⚠ Tradier closed test conn with code=1000 (Normal Closure)")
        print(f"     This is the cap-rejection signature seen in past flap.")
        print(f"     Token-level cap is below {7117 + len(test_symbols)}.")
        return 1
    elif closed:
        print(f"  ⚠ Test conn closed unexpectedly (code={close_code})")
        return 1
    else:
        print(f"  ⚠ Test conn never connected — auth or network issue")
        return 1


if __name__ == '__main__':
    sys.exit(main())
