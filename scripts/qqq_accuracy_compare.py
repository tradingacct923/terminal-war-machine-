#!/usr/bin/env python3
"""
QQQ Schwab vs Tradier Accuracy Probe

Pulls the SAME QQQ option contracts from both Schwab REST and Tradier REST
side-by-side, compares bid/ask/last/IV/Delta/OI to verify accuracy.

If the two vendors agree within tolerance → both accurate.
If they disagree → one is delayed or has wrong data.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

CONFIG_PATH = os.path.join(project_root, 'config.json')
with open(CONFIG_PATH) as f:
    cfg = json.load(f)
TRADIER_TOKEN = cfg.get('options_api_key', '')

from server import _schwab_chain_raw, _schwab_quote


def tradier_chain(exp):
    req = urllib.request.Request(
        f'https://api.tradier.com/v1/markets/options/chains?symbol=QQQ&expiration={exp}&greeks=true',
        headers={'Authorization': f'Bearer {TRADIER_TOKEN}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    return (d.get('options') or {}).get('option') or []


def main():
    print('═' * 90)
    print(' QQQ Schwab vs Tradier Accuracy Probe')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 90)
    print()

    spot = _schwab_quote('QQQ')
    print(f'Schwab QQQ spot: {spot:.2f}')
    print()

    # Pick a near-term active expiration (1-2 weeks out)
    from datetime import timedelta
    today = date.today()
    target_exp = (today + timedelta(days=7)).strftime('%Y-%m-%d')
    # Find nearest actual expiration
    print(f'Target expiration ~7 DTE: {target_exp}')

    # Pull both vendors
    print(f'Fetching from Schwab REST...')
    t1 = time.time()
    schwab_chain, schwab_spot = _schwab_chain_raw('QQQ', target_exp)
    schwab_fetch_ms = int((time.time() - t1) * 1000)
    print(f'  Schwab returned {len(schwab_chain)} contracts in {schwab_fetch_ms}ms')

    print(f'Fetching from Tradier REST...')
    t1 = time.time()
    tradier_chain_raw = tradier_chain(target_exp)
    tradier_fetch_ms = int((time.time() - t1) * 1000)
    print(f'  Tradier returned {len(tradier_chain_raw)} contracts in {tradier_fetch_ms}ms')
    print()

    # Index Tradier by OCC symbol (with spaces stripped)
    tradier_by_sym = {}
    for o in tradier_chain_raw:
        sym = o.get('symbol', '').replace(' ', '')
        if sym:
            tradier_by_sym[sym] = o

    # ── Compare ATM strikes (most actively traded, both vendors' data should be fresh) ─
    atm_strike_round = round(spot)
    near_atm = [c for c in schwab_chain
                 if abs(float(c.get('strike', 0)) - atm_strike_round) <= 5
                 and c.get('option_type') in ('call', 'put')]
    near_atm.sort(key=lambda c: (float(c.get('strike', 0)), c.get('option_type', '')))

    print('═' * 90)
    print(f' ATM ±$5 COMPARISON (most active contracts)')
    print('═' * 90)
    print(f'{"OCC SYMBOL":<22s}  {"K/SD":<8s}  {"VENDOR":<7s}  '
          f'{"BID":>6s}  {"ASK":>6s}  {"LAST":>6s}  {"Δ":>7s}  '
          f'{"IV":>7s}  {"OI":>5s}  {"VOL":>5s}')
    print('-' * 90)

    discrepancies = []
    matches = 0
    seen = 0

    for sw in near_atm[:20]:  # Limit to ~20 contracts for readability
        sw_sym = sw.get('symbol', '').strip().replace(' ', '')
        sw_strike = float(sw.get('strike', 0))
        sw_side = 'C' if sw.get('option_type') == 'call' else 'P'
        sw_bid = sw.get('bid', 0)
        sw_ask = sw.get('ask', 0)
        sw_last = sw.get('last', 0)
        sw_delta = sw.get('delta', 0)
        sw_iv = sw.get('volatility', 0)  # Schwab uses 'volatility' as percent (decimal × 100)
        sw_oi = sw.get('open_interest', 0)
        sw_vol = sw.get('volume', 0)

        td = tradier_by_sym.get(sw_sym)
        seen += 1

        # Print Schwab row
        print(f'{sw_sym:<22s}  {sw_strike:>5.0f}{sw_side:<2s}  '
              f'{"SCHWAB":<7s}  '
              f'{sw_bid:>6.2f}  {sw_ask:>6.2f}  {sw_last:>6.2f}  '
              f'{(sw_delta or 0):>+7.3f}  {(sw_iv or 0):>7.2f}  '
              f'{int(sw_oi or 0):>5d}  {int(sw_vol or 0):>5d}')

        if td:
            td_bid = float(td.get('bid', 0) or 0)
            td_ask = float(td.get('ask', 0) or 0)
            td_last = float(td.get('last', 0) or 0)
            greeks = td.get('greeks') or {}
            td_delta = float(greeks.get('delta', 0) or 0)
            # Tradier IV is decimal (0.25 = 25%); Schwab is percent (25.0 = 25%)
            td_iv = float(greeks.get('mid_iv', 0) or 0) * 100
            td_oi = int(td.get('open_interest', 0) or 0)
            td_vol = int(td.get('volume', 0) or 0)

            print(f'{"":<22s}  {"":<8s}  '
                  f'{"TRADIER":<7s}  '
                  f'{td_bid:>6.2f}  {td_ask:>6.2f}  {td_last:>6.2f}  '
                  f'{td_delta:>+7.3f}  {td_iv:>7.2f}  '
                  f'{td_oi:>5d}  {td_vol:>5d}')

            # Compute discrepancies
            tol = max(0.01, 0.01 * sw_last)  # 1% or $0.01 minimum
            issues = []
            if abs(sw_bid - td_bid) > tol:
                issues.append(f'bid Δ={sw_bid - td_bid:+.3f}')
            if abs(sw_ask - td_ask) > tol:
                issues.append(f'ask Δ={sw_ask - td_ask:+.3f}')
            if abs(sw_last - td_last) > tol:
                issues.append(f'last Δ={sw_last - td_last:+.3f}')
            if abs(sw_delta - td_delta) > 0.05:
                issues.append(f'Δ_delta={sw_delta - td_delta:+.3f}')
            if abs(sw_iv - td_iv) > 5.0:
                issues.append(f'IV_diff={sw_iv - td_iv:+.1f}')
            if issues:
                print(f'{"  ⚠ DISCREPANCY:":<22s}  {"":<8s}  {"":<7s}  {", ".join(issues)}')
                discrepancies.append((sw_sym, issues))
            else:
                matches += 1
        else:
            print(f'{"":<22s}  {"":<8s}  {"TRADIER":<7s}  ❌ contract not in Tradier chain')
        print()

    print('═' * 90)
    print(' SUMMARY')
    print('═' * 90)
    print(f'  Contracts compared:   {seen}')
    print(f'  Match (within tol):   {matches}')
    print(f'  Discrepancies:        {len(discrepancies)}')
    if matches >= seen * 0.9:
        print(f'  ✅ Schwab and Tradier ACCURATE — agree on {100*matches/seen:.0f}% of contracts')
    elif matches >= seen * 0.7:
        print(f'  ⚠ Mostly accurate — {100*matches/seen:.0f}% agreement')
    else:
        print(f'  ❌ Significant disagreement — {100*matches/seen:.0f}% match only')

    print()
    if discrepancies:
        print('  Discrepancy detail:')
        for sym, issues in discrepancies[:5]:
            print(f'    {sym}: {", ".join(issues)}')


if __name__ == '__main__':
    main()
