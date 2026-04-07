#!/usr/bin/env python3
"""
Phase 2 Forensic Audit: Regime Transition & Acceleration
Analyzes iceberg_outcomes.jsonl (post 10:48 AM fix)
"""
import json, os, sys

LOGS_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(LOGS_DIR, "iceberg_outcomes.jsonl")

def load_data():
    if not os.path.exists(PATH): return []
    data = []
    with open(PATH) as f:
        for l in f:
            l = l.strip()
            if not l or not l.startswith('{'):
                continue
            try:
                obj = json.loads(l)
                if obj.get('ts', 0) > 1775054900 and obj.get('confidence') == 'high':
                    data.append(obj)
            except json.JSONDecodeError:
                continue
    return data

data = load_data()
if not data:
    print("No post-fix high-confidence data found.")
    sys.exit()

data.sort(key=lambda x: x['ts'])

print("=" * 80)
print("  PHASE 2 AUDIT: REGIME TRANSITION & ACCELERATION")
print(f"  Dataset: {len(data)} High-Confidence Signals")
print("=" * 80)

# ── 1. The Reversal Bleed ──
# Find the exact timestamp of the macro trend reversal (the lowest price)
min_price = min(data, key=lambda x: x['price'])['price']
reversal_points = [x for x in data if x['price'] == min_price]
reversal_ts = reversal_points[0]['ts']
rev_time_str = reversal_points[0]['ts_human']

print(f"\n[1] THE REVERSAL BLEED")
print(f"  Macro Reversal Detected @ {rev_time_str} | Price: {min_price}")

# 5-minute window after reversal
window_end = reversal_ts + 300
bounce_signals = [x for x in data if reversal_ts < x['ts'] <= window_end]
short_signals = [x for x in bounce_signals if x['side'] == 's']

short_wins = sum(1 for x in short_signals if x['win_30s'])
total_drag = sum(x['outcome_30s'] - 0.25 for x in short_signals if x['outcome_30s'] is not None)  # 0.25 tick slip

print(f"  Signals in 5m Reversal Window : {len(bounce_signals)}")
print(f"  Counter-Trend Shorts Breached : {len(short_signals)}")
if short_signals:
    print(f"  Counter-Trend WR @ 30s        : {(short_wins/len(short_signals))*100:.1f}%")
print(f"  Aggregate PnL Drag (Net)      : {total_drag:+.2f} points")

# ── 2. Acceleration vs. Velocity (Filter Calibration) ──
print(f"\n[2] FILTER CALIBRATION: VELOCITY VS TIME WINDOW")
# We use the sequence of iceberg prices to approximate the price curve
def get_price_at(timestamp, fallback_price):
    # Find the most recent price <= timestamp
    past = [x for x in data if x['ts'] <= timestamp]
    if past:
        return past[-1]['price']
    return fallback_price

# Calculate optimal T and W
windows = [10, 15, 20]
print(f"  Testing Velocity Filters (suppress shorts if price rose > T in last W sec):")

for w in windows:
    print(f"  -- Window: {w}s --")
    best_t = 0
    best_score = -9999
    best_metrics = {}
    
    # Test point thresholds from 1 to 15
    for t in range(1, 16):
        filtered_shorts = 0
        preserved_longs = 0
        total_shorts = 0
        total_longs = 0
        
        for s in bounce_signals:
            if s['outcome_30s'] is None: continue
            
            p_now = s['price']
            p_past = get_price_at(s['ts'] - w, p_now)
            move = p_now - p_past
            
            # Filter condition: if price rose > T, suppress short. 
            # (Inversely, if price fell < -T, suppress long - but we are testing rally)
            suppressed = move > t
            
            if s['side'] == 's':
                total_shorts += 1
                if suppressed:
                    filtered_shorts += 1
            else:
                total_longs += 1
                if not suppressed:
                    preserved_longs += 1
                    
        pct_shorts_killed = (filtered_shorts / total_shorts * 100) if total_shorts else 0
        pct_longs_kept = (preserved_longs / total_longs * 100) if total_longs else 0
        
        # We want to kill 90% shorts, keep 90% longs
        score = pct_shorts_killed + pct_longs_kept
        if score > best_score:
            best_score = score
            best_t = t
            best_metrics = (pct_shorts_killed, pct_longs_kept, total_shorts, total_longs)
            
    print(f"    Optimal Threshold : T = {best_t} points")
    print(f"    Shorts Suppressed : {best_metrics[0]:.1f}% ({int((best_metrics[0]/100)*best_metrics[2])}/{best_metrics[2]})")
    print(f"    Longs Preserved   : {best_metrics[1]:.1f}% ({int((best_metrics[1]/100)*best_metrics[3])}/{best_metrics[3]})")


# ── 3. Asymmetric Exit Trigger ──
print(f"\n[3] THE ASYMMETRIC EXIT TRIGGER")
print("  Analyzing signal decay between T+10s peak and T+30s trough...")

# Find trades that were winning at 10s but lost at 30s
decay_trades = []
for s in data:
    if s['outcome_10s'] is not None and s['outcome_30s'] is not None:
        if s['outcome_10s'] > 0 and s['outcome_30s'] < 0:
            decay_trades.append(s)

if decay_trades:
    avg_peak = sum(x['outcome_10s'] for x in decay_trades) / len(decay_trades)
    avg_trough = sum(x['outcome_30s'] for x in decay_trades) / len(decay_trades)
    decay = avg_peak - avg_trough
    
    print(f"  V-Reversal Traps (Win @ 10s -> Loss @ 30s): {len(decay_trades)} signals")
    print(f"  Avg Peak (T+10s) : {avg_peak:+.2f} pts")
    print(f"  Avg Trough (T+30s): {avg_trough:+.2f} pts")
    print(f"  Bleed per signal : {decay:+.2f} pts")
    
    # Trailing stop math
    # We want to lock in the 10s peak. 
    # If the average peak is ~+5 pts, a 2.5 pt trailing stop from peak would save it.
    stop = max(1.0, avg_peak * 0.5)
    print(f"  -> PROPOSED ASYMMETRIC EXIT: Trailing Stop = {stop:.2f} pts ({(stop/0.25):.0f} ticks)")
else:
    print("  No V-reversal traps detected in this sample.")

print("\n(Note: MAE intra-window and Volume Clock states require higher-resolution tick logging, architectural upgrade recommended.)")
print("=" * 80)
