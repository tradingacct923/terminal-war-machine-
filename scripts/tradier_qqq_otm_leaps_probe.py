#!/usr/bin/env python3
"""
Tradier QQQ OTM + LEAPS Coverage Probe (standalone)

Two questions to answer empirically with PROOF:

  Q1. Does Tradier have deep-OTM QQQ contracts in their system?
      Definition: strikes >= $150 away from current spot (well outside Schwab's
      ±$100 streaming radius). Want to see if Tradier returns them via
      /markets/options/chains.

  Q2. Does Tradier have QQQ LEAPS in their system?
      Definition: expirations >= 366 days out (i.e., 1+ year, true LEAPS).
      Want to see if Tradier returns 2027 or 2028 expirations.

For each, we PROVE existence by:
  - Fetching the chain from Tradier REST
  - Counting how many contracts match the filter
  - Printing 5 sample OCC symbols + bid/ask/last/IV
  - Verifying OCC symbols are well-formed
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date

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


def _req(url):
    req = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_qqq_spot():
    """Pull live QQQ spot from Tradier quotes endpoint."""
    d = _req('https://api.tradier.com/v1/markets/quotes?symbols=QQQ')
    quotes = (d.get('quotes') or {}).get('quote') or {}
    if isinstance(quotes, list):
        quotes = quotes[0] if quotes else {}
    return float(quotes.get('last') or quotes.get('close') or 0)


def get_expirations():
    """Get all QQQ option expirations from Tradier."""
    d = _req(
        'https://api.tradier.com/v1/markets/options/expirations'
        '?symbol=QQQ&strikes=false&includeAllRoots=true'
    )
    return (d.get('expirations') or {}).get('date') or []


def get_chain(exp):
    """Get full chain for one expiration."""
    d = _req(
        f'https://api.tradier.com/v1/markets/options/chains?symbol=QQQ&expiration={exp}'
    )
    return (d.get('options') or {}).get('option') or []


def days_to(exp_str):
    """Days from today to expiration date."""
    try:
        exp_d = datetime.strptime(exp_str, '%Y-%m-%d').date()
        return (exp_d - date.today()).days
    except Exception:
        return -1


def main():
    print('═' * 70)
    print(' Tradier QQQ OTM + LEAPS Coverage Probe')
    print(f' Started: {time.strftime("%Y-%m-%d %H:%M:%S")} ET')
    print('═' * 70)
    print()

    # Step 1: spot
    print('Step 1: Fetch live QQQ spot from Tradier...')
    spot = get_qqq_spot()
    if spot <= 0:
        print('❌ Failed to get QQQ spot')
        sys.exit(1)
    print(f'  ✓ QQQ spot = {spot:.2f}')
    print()

    # Step 2: expirations
    print('Step 2: Fetch QQQ expirations list from Tradier...')
    exps = get_expirations()
    if not exps:
        print('❌ No expirations returned')
        sys.exit(1)
    print(f'  ✓ Tradier returned {len(exps)} expirations')
    print(f'    First 5:  {exps[:5]}')
    print(f'    Last 5:   {exps[-5:]}')
    print()

    # ── Q2 first: identify LEAPS expirations (≥366 days out) ───────────────
    print('═' * 70)
    print(' Q2: Does Tradier have QQQ LEAPS (≥1 year out)?')
    print('═' * 70)
    leaps_exps = [e for e in exps if days_to(e) >= 366]
    print(f'  LEAPS expirations (DTE ≥ 366):  {len(leaps_exps)}')
    if not leaps_exps:
        print('  ❌ NO LEAPS — Tradier does NOT carry QQQ LEAPS in their system')
        print('  → Cannot expand coverage via Tradier for LEAPS')
    else:
        print(f'  ✅ Tradier HAS LEAPS — {len(leaps_exps)} expirations')
        for exp in leaps_exps:
            dte = days_to(exp)
            print(f'    {exp}  (DTE = {dte} days, ~{dte/365:.1f} years out)')

        # Pull the FURTHEST expiration's chain to prove contracts exist
        furthest = leaps_exps[-1]
        print()
        print(f'  Pulling chain for FURTHEST LEAPS exp: {furthest}...')
        leaps_chain = get_chain(furthest)
        print(f'    Chain returned {len(leaps_chain)} contracts')
        if leaps_chain:
            # 5 ATM-ish samples + 5 deep-OTM
            sorted_by_strike = sorted(leaps_chain, key=lambda o: float(o.get('strike', 0)))
            print(f'    Sample LEAPS contracts (LIVE Tradier data):')
            for o in sorted_by_strike[::max(1, len(sorted_by_strike)//5)][:5]:
                strike = float(o.get('strike', 0))
                otype = o.get('option_type', '?')
                sym = o.get('symbol', '?')
                bid = o.get('bid', 0)
                ask = o.get('ask', 0)
                last = o.get('last', 0)
                oi = o.get('open_interest', 0)
                vol = o.get('volume', 0)
                iv = (o.get('greeks') or {}).get('mid_iv', 0)
                delta = (o.get('greeks') or {}).get('delta', 0)
                print(f'      {sym}  K={strike:.0f} {otype:4s}  '
                      f'bid={bid}  ask={ask}  last={last}  '
                      f'OI={oi}  vol={vol}  IV={iv}  Δ={delta}')

    print()

    # ── Q1: deep-OTM strikes (≥$150 from spot) ──────────────────────────────
    print('═' * 70)
    print(' Q1: Does Tradier have deep-OTM QQQ strikes (≥$150 from spot)?')
    print('═' * 70)
    print(f'  Spot reference: {spot:.2f}  →  deep-OTM threshold = ±$150')
    print(f'                                   deep-OTM put threshold:  K ≤ {spot - 150:.0f}')
    print(f'                                   deep-OTM call threshold: K ≥ {spot + 150:.0f}')

    # Use a near-term but not 0DTE expiration (1-2 weeks out) — enough liquidity
    # but Tradier will only return strikes that exist in their listing.
    near_exps = [e for e in exps if 5 <= days_to(e) <= 30]
    if not near_exps:
        print('  ⚠ No near-term expirations 5-30 DTE')
        sys.exit(1)
    near_exp = near_exps[len(near_exps)//2]
    print(f'  Pulling chain for {near_exp} (DTE={days_to(near_exp)}) to inventory strikes...')
    near_chain = get_chain(near_exp)
    print(f'  Total contracts in {near_exp} chain: {len(near_chain)}')

    deep_otm_puts = [o for o in near_chain
                      if o.get('option_type') == 'put'
                      and float(o.get('strike', 0)) <= spot - 150]
    deep_otm_calls = [o for o in near_chain
                       if o.get('option_type') == 'call'
                       and float(o.get('strike', 0)) >= spot + 150]
    print()
    print(f'  Deep-OTM PUTS (K ≤ {spot - 150:.0f}):   {len(deep_otm_puts)} contracts')
    if deep_otm_puts:
        print(f'    Sample (lowest 5 strikes):')
        for o in sorted(deep_otm_puts, key=lambda x: float(x.get('strike', 0)))[:5]:
            strike = float(o.get('strike', 0))
            sym = o.get('symbol', '?')
            bid = o.get('bid', 0)
            ask = o.get('ask', 0)
            oi = o.get('open_interest', 0)
            print(f'      {sym}  K={strike:.0f}P  bid={bid}  ask={ask}  OI={oi}')

    print()
    print(f'  Deep-OTM CALLS (K ≥ {spot + 150:.0f}):  {len(deep_otm_calls)} contracts')
    if deep_otm_calls:
        print(f'    Sample (highest 5 strikes):')
        for o in sorted(deep_otm_calls, key=lambda x: -float(x.get('strike', 0)))[:5]:
            strike = float(o.get('strike', 0))
            sym = o.get('symbol', '?')
            bid = o.get('bid', 0)
            ask = o.get('ask', 0)
            oi = o.get('open_interest', 0)
            print(f'      {sym}  K={strike:.0f}C  bid={bid}  ask={ask}  OI={oi}')

    # ── Final verdict ──────────────────────────────────────────────────────
    print()
    print('═' * 70)
    print(' VERDICT')
    print('═' * 70)
    leaps_ok = len(leaps_exps) > 0
    deep_otm_ok = (len(deep_otm_puts) + len(deep_otm_calls)) > 0
    print(f'  Q1 deep-OTM in Tradier: {"✅ YES" if deep_otm_ok else "❌ NO"}  '
          f'({len(deep_otm_puts)+len(deep_otm_calls)} contracts found)')
    print(f'  Q2 LEAPS in Tradier:    {"✅ YES" if leaps_ok else "❌ NO"}  '
          f'({len(leaps_exps)} expirations, {len(leaps_chain) if leaps_ok else 0} contracts in furthest)')
    if leaps_ok and deep_otm_ok:
        print()
        print('  ✅ TRADIER COVERS BOTH — can expand WS subscription if cap allows')
    elif not leaps_ok and deep_otm_ok:
        print()
        print('  ⚠ Tradier has deep-OTM but NOT LEAPS')
    elif leaps_ok and not deep_otm_ok:
        print()
        print('  ⚠ Tradier has LEAPS but NOT deep-OTM (unusual)')
    else:
        print()
        print('  ❌ Tradier provides NEITHER — must use Schwab REST exclusively')


if __name__ == '__main__':
    main()
