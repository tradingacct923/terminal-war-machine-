#!/usr/bin/env python3
"""
QQQ X-Y Accuracy Grid — full 2D coverage check

Samples QQQ contracts across the 2D grid:
  Y-axis (expiration tier): 0DTE, weekly, monthly, quarterly, 6mo, LEAPS-2yr, LEAPS-3yr
  X-axis (strike): ATM, ATM+25, ATM+50, ATM-25, ATM-50

For each grid cell pick one contract, compare Schwab REST vs Tradier REST:
  - last (trade price)
  - bid/ask
  - delta
  - IV
  - OI
  - volume

Goal: prove that across the FULL 2D grid (every DTE × strike combination
the FLOW chart shows), Schwab and Tradier data agree.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

CONFIG_PATH = os.path.join(project_root, 'config.json')
with open(CONFIG_PATH) as f:
    cfg = json.load(f)
TRADIER_TOKEN = cfg.get('options_api_key', '')

from server import _schwab_chain_raw, _schwab_quote


def days_to(exp_str):
    try:
        d = datetime.strptime(exp_str, '%Y-%m-%d').date()
        return (d - date.today()).days
    except Exception:
        return -1


def tradier_chain(exp):
    req = urllib.request.Request(
        f'https://api.tradier.com/v1/markets/options/chains?symbol=QQQ&expiration={exp}&greeks=true',
        headers={'Authorization': f'Bearer {TRADIER_TOKEN}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    return (d.get('options') or {}).get('option') or []


def tradier_expirations():
    req = urllib.request.Request(
        'https://api.tradier.com/v1/markets/options/expirations'
        '?symbol=QQQ&strikes=false&includeAllRoots=true',
        headers={'Authorization': f'Bearer {TRADIER_TOKEN}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    return (d.get('expirations') or {}).get('date') or []


def main():
    print('═' * 96)
    print(' QQQ X-Y Accuracy Grid — Schwab vs Tradier across the FLOW chart 2D space')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 96)
    print()

    spot = _schwab_quote('QQQ')
    print(f'Schwab QQQ spot: {spot:.2f}')
    print()

    # ── Pick one expiration per Y-axis tier ─────────────────────────────
    exps = tradier_expirations()
    print(f'Tradier total expirations: {len(exps)}')

    target_dtes = [
        ('0DTE',         lambda d: d == 0),
        ('weekly (3-7)', lambda d: 3 <= d <= 7),
        ('monthly',      lambda d: 8 <= d <= 30),
        ('quarterly',    lambda d: 31 <= d <= 90),
        ('6mo',          lambda d: 91 <= d <= 180),
        ('1yr',          lambda d: 181 <= d <= 365),
        ('LEAPS 1-2yr',  lambda d: 366 <= d <= 730),
        ('LEAPS 2-3yr',  lambda d: d > 730),
    ]

    chosen_exps = {}
    for tier_name, predicate in target_dtes:
        candidates = [e for e in exps if predicate(days_to(e))]
        if candidates:
            # Pick the median DTE in tier (most representative)
            chosen_exps[tier_name] = candidates[len(candidates)//2]

    print(f'Target expirations per tier:')
    for tier, exp in chosen_exps.items():
        dte = days_to(exp)
        print(f'  {tier:<14s}  {exp}  (DTE={dte})')
    print()

    # ── Pick strike offsets ─────────────────────────────────────────────
    atm_strike = round(spot)
    strike_offsets = [-25, -10, 0, 10, 25]  # ATM-25, ATM-10, ATM, ATM+10, ATM+25
    target_strikes = [atm_strike + o for o in strike_offsets]

    print(f'Target strikes (ATM={atm_strike}):  {target_strikes}')
    print()

    # ── Build the comparison grid ──────────────────────────────────────
    grid_rows = []  # rows = expirations, cols = strikes

    print('═' * 96)
    print(' COMPARISON: Schwab REST (top) vs Tradier REST (bottom) per grid cell')
    print('═' * 96)

    for tier_name, exp in chosen_exps.items():
        print()
        print(f'═ {tier_name} ({exp}, DTE={days_to(exp)}) ═')
        # Pull both vendors for this expiration
        try:
            sw_chain, _ = _schwab_chain_raw('QQQ', exp)
        except Exception as e:
            print(f'  Schwab fetch error: {e}')
            continue
        try:
            td_chain = tradier_chain(exp)
        except Exception as e:
            print(f'  Tradier fetch error: {e}')
            continue

        # Index by (strike, side)
        sw_by_key = {}
        for c in sw_chain:
            k = (float(c.get('strike', 0)), c.get('option_type', '')[0].upper())
            sw_by_key[k] = c

        td_by_key = {}
        for c in td_chain:
            k = (float(c.get('strike', 0)), c.get('option_type', '')[0].upper())
            td_by_key[k] = c

        # Print header
        print(f'  {"STRIKE":<8s} {"SIDE":<4s} {"VENDOR":<7s}  '
              f'{"BID":>6s}  {"ASK":>6s}  {"LAST":>6s}  '
              f'{"Δ":>7s}  {"IV":>6s}  {"OI":>5s}  {"VOL":>5s}  {"MATCH":>6s}')
        print(f'  {"-"*78}')

        for strike in target_strikes:
            for side in ('C', 'P'):
                sw = sw_by_key.get((float(strike), side))
                td = td_by_key.get((float(strike), side))

                if sw:
                    sw_bid = float(sw.get('bid', 0) or 0)
                    sw_ask = float(sw.get('ask', 0) or 0)
                    sw_last = float(sw.get('last', 0) or 0)
                    sw_delta = float(sw.get('delta', 0) or 0)
                    sw_iv = float(sw.get('volatility', 0) or 0)
                    sw_oi = int(sw.get('open_interest', 0) or 0)
                    sw_vol = int(sw.get('volume', 0) or 0)
                    print(f'  {strike:<8.0f} {side:<4s} {"SCHWAB":<7s}  '
                          f'{sw_bid:>6.2f}  {sw_ask:>6.2f}  {sw_last:>6.2f}  '
                          f'{sw_delta:>+7.3f}  {sw_iv:>6.2f}  {sw_oi:>5d}  {sw_vol:>5d}')

                if td:
                    td_bid = float(td.get('bid', 0) or 0)
                    td_ask = float(td.get('ask', 0) or 0)
                    td_last = float(td.get('last', 0) or 0)
                    greeks = td.get('greeks') or {}
                    td_delta = float(greeks.get('delta', 0) or 0)
                    td_iv = float(greeks.get('mid_iv', 0) or 0) * 100
                    td_oi = int(td.get('open_interest', 0) or 0)
                    td_vol = int(td.get('volume', 0) or 0)

                    last_match = 'OK' if (sw and abs(sw_last - td_last) < 0.05) else 'DIFF'
                    print(f'  {"":<8s} {"":<4s} {"TRADIER":<7s}  '
                          f'{td_bid:>6.2f}  {td_ask:>6.2f}  {td_last:>6.2f}  '
                          f'{td_delta:>+7.3f}  {td_iv:>6.2f}  {td_oi:>5d}  {td_vol:>5d}'
                          f'   {last_match:>4s}')
                elif sw:
                    print(f'  {"":<8s} {"":<4s} {"TRADIER":<7s}  ❌ contract not in Tradier chain')
                if not (sw or td):
                    pass  # both missing — skip


if __name__ == '__main__':
    main()
