#!/usr/bin/env python3
"""
Tradier Conn-B push test — empirical cap probe.

Plan:
  1. Pull existing _tradier_streamer_b (currently at 1,366 syms)
  2. Pull full Mag-8 chains via Tradier REST (ATM ±$X across all expirations)
  3. Build target ~1,688 ADDITIONAL OCC symbols (bringing total to ~3,054)
  4. Call _tradier_streamer_b.subscribe(extra_symbols)
  5. Wait 30 sec for Tradier to process / reject
  6. Report:
     - WS connection state (still ESTABLISHED?)
     - Reconnect events triggered
     - Recv-Q backlog growth
     - Any error log entries
  7. Roll back if rejected (unsubscribe the extras)

Risk: subscribes survive past the test until restart. If Tradier accepts
without complaint, the subs persist (and that's actually what we want).
If rejected, we unsubscribe immediately.

Run: source venv/bin/activate && python scripts/tradier_conn_b_push_test.py
"""
import os
import sys
import time
import json
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    print("=" * 78)
    print(" TRADIER CONN-B PUSH TEST — empirical cap probe")
    print("=" * 78)
    print()

    # 1. Get reference to streamer_b via the running server's module state
    from background_engine import schwab_bridge
    streamer_b = schwab_bridge._tradier_streamer_b
    if streamer_b is None:
        print("  ERROR: _tradier_streamer_b is None")
        return 1
    print(f"  Got streamer_b reference, current state:")
    print(f"    connected: {streamer_b._ws_connected.is_set()}")
    print(f"    symbols subscribed: {len(streamer_b.symbols)}")
    print()

    # 2. Read Tradier token
    cfg_path = os.path.join(ROOT, 'config.json')
    with open(cfg_path) as f:
        cfg = json.load(f)
    token = cfg.get('tradier_token', '')
    if not token:
        print("  ERROR: No tradier_token in config.json")
        return 1

    # 3. Pull full Mag-8 chains via Tradier REST
    MAG8 = ['NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO']
    target_per_ticker = 220   # 8 × 220 = 1,760 — close to our 1,688 target
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    extra_symbols = []
    for ticker in MAG8:
        # Get spot
        try:
            req = urllib.request.Request(
                f'https://api.tradier.com/v1/markets/quotes?symbols={ticker}',
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                qd = json.loads(r.read())
            spot = float(qd['quotes']['quote']['last'] or 0)
            if spot <= 0:
                print(f"  {ticker}: no spot, skipping")
                continue

            # Get expirations
            req = urllib.request.Request(
                f'https://api.tradier.com/v1/markets/options/expirations?symbol={ticker}&includeAllRoots=true',
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                exp_data = json.loads(r.read())
            exps = (exp_data.get('expirations') or {}).get('date') or []
            if isinstance(exps, str):
                exps = [exps]
            exps = exps[:6]   # 6 nearest expirations

            # For each expiration, fetch chain and pick ATM ±15% strikes
            radius = max(2.0, spot * 0.15)   # wider than current 5%
            ticker_syms = []
            for exp in exps:
                try:
                    req = urllib.request.Request(
                        f'https://api.tradier.com/v1/markets/options/chains?symbol={ticker}&expiration={exp}&greeks=false',
                        headers=headers,
                    )
                    with urllib.request.urlopen(req, timeout=8) as r:
                        cd = json.loads(r.read())
                    options = (cd.get('options') or {}).get('option') or []
                    if isinstance(options, dict):
                        options = [options]
                    for opt in options:
                        K = float(opt.get('strike') or 0)
                        if abs(K - spot) > radius:
                            continue
                        sym = opt.get('symbol') or ''   # already in Tradier OCC format
                        if sym:
                            ticker_syms.append(sym)
                except Exception as e:
                    print(f"  {ticker} {exp} failed: {e}")
                    continue
            # Cap per ticker
            ticker_syms = ticker_syms[:target_per_ticker]
            print(f"  {ticker:<6s}  spot=${spot:>7.2f}  collected {len(ticker_syms):>4d} OCC syms")
            extra_symbols.extend(ticker_syms)

        except Exception as e:
            print(f"  {ticker}: ERROR {e}")
            continue

    # Filter out symbols that are ALREADY subscribed
    existing = set(streamer_b.symbols)
    new_only = [s for s in extra_symbols if s not in existing]
    print()
    print(f"  Total collected:  {len(extra_symbols)}")
    print(f"  Already subscribed: {len(extra_symbols) - len(new_only)}")
    print(f"  NEW symbols to add: {len(new_only)}")
    print()

    if not new_only:
        print("  No new symbols to add. Test cannot proceed.")
        return 0

    target_total = len(streamer_b.symbols) + len(new_only)
    print(f"  Conn-B will go: {len(streamer_b.symbols)} → {target_total}")
    print()

    # 4. Subscribe!
    pre_recvq = _measure_recvq()
    pre_reconns = _count_reconnects()

    print("  ▶️  Calling _tradier_streamer_b.subscribe() with new Mag-8 wings...")
    t_sub = time.time()
    try:
        streamer_b.subscribe(new_only)
    except Exception as e:
        print(f"  ❌ subscribe() raised: {e}")
        return 1
    print(f"  subscribe() returned in {(time.time()-t_sub)*1000:.0f}ms")
    print(f"  symbols on streamer now: {len(streamer_b.symbols)}")
    print()

    # 5. Watch for 30 seconds
    print("  ⏱  Watching for 30 sec — looking for: WS close, reconnect, error logs")
    monitor_start = time.time()
    drops = 0
    for i in range(6):   # 6 × 5s = 30s
        time.sleep(5)
        connected = streamer_b._ws_connected.is_set()
        recvq = _measure_recvq()
        reconns = _count_reconnects()
        delta_reconns = reconns - pre_reconns
        if not connected:
            drops += 1
        elapsed = time.time() - monitor_start
        flag = "✓" if connected else "❌"
        print(f"  T+{elapsed:>5.1f}s  WS={flag}  Recv-Q={recvq:>10d}KB  reconnects=+{delta_reconns}")

    # 6. Final state
    print()
    print("=" * 78)
    print(" RESULTS")
    print("=" * 78)
    final_reconns = _count_reconnects() - pre_reconns
    final_connected = streamer_b._ws_connected.is_set()
    final_symbol_count = len(streamer_b.symbols)
    print(f"  Subscribed (target):   {target_total}")
    print(f"  Subscribed (actual):   {final_symbol_count}")
    print(f"  WS still connected:    {final_connected}")
    print(f"  Reconnects triggered:  {final_reconns}")
    print(f"  Connection drops seen: {drops}/6 samples")
    print()

    if final_reconns == 0 and final_connected and drops == 0:
        print(f"  ✅ ✅  Test PASSED — Conn-B accepted +{len(new_only)} symbols ({target_total} total)")
        print(f"        No flap, no rejection. Mag-8 wings now flowing.")
        return 0
    else:
        print(f"  ⚠️ ROLLING BACK — Conn-B rejected the load")
        print(f"     Calling unsubscribe() on the {len(new_only)} added symbols...")
        try:
            streamer_b.unsubscribe(new_only)
            print(f"     Unsubscribe issued")
        except Exception as e:
            print(f"     Unsubscribe FAILED: {e}")
        return 1


def _measure_recvq():
    """Total Tradier WS Recv-Q in KB."""
    import subprocess
    try:
        out = subprocess.check_output(['netstat', '-an'], stderr=subprocess.DEVNULL).decode()
        total = 0
        for line in out.split('\n'):
            if '184.72.242.124' in line or '52.7.143.130' in line:
                parts = line.split()
                if len(parts) > 2 and parts[1].isdigit():
                    total += int(parts[1])
        return total // 1024
    except Exception:
        return -1


def _count_reconnects():
    try:
        with open('/tmp/server_restart.log') as f:
            return sum(1 for line in f if '🔁 Reconnect' in line)
    except Exception:
        return -1


if __name__ == '__main__':
    sys.exit(main())
