#!/usr/bin/env python3
"""
Local Iceberg Detection Test — Zero API connections required.
Simulates realistic NQ futures trades and shows the full detection pipeline.

Run: python test_iceberg_local.py
"""
import sys, time, random, math, json

sys.path.insert(0, ".")

from background_engine.l2_worker import (
    _detect_iceberg, _detect_drifting_iceberg,
    _update_market_stats, _get_adaptive_thresholds,
    _MARKET_STATS, _ADAPTIVE_WARMUP,
    update_regime,
)

# ─── Config ──────────────────────────────────────────────────────────
SYMBOL = "NQ"
TICK = 0.25
BASE_PRICE = 21500.0
random.seed(42)

def fmt(d):
    """Pretty-print a detection result dict."""
    if not d:
        return None
    # pick key fields
    keys = ["clips","cv","confidence","side","size_rank","urgency",
            "absorbing","stickiness","pressure","decay","regime",
            "adaptive","adaptive_cv_threshold","adaptive_fill_threshold"]
    return {k: d[k] for k in keys if k in d}


def sim_trade(price, vol, side, ts):
    """Feed one trade through the detection engine."""
    ps = str(round(price, 2))
    ice = _detect_iceberg(SYMBOL, ps, vol, ts, side)
    drift = _detect_drifting_iceberg(SYMBOL, price, vol, ts, side)
    return ice, drift


print("=" * 70)
print("  ICEBERG DETECTION — LOCAL SIMULATION TEST")
print("=" * 70)

# ─── Phase 1: Warmup (500 random trades) ─────────────────────────────
print("\n📊 PHASE 1: Warmup (feeding 500 random trades)...")
ts = time.time()
for i in range(500):
    price = BASE_PRICE + random.choice([-2, -1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1, 2])
    vol = random.choice([1, 1, 1, 2, 2, 3, 3, 5, 5, 8, 10])
    side = random.choice(["b", "b", "b", "s", "s"])
    sim_trade(price, vol, side, ts + i * 0.1)

ms = _MARKET_STATS[SYMBOL]
print(f"   ✅ Warmup done: {ms['total_trades']} trades processed")
print(f"   📈 Fill rate: buy={ms['fill_rate_b']:.1f}/s  sell={ms['fill_rate_s']:.1f}/s")
print(f"   📊 Mean CV: {ms['mean_cv']:.3f}  StdDev CV: {ms['std_cv']:.3f}")
print(f"   📊 Mean clip: {ms['mean_clip']:.1f}  StdDev clip: {ms['std_clip']:.1f}")
print(f"   🎲 P(coincidence): {ms['p_coincidence']:.3f}")

# ─── Phase 2: Show adaptive thresholds ────────────────────────────────
print("\n🎯 PHASE 2: Adaptive thresholds (post-warmup)...")

# Simulate regime update from options data
update_regime(
    spot=21500, gamma_flip=21400, total_gex=0.8,
    call_wall=21800, put_wall=21200,
    flow_ratio=0.58, iv_rv_spread=-1.2
)

thresholds = _get_adaptive_thresholds(SYMBOL, "b")
print(f"   Regime: {thresholds.get('_adaptive', False) and 'ADAPTIVE' or 'HARDCODED FALLBACK'}")
print(f"   {'─'*50}")
print(f"   CV threshold:     {thresholds['ice_cv']}")
print(f"   Min fills (ice):  {thresholds['ice_refill_count']}  (p={thresholds.get('_p_coincidence', '?')}, target={thresholds.get('_confidence_target', '?')})")
print(f"   Min fills (drift):{thresholds['drift_min_fills']}")
print(f"   Min clip (σ):     {thresholds['min_clip_sigma']}")
print(f"   Drift spread:     {thresholds['drift_min_spread']} ticks")

# ─── Phase 3: Inject a REAL iceberg pattern ──────────────────────────
print("\n🧊 PHASE 3: Injecting iceberg pattern (10-lot buy wall at 21500.00)...")
ts_ice = ts + 100
ice_price = 21500.00
detections = []

