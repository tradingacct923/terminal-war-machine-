#!/usr/bin/env python3
"""
Schwab LEVELONE_EQUITIES Cap Probe — empirical answer to:

  Q. Is the LEVELONE_EQUITIES per-service per-token cap really 3,000
     (the same as LEVELONE_OPTIONS), or something different (500 / 1,500)?

How it works:
  Opens a fresh out-of-process WS using the same OAuth2 token, sends
  one big SUBS request with many syntactically-valid fake tickers
  (AAAA..ZZZZ pattern), and reads the cap directly from the Schwab
  code=19 response — Schwab reports it inline, e.g.:

      "LEVELONE_OPTIONS=3000, DISCARDED=80"

  Whatever number appears after `LEVELONE_EQUITIES=` IS the cap.

Safety:
  - Out-of-process. Does NOT touch the running terminal.
  - Schwab supports multiple WS conns per token (we already use 2).
  - The probe socket disconnects 5 sec after the response.
"""
import os
import sys
import json
import time
import threading
import asyncio

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from connectors.schwab_auth import SchwabAuth
import websockets

# Generate up to 5,000 syntactically-valid 4-letter tickers
def gen_fake_tickers(n=5000):
    out = []
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for a in chars:
        for b in chars:
            for c in chars:
                for d in chars:
                    out.append(a + b + c + d)
                    if len(out) >= n:
                        return out
    return out


async def probe():
    auth = SchwabAuth()
    if not auth.access_token:
        auth.refresh()

    # Fetch streamer info via REST
    import requests
    resp = requests.get(
        'https://api.schwabapi.com/trader/v1/userPreference',
        headers=auth.get_headers()
    )
    prefs = resp.json()
    info = prefs.get('streamerInfo', [prefs])[0] if isinstance(prefs.get('streamerInfo'), list) else prefs.get('streamerInfo', prefs)
    streamer_url = info.get('streamerSocketUrl', info.get('schwabClientUrl', ''))
    if not streamer_url.startswith('wss://'):
        streamer_url = 'wss://' + streamer_url.lstrip('https://').lstrip('http://')
    customer_id = info.get('schwabClientCustomerId', '')
    correl_id   = info.get('schwabClientCorrelId', '')
    channel     = info.get('schwabClientChannel', 'N9')
    function_id = info.get('schwabClientFunctionId', 'APIAPP')

    print(f"[PROBE] Streamer URL: {streamer_url}")
    print(f"[PROBE] Connecting probe WS (separate conn from running terminal)...")

    ws = await websockets.connect(streamer_url, ping_interval=30, ping_timeout=20)

    # LOGIN
    login_req = {
        "requests": [{
            "service":   "ADMIN",
            "command":   "LOGIN",
            "requestid": 0,
            "SchwabClientCustomerId":  customer_id,
            "SchwabClientCorrelId":    correl_id,
            "parameters": {
                "Authorization":             auth.access_token,
                "SchwabClientChannel":       channel,
                "SchwabClientFunctionId":    function_id,
            }
        }]
    }
    await ws.send(json.dumps(login_req))
    login_resp = await asyncio.wait_for(ws.recv(), timeout=10)
    print(f"[PROBE] Login response: {login_resp[:200]}")

    # Generate 4,000 fake tickers (more than 3,000 expected cap)
    fakes = gen_fake_tickers(4000)
    print(f"[PROBE] Generated {len(fakes)} fake tickers (AAAA..)")

    # Single SUBS request to LEVELONE_EQUITIES
    subs_req = {
        "requests": [{
            "service":   "LEVELONE_EQUITIES",
            "command":   "SUBS",
            "requestid": 1,
            "SchwabClientCustomerId":  customer_id,
            "SchwabClientCorrelId":    correl_id,
            "parameters": {
                "keys":   ",".join(fakes),
                "fields": "0,1,2",
            }
        }]
    }
    payload = json.dumps(subs_req)
    print(f"[PROBE] Sending SUBS for {len(fakes)} keys (payload {len(payload)} bytes)")

    if len(payload) > 65000:
        print(f"[PROBE] ⚠️  Payload exceeds Schwab 65,535-byte WS frame limit; chunking required.")
        # Schwab will reject as bad-frame; cap not measurable in single shot.
        # Send a smaller batch (1,500 keys)
        small = fakes[:1500]
        subs_req["requests"][0]["parameters"]["keys"] = ",".join(small)
        payload = json.dumps(subs_req)
        print(f"[PROBE] Retry with {len(small)} keys (payload {len(payload)} bytes)")

    await ws.send(payload)

    # Collect all responses for 8 seconds
    print("[PROBE] Listening for responses (8s)...")
    end_t = time.time() + 8.0
    responses = []
    while time.time() < end_t:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            responses.append(msg)
            print(f"[PROBE]   recv: {msg[:300]}")
            # Once we see a code=19, we have our answer
            if 'code":19' in msg or 'code":"19"' in msg or '"code":19' in msg:
                print(f"[PROBE] ✓ Got code=19 — cap revealed in this response.")
                # parse out the cap from message
                import re
                m = re.search(r'LEVELONE_EQUITIES=(\d+)', msg)
                if m:
                    print(f"[PROBE] EMPIRICAL CAP: LEVELONE_EQUITIES = {m.group(1)}")
                break
        except asyncio.TimeoutError:
            continue

    await ws.close()
    print("[PROBE] Disconnected.")

    if not any('code":19' in r or '"code":19' in r for r in responses):
        # No cap hit — either the cap is HIGHER than 1,500 (rare per docs)
        # or the SUBS succeeded silently. Look for OK/code=0.
        print("[PROBE] No code=19 in responses. Either cap > 1,500 OR SUBS succeeded.")
        print("[PROBE] Response summary:")
        for r in responses:
            print(f"        {r[:200]}")


if __name__ == '__main__':
    asyncio.run(probe())
