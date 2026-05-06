#!/usr/bin/env python3
"""
3-way comparison of 0DTE QQQ ATM Δ:
  1. Schwab REST (their BSM model)
  2. Tradier ORATS REST (their smoothed surface)
  3. Our pure-Python BSM solver

Hits Schwab REST directly via saved tokens (no server.py import → no gevent collision).
"""
import json
import os
import sys
import time
import math
import urllib.request

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from connectors.bsm_solver import bsm_greeks, solve_iv, compute_greeks_from_market

# ── Schwab REST direct (no server.py import) ─────────────────────────────
TOKEN_FILE = os.path.join(project_root, 'connectors', '.schwab_tokens.json')


def schwab_load_tokens():
    with open(TOKEN_FILE) as f:
        return json.load(f)


def schwab_get(path, params=None):
    tokens = schwab_load_tokens()
    access = tokens.get('access_token', '')
    qs = ''
    if params:
        from urllib.parse import urlencode
        qs = '?' + urlencode(params)
    req = urllib.request.Request(
        f'https://api.schwabapi.com{path}{qs}',
        headers={'Authorization': f'Bearer {access}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── Tradier REST ─────────────────────────────────────────────────────────
def tradier_chain(exp):
    cfg = json.load(open(os.path.join(project_root, 'config.json')))
    token = cfg.get('options_api_key', '')
    req = urllib.request.Request(
        f'https://api.tradier.com/v1/markets/options/chains'
        f'?symbol=QQQ&expiration={exp}&greeks=true',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    return (d.get('options') or {}).get('option') or []


def hours_to_close():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now()
    minutes_now = now.hour * 60 + now.minute
    minutes_close = 16 * 60
    if minutes_now < (9 * 60 + 30):
        return 6.5
    if minutes_now >= minutes_close:
        return 0.0
    return (minutes_close - minutes_now) / 60.0


def main():
    print('═' * 90)
    print(' 3-Way Δ Comparison @ 0DTE: Schwab vs Tradier ORATS vs Our BSM')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 90)

    # Spot
    quote = schwab_get('/marketdata/v1/quotes', {'symbols': 'QQQ', 'fields': 'quote'})
    spot = float(quote.get('QQQ', {}).get('quote', {}).get('lastPrice', 0))
    if spot <= 0:
        spot = float(quote.get('QQQ', {}).get('quote', {}).get('mark', 0))
    print(f'Schwab QQQ spot:  {spot:.2f}')

    # Risk-free rate (constant approximation)
    r = 0.044
    q = 0.005
    print(f'Risk-free:        {r*100:.1f}%')
    print(f'Dividend yield:   {q*100:.1f}%')

    # T for 0DTE
    hrs = max(0.5, hours_to_close())
    T = hrs / (365.0 * 24.0)
    print(f'Hours to close:   {hrs:.2f}')
    print(f'T (years):        {T:.7f}')
    print()

    # Today is 0DTE
    from datetime import date
    today = date.today().isoformat()

    # ── Pull Schwab REST chain ─────────────────────────────────────────
    print(f'Fetching Schwab REST chain for {today}...')
    sw_chain_data = schwab_get('/marketdata/v1/chains', {
        'symbol': 'QQQ',
        'fromDate': today,
        'toDate': today,
        'strikeCount': 50,
        'contractType': 'ALL',
    })
    sw_contracts = []
    for typ in ('callExpDateMap', 'putExpDateMap'):
        mp = sw_chain_data.get(typ, {})
        for date_key, strikes in mp.items():
            for strike_str, lst in strikes.items():
                for c in (lst or []):
                    sw_contracts.append({
                        'strike': float(strike_str),
                        'side': 'C' if typ.startswith('call') else 'P',
                        'symbol': c.get('symbol', ''),
                        'bid': c.get('bid', 0),
                        'ask': c.get('ask', 0),
                        'last': c.get('last', 0),
                        'mark': c.get('mark', 0),
                        'delta': c.get('delta', 0),
                        'gamma': c.get('gamma', 0),
                        'iv': c.get('volatility', 0),
                        'oi': c.get('openInterest', 0),
                        'volume': c.get('totalVolume', 0),
                    })
    print(f'  Schwab returned {len(sw_contracts)} 0DTE contracts')

    # ── Pull Tradier REST chain ───────────────────────────────────────
    print(f'Fetching Tradier REST chain for {today}...')
    td_chain = tradier_chain(today)
    td_by_key = {}
    for c in td_chain:
        K = float(c.get('strike', 0))
        side = 'C' if c.get('option_type') == 'call' else 'P'
        td_by_key[(K, side)] = c
    print(f'  Tradier returned {len(td_chain)} 0DTE contracts')
    print()

    # ── Compare ATM ±$10 ─────────────────────────────────────────────
    atm = round(spot)
    target_strikes = [atm - 10, atm - 5, atm, atm + 5, atm + 10]

    print('═' * 90)
    print(f' ATM ±$10 0DTE comparison (spot={spot:.2f})')
    print('═' * 90)
    print(f'{"K":<5s} {"side":<4s} {"mkt":>6s}   '
          f'{"Schwab Δ":>10s} {"Tradier Δ":>10s} {"Our BSM Δ":>11s}   '
          f'{"sw_iv":>6s} {"td_iv":>6s} {"our_iv":>7s}')
    print('-' * 90)

    for K in target_strikes:
        for side in ('C', 'P'):
            sw = next((c for c in sw_contracts if c['strike'] == K and c['side'] == side), None)
            td = td_by_key.get((float(K), side))
            if not sw or not td:
                continue

            mkt = (float(sw['bid']) + float(sw['ask'])) / 2.0
            if mkt <= 0:
                continue

            sw_delta = float(sw['delta'] or 0)
            sw_iv = float(sw['iv'] or 0) / 100.0  # Schwab uses percent

            td_greeks = td.get('greeks') or {}
            td_delta = float(td_greeks.get('delta') or 0)
            td_iv = float(td_greeks.get('mid_iv') or 0)  # Tradier uses decimal

            # Compute our BSM (with boundary handling)
            our_result = compute_greeks_from_market(spot, float(K), T, r, q, mkt, side)
            if our_result:
                our_delta = our_result['delta']
                our_iv = our_result['iv']
                our_src = our_result.get('source', 'bsm')
            else:
                our_iv = 0
                our_delta = 0
                our_src = 'fail'

            src_tag = '' if our_src == 'bsm' else f' [{our_src}]'
            print(f'{int(K):<5d} {side:<4s} {mkt:>6.2f}   '
                  f'{sw_delta:>+10.4f} {td_delta:>+10.4f} {our_delta:>+11.4f}{src_tag:<14s}   '
                  f'{sw_iv:>6.3f} {td_iv:>6.3f} {our_iv:>7.4f}')

    print()
    print('═' * 90)
    print(' DIFFERENCE ANALYSIS')
    print('═' * 90)
    diffs_sw_vs_bsm = []
    diffs_td_vs_bsm = []
    diffs_sw_vs_td = []

    for K in target_strikes:
        for side in ('C', 'P'):
            sw = next((c for c in sw_contracts if c['strike'] == K and c['side'] == side), None)
            td = td_by_key.get((float(K), side))
            if not sw or not td:
                continue
            mkt = (float(sw['bid']) + float(sw['ask'])) / 2.0
            if mkt <= 0:
                continue
            sw_delta = float(sw['delta'] or 0)
            td_delta = float((td.get('greeks') or {}).get('delta') or 0)
            our_result = compute_greeks_from_market(spot, float(K), T, r, q, mkt, side)
            our_delta = our_result['delta'] if our_result else 0
            diffs_sw_vs_bsm.append(sw_delta - our_delta)
            diffs_td_vs_bsm.append(td_delta - our_delta)
            diffs_sw_vs_td.append(sw_delta - td_delta)

    if diffs_sw_vs_bsm:
        import statistics
        print(f'\nSchwab Δ vs Our BSM Δ:')
        print(f'  mean abs diff:   {statistics.mean(abs(d) for d in diffs_sw_vs_bsm):.4f}')
        print(f'  median abs diff: {statistics.median(sorted(abs(d) for d in diffs_sw_vs_bsm)):.4f}')
        print(f'  max abs diff:    {max(abs(d) for d in diffs_sw_vs_bsm):.4f}')

        print(f'\nTradier ORATS Δ vs Our BSM Δ:')
        print(f'  mean abs diff:   {statistics.mean(abs(d) for d in diffs_td_vs_bsm):.4f}')
        print(f'  max abs diff:    {max(abs(d) for d in diffs_td_vs_bsm):.4f}')

        print(f'\nSchwab Δ vs Tradier ORATS Δ (vendor disagreement):')
        print(f'  mean abs diff:   {statistics.mean(abs(d) for d in diffs_sw_vs_td):.4f}')
        print(f'  max abs diff:    {max(abs(d) for d in diffs_sw_vs_td):.4f}')


if __name__ == '__main__':
    main()