for i in range(12):
    vol = 10 + random.randint(-1, 1)  # clips of 9-11 (consistent)
    t = ts_ice + i * 2.5  # every 2.5 seconds (algo-like timing)
    
    # Also inject some random noise FAR from the iceberg (outside ±2 tick zone)
    for _ in range(3):
        noise_price = ice_price + random.choice([-5, -4, -3, 3, 4, 5]) 
        noise_vol = random.choice([1, 2, 3])
        noise_side = random.choice(["b", "s"])
        sim_trade(noise_price, noise_vol, noise_side, t - random.uniform(0.1, 0.5))
    
    # The iceberg fill
    ice, drift = sim_trade(ice_price, vol, "b", t)
    if ice:
        detections.append(("ICEBERG", i+1, ice))
        print(f"   🧊 Fill #{i+1}: BUY {vol} @ {ice_price} → ICEBERG DETECTED!")
        result = fmt(ice)
        for k, v in result.items():
            print(f"      {k}: {v}")
    else:
        print(f"   ⬜ Fill #{i+1}: BUY {vol} @ {ice_price} → not yet (need more fills)")

# ─── Phase 4: Inject a DRIFTING iceberg ──────────────────────────────
print(f"\n🌊 PHASE 4: Injecting drifting iceberg (buyer walking up through 5+ levels)...")
ts_drift = ts_ice + 60
drift_detections = []

for i in range(15):
    # Drifting: price moves up but clip sizes stay consistent
    drift_price = 21500.00 + i * TICK * 2  # walks up 2 ticks each trade
    vol = 8 + random.randint(-1, 1)  # clips of 7-9
    t = ts_drift + i * 1.8
    
    ice, drift = sim_trade(drift_price, vol, "b", t)
    if drift:
        drift_detections.append(("DRIFT", i+1, drift))
        print(f"   🌊 Fill #{i+1}: BUY {vol} @ {drift_price:.2f} → DRIFTING ICEBERG!")
        print(f"      confidence: {drift.get('confidence', '?')}")
        print(f"      cv: {drift.get('cv', '?')}")
        print(f"      prices: {drift.get('n_prices', '?')} levels")
    else:
        print(f"   ⬜ Fill #{i+1}: BUY {vol} @ {drift_price:.2f} → not yet")

# ─── Phase 5: Inject random noise (should NOT trigger) ───────────────
print(f"\n🔇 PHASE 5: Injecting 200 random trades (should NOT trigger false icebergs)...")
ts_noise = ts_drift + 60
false_positives = 0
for i in range(200):
    price = 21500.00 + random.choice([-3, -2, -1, 0, 1, 2, 3]) * TICK
    vol = random.choice([1, 1, 2, 2, 3, 5, 8, 15, 20, 50])
    side = random.choice(["b", "s"])
    ice, drift = sim_trade(price, vol, side, ts_noise + i * 0.3)
    if ice:
        false_positives += 1
        print(f"   ⚠️  FALSE POSITIVE at trade #{i}: {fmt(ice)}")

if false_positives == 0:
    print(f"   ✅ Zero false positives out of 200 random trades!")
else:
    print(f"   ⚠️  {false_positives} false positives out of 200 trades")

# ─── Summary ──────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  RESULTS SUMMARY")
print("=" * 70)
print(f"  Warmup trades:        {ms['total_trades'] - 12 - 15 - 200}")
print(f"  Iceberg detections:   {len(detections)} (from 12 injected fills)")
print(f"  Drifting detections:  {len(drift_detections)} (from 15 injected fills)")
print(f"  False positives:      {false_positives} (from 200 random trades)")
print(f"  P(coincidence):       {ms['p_coincidence']:.3f}")
print(f"  Adaptive CV:          {thresholds['ice_cv']}")
print(f"  Adaptive min fills:   {thresholds['ice_refill_count']}")
print("=" * 70)
