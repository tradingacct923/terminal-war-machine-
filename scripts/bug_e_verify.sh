#!/bin/bash
# Bug E (HP by_exchange unit conversion) verification — one-shot.
# Fires Mon 2026-04-27 09:35 EDT via ~/Library/LaunchAgents/com.kaali.bug_e_verify.plist.
# Output: /tmp/bug_e_verify_YYYYMMDD_HHMM.txt (also stdout via launchd log).

set -u

BASE='http://localhost:3001'
SECRET='wm-greeksite-secret-key-2024'
USER_NAME='Kaali4426'
OUT="/tmp/bug_e_verify_$(date '+%Y%m%d_%H%M').txt"
PLIST="$HOME/Library/LaunchAgents/com.kaali.bug_e_verify.plist"

{
  echo "=== Bug E verification ==="
  echo "fired: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo ""

  # Mint HMAC token (3-segment ts_hex.user_hex.sig[:32])
  TOKEN=$(/usr/bin/python3 -c "
import hmac, hashlib, time
SECRET=b'${SECRET}'
USER='${USER_NAME}'
ts=int(time.time())
ts_hex=format(ts,'x')
user_hex=USER.encode().hex()
msg=f'{ts_hex}.{user_hex}'.encode()
sig=hmac.new(SECRET,msg,hashlib.sha256).hexdigest()[:32]
print(f'{ts_hex}.{user_hex}.{sig}')
")
  echo "token (prefix): ${TOKEN:0:24}..."
  echo ""

  # Confirm server up
  if ! /usr/bin/curl -s -o /dev/null -w '%{http_code}' "$BASE/api/spot?ticker=QQQ" -H "X-Auth-Token: $TOKEN" | grep -q '^200$'; then
    echo "FAIL: server not reachable on $BASE"
    exit 1
  fi

  echo "=== HP per-strike (Σγ_sh — reference) ==="
  /usr/bin/curl -s -H "X-Auth-Token: $TOKEN" "$BASE/api/hedge_pressure/QQQ" | /usr/bin/python3 -c "
import json,sys
d=json.load(sys.stdin)
t=d.get('totals',{})
print('strikes:', len(d.get('strikes',[])),
      'spot:', d.get('spot'),
      'Σγ_sh_1pct:', round(t.get('hp_gamma_shares_1pct',0)),
      'OI_bal_γ:', d.get('oi_balance_strike_gamma'))
"
  echo ""

  echo "=== HP by_exchange (THE FIX) ==="
  /usr/bin/curl -s -H "X-Auth-Token: $TOKEN" "$BASE/api/hedge_pressure/QQQ/by_exchange" | /usr/bin/python3 -c "
import json,sys
d=json.load(sys.stdin)
exchs=d.get('exchanges',[]) or []
print('n_exch:', len(exchs), 'spot:', d.get('spot'))
print()
print(f'{\"exch\":>6}  {\"posted_sh\":>14}  {\"caught_sh\":>14}  {\"diff_sh\":>14}  {\"unit_check\":>10}')
fails=0
top3=[]
for e in exchs:
    p=e.get('hp_gamma_posted',0) or 0
    c=e.get('hp_gamma_caught',0) or 0
    df=e.get('diff',0) or 0
    unit='shares' if abs(p)<1e6 else 'DOLLAR-γ'
    if abs(p) >= 1e6: fails += 1
    top3.append((e.get('exch'), p, c, df, unit))
for ex,p,c,df,unit in top3[:3]:
    print(f'{ex:>6}  {round(p):>14}  {round(c):>14}  {round(df):>14}  {unit:>10}')
print()
if not exchs:
    print('FAIL: by_exchange empty (no MM activity captured — option market closed or stream not live)')
elif fails == 0:
    print(f'PASS: all {len(exchs)} venues report shares (|posted| < 1e6)')
else:
    print(f'FAIL: {fails}/{len(exchs)} venues still report dollar-γ scale — Bug E fix not active')
    print('Inspect: background_engine/schwab_bridge.py:4015-4020 (_scale = -1.0/spot multiplied into posted/caught)')
"
  echo ""
  echo "=== done $(date '+%H:%M:%S %Z') ==="
} > "$OUT" 2>&1

# Self-disable so it doesn't fire again next year
/bin/launchctl unload "$PLIST" 2>/dev/null
/bin/rm -f "$PLIST"

# Also drop a quick visible breadcrumb on the desktop
/bin/cp -f "$OUT" "$HOME/Desktop/bug_e_verify_result.txt" 2>/dev/null || true

exit 0
