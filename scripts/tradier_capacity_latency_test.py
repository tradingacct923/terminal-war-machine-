#!/usr/bin/env python3
"""
Tradier WS Capacity + Latency Probe (standalone)

Two questions to answer empirically:

  1. What is the ACTUAL max symbol limit per Tradier WS connection?
     (Earlier we know 1,381 ✓ and 2,793 ❌ — find the precise threshold)

  2. Are the events we receive truly LIVE (no delay)?
     Measures: latency = (now - event_timestamp) per print
"""
import json
import os
import statistics
import sys
import threading
import time
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


# ── Get high-volume symbols to subscribe ─────────────────────────────────
# Use known-active equities + Mag-8 for guaranteed event flow during RTH

def fetch_qqq_chain():
    """Get QQQ option chain for the next 4 expirations."""
    req = urllib.request.Request(
        'https://api.tradier.com/v1/markets/options/expirations'
        '?symbol=QQQ&strikes=false&includeAllRoots=true',
        headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    exps = (d.get('expirations') or {}).get('date') or []
    if not exps:
        return []
    syms = []
    for exp in exps[:4]:
        chain_req = urllib.request.Request(
            f'https://api.tradier.com/v1/markets/options/chains?symbol=QQQ&expiration={exp}',
            headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'}
        )
        with urllib.request.urlopen(chain_req, timeout=10) as r:
            d = json.loads(r.read())
        opts = (d.get('options') or {}).get('option') or []
        for o in opts:
            sym = o.get('symbol', '')
            if sym:
                syms.append(sym)
    return syms


# ── Test runner ────────────────────────────────────────────────────────────

class Probe:
    def __init__(self, name, n_symbols):
        self.name = name
        self.n_symbols = n_symbols
        self.session_id = None
        self.ws = None
        self.events = 0
        self.connect_time = None
        self.disconnect_time = None
        self.disconnect_reason = None
        self.latencies = []  # per-event latency in ms
        self.first_event_time = None

    def request_session(self):
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
        self.session_id = d.get('stream', {}).get('sessionid')
        return self.session_id

    def make_callbacks(self, symbols):
        def on_open(ws):
            self.connect_time = time.time()
            print(f'  [{self.name}] WS connected — sending subscribe of {len(symbols)} symbols')
            payload = {
                'symbols': symbols,
                'sessionid': self.session_id,
                'linebreak': True,
                'filter': ['timesale', 'trade', 'quote', 'tradex'],
            }
            try:
                ws.send(json.dumps(payload))
            except Exception as e:
                print(f'  [{self.name}] subscribe send failed: {e}')

        def on_message(ws, raw):
            try:
                data = json.loads(raw)
                self.events += 1
                if self.first_event_time is None:
                    self.first_event_time = time.time()
                # Compute latency: data.timestamp_ms (server side) → now (client)
                ts_ms = data.get('timestamp_ms') or data.get('timestamp') or 0
                if isinstance(ts_ms, (int, float)) and ts_ms > 0:
                    # Some Tradier events use seconds, some ms — heuristic
                    if ts_ms < 1e12:  # seconds
                        ts_s = float(ts_ms)
                    else:  # ms
                        ts_s = ts_ms / 1000.0
                    lat_ms = (time.time() - ts_s) * 1000
                    if 0 < lat_ms < 60_000:  # sane bound
                        self.latencies.append(lat_ms)
            except Exception:
                pass

        def on_close(ws, code, msg):
            self.disconnect_time = time.time()
            self.disconnect_reason = f'code={code} msg={msg!r}'
            up = self.disconnect_time - (self.connect_time or self.disconnect_time)
            print(f'  [{self.name}] CLOSED uptime={up:.1f}s {self.disconnect_reason}')

        def on_error(ws, err):
            print(f'  [{self.name}] WS error: {err!r}')

        return on_open, on_message, on_close, on_error

    def run(self, symbols, duration_s=30):
        on_open, on_message, on_close, on_error = self.make_callbacks(symbols)
        self.ws = websocket.WebSocketApp(
            'wss://ws.tradier.com/v1/markets/events',
            on_open=on_open, on_message=on_message,
            on_close=on_close, on_error=on_error,
        )
        t = threading.Thread(
            target=lambda: self.ws.run_forever(ping_interval=30, ping_timeout=10),
            daemon=True
        )
        t.start()
        time.sleep(duration_s)
        try:
            self.ws.close()
        except Exception:
            pass


def latency_summary(probe):
    if not probe.latencies:
        return 'no latency samples'
    lat = probe.latencies
    return (
        f'samples={len(lat)} | '
        f'min={min(lat):.0f}ms p50={statistics.median(lat):.0f}ms '
        f'p95={statistics.quantiles(lat, n=20)[18]:.0f}ms '
        f'p99={statistics.quantiles(lat, n=100)[98]:.0f}ms '
        f'max={max(lat):.0f}ms'
    )


def main():
    print('═' * 70)
    print(' Tradier WS Capacity + Latency Probe')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 70)
    print()

    print('Fetching QQQ chain (4 expirations) for capacity test symbols...')
    qqq_symbols = fetch_qqq_chain()
    print(f'  Got {len(qqq_symbols)} QQQ option symbols')

    if len(qqq_symbols) < 1500:
        print('⚠ Need more symbols — pulling additional Mag-8 chains')
        for ticker in ('SPX', 'NVDA', 'AAPL', 'MSFT', 'TSLA', 'META'):
            req = urllib.request.Request(
                f'https://api.tradier.com/v1/markets/options/expirations'
                f'?symbol={ticker}&strikes=false',
                headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'}
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read())
                exps = (d.get('expirations') or {}).get('date') or []
                if not exps:
                    continue
                for exp in exps[:2]:
                    chain_req = urllib.request.Request(
                        f'https://api.tradier.com/v1/markets/options/chains'
                        f'?symbol={ticker}&expiration={exp}',
                        headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'}
                    )
                    with urllib.request.urlopen(chain_req, timeout=10) as r:
                        d = json.loads(r.read())
                    opts = (d.get('options') or {}).get('option') or []
                    for o in opts:
                        s = o.get('symbol', '')
                        if s and s not in qqq_symbols:
                            qqq_symbols.append(s)
                    if len(qqq_symbols) >= 4000:
                        break
                if len(qqq_symbols) >= 4000:
                    break
            except Exception as e:
                print(f'  [{ticker}] chain fetch error: {e}')

    print(f'  Total available test symbols: {len(qqq_symbols)}')
    print()

    # ── PHASE 1: PER-CONNECTION CAPACITY PROBE ────────────────────────────
    print('═' * 70)
    print(' PHASE 1 — Find max symbols per single WS connection')
    print('═' * 70)
    print()

    test_sizes = [1500, 2000, 2500, 3000, 4000]
    threshold = None
    for n in test_sizes:
        if n > len(qqq_symbols):
            print(f'  Skipping {n} (only have {len(qqq_symbols)} symbols available)')
            continue
        print(f'\nTesting {n} symbols on single WS:')
        probe = Probe(f'CAP-{n}', n)
        probe.request_session()
        probe.run(qqq_symbols[:n], duration_s=20)
        time.sleep(2)
        if probe.disconnect_time:
            up = probe.disconnect_time - (probe.connect_time or probe.disconnect_time)
            if up < 5:
                print(f'  ❌ {n} symbols: CLOSED in {up:.1f}s — Tradier rejected')
                if threshold is None:
                    threshold = n
                break
            else:
                print(f'  ✓ {n} symbols: stayed up {up:.1f}s, {probe.events} events')
        else:
            print(f'  ✓ {n} symbols: STAYED ALIVE for full 20s, {probe.events} events')
            print(f'    {latency_summary(probe)}')
    if threshold:
        print(f'\n  → Per-connection cap: between {test_sizes[test_sizes.index(threshold)-1] if threshold > test_sizes[0] else "unknown"} and {threshold}')
    else:
        print(f'\n  → Per-connection cap: ≥{n} (didn\'t hit ceiling in test)')

    # ── PHASE 2: LATENCY MEASUREMENT ──────────────────────────────────────
    print()
    print('═' * 70)
    print(' PHASE 2 — Latency probe with high-volume equity symbols')
    print('═' * 70)
    print()
    # Use equities for guaranteed continuous flow
    eq_symbols = ['QQQ', 'SPY', 'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO']
    probe = Probe('LATENCY', len(eq_symbols))
    probe.request_session()
    print(f'Subscribing to {len(eq_symbols)} high-volume equities, 30s probe...')
    probe.run(eq_symbols, duration_s=30)
    time.sleep(1)
    print()
    print(f'  Events received:  {probe.events}')
    print(f'  Event rate:       {probe.events/30:.1f}/sec')
    print(f'  Latency:          {latency_summary(probe)}')
    if probe.latencies:
        median = statistics.median(probe.latencies)
        if median < 500:
            print(f'  ✓ LIVE STREAMING — median latency {median:.0f}ms < 500ms')
        elif median < 5000:
            print(f'  ⚠ Slightly delayed — median {median:.0f}ms (acceptable but not zero)')
        else:
            print(f'  ❌ Significant delay — median {median:.0f}ms ({median/1000:.1f} sec)')

    print()
    print('═' * 70)
    print(' SUMMARY')
    print('═' * 70)
    if threshold:
        print(f'  Per-WS symbol cap:    ~{threshold-500} (failed at {threshold})')
    else:
        print(f'  Per-WS symbol cap:    Tested up to {test_sizes[-1] if not threshold else threshold} successfully')
    if probe.latencies:
        print(f'  Median latency:       {statistics.median(probe.latencies):.0f}ms')
        print(f'  P95 latency:          {statistics.quantiles(probe.latencies, n=20)[18]:.0f}ms')
        print(f'  Multi-session ceiling:  ~{(threshold or 3000) * 2} effective')


if __name__ == '__main__':
    main()
