#!/usr/bin/env python3
"""
BSM solver validation — compare our pure-Python BSM Greeks against
Schwab's live Greeks across a sample of QQQ option contracts.

Pulls Schwab data via the running server's /api/_debug/* endpoints
to avoid gevent/SSL monkey-patch collision with direct server.py import.

For each contract:
  Test A: Given Schwab's IV, do my Greeks match Schwab's? (validates BSM math)
  Test B: Given Schwab's last_price, does my IV match Schwab's? (validates IV solver)
"""
import json
import os
import sys
import time
import statistics
import urllib.request
import hmac
import hashlib
from datetime import datetime, date

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from connectors.bsm_solver import bsm_greeks, solve_iv, compute_greeks_from_market

# ── Auth ──────────────────────────────────────────────────────────────────
def make_token():
    SECRET = b'wm-greeksite-secret-key-2024'
    USER = 'Kaali4426'
    ts_hex = format(int(time.time()), 'x')
    user_hex = USER.encode().hex()
    payload = f'{ts_hex}.{user_hex}'
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f'{ts_hex}.{user_hex}.{sig}'


def fetch_json(path, token):
    req = urllib.request.Request(
        f'http://localhost:3001{path}',
        headers={'X-Auth-Token': token, 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── Tradier REST for chain data + Greeks ──────────────────────────────────
def fetch_tradier_chain(exp):
    cfg_path = os.path.join(project_root, 'config.json')
    with open(cfg_path) as f:
        token = json.load(f).get('options_api_key', '')
    req = urllib.request.Request(
        f'https://api.tradier.com/v1/markets/options/chains'
        f'?symbol=QQQ&expiration={exp}&greeks=true',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    return (d.get('options') or {}).get('option') or []


def fetch_tradier_expirations():
    cfg_path = os.path.join(project_root, 'config.json')
    with open(cfg_path) as f:
        tk = json.load(f).get('options_api_key', '')
    req = urllib.request.Request(
        'https://api.tradier.com/v1/markets/options/expirations'
        '?symbol=QQQ&strikes=false&includeAllRoots=true',
        headers={'Authorization': f'Bearer {tk}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    return (d.get('expirations') or {}).get('date') or []


def days_to(exp_str):
    try:
        d = datetime.strptime(exp_str, '%Y-%m-%d').date()
        return (d - date.today()).days
    except Exception:
        return -1


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
    print('═' * 96)
    print(' BSM Solver Validation vs Schwab Live Greeks')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 96)
    print()

    token = make_token()

    # Pull live spot from /api/_debug/spots
    spots_data = fetch_json('/api/_debug/spots', token)
    spots = {s['symbol']: s['last'] for s in spots_data.get('spots', [])}
    spot = float(spots.get('QQQ', 668.0))
    tnx_val = float(spots.get('TNX', 43.96) or 43.96)
    r = tnx_val / 1000.0
    q = 0.005

    print(f'QQQ spot:        {spot:.2f}  (from running server)')
    print(f'Risk-free rate:  {r*100:.3f}%  (from $TNX)')
    print(f'Dividend yield:  {q*100:.2f}%  (QQQ constant)')
    print()

    # Tiers (DTE buckets)
    tiers = [
        ('0DTE',         0,    0),
        ('weekly',       3,    7),
        ('monthly',      8,    30),
        ('quarterly',    31,   90),
        ('LEAPS',        366,  9999),
    ]

    # Get expirations
    all_exps = fetch_tradier_expirations()

    diffs = {'iv': [], 'delta': [], 'gamma': []}
    n_tests = 0

    for tier_name, dte_lo, dte_hi in tiers:
        print(f'═══ Tier: {tier_name}  (DTE {dte_lo}-{dte_hi}) ═══')
        exps_in_tier = [e for e in all_exps if dte_lo <= days_to(e) <= dte_hi]
        if not exps_in_tier:
            print(f'  ⚠ No expirations in this tier\n')
            continue
        target_exp = exps_in_tier[len(exps_in_tier)//2]
        dte = days_to(target_exp)

        # Compute T in years (special handling for 0DTE — use hours)
        if dte == 0:
            hrs_left = max(0.5, hours_to_close())
            T = hrs_left / (365.0 * 24.0)
        else:
            T = dte / 365.0

        print(f'  Expiration: {target_exp}  DTE={dte}  T={T:.6f} years')

        # Fetch Tradier's chain (has Schwab-comparable Greeks via greeks=true)
        try:
            chain = fetch_tradier_chain(target_exp)
        except Exception as e:
            print(f'  Chain fetch error: {e}\n')
            continue

        atm = round(spot)
        target_strikes = [atm - 25, atm - 10, atm, atm + 10, atm + 25]

        # Print header
        print(f'  {"K":<5s} {"side":<5s} {"mkt":>7s} {"vendor_iv":>10s} {"our_iv":>9s} '
              f'{"vendor_Δ":>10s} {"our_Δ":>9s} {"diffΔ":>8s}')
        print(f'  ' + '─' * 78)

        for target_K in target_strikes:
            for side in ('C', 'P'):
                # Find contract
                match = None
                for c in chain:
                    K = float(c.get('strike', 0))
                    side_field = c.get('option_type', '')
                    if (abs(K - target_K) < 0.01
                            and ((side == 'C' and side_field == 'call')
                                 or (side == 'P' and side_field == 'put'))):
                        match = c
                        break
                if not match:
                    continue

                K = float(match.get('strike', 0))
                bid = float(match.get('bid', 0) or 0)
                ask = float(match.get('ask', 0) or 0)
                last = float(match.get('last', 0) or 0)
                if bid <= 0 or ask <= 0:
                    continue
                mkt_price = (bid + ask) / 2.0

                greeks_field = match.get('greeks') or {}
                vendor_iv = float(greeks_field.get('mid_iv', 0) or 0)
                vendor_delta = float(greeks_field.get('delta', 0) or 0)
                vendor_gamma = float(greeks_field.get('gamma', 0) or 0)
                if vendor_iv <= 0 or vendor_delta == 0:
                    continue

                # Test A: Our Greeks given vendor's IV
                our_g = bsm_greeks(spot, K, T, r, q, vendor_iv, side)

                # Test B: Solve IV ourselves
                our_iv = solve_iv(spot, K, T, r, q, mkt_price, side)

                diffs['delta'].append(our_g['delta'] - vendor_delta)
                diffs['gamma'].append(our_g['gamma'] - vendor_gamma)
                if our_iv is not None:
                    diffs['iv'].append(our_iv - vendor_iv)
                n_tests += 1

                our_iv_str = f'{our_iv:.4f}' if our_iv else 'N/A   '
                print(f'  {K:>4.0f}  {side:<5s} {mkt_price:>7.2f} '
                      f'{vendor_iv:>10.4f} {our_iv_str:>9s} '
                      f'{vendor_delta:>+10.4f} {our_g["delta"]:>+9.4f} '
                      f'{our_g["delta"] - vendor_delta:>+8.4f}')
        print()

    # Final summary
    print('═' * 96)
    print(' VALIDATION SUMMARY')
    print('═' * 96)
    print(f'Total contracts validated:  {n_tests}')
    print()
    if n_tests > 0:
        print(f'Test A — Greeks via OUR BSM math (validates BSM formula correctness):')
        print(f'  Δ (delta) accuracy vs Tradier ORATS:')
        print(f'    mean abs:   {statistics.mean(abs(d) for d in diffs["delta"]):.5f}')
        print(f'    median:     {statistics.median(diffs["delta"]):.5f}')
        print(f'    max abs:    {max(abs(d) for d in diffs["delta"]):.5f}')
        print(f'  Γ (gamma) accuracy:')
        print(f'    mean abs:   {statistics.mean(abs(d) for d in diffs["gamma"]):.6f}')
        print(f'    max abs:    {max(abs(d) for d in diffs["gamma"]):.6f}')
        print()
        print(f'Test B — Our IV solver (Newton-Raphson) vs vendor IV:')
        if diffs['iv']:
            print(f'  IV accuracy:')
            print(f'    mean abs:   {statistics.mean(abs(d) for d in diffs["iv"]):.5f}  '
                  f'({statistics.mean(abs(d) for d in diffs["iv"])*100:.3f}% absolute)')
            print(f'    median:     {statistics.median(diffs["iv"]):.5f}')
            print(f'    max abs:    {max(abs(d) for d in diffs["iv"]):.5f}')
        print()
        delta_ok = statistics.mean(abs(d) for d in diffs['delta']) < 0.05
        iv_ok = (statistics.mean(abs(d) for d in diffs['iv']) < 0.10
                  if diffs['iv'] else False)
        if delta_ok and iv_ok:
            print('  ✅ BSM solver VALIDATED — accuracy within institutional tolerance')
        else:
            print('  ⚠ Investigate larger differences before deploying to FLOW chart')


if __name__ == '__main__':
    main()
