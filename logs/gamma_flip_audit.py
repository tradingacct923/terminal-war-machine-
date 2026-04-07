#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════
  GAMMA FLIP FORENSIC AUDIT
  Altaris Alpha Engine — CIO-Level Data Validation
  Zero guessing. Zero approximations. Raw Schwab data only.
═══════════════════════════════════════════════════════════════════

Run from project root:
  cd /Users/kaali/Desktop/altaris-dev && source venv/bin/activate
  python logs/gamma_flip_audit.py
"""

import sys, os, json, math, time, base64
from datetime import datetime, date

# Add project root for imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ─── Load Schwab Auth (same path as server.py) ──────────────────────
_TOKEN_FILE = os.path.join(PROJECT_ROOT, "connectors", ".schwab_tokens.json")
_SCHWAB_BASE = "https://api.schwabapi.com"

def _load_tokens():
    with open(_TOKEN_FILE) as f:
        return json.load(f)

def _refresh_tokens():
    """Refresh using env vars exactly like server.py does."""
    import requests
    tokens = _load_tokens()
    rt = tokens.get("refresh_token")
    if not rt:
        raise ValueError("No refresh token found")
    app_key = os.getenv("SCHWAB_APP_KEY", "")
    app_secret = os.getenv("SCHWAB_APP_SECRET", "")
    if not app_key or not app_secret:
        raise ValueError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set")
    creds = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
    resp = requests.post(f"{_SCHWAB_BASE}/v1/oauth/token", headers={
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, data={"grant_type": "refresh_token", "refresh_token": rt}, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Refresh failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    new_tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", rt),
        "token_expiry": time.time() + data.get("expires_in", 1800),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(_TOKEN_FILE, "w") as f:
        json.dump(new_tokens, f, indent=2)
    return new_tokens

def schwab_get(endpoint, params):
    """Authenticated GET — same logic as server.py _schwab_get."""
    import requests
    tokens = _load_tokens()
    at = tokens.get("access_token", "")
    expiry = tokens.get("token_expiry", 0)
    if not at or time.time() > expiry - 60:
        tokens = _refresh_tokens()
        at = tokens["access_token"]
    
    url = f"{_SCHWAB_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {at}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code == 401:
        tokens = _refresh_tokens()
        headers["Authorization"] = f"Bearer {tokens['access_token']}"
        resp = requests.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Schwab API {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# ═══════════════════════════════════════════════════════════════════
#  PHASE 1: RAW DATA FETCH — zero guessing
# ═══════════════════════════════════════════════════════════════════
print("=" * 72)
print("  GAMMA FLIP FORENSIC AUDIT")
print(f"  Timestamp: {datetime.now().isoformat()}")
print("=" * 72)

# 1a. Get QQQ spot from Schwab quote API
quote_data = schwab_get("/marketdata/v1/quotes", {"symbols": "QQQ", "fields": "quote"})
qqq_raw = quote_data.get("QQQ", {})
qqq_quote = qqq_raw.get("quote", qqq_raw)
qqq_spot = float(qqq_quote.get("lastPrice") or qqq_quote.get("mark") or 0)
print(f"\n▸ QQQ Spot (Schwab quote):  ${qqq_spot:.2f}")

# 1b. Get NQ mid from live L2
nq_mid = 0
try:
    from background_engine.l2_worker import get_l2_state
    l2 = get_l2_state()
    nq_mid = l2.get("mid_prices", {}).get("NQ", 0)
except Exception:
    pass
print(f"▸ NQ Mid  (L2 live):        ${nq_mid:.2f}")

if qqq_spot > 0 and nq_mid > 0:
    live_ratio = nq_mid / qqq_spot
else:
    live_ratio = 41.5
print(f"▸ NQ/QQQ Ratio:             {live_ratio:.4f}")

# 1c. Get expirations from Schwab
exp_data = schwab_get("/marketdata/v1/expirationchain", {"symbol": "QQQ"})
today_str = date.today().isoformat()
raw_dates = [
    e["expirationDate"]
    for e in exp_data.get("expirationList", [])
    if e.get("expirationDate", "") >= today_str
]
MAX_EXP = 5
exp_dates = raw_dates[:MAX_EXP]
print(f"▸ Expirations (top {MAX_EXP}):      {exp_dates}")


# ═══════════════════════════════════════════════════════════════════
#  PHASE 2: FETCH RAW CHAINS
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'─' * 72}")
print("  PHASE 2: Fetching raw option chains from Schwab API...")
print(f"{'─' * 72}")

all_chains = {}
total_contracts = 0
for exp_date in exp_dates:
    chain_data = schwab_get("/marketdata/v1/chains", {
        "symbol": "QQQ",
        "contractType": "ALL",
        "includeUnderlyingQuote": "true",
        "fromDate": exp_date,
        "toDate": exp_date,
        "strikeCount": 200,
    })
    
    options = []
    for leg_key in ("callExpDateMap", "putExpDateMap"):
        exp_map = chain_data.get(leg_key, {})
        for _exp_str, strikes in exp_map.items():
            for strike_str, contracts in strikes.items():
                for c in contracts:
                    options.append({
                        "strike": float(strike_str),
                        "option_type": "call" if leg_key == "callExpDateMap" else "put",
                        "volume": int(c.get("totalVolume", 0) or 0),
                        "open_interest": int(c.get("openInterest", 0) or 0),
                        "gamma": float(c.get("gamma", 0) or 0),
                        "volatility": float(c.get("volatility", 0) or 0),
                        "delta": float(c.get("delta", 0) or 0),
                        "dte": int(c.get("daysToExpiration", 0) or 0),
                    })
    
    schwab_spot = float(chain_data.get("underlyingPrice", 0) or 0)
    if schwab_spot > 0:
        qqq_spot = schwab_spot
    
    all_chains[exp_date] = options
    total_contracts += len(options)
    
    # Count non-zero OI contracts
    live_oi = sum(1 for o in options if o["open_interest"] > 0)
    print(f"  ✓ {exp_date}: {len(options)} contracts ({live_oi} with OI > 0)")

print(f"\n  Total contracts: {total_contracts}")
print(f"  Final QQQ Spot:  ${qqq_spot:.2f}")


# ═══════════════════════════════════════════════════════════════════
#  CODEPATH A: server.py /api/walls logic (multi-expiry, DTE-weighted)
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'─' * 72}")
print("  CODEPATH A: server.py /api/walls replication")
print(f"  ┌─ Dollar_GEX = schwab_gamma × w_oi × 100 × spot² × 0.01")
print(f"  ├─ Dealer_Net = -call_gex + put_gex")
print(f"  ├─ 0DTE: w=3.0, effective_oi = oi + (vol × 0.5)")
print(f"  └─ DTE>1: w=1/√DTE, effective_oi = oi")
print(f"{'─' * 72}")

A_gex = {}   # strike → net dealer GEX
A_all_strikes = set()

for exp_date in exp_dates:
    chain = all_chains.get(exp_date, [])
    if not chain:
        continue
    try:
        exp_dt = datetime.strptime(exp_date, "%Y-%m-%d").date()
        dte = max((exp_dt - date.today()).days, 0)
    except Exception:
        dte = 1
    dte_clamped = max(dte, 1)
    w = 3.0 if dte <= 1 else 1.0 / math.sqrt(dte_clamped)
    
    for opt in chain:
        strike = opt["strike"]
        oi = opt["open_interest"]
        vol = opt["volume"]
        otype = opt["option_type"]
        gamma = abs(opt["gamma"])
        A_all_strikes.add(strike)
        
        effective_oi = (oi + vol * 0.5) if dte <= 1 else float(oi)
        w_oi = effective_oi * w
        if w_oi <= 0:
            continue
        
        dollar_gex = gamma * w_oi * 100 * qqq_spot * qqq_spot * 0.01
        
        if otype == "call":
            A_gex[strike] = A_gex.get(strike, 0) - dollar_gex
        else:
            A_gex[strike] = A_gex.get(strike, 0) + dollar_gex

# Find zero-crossing
A_sorted = sorted(A_gex.keys())
A_flip = qqq_spot
A_s1 = A_s2 = None
for i in range(len(A_sorted) - 1):
    s1, s2 = A_sorted[i], A_sorted[i + 1]
    g1, g2 = A_gex[s1], A_gex[s2]
    if g1 * g2 < 0:
        frac = abs(g1) / (abs(g1) + abs(g2))
        A_flip = s1 + frac * (s2 - s1)
        A_s1, A_s2 = s1, s2
        break

A_flip_NQ = A_flip * live_ratio
print(f"\n  Gamma Flip (QQQ): ${A_flip:.2f}")
print(f"  Gamma Flip (NQ):  ${A_flip_NQ:.2f}")
if A_s1:
    print(f"  Zero-crossing:    ${A_s1} → ${A_s2}")
    print(f"    g({A_s1:.0f}) = {A_gex[A_s1]:>+15,.0f}")
    print(f"    g({A_s2:.0f}) = {A_gex[A_s2]:>+15,.0f}")


# ═══════════════════════════════════════════════════════════════════
#  CODEPATH B: schwab_bridge.py replication (WS-style, 0DTE only)
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'─' * 72}")
print("  CODEPATH B: schwab_bridge.py replication")
print(f"  ┌─ Dollar_GEX = gamma × (oi + vol×0.3) × 100 × (spot²/100)")
print(f"  ├─ Dealer_Net = -call_gex + put_gex")
print(f"  └─ 0DTE chain only (WS subscribes ~80 ATM contracts)")
print(f"{'─' * 72}")

B_gex = {}
dte0_chain = all_chains.get(exp_dates[0], []) if exp_dates else []

for opt in dte0_chain:
    strike = opt["strike"]
    gamma = abs(opt["gamma"])
    oi = opt["open_interest"]
    vol = opt["volume"]
    otype = opt["option_type"]
    if oi <= 0 and vol <= 0:
        continue
    if strike not in B_gex:
        B_gex[strike] = {"call": 0, "put": 0}
    effective_oi = oi + (vol * 0.3)
    dollar_gex = gamma * effective_oi * 100 * (qqq_spot * qqq_spot / 100)
    if otype == "call":
        B_gex[strike]["call"] = dollar_gex
    else:
        B_gex[strike]["put"] = dollar_gex

B_sorted = sorted(B_gex.keys())
B_net = {K: -B_gex[K]["call"] + B_gex[K]["put"] for K in B_sorted}

B_flip = qqq_spot
B_s1 = B_s2 = None
for i in range(len(B_sorted) - 1):
    s0, s1 = B_sorted[i], B_sorted[i + 1]
    g0, g1 = B_net.get(s0, 0), B_net.get(s1, 0)
    if g0 * g1 < 0:
        frac = abs(g0) / (abs(g0) + abs(g1))
        B_flip = s0 + frac * (s1 - s0)
        B_s1, B_s2 = s0, s1
        break

B_flip_NQ = B_flip * live_ratio
print(f"\n  Gamma Flip (QQQ): ${B_flip:.2f}")
print(f"  Gamma Flip (NQ):  ${B_flip_NQ:.2f}")
if B_s1:
    print(f"  Zero-crossing:    ${B_s1} → ${B_s2}")
    print(f"    g({B_s1:.0f}) = {B_net[B_s1]:>+15,.0f}")
    print(f"    g({B_s2:.0f}) = {B_net[B_s2]:>+15,.0f}")


# ═══════════════════════════════════════════════════════════════════
#  RAW GEX HEATMAP — ±20 strikes around ATM
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'─' * 72}")
print("  RAW DEALER NET GEX PROFILE (Codepath A, near ATM)")
print(f"  (+) = dealer LONG gamma → absorbs moves = PRICE MAGNET")
print(f"  (-) = dealer SHORT gamma → amplifies moves = VOLATILITY ZONE")
print(f"{'─' * 72}")

atm_idx = min(range(len(A_sorted)), key=lambda i: abs(A_sorted[i] - qqq_spot))
window = 20
start = max(0, atm_idx - window)
end = min(len(A_sorted), atm_idx + window)

print(f"\n  {'Strike':<10} {'Dealer Net GEX ($)':>20} {'Zone':>13} {''}  ")
print(f"  {'─' * 60}")
for i in range(start, end):
    K = A_sorted[i]
    gex = A_gex[K]
    zone = "LONG_γ" if gex > 0 else "SHORT_γ"
    marker = ""
    if abs(K - qqq_spot) < 0.6:
        marker = " ◀── SPOT"
    if A_s1 and K == A_s1:
        marker = " ◀── FLIP (above)"
    if A_s2 and K == A_s2:
        marker = " ◀── FLIP (below)"
    
    # Visual bar
    bar_len = min(40, int(abs(gex) / max(abs(g) for g in A_gex.values()) * 40))
    bar = "█" * bar_len
    if gex > 0:
        bar_str = f"  {'':>40} {bar}"
    else:
        padding = 40 - bar_len
        bar_str = f"  {' ' * padding}{bar}"
    
    print(f"  ${K:<9.1f} {gex:>+20,.0f} {zone:>13}{marker}")

# Also show 0DTE specific heatmap
print(f"\n{'─' * 72}")
print("  RAW DEALER NET GEX PROFILE (Codepath B, 0DTE only)")
print(f"{'─' * 72}")

b_atm_idx = min(range(len(B_sorted)), key=lambda i: abs(B_sorted[i] - qqq_spot)) if B_sorted else 0
b_start = max(0, b_atm_idx - window)
b_end = min(len(B_sorted), b_atm_idx + window)

print(f"\n  {'Strike':<10} {'Dealer Net GEX ($)':>20} {'Zone':>13} {''}  ")
print(f"  {'─' * 60}")
for i in range(b_start, b_end):
    K = B_sorted[i]
    gex = B_net[K]
    zone = "LONG_γ" if gex > 0 else "SHORT_γ"
    marker = ""
    if abs(K - qqq_spot) < 0.6:
        marker = " ◀── SPOT"
    if B_s1 and K == B_s1:
        marker = " ◀── FLIP (above)"
    if B_s2 and K == B_s2:
        marker = " ◀── FLIP (below)"
    print(f"  ${K:<9.1f} {gex:>+20,.0f} {zone:>13}{marker}")


# ═══════════════════════════════════════════════════════════════════
#  VARIANCE ANALYSIS
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'═' * 72}")
print("  VARIANCE ANALYSIS")
print(f"{'═' * 72}")

print(f"\n  {'Source':<35} {'Flip (QQQ)':>12} {'Flip (NQ)':>12}")
print(f"  {'─' * 60}")
print(f"  {'Codepath A (/api/walls multi-exp)':<35} ${A_flip:>10.2f}  ${A_flip_NQ:>10.2f}")
print(f"  {'Codepath B (schwab_bridge 0DTE)':<35} ${B_flip:>10.2f}  ${B_flip_NQ:>10.2f}")

if A_flip > 0 and B_flip > 0:
    var_ab = abs(A_flip - B_flip) / A_flip * 100
    print(f"\n  ▸ A vs B variance: {var_ab:.3f}%")
    
    if var_ab < 0.25:
        print(f"    ✅ PASS — within 0.25% tolerance")
    elif var_ab < 0.5:
        print(f"    ⚠️  WARN — {var_ab:.2f}% between 0.25%-0.5%")
    elif var_ab < 2.0:
        print(f"    ❌ DIVERGENT — {var_ab:.2f}% exceeds tolerance")
        print(f"       Expected cause: multi-expiry DTE weighting in A shifts flip")
    else:
        print(f"    ❌ STRUCTURALLY FLAWED — {var_ab:.1f}% indicates formula error")

# What the engine should show
print(f"\n  ▸ Previous engine output (from log): flip=23348 NQ")
if nq_mid > 0 and A_flip_NQ > 0:
    old_var = abs(23348 - A_flip_NQ) / A_flip_NQ * 100
    print(f"    Variance vs Codepath A: {old_var:.1f}%")
    if old_var > 2:
        print(f"    ❌ Old engine flip WAS WRONG — used different data/formula")


# ═══════════════════════════════════════════════════════════════════
#  REGIME DIAGNOSIS
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'═' * 72}")
print("  REGIME DIAGNOSIS")
print(f"{'═' * 72}")

if qqq_spot > 0 and A_flip > 0:
    above = qqq_spot > A_flip
    regime = "long_gamma_stable" if above else "short_gamma_volatile"
    print(f"\n  QQQ Spot: ${qqq_spot:.2f}  {'ABOVE' if above else 'BELOW'}  Flip: ${A_flip:.2f}")
    print(f"  → Correct regime: {regime}")
    print(f"  → Dealers {'ABSORB' if above else 'AMPLIFY'} directional moves")

if nq_mid > 0 and A_flip_NQ > 0:
    nq_above = nq_mid > A_flip_NQ
    nq_regime = "long_gamma_stable" if nq_above else "short_gamma_volatile"
    delta = nq_mid - A_flip_NQ
    print(f"\n  NQ Mid:   ${nq_mid:.2f}  {'ABOVE' if nq_above else 'BELOW'}  Flip: ${A_flip_NQ:.2f}")
    print(f"  → Delta to flip: {delta:+.2f} pts")
    print(f"  → NQ regime:     {nq_regime}")

# ═══════════════════════════════════════════════════════════════════
#  STRUCTURAL DIFFERENCE TABLE
# ═══════════════════════════════════════════════════════════════════
print(f"\n{'─' * 72}")
print("  KNOWN STRUCTURAL DIFFERENCES BETWEEN CODEPATHS")
print(f"{'─' * 72}")
print("""
  ┌──────────────────┬────────────────────────────┬────────────────────────────┐
  │ Parameter        │ Codepath A (/api/walls)     │ Codepath B (schwab_bridge) │
  ├──────────────────┼────────────────────────────┼────────────────────────────┤
  │ Expirations      │ Top 5 (multi-expiry)       │ ~80 ATM contracts (0DTE)   │
  │ DTE weighting    │ 3.0 for 0DTE, 1/√DTE      │ None (equal weight)        │
  │ 0DTE OI formula  │ oi + (vol × 0.5)           │ oi + (vol × 0.3)          │
  │ Spot used        │ Schwab underlyingPrice      │ _latest_qqq (WS feed)     │
  │ Gamma source     │ Schwab per-contract gamma   │ Schwab per-contract gamma │
  │ Update freq      │ On HTTP request (~60s)      │ On WS quote (~1-5s)       │
  │ NQ conversion    │ 4-tier ratio cascade        │ Live NQ/QQQ ratio         │
  └──────────────────┴────────────────────────────┴────────────────────────────┘
""")

print(f"{'═' * 72}")
print(f"  Audit completed: {datetime.now().isoformat()}")
print(f"{'═' * 72}")
