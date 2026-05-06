#!/bin/bash
# Investigation snapshot worker — runs every 5 min until 09:00 EDT 2026-04-27.
# Captures full /api/l2 + /api/walls + adaptive-floor state to JSON files.
# Run via launchd plist (see com.kaali.investigation_snapshot.plist).

INVEST_DIR="/Users/kaali/Desktop/altaris-dev/investigation"
SECRET="wm-greeksite-secret-key-2024"
USER_NAME="Kaali4426"
DEADLINE_EPOCH=$(date -j -f '%Y-%m-%d %H:%M:%S' '2026-04-27 09:00:00' '+%s' 2>/dev/null || echo 1777252800)

# Stop after deadline — also unloads the launchd job
NOW=$(date +%s)
if [ "$NOW" -gt "$DEADLINE_EPOCH" ]; then
  /bin/launchctl unload "$HOME/Library/LaunchAgents/com.kaali.investigation_snapshot.plist" 2>/dev/null
  /bin/rm -f "$HOME/Library/LaunchAgents/com.kaali.investigation_snapshot.plist"
  exit 0
fi

# Mint stateless HMAC token
TOKEN=$(/usr/bin/python3 -c "
import hmac, hashlib, time
ts=int(time.time())
ts_hex=format(ts,'x')
user_hex='${USER_NAME}'.encode().hex()
sig=hmac.new(b'${SECRET}',f'{ts_hex}.{user_hex}'.encode(),hashlib.sha256).hexdigest()[:32]
print(f'{ts_hex}.{user_hex}.{sig}')
")

TS=$(date '+%Y%m%d_%H%M%S')
OUT="${INVEST_DIR}/snapshots/snapshot_${TS}.json"

/usr/bin/python3 -c "
import urllib.request, json, time, sys, os

base = 'http://localhost:3001'
hdrs = {'X-Auth-Token': '${TOKEN}'}

def fetch(path):
    req = urllib.request.Request(base + path, headers=hdrs)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())

snap = {'captured_at_ms': int(time.time() * 1000), 'captured_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
try:
    snap['l2'] = fetch('/api/l2')
except Exception as e: snap['l2_err'] = str(e)
try:
    snap['walls'] = fetch('/api/walls')
except Exception as e: snap['walls_err'] = str(e)
try:
    snap['data'] = fetch('/api/data')
except Exception as e: snap['data_err'] = str(e)
try:
    snap['mm_summary'] = fetch('/api/mm_attribution/contracts?metric=events&limit=1')
except Exception as e: snap['mm_err'] = str(e)

with open('${OUT}', 'w') as f:
    json.dump(snap, f, indent=None, default=str)
print('SNAP_OK ${OUT}')
" >> "${INVEST_DIR}/snapshots/snapshot_runs.log" 2>&1
