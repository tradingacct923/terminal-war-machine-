#!/usr/bin/env python3
"""
Verify Tradier WS delivers per-print TIMESALE for SPY/IWM/sector ETFs.

Opens a SEPARATE WS connection (not touching production) and subscribes
to ~50 SPY/IWM/XLK/XLE/XLF ATM OCC symbols. Watches for 'trade' or
'timesale' messages over 60 sec.

If we get prints → Tradier WS supports these tickers (Phase 17B Part 2 viable)
If silent → Tradier WS may not stream them (rollback was correct)
"""
import os
import sys
import json
import time
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    print("=" * 78)
    print(" TRADIER WS SPY/IWM/SECTOR-ETF PRINT TEST")
    print("=" * 78)

    # Token
    cfg_path = os.path.join(ROOT, 'config.json')
    with open(cfg_path) as f:
        cfg = json.load(f)
    token = cfg.get('options_api_key') or os.getenv('TRADIER_TOKEN', '')
    if not token:
        print("  ERROR: No token")
        return 1
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    # Get ATM strikes for each ticker — just front 1-2 expirations
    TARGETS = {
        'SPY': 723.0,
        'IWM': 278.0,
        'XLK': 162.0,
        'XLE': 59.0,
        'XLF': 52.0,
    }
    test_symbols = []
    for ticker, spot in TARGETS.items():
        # Get nearest 2 expirations
        try:
            req = urllib.request.Request(
                f'https://api.tradier.com/v1/markets/options/expirations?symbol={ticker}',
                headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                ed = json.loads(r.read())
            exps = (ed.get('expirations') or {}).get('date') or []
            if isinstance(exps, str):
                exps = [exps]
            exps = exps[:2]   # 2 nearest
            radius = max(2.0, spot * 0.03)  # ATM ±3%
            ticker_syms = []
            for exp in exps:
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
            ticker_syms = ticker_syms[:20]  # 20 per ticker = 100 total
            print(f"  {ticker}: {len(ticker_syms)} ATM ±3% syms")
            test_symbols.extend(ticker_syms)
        except Exception as e:
            print(f"  {ticker}: ERROR {e}")
    print()
    print(f"  Total test symbols: {len(test_symbols)}")
    print()

    # Create session
    print("  Creating session...")
    req = urllib.request.Request(
        'https://api.tradier.com/v1/markets/events/session',
        data=b'',
        headers=headers,
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        sd = json.loads(r.read())
    session_id = sd['stream']['sessionid']
    print(f"  Session: {session_id[:16]}...")
    print()

    # Open WS conn
    from websocket import WebSocketApp
    state = {
        'connected':       False,
        'closed':          False,
        'msgs_per_ticker': {},
        'sample_msgs':     [],
        'all_event_types': set(),
    }
    state_lock = threading.Lock()

    def on_open(ws):
        with state_lock:
            state['connected'] = True
        print(f"  [WS] ✅ Connected, sending SUBS for {len(test_symbols)} symbols")
        ws.send(json.dumps({
            'symbols':   test_symbols,
            'sessionid': session_id,
            'linebreak': True,
            'filter':    ['trade', 'timesale'],
        }))

    def on_message(ws, msg):
        with state_lock:
            for line in msg.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get('type', '?')
                state['all_event_types'].add(t)
                sym = d.get('symbol', '?')
                # Extract underlying root
                # SPY260501P00723000 → SPY
                # XLE260501C00059000 → XLE
                root = sym[:3] if len(sym) >= 3 else '?'
                if root in ('SPY', 'IWM', 'XLK', 'XLE', 'XLF'):
                    state['msgs_per_ticker'].setdefault(root, 0)
                    state['msgs_per_ticker'][root] += 1
                    if len(state['sample_msgs']) < 5:
                        state['sample_msgs'].append({
                            'type':  t,
                            'symbol': sym,
                            'price': d.get('price') or d.get('last'),
                            'size':  d.get('size'),
                            'exch':  d.get('exch'),
                        })

    def on_close(ws, code, reason):
        with state_lock:
            state['closed'] = True

    ws_app = WebSocketApp(
        'wss://ws.tradier.com/v1/markets/events',
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
    )

    def run_ws():
        ws_app.run_forever(ping_interval=0)

    threading.Thread(target=run_ws, daemon=True).start()

    # Watch for 60 sec
    print(f"  Watching for prints over 60 sec...")
    print()
    for i in range(6):
        time.sleep(10)
        with state_lock:
            connected = state['connected'] and not state['closed']
            ticker_msgs = dict(state['msgs_per_ticker'])
            event_types = set(state['all_event_types'])
        elapsed = (i + 1) * 10
        flag = '✓' if connected else '❌'
        print(f"  T+{elapsed:>3}s  WS={flag}  events={dict(ticker_msgs)}")

    try:
        ws_app.close()
    except Exception:
        pass
    print()

    # Final analysis
    print("=" * 78)
    print(" RESULTS")
    print("=" * 78)
    with state_lock:
        ticker_msgs = dict(state['msgs_per_ticker'])
        sample_msgs = list(state['sample_msgs'])
        event_types = set(state['all_event_types'])
    total_prints = sum(ticker_msgs.values())
    print(f"  Total trade/timesale events: {total_prints}")
    print(f"  Event types observed:        {sorted(event_types)}")
    print(f"  Per-ticker message counts:")
    for sym in ('SPY', 'IWM', 'XLK', 'XLE', 'XLF'):
        n = ticker_msgs.get(sym, 0)
        print(f"    {sym}: {n}")
    print()
    if sample_msgs:
        print(f"  Sample messages (first 5):")
        for m in sample_msgs[:5]:
            print(f"    {m}")
    print()
    if total_prints > 0:
        print(f"  ✅ Tradier WS DOES deliver per-print events for SPY/IWM/sector ETFs")
        print(f"     Phase 17B Part 2 restore is technically viable.")
        return 0
    else:
        print(f"  ⚠ Zero prints in 60 sec — Tradier WS may not stream these tickers,")
        print(f"     OR they're just very low-volume right now.")
        print(f"     Recommend: try with longer test window or higher-volume strikes.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
