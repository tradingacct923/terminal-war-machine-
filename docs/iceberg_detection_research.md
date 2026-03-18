# ICEBERG DETECTION — MASTER BUILD PROMPT
# Complete Specification for Elite-Level Iceberg Detection System
# Version: v4 (Full Intelligence + Drifting + DOM + Options Fusion)
# Date: March 18, 2026
# Platform: NQ Futures Trading Terminal (War Machine)

---

## OVERVIEW

You are building the most advanced iceberg detection system for a real-time
NQ futures trading terminal. This system must detect hidden institutional
orders (icebergs) across multiple sophistication levels — from basic
same-price refills to advanced drifting icebergs that move across price
levels to avoid detection.

The system has three components:
1. BACKEND ENGINE (Python, l2_worker.py) — detection logic
2. FRONTEND RENDERER (JavaScript, volume_bubbles.js) — chart visualization
3. OPTIONS FUSION (JavaScript) — cross-referencing options flow with icebergs

Hardware: Mac Mini, 15GB RAM currently, max 20GB.
All features combined must use less than 50MB additional RAM.

---

## TABLE OF CONTENTS

PART 1: WHAT IS ALREADY BUILT (v3 — LIVE IN PRODUCTION)
  1.1  Architecture Overview
  1.2  Constants and Configuration
  1.3  Data Structures and State
  1.4  Adaptive Min Clip Size — Math and Logic
  1.5  Coefficient of Variation (CV) — Math and Logic
  1.6  Zone Detection (±2 Ticks) — Math and Logic
  1.7  Fill Exhaustion (Linear Regression Slope) — Math and Logic
  1.8  Absorption Context — Math and Logic
  1.9  Size Rank (σ Distance) — Math and Logic
  1.10 Urgency Score (Composite) — Math and Logic
  1.11 Pressure Signal (Decision Tree) — Math and Logic
  1.12 Estimated Hidden Size — Math and Logic
  1.13 Frontend Diamond Rendering — Current Implementation
  1.14 Frontend Badge System — Current Implementation

PART 2: ELITE FEATURE #1 — DOM CROSS-VALIDATION
  2.1  Problem Statement
  2.2  How It Works (Step by Step)
  2.3  Math: Refill Amount Calculation
  2.4  Handling Varying Show Sizes
  2.5  Edge Cases and Robustness
  2.6  Integration with Existing v3
  2.7  Backend Code Specification
  2.8  Frontend Badge Update
  2.9  Test Scenarios

PART 3: ELITE FEATURE #2 — INTER-FILL TIMING ANALYSIS
  3.1  Problem Statement
  3.2  Algo vs Random Timing Distributions
  3.3  Math: Gap CV Calculation
  3.4  Integration with Confidence Score
  3.5  Backend Code Specification
  3.6  Test Scenarios

PART 4: ELITE FEATURE #3 — COMPLETION COUNTDOWN
  4.1  Problem Statement
  4.2  Countdown Math
  4.3  State Transitions (Fresh → Active → Depleting → Gone)
  4.4  Backend Code Specification
  4.5  Frontend Rendering — Countdown Timer
  4.6  Frontend Rendering — Flash on Depletion

PART 5: ELITE FEATURE #4 — DRIFTING ICEBERG DETECTION (3 LAYERS)
  5.1  Problem Statement — Why Basic Detection Fails
  5.2  Layer 1: Behavioral Fingerprint
  5.3  Layer 2: DOM Total Depth Anomaly
  5.4  Layer 3: Timing Regularity Across Band
  5.5  Combining All 3 Layers
  5.6  Backend Code Specification
  5.7  Frontend Rendering — Band Overlay
  5.8  What They Can vs Cannot Fake

PART 6: ELITE FEATURE #5 — LEVEL MEMORY HEATMAP
  6.1  Problem Statement
  6.2  Data Structure
  6.3  Scoring and Ranking Levels
  6.4  Preemptive Alerts
  6.5  Backend Code Specification
  6.6  Frontend — Level Markers on Chart
  6.7  Session Persistence

PART 7: ELITE FEATURE #6 — POST-ICEBERG PRICE PREDICTION
  7.1  Problem Statement
  7.2  Outcome Tracking
  7.3  Statistical Analysis
  7.4  Win Rate and Expected Value
  7.5  Backend Code Specification
  7.6  Frontend — Prediction Badge
  7.7  Minimum Sample Size

PART 8: OPTIONS CHAIN FUSION
  8.1  NQ Equivalent Price Column
  8.2  Put/Call Ratio Column
  8.3  GEX (Gamma Exposure) Column
  8.4  Row Highlighting on Iceberg Match
  8.5  Gamma Wall Lines on Chart
  8.6  Gamma-Backed Iceberg Flag
  8.7  Fusion Signal Box

PART 9: RAM BUDGET AND PERFORMANCE
  9.1  Per-Feature RAM Breakdown
  9.2  CPU Impact Analysis
  9.3  Cleanup and Garbage Collection

PART 10: BUILD ORDER AND DEPENDENCIES

---

## PART 1: WHAT IS ALREADY BUILT (v3 — LIVE IN PRODUCTION)

### 1.1 Architecture Overview

The iceberg detection runs inside `l2_worker.py`, a Python background
worker that processes real-time Level 2 market data from the TopStepX
connector. Every trade that prints on the tape calls `_detect_iceberg()`.

Data flow:
```
TopStepX WebSocket → l2_worker.py → _detect_iceberg() → result dict
                                  → _detect_sweep()
                                  → _detect_delta_divergence()
                                  → _detect_momentum_ignition()
                                  → _detect_spoofing()
                                  ↓
                            L2_STATE dict → WebSocket → Frontend
                                  ↓
                        volume_bubbles.js → Canvas rendering
```

The detection function is called on EVERY trade. It must be fast.
No blocking, no network calls, no file I/O. Pure in-memory computation.

### 1.2 Constants and Configuration

```python
# ── Iceberg Detection Constants ──
_ICE_REFILL_COUNT     = 3       # min refills at same price to trigger
_ICE_CV_THRESHOLD     = 0.35    # max coefficient of variation (stddev/mean)
_ICE_MIN_CLIP_FLOOR   = 1       # absolute minimum clip size
_ICE_ZONE_TICKS       = 2       # ±2 ticks = adjacent prices count as same zone
_ICE_ABSORB_MAX_MOVE  = 2       # price must stay within ±2 ticks to be "absorbing"

# Tiered detection windows: (window_seconds, confidence_label)
_ICE_WINDOWS = [
    (5.0,  "high"),    # fast refill = definitely iceberg
    (15.0, "medium"),  # patient algo
    (60.0, "low"),     # very patient, could be coincidence
]
```

Tick sizes per symbol:
```python
TICK_SIZES = {
    "NQ": 0.25,
    "ES": 0.25,
    "YM": 1.0,
    "RTY": 0.10,
    "MNQ": 0.25,
    "MES": 0.25,
}
DEFAULT_TICK_SIZE = 0.25
```

### 1.3 Data Structures and State

```python
# Rolling trade sizes for adaptive thresholds (per symbol)
# {symbol: deque(maxlen=500)}
_TRADE_SIZE_HISTORY: dict = defaultdict(lambda: deque(maxlen=500))

# Recent trade prices for absorption detection (per symbol)
# {symbol: deque of (timestamp, price), maxlen=200}
_ICE_PRICE_HISTORY: dict = defaultdict(lambda: deque(maxlen=200))

# Fill tracker: {symbol: {price_str: [(timestamp, volume, side), ...]}}
_ICE_TRACKER: dict = defaultdict(lambda: defaultdict(list))

# Detection results attached to candle: {symbol: {tf: {icebergs: {}, sweeps: []}}}
_DETECT_RESULTS: dict = defaultdict(lambda: defaultdict(dict))
```

### 1.4 Adaptive Min Clip Size — Math and Logic

PROBLEM:
A fixed minimum clip size fails across different market conditions.
During the overnight session, average trade size is 1-3 contracts.
During NY open (9:30 AM), average trade size is 15-25 contracts.
A fixed threshold of 3 would catch noise during overnight and miss
institutional clips during the day.

SOLUTION:
Use a rolling average of the last 500 trade sizes to adapt the minimum.

MATH:
```
trade_history = deque of last 500 trade volumes
avg_trade = sum(trade_history) / len(trade_history)
min_clip = max(_ICE_MIN_CLIP_FLOOR, int(avg_trade * 0.5))
```

EXAMPLES:
```
Overnight session:
  trade_history = [1, 2, 1, 3, 1, 2, 1, 2, 3, 1, ...]
  avg_trade = 1.7
  min_clip = max(1, int(1.7 * 0.5)) = max(1, 0) = 1
  Result: Even 1-lot refills can trigger detection

NY Open:
  trade_history = [15, 22, 18, 30, 12, 25, 20, 17, ...]
  avg_trade = 19.9
  min_clip = max(1, int(19.9 * 0.5)) = max(1, 9) = 9
  Result: Random 3-lot fills filtered as noise

Mid-day:
  trade_history = [5, 8, 3, 10, 6, 4, 7, 5, ...]
  avg_trade = 6.0
  min_clip = max(1, int(6.0 * 0.5)) = max(1, 3) = 3
  Result: Balanced threshold
```

CODE:
```python
_TRADE_SIZE_HISTORY[symbol].append(volume)
trade_hist = _TRADE_SIZE_HISTORY[symbol]
if len(trade_hist) > 10:
    avg_trade = sum(trade_hist) / len(trade_hist)
    min_clip = max(_ICE_MIN_CLIP_FLOOR, int(avg_trade * 0.5))
else:
    avg_trade = float(volume)
    min_clip = _ICE_MIN_CLIP_FLOOR

if volume < min_clip:
    return None  # too small to be an iceberg clip
```

### 1.5 Coefficient of Variation (CV) — Math and Logic

PROBLEM:
The old detection used a fixed ±30% tolerance around the median clip size.
This was brittle. Example: clips of [10, 15] — median is 12.5, but 10 is
20% below and 15 is 20% above. If clips alternate [10, 15, 10, 15], the
old method might reject because individual clips exceed the tolerance of
a slightly different median.

Real icebergs randomize clip sizes slightly (e.g., 10, 12, 9, 11, 13)
to avoid detection. We need a STATISTICAL measure of consistency.

SOLUTION:
Use Coefficient of Variation (CV) = standard deviation / mean.
CV measures relative dispersion regardless of scale.

MATH:
```
vols = [v₁, v₂, v₃, ..., vₙ]   (clip sizes in window)
mean = Σvᵢ / n
variance = Σ(vᵢ - mean)² / n
stddev = √variance
CV = stddev / mean
```

INTERPRETATION:
```
CV < 0.15  → very consistent (likely same clip size with jitter)
CV < 0.35  → consistent enough (iceberg with randomization)
CV > 0.35  → too dispersed (probably random different orders)
CV > 1.0   → wildly different sizes (definitely not an iceberg)
```

EXAMPLES:
```
Example 1 — Clean iceberg (all same size):
  vols = [10, 10, 10, 10]
  mean = 10, stddev = 0, CV = 0.0 → ICEBERG ✅

Example 2 — Randomized iceberg (varying clips):
  vols = [10, 15, 10, 15, 12]
  mean = 12.4
  variance = (5.76 + 6.76 + 5.76 + 6.76 + 0.16) / 5 = 5.04
  stddev = 2.245
  CV = 2.245 / 12.4 = 0.181 → ICEBERG ✅

Example 3 — Noise (random fills):
  vols = [3, 50, 8, 100, 2]
  mean = 32.6, stddev = 38.7
  CV = 38.7 / 32.6 = 1.19 → REJECTED ✅

Example 4 — Slightly dispersed but still algo:
  vols = [8, 14, 10, 16, 9, 13]
  mean = 11.67, stddev = 2.94
  CV = 2.94 / 11.67 = 0.252 → ICEBERG ✅ (under 0.35)
```

CODE:
```python
vols = [f[1] for f in fills_in_window]
mean_vol = sum(vols) / len(vols)
if mean_vol == 0:
    continue
variance = sum((v - mean_vol) ** 2 for v in vols) / len(vols)
stddev_vol = variance ** 0.5
cv = stddev_vol / mean_vol

if cv > _ICE_CV_THRESHOLD:
    continue  # too dispersed, not an iceberg
```

### 1.6 Zone Detection (±2 Ticks) — Math and Logic

PROBLEM:
Real icebergs often shift the order by ±1 tick to avoid per-price
detection. Example: algo places hidden order at 25200.00, gets filled,
then moves to 25200.25, gets filled, then back to 25199.75. Our old
per-price tracker sees 1 fill at each price → no detection.

SOLUTION:
Group all fills within ±N ticks of the current fill price as belonging
to the same zone. If the ZONE has enough fills with consistent CV and
same side, it is an iceberg.

MATH:
```
tick_size = TICK_SIZES[symbol]  (e.g., 0.25 for NQ)
zone_range = _ICE_ZONE_TICKS * tick_size  (e.g., 2 * 0.25 = 0.50)

For a fill at price P:
  zone = [P - 0.50, P - 0.25, P, P + 0.25, P + 0.50]
  (5 price levels for ±2 ticks)
  
Collect all fills from all 5 price levels.
Apply the same CV, side check, and min refill count.
```

EXAMPLES:
```
Fills in last 5 seconds:
  25200.00: [(t=1, vol=9, side="b")]
  25200.25: [(t=2, vol=13, side="b")]
  25200.00: [(t=3, vol=11, side="b")]
  25199.75: [(t=4, vol=14, side="b")]

Per-price detection:
  25200.00 → 2 fills → below _ICE_REFILL_COUNT (3) → MISSED ❌
  25200.25 → 1 fill → MISSED ❌
  25199.75 → 1 fill → MISSED ❌

Zone detection for price 25200.00:
  Zone: 25199.50 to 25200.50
  All fills in zone: [9, 13, 11, 14] = 4 fills
  All same side: [b, b, b, b] ✅
  CV = stddev(9,13,11,14) / mean(9,13,11,14) = 1.92/11.75 = 0.163 ✅
  zone_levels_hit = {25200.00, 25200.25, 25199.75} = 3 levels
  is_zone = True, zone_levels = 3
  
  ZONE ICEBERG DETECTED ✅
```

CODE:
```python
tick_size = TICK_SIZES.get(symbol, DEFAULT_TICK_SIZE)
zone_fills_all = []
zone_levels_hit = set()

for offset in range(-_ICE_ZONE_TICKS, _ICE_ZONE_TICKS + 1):
    adj_price = round(price_f + offset * tick_size, 2)
    adj_key = str(adj_price)
    adj_fills = _ICE_TRACKER[symbol].get(adj_key, [])
    if adj_fills:
        zone_levels_hit.add(adj_key)
        zone_fills_all.extend(adj_fills)

is_zone = len(zone_levels_hit) > 1
```

### 1.7 Fill Exhaustion (Linear Regression Slope) — Math and Logic

PROBLEM:
Knowing an iceberg exists is not enough. The trader needs to know if
the iceberg is DRAINING (about to run out of ammo) or STRENGTHENING
(adding more hidden size). This determines whether to trade WITH the
wall or AGAINST it.

SOLUTION:
Calculate the linear regression slope of clip sizes over time.
If clips are getting smaller → exhausting (wall draining).
If clips are getting bigger → strengthening (wall adding).
If stable → holding.

MATH:
```
Given fills: [(t₁, v₁), (t₂, v₂), ..., (tₙ, vₙ)]

Normalize timestamps to [0, 1]:
  t_range = tₙ - t₁
  xᵢ = (tᵢ - t₁) / t_range

Compute means:
  x̄ = Σxᵢ / n
  ȳ = Σvᵢ / n

Linear regression slope:
  numerator   = Σ(xᵢ - x̄)(vᵢ - ȳ)
  denominator = Σ(xᵢ - x̄)²
  slope = numerator / denominator

Normalize by mean clip size:
  norm_slope = slope / ȳ

Classification:
  norm_slope < -0.15 → "exhausting" (clips shrinking)
  norm_slope > +0.15 → "strengthening" (clips growing)
  else               → "holding" (stable)
```

FULL WORKED EXAMPLE:
```
Fills: [(t=0s, vol=15), (t=3s, vol=14), (t=7s, vol=12), (t=12s, vol=8)]

Step 1: Normalize time
  t_range = 12 - 0 = 12
  xs = [0/12, 3/12, 7/12, 12/12] = [0, 0.25, 0.583, 1.0]
  ys = [15, 14, 12, 8]

Step 2: Means
  x̄ = (0 + 0.25 + 0.583 + 1.0) / 4 = 0.458
  ȳ = (15 + 14 + 12 + 8) / 4 = 12.25

Step 3: Numerator
  (0 - 0.458)(15 - 12.25) = (-0.458)(2.75)   = -1.260
  (0.25 - 0.458)(14 - 12.25) = (-0.208)(1.75) = -0.364
  (0.583 - 0.458)(12 - 12.25) = (0.125)(-0.25) = -0.031
  (1.0 - 0.458)(8 - 12.25) = (0.542)(-4.25)   = -2.304
  numerator = -1.260 + -0.364 + -0.031 + -2.304 = -3.959

Step 4: Denominator
  (0 - 0.458)² = 0.210
  (0.25 - 0.458)² = 0.043
  (0.583 - 0.458)² = 0.016
  (1.0 - 0.458)² = 0.294
  denominator = 0.210 + 0.043 + 0.016 + 0.294 = 0.563

Step 5: Slope
  slope = -3.959 / 0.563 = -7.03

Step 6: Normalize
  norm_slope = -7.03 / 12.25 = -0.574

Step 7: Classify
  -0.574 < -0.15 → decay = "EXHAUSTING" ⬇
  Meaning: clips are shrinking fast, wall is running out
```

### 1.8 Absorption Context — Math and Logic

PROBLEM:
An iceberg that absorbs opposing flow while keeping price stable is
a STRONG signal. If price doesn't move despite heavy filling, the
wall is winning. If price moves through the wall, it is failing.

SOLUTION:
Check the price range during the iceberg's window. If the range stays
within ±N ticks, the wall is absorbing.

MATH:
```
recent_prices = _ICE_PRICE_HISTORY[symbol]
prices_during_window = [p for t, p in recent_prices if t >= window_cutoff]

if len(prices_during_window) >= 2:
    price_range = max(prices_during_window) - min(prices_during_window)
    price_ticks = price_range / tick_size
    absorbing = (price_ticks <= _ICE_ABSORB_MAX_MOVE)
else:
    absorbing = False
```

EXAMPLES:
```
Case 1 — Absorbing (wall holding):
  Prices during window: [25200.00, 25200.25, 25199.75, 25200.00, 25200.25]
  Range: 25200.25 - 25199.75 = 0.50
  Ticks: 0.50 / 0.25 = 2.0
  2.0 ≤ 2 → absorbing = TRUE ✅
  Meaning: wall is successfully absorbing all opposing flow

Case 2 — Not absorbing (wall failing):
  Prices: [25200.00, 25199.00, 25197.50, 25196.00]
  Range: 25200.00 - 25196.00 = 4.00
  Ticks: 4.00 / 0.25 = 16.0
  16.0 > 2 → absorbing = FALSE
  Meaning: price is blowing through the wall
```

### 1.9 Size Rank (σ Distance) — Math and Logic

PROBLEM:
Is this a retail iceberg (10 contracts) or a whale (500 contracts)?
The raw clip size alone doesn't tell you — 10 contracts during overnight
is huge, but during NY open it is tiny.

SOLUTION:
Compare the average iceberg clip to the rolling standard deviation
of recent trade sizes. Rank by number of standard deviations.

MATH:
```
trade_hist = _TRADE_SIZE_HISTORY[symbol]
th_mean = sum(trade_hist) / len(trade_hist)
th_variance = sum((v - th_mean)² for v in trade_hist) / len(trade_hist)
th_std = √th_variance

sigma_distance = (avg_clip - th_mean) / max(th_std, 0.01)

Ranking:
  sigma < 1.0  → "retail"          (normal sized)
  1.0 ≤ σ < 2.0 → "professional"   (above average)
  2.0 ≤ σ < 3.0 → "institutional"  (well above normal)
  σ ≥ 3.0       → "whale"          (massively outsized)
```

### 1.10 Urgency Score (Composite) — Math and Logic

```
time_factor     = min(fill_rate / 2.0, 1.0)     # weight: 40%
size_factor     = min(avg_clip / avg_trade, 1.0)  # weight: 30%
remaining_factor = 1.0 - fill_pct                 # weight: 30%

urgency = time_factor * 0.4 + size_factor * 0.3 + remaining_factor * 0.3

Range: 0.0 to 1.0
  > 0.8 → extremely urgent (algo in a hurry)
  > 0.5 → moderate urgency
  < 0.3 → passive (slow accumulation)
```

### 1.11 Pressure Signal (Decision Tree) — Math and Logic

```python
if decay == "exhausting" and fill_pct > 0.5:
    pressure = "wall_exhausted"      # almost done, running out
elif decay == "exhausting" and not absorbing:
    pressure = "wall_breaking"       # losing AND draining
elif absorbing and fill_pct < 0.3:
    if side == "b":
        pressure = "bullish_wall"    # fresh buy wall holding
    else:
        pressure = "bearish_wall"    # fresh sell wall holding
elif absorbing and fill_pct >= 0.3:
    if side == "b":
        pressure = "bullish_wall"
    else:
        pressure = "bearish_wall"
elif not absorbing and decay == "holding":
    pressure = "wall_holding"
else:
    if fill_pct < 0.2:
        pressure = "wall_fresh"      # just appeared
    else:
        pressure = "wall_active"     # generic
```

### 1.12 Estimated Hidden Size — Math and Logic

```
visible_total     = sum of all observed fills
time_elapsed      = latest_fill_time - earliest_fill_time
fill_rate         = n_fills / time_elapsed  (fills per second)
avg_clip          = mean(clip_sizes)
est_duration      = 90.0 seconds (assumed typical algo run)

remaining_time    = est_duration - time_elapsed
est_remaining     = fill_rate × remaining_time
est_hidden        = visible_total + (est_remaining × avg_clip)

fill_pct          = visible_total / est_hidden
est_remaining_sec = remaining_time
```

### 1.13 Frontend Diamond Rendering

The iceberg is rendered as a diamond shape on the candlestick chart:
- Green glow for buy-side icebergs
- Red glow for sell-side icebergs
- Pulse speed varies by confidence (high=fast, low=slow)
- Diamond size is fixed (BUBBLE_CONFIG.ICE_DIAMOND_SIZE)
- Volume label inside: "visible/~estimated"

### 1.14 Frontend Badge System

Below the diamond:
```
[zone prefix][pressure label][decay arrow]
Z:BUY WALL⬇

Colors:
  bullish_wall  → #00e676 (green)
  bearish_wall  → #ff1744 (red)
  wall_breaking → #ffab00 (yellow)
  wall_exhausted → #ff6d00 (orange)
  wall_fresh    → #00b0ff (blue)

Decay arrows:
  exhausting    → ⬇
  strengthening → ⬆
  holding       → ─

Fill % micro-bar:
  2px tall bar below badge showing fill_pct progress
  Background: rgba(255,255,255,0.15)
  Fill: pressureColor
```

---

## PART 2: ELITE FEATURE #1 — DOM CROSS-VALIDATION

### 2.1 Problem Statement

Current detection is TAPE-ONLY. We look at trades that print and infer
iceberg behavior from clip consistency. But this gives us PROBABILITY,
not CERTAINTY. A DOM cross-validation gives us certainty.

The tape tells us: "10 contracts sold at 25200."
It does NOT tell us: "Was the order at 25200 refilled afterward?"

Only the DOM (order book) can answer that. We already receive DOM
snapshots via the TopStepX connector — we just need to cross-reference.

### 2.2 How It Works (Step by Step)

```
STEP 1: Store "before" DOM snapshot
  Every time we receive a DOM update, store the bid/ask sizes
  at each price level for the symbol.
  
  _DOM_SNAPSHOT_PREV[symbol] = _DOM_SNAPSHOT_CURR[symbol].copy()
  _DOM_SNAPSHOT_CURR[symbol] = {
      "25200.00": {"bid": 15, "ask": 0},
      "25200.25": {"bid": 8, "ask": 0},
      "25199.75": {"bid": 22, "ask": 0},
      ...
  }

STEP 2: When a trade prints at a price
  We know:
    - dom_before = size at that price BEFORE the trade
    - trade_volume = how many contracts were filled
    - dom_after = size at that price AFTER the trade (next snapshot)
  
STEP 3: Calculate expected remaining
  expected_remaining = max(0, dom_before - trade_volume)
  
STEP 4: Calculate refill amount
  refill_amount = dom_after - expected_remaining
  
  If refill_amount > 0 → hidden liquidity was added
  If refill_amount ≤ 0 → normal fill, no refill
```

### 2.3 Math: Refill Amount Calculation

```
FORMULA:
  refill = dom_after - max(0, dom_before - trade_volume)

CASE 1: Normal fill (no iceberg)
  dom_before = 15
  trade_volume = 10
  dom_after = 5
  expected = max(0, 15 - 10) = 5
  refill = 5 - 5 = 0  → NO REFILL (normal order)

CASE 2: Full refill (classic iceberg)
  dom_before = 15
  trade_volume = 10
  dom_after = 15
  expected = max(0, 15 - 10) = 5
  refill = 15 - 5 = 10  → REFILLED 10 contracts

CASE 3: Partial refill with different show size (smart iceberg)
  dom_before = 15
  trade_volume = 10
  dom_after = 8
  expected = max(0, 15 - 10) = 5
  refill = 8 - 5 = 3  → REFILLED 3 contracts (varying show size)

CASE 4: Show size increased (iceberg adding more)
  dom_before = 12
  trade_volume = 8
  dom_after = 20
  expected = max(0, 12 - 8) = 4
  refill = 20 - 4 = 16  → REFILLED 16 (showing more to attract flow)

CASE 5: Full fill, order gone (NOT iceberg)
  dom_before = 10
  trade_volume = 10
  dom_after = 0
  expected = max(0, 10 - 10) = 0
  refill = 0 - 0 = 0  → NO REFILL (order fully consumed)
```

### 2.4 Handling Varying Show Sizes

Real icebergs randomize their show size just like they randomize clips.
The DOM might show:
  Time 1: bid = 12 → fill 10 → bid = 8 (refilled +6, show reduced)
  Time 2: bid = 8  → fill 7  → bid = 15 (refilled +14, show increased)
  Time 3: bid = 15 → fill 12 → bid = 10 (refilled +7, show reduced)

The SIZES are all different. But the SUBTRACTION always reveals refill > 0.
This is unfakeable — you can randomize the size, but you cannot hide
the fact that new contracts appeared where none should remain.

### 2.5 Refill Ratio Classification

```python
refill_ratio = refill_amount / max(trade_volume, 1)

if refill_ratio > 0.8:
    dom_confidence = "confirmed"    # strong refill, definitely iceberg
elif refill_ratio > 0.3:
    dom_confidence = "likely"       # partial refill, probably iceberg
elif refill_ratio > 0.0:
    dom_confidence = "possible"     # minimal refill, could be coincidence
else:
    dom_confidence = "unconfirmed"  # no refill detected
```

### 2.6 Backend Code Specification

```python
# ── DOM Cross-Validation State ──
# {symbol: {price_str: {"bid": size, "ask": size}}}
_DOM_SNAPSHOT_PREV: dict = defaultdict(dict)
_DOM_SNAPSHOT_CURR: dict = defaultdict(dict)
# {symbol: {price_str: count of confirmed refills}}
_DOM_REFILL_COUNT: dict = defaultdict(lambda: defaultdict(int))

def _dom_cross_validate(symbol: str, price_str: str,
                        volume: int, side: str) -> tuple:
    """Returns (dom_confidence, refill_amount)."""
    book_side = "bid" if side == "b" else "ask"
    
    prev = _DOM_SNAPSHOT_PREV[symbol].get(price_str, {})
    curr = _DOM_SNAPSHOT_CURR[symbol].get(price_str, {})
    
    dom_before = prev.get(book_side, 0)
    dom_after = curr.get(book_side, 0)
    
    if dom_before == 0:
        return ("unconfirmed", 0)
    
    expected = max(0, dom_before - volume)
    refill = dom_after - expected
    
    if refill <= 0:
        return ("unconfirmed", 0)
    
    _DOM_REFILL_COUNT[symbol][price_str] += 1
    ratio = refill / max(volume, 1)
    
    if ratio > 0.8:
        return ("confirmed", refill)
    elif ratio > 0.3:
        return ("likely", refill)
    else:
        return ("possible", refill)
```

### 2.7 Integration with Iceberg Result

Add to the return dict from _detect_iceberg:
```python
return {
    ...existing fields...
    "dom_confirmed": dom_confidence,  # "confirmed"/"likely"/"possible"/"unconfirmed"
    "dom_refills": total_dom_refills, # number of DOM-confirmed refills
    "dom_refill_avg": avg_refill_size, # average refill amount
}
```

### 2.8 Frontend Badge Update

```javascript
// After existing badge text
if (ice.dom_confirmed === 'confirmed') {
    badgeText += ' ✓DOM';  // DOM-confirmed iceberg
} else if (ice.dom_confirmed === 'likely') {
    badgeText += ' ~DOM';  // DOM likely confirms
}
// "unconfirmed" → no DOM badge (tape-only detection)
```

---

## PART 3: ELITE FEATURE #2 — INTER-FILL TIMING ANALYSIS

### 3.1 Problem Statement

Random trading activity can accidentally produce fills that look like
an iceberg: 3 buys of similar size at the same price within 15 seconds.
But random fills have RANDOM timing (Poisson process), while algo
icebergs have REGULAR timing (algorithmic scheduling).

By analyzing the time BETWEEN fills, we can distinguish algo-driven
refills from coincidental same-price fills.

### 3.2 Algo vs Random Timing

```
ALGO TIMING (iceberg):
  Fill at t=0.0s
  Fill at t=1.2s    gap = 1.2
  Fill at t=2.1s    gap = 0.9
  Fill at t=3.5s    gap = 1.4
  Fill at t=4.3s    gap = 0.8
  Fill at t=5.6s    gap = 1.3
  
  Gaps: [1.2, 0.9, 1.4, 0.8, 1.3]
  Mean gap: 1.12
  Stddev: 0.24
  Gap CV: 0.24 / 1.12 = 0.214  → REGULAR (algo) ✅

RANDOM TIMING (coincidence):
  Fill at t=0.0s
  Fill at t=0.1s    gap = 0.1
  Fill at t=8.3s    gap = 8.2
  Fill at t=8.6s    gap = 0.3
  Fill at t=53.2s   gap = 44.6
  Fill at t=53.4s   gap = 0.2
  
  Gaps: [0.1, 8.2, 0.3, 44.6, 0.2]
  Mean gap: 10.68
  Stddev: 18.9
  Gap CV: 18.9 / 10.68 = 1.77  → IRREGULAR (random) ❌
```

### 3.3 Math: Gap CV Calculation

```python
def _analyze_fill_timing(fills_in_window):
    """Returns (gap_cv, timing_confidence)."""
    if len(fills_in_window) < 3:
        return (None, "insufficient")
    
    timestamps = sorted([f[0] for f in fills_in_window])
    gaps = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
    
    if not gaps or min(gaps) <= 0:
        return (None, "invalid")
    
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap == 0:
        return (0.0, "instant")
    
    variance = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
    gap_cv = (variance ** 0.5) / mean_gap
    
    if gap_cv < 0.3:
        return (gap_cv, "algo_confirmed")  # very regular timing
    elif gap_cv < 0.6:
        return (gap_cv, "algo_likely")     # somewhat regular
    elif gap_cv < 1.0:
        return (gap_cv, "mixed")           # unclear
    else:
        return (gap_cv, "random")          # definitely random
```

### 3.4 Integration with Confidence

The timing confidence can UPGRADE or DOWNGRADE the existing confidence:
```
Original confidence "medium" + timing "algo_confirmed" → "high"
Original confidence "high" + timing "random" → "medium" (downgrade)
Original confidence "low" + timing "algo_confirmed" → "medium" (upgrade)
```

---

## PART 4: ELITE FEATURE #3 — COMPLETION COUNTDOWN

### 4.1 Problem Statement

Traders need to know WHEN to act. "There's an iceberg" is less useful
than "This iceberg will be depleted in approximately 12 seconds."

### 4.2 Countdown Math

```
fill_rate = n_fills / time_elapsed       # fills per second
avg_clip = mean(clip_sizes)              # average fill size
remaining = est_hidden - visible_total   # estimated remaining contracts
depletes_in = remaining / (fill_rate * avg_clip)  # seconds until empty
```

### 4.3 State Transitions

```
FRESH      → fill_pct < 0.15    → badge: "FRESH 🟦"
ACTIVE     → fill_pct 0.15-0.50 → badge: "BUY WALL ⬇ 45s"
DEPLETING  → fill_pct 0.50-0.85 → badge: "DEPLETING 12s" (flashing)
CRITICAL   → fill_pct 0.85-1.00 → badge: "⚠️ BREAKING 3s" (rapid flash)
GONE       → no refill for 3s   → badge: "WALL GONE ✅" (for 5 seconds)
```

### 4.4 Frontend Rendering

```javascript
// Countdown timer below badge
if (ice.est_remaining_sec && ice.est_remaining_sec > 0) {
    const secs = Math.round(ice.est_remaining_sec);
    ctx.font = BUBBLE_CONFIG.FONT_BADGE;
    ctx.fillStyle = secs < 10 ? '#ff1744' : secs < 30 ? '#ffab00' : '#aaa';
    ctx.fillText(`${secs}s`, x, y + ds + 22);
}

// Rapid flash when critical
if (ice.fill_pct > 0.85) {
    const flashRate = 200; // ms
    const flashOn = (performance.now() % flashRate) < flashRate / 2;
    if (!flashOn) return; // skip rendering every other frame
}
```

---

## PART 5: ELITE FEATURE #4 — DRIFTING ICEBERG DETECTION

### 5.1 Problem Statement

The most sophisticated iceberg algos DON'T stay at one price.
They accumulate across a BAND of prices: buy 10 at 25200, then
move the order to 25197.50, buy 12, move to 25202.25, buy 9, etc.

Our zone detection catches ±2 ticks (±0.50 on NQ). But a drifting
iceberg can spread across ±20 ticks (±5.00 on NQ). Each individual
price level sees only 1-2 fills — below our _ICE_REFILL_COUNT threshold.

### 5.2 Layer 1: Behavioral Fingerprint

CONCEPT: Ignore price entirely. Group trades by SIDE and analyze
whether the same-side clips have iceberg-like consistency.

```python
# ── Drifting Iceberg State ──
# {symbol: {side: deque of (timestamp, price, volume), maxlen=100}}
_DRIFT_TRACKER: dict = defaultdict(lambda: {"b": deque(maxlen=100),
                                             "s": deque(maxlen=100)})

_DRIFT_WINDOW_SEC     = 30.0    # look back 30 seconds
_DRIFT_MIN_FILLS      = 5       # need at least 5 same-side fills
_DRIFT_MAX_CV          = 0.40   # clip consistency threshold (looser for drift)
_DRIFT_MIN_PRICE_SPREAD = 3     # must span at least 3 distinct prices

def _detect_drifting_iceberg(symbol, price_f, volume, timestamp, side):
    if side == "n":
        return None
    
    tracker = _DRIFT_TRACKER[symbol][side]
    tracker.append((timestamp, price_f, volume))
    
    # Get fills in window
    cutoff = timestamp - _DRIFT_WINDOW_SEC
    recent = [(t, p, v) for t, p, v in tracker if t >= cutoff]
    
    if len(recent) < _DRIFT_MIN_FILLS:
        return None
    
    # Must span multiple distinct prices
    prices = set(round(p, 2) for _, p, _ in recent)
    if len(prices) < _DRIFT_MIN_PRICE_SPREAD:
        return None
    
    # Check clip consistency
    vols = [v for _, _, v in recent]
    mean_v = sum(vols) / len(vols)
    if mean_v == 0:
        return None
    var = sum((v - mean_v)**2 for v in vols) / len(vols)
    cv = (var ** 0.5) / mean_v
    
    if cv > _DRIFT_MAX_CV:
        return None
    
    # DRIFTING ICEBERG DETECTED
    price_range = max(p for _, p, _ in recent) - min(p for _, p, _ in recent)
    
    return {
        "type": "drifting",
        "fills": len(recent),
        "prices_hit": len(prices),
        "band_low": min(p for _, p, _ in recent),
        "band_high": max(p for _, p, _ in recent),
        "band_range": price_range,
        "total_vol": sum(vols),
        "avg_clip": round(mean_v, 1),
        "cv": round(cv, 3),
        "side": side,
    }
```

### 5.3 Layer 2: DOM Total Depth Anomaly

CONCEPT: Track the TOTAL visible depth on one side of the book within
a price band. If trades keep hitting but total depth barely drops,
hidden liquidity is being added somewhere in the band.

```python
# ── DOM Band Depth Tracking ──
_DOM_BAND_DEPTH: dict = defaultdict(lambda: {"bid_total": 0, "ask_total": 0,
                                              "fills_since": 0, "vol_since": 0})

def _track_band_depth(symbol, dom_snapshot, side_fills, side_vol):
    """Called when DOM updates. Tracks total depth vs fills."""
    band = _DOM_BAND_DEPTH[symbol]
    
    # Calculate total bid and ask depth across all visible levels
    total_bid = sum(level.get("bid", 0) for level in dom_snapshot.values())
    total_ask = sum(level.get("ask", 0) for level in dom_snapshot.values())
    
    prev_bid = band["bid_total"]
    prev_ask = band["ask_total"]
    
    # Update
    band["bid_total"] = total_bid
    band["ask_total"] = total_ask
    band["fills_since"] += side_fills
    band["vol_since"] += side_vol
    
    # Check for anomaly: lots of fills but depth didn't drop
    if band["vol_since"] >= 30:  # enough volume to analyze
        if side_vol > 0:
            expected_drop = band["vol_since"]
            if prev_bid > 0:
                actual_drop = prev_bid - total_bid
                leak_ratio = 1.0 - (actual_drop / max(expected_drop, 1))
                
                if leak_ratio > 0.5:
                    # More than 50% of fills were refilled
                    return {
                        "dom_leak": True,
                        "leak_ratio": round(leak_ratio, 2),
                        "hidden_added": int(expected_drop - actual_drop),
                        "fills_analyzed": band["fills_since"],
                    }
        
        # Reset counters after analysis
        band["fills_since"] = 0
        band["vol_since"] = 0
    
    return None
```

### 5.4 Layer 3: Timing Regularity Across Band

Same as inter-fill timing (Part 3) but applied to the drifting
iceberg fills. If same-side fills across DIFFERENT prices have
regular timing, it is one entity.

```python
def _drift_timing_analysis(recent_fills):
    """Analyze timing regularity of drifting fills."""
    if len(recent_fills) < 4:
        return None
    
    timestamps = sorted([f[0] for f in recent_fills])
    gaps = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
    
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap == 0:
        return {"gap_cv": 0.0, "timing": "instant"}
    
    var = sum((g - mean_gap)**2 for g in gaps) / len(gaps)
    gap_cv = (var ** 0.5) / mean_gap
    
    return {
        "gap_cv": round(gap_cv, 3),
        "mean_gap": round(mean_gap, 3),
        "timing": "algo" if gap_cv < 0.5 else "mixed" if gap_cv < 1.0 else "random"
    }
```

### 5.5 Combining All 3 Layers

```python
def _evaluate_drifting_iceberg(symbol, price_f, volume, timestamp, side):
    """Master function: combine all 3 drifting detection layers."""
    
    # Layer 1: Behavioral fingerprint
    drift = _detect_drifting_iceberg(symbol, price_f, volume, timestamp, side)
    if not drift:
        return None
    
    # Layer 2: DOM depth anomaly (if available)
    dom_leak = _DOM_BAND_DEPTH.get(symbol, {}).get("last_leak")
    if dom_leak:
        drift["dom_leak"] = dom_leak["leak_ratio"]
        drift["hidden_added"] = dom_leak["hidden_added"]
    
    # Layer 3: Timing analysis
    recent = [(t, p, v) for t, p, v in _DRIFT_TRACKER[symbol][side]
              if t >= timestamp - _DRIFT_WINDOW_SEC]
    timing = _drift_timing_analysis(recent)
    if timing:
        drift["gap_cv"] = timing["gap_cv"]
        drift["timing"] = timing["timing"]
    
    # Composite confidence
    score = 0
    if drift["cv"] < 0.25: score += 2
    elif drift["cv"] < 0.40: score += 1
    
    if timing and timing["timing"] == "algo": score += 2
    elif timing and timing["timing"] == "mixed": score += 1
    
    if dom_leak and dom_leak["leak_ratio"] > 0.5: score += 2
    elif dom_leak and dom_leak["leak_ratio"] > 0.3: score += 1
    
    if score >= 4:
        drift["drift_confidence"] = "confirmed"
    elif score >= 2:
        drift["drift_confidence"] = "likely"
    else:
        drift["drift_confidence"] = "possible"
    
    return drift
```

### 5.6 What They Can vs Cannot Fake

```
THEY CAN RANDOMIZE:           THEY CANNOT HIDE:
✅ Individual clip sizes  →    ❌ That clips have low CV (algo precision)
✅ Prices across band     →    ❌ That total DOM depth doesn't drop  
✅ Show sizes on DOM      →    ❌ That fills are all same side
✅ Time jitter            →    ❌ That timing is regular (not Poisson)
✅ Individual orders      →    ❌ That net flow is persistently one-way

The only way to truly hide is to make everything completely random.
But then it is not an iceberg — it is just slow buying.
And slow random buying does not provide concentrated support at a level.
```


---

## PART 6: ELITE FEATURE #5 — LEVEL MEMORY HEATMAP

### 6.1 Problem Statement

Every time an iceberg appears and disappears, we lose that intelligence.
But price levels where icebergs repeatedly appear are INSTITUTIONAL LEVELS.
If an iceberg has appeared at 25200 seven times today, that is not random.
Someone is defending that level.

By remembering where icebergs appeared, we can:
1. Alert when price approaches a known institutional level
2. Show historical iceberg frequency as a heatmap on the DOM
3. Predict where icebergs will reappear

### 6.2 Data Structure

```python
# ── Level Memory State ──
# {symbol: {price_str: {
#     "count": int,        # number of icebergs detected here
#     "total_vol": int,    # total estimated hidden volume
#     "last_side": str,    # last detected side ("b" or "s")
#     "last_ts": float,    # timestamp of last detection
#     "avg_size": float,   # average estimated hidden size
#     "sessions": int,     # number of distinct sessions with icebergs here
# }}}
_ICE_LEVEL_MEMORY: dict = defaultdict(lambda: defaultdict(lambda: {
    "count": 0, "total_vol": 0, "last_side": "", "last_ts": 0,
    "avg_size": 0.0, "sessions": 0
}))
```

### 6.3 Updating Level Memory

```python
def _update_level_memory(symbol, price_str, iceberg_result):
    """Called whenever an iceberg is confirmed."""
    mem = _ICE_LEVEL_MEMORY[symbol][price_str]
    mem["count"] += 1
    mem["total_vol"] += iceberg_result.get("est_hidden", 0)
    mem["last_side"] = iceberg_result.get("side", "")
    mem["last_ts"] = iceberg_result.get("ts", time.time())
    mem["avg_size"] = mem["total_vol"] / mem["count"]
```

### 6.4 Scoring and Ranking

```python
def _get_hot_levels(symbol, current_price, tick_size, n=10):
    """Return top N most active iceberg levels near current price."""
    levels = _ICE_LEVEL_MEMORY[symbol]
    scored = []
    
    for price_str, mem in levels.items():
        if mem["count"] < 2:
            continue  # need at least 2 to be significant
        
        distance = abs(float(price_str) - current_price)
        recency = time.time() - mem["last_ts"]
        
        # Score: higher = more important
        score = (mem["count"] * 2.0)              # frequency matters most
        score += (mem["avg_size"] / 100.0)        # bigger icebergs matter
        score *= max(0.1, 1.0 - recency / 3600)  # decay over 1 hour
        score *= max(0.1, 1.0 - distance / 50.0)  # closer matters more
        
        scored.append({
            "price": price_str,
            "score": round(score, 2),
            "count": mem["count"],
            "avg_size": int(mem["avg_size"]),
            "last_side": mem["last_side"],
        })
    
    return sorted(scored, key=lambda x: -x["score"])[:n]
```

### 6.5 Preemptive Alerts

```python
def _check_approaching_level(symbol, current_price, tick_size):
    """Alert if price is approaching a known institutional level."""
    levels = _ICE_LEVEL_MEMORY[symbol]
    alerts = []
    
    for price_str, mem in levels.items():
        if mem["count"] < 3:
            continue
        
        level_price = float(price_str)
        distance_ticks = abs(current_price - level_price) / tick_size
        
        if distance_ticks <= 10:  # within 10 ticks
            alerts.append({
                "type": "approaching_institutional_level",
                "price": price_str,
                "distance_ticks": int(distance_ticks),
                "ice_count": mem["count"],
                "avg_size": int(mem["avg_size"]),
                "expected_side": mem["last_side"],
                "message": f"Approaching known {'buy' if mem['last_side'] == 'b' else 'sell'}"
                           f" iceberg level at {price_str}"
                           f" ({mem['count']} previous, avg {int(mem['avg_size'])} hidden)"
            })
    
    return alerts
```

### 6.6 Frontend — Level Markers

```javascript
// Draw horizontal dotted lines at known institutional levels
for (const level of hotLevels) {
    const y = priceToY(parseFloat(level.price));
    const alpha = Math.min(1, level.score / 10);
    
    ctx.strokeStyle = level.last_side === 'b'
        ? `rgba(0, 230, 118, ${alpha})`   // green for buy levels
        : `rgba(255, 23, 68, ${alpha})`;   // red for sell levels
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(chartWidth, y);
    ctx.stroke();
    ctx.setLineDash([]);
    
    // Label: "ICE x7 ~350avg"
    ctx.font = '9px monospace';
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(`ICE x${level.count} ~${level.avg_size}`, 5, y - 3);
}
```

---

## PART 7: ELITE FEATURE #6 — POST-ICEBERG PRICE PREDICTION

### 7.1 Problem Statement

Detection alone is not a trading edge. An edge requires PREDICTION:
after an iceberg completes, what happens to price?

If we track what happened after the last 50 buy icebergs, we can say:
"After buy icebergs like this, price moved +6.2 points in 30s, win rate 85%."

THAT is alpha. THAT is what hedge funds pay for.

### 7.2 Outcome Tracking

```python
# ── Post-Iceberg Outcome Tracker ──
# {symbol: deque of {
#     "side": "b"/"s",
#     "price": float,       # iceberg detection price
#     "ts": float,           # detection timestamp
#     "size_rank": str,       # whale/institutional/etc
#     "confidence": str,      # high/medium/low
#     "completed": bool,      # True when iceberg finished
#     "completion_ts": float, # when it finished
#     "outcome_10s": float,   # price move 10s after completion
#     "outcome_30s": float,   # price move 30s after completion
#     "outcome_60s": float,   # price move 60s after completion
# }, maxlen=100}
_ICE_OUTCOMES: dict = defaultdict(lambda: deque(maxlen=100))

# Pending outcomes waiting for time to elapse
_ICE_PENDING: dict = defaultdict(list)
```

### 7.3 Recording Outcomes

```python
def _record_iceberg_completion(symbol, price, side, ts, size_rank, confidence):
    """Mark an iceberg as completed and schedule outcome checks."""
    _ICE_PENDING[symbol].append({
        "side": side,
        "price": price,
        "ts": ts,
        "size_rank": size_rank,
        "confidence": confidence,
        "check_10s": ts + 10,
        "check_30s": ts + 30,
        "check_60s": ts + 60,
        "outcome_10s": None,
        "outcome_30s": None,
        "outcome_60s": None,
    })

def _check_pending_outcomes(symbol, current_price, current_ts):
    """Check if any pending outcomes can be resolved."""
    still_pending = []
    
    for pending in _ICE_PENDING[symbol]:
        if pending["outcome_10s"] is None and current_ts >= pending["check_10s"]:
            direction = 1 if pending["side"] == "b" else -1
            pending["outcome_10s"] = (current_price - pending["price"]) * direction
        
        if pending["outcome_30s"] is None and current_ts >= pending["check_30s"]:
            direction = 1 if pending["side"] == "b" else -1
            pending["outcome_30s"] = (current_price - pending["price"]) * direction
        
        if pending["outcome_60s"] is None and current_ts >= pending["check_60s"]:
            direction = 1 if pending["side"] == "b" else -1
            pending["outcome_60s"] = (current_price - pending["price"]) * direction
            # All outcomes recorded — move to completed
            _ICE_OUTCOMES[symbol].append(pending)
            continue
        
        still_pending.append(pending)
    
    _ICE_PENDING[symbol] = still_pending
```

### 7.4 Statistical Analysis

```python
def _get_prediction(symbol, side):
    """Get prediction based on historical outcomes."""
    outcomes = [o for o in _ICE_OUTCOMES[symbol] if o["side"] == side]
    
    if len(outcomes) < 5:
        return None  # not enough data
    
    moves_30s = [o["outcome_30s"] for o in outcomes if o["outcome_30s"] is not None]
    
    if not moves_30s:
        return None
    
    avg_move = sum(moves_30s) / len(moves_30s)
    wins = sum(1 for m in moves_30s if m > 0)
    win_rate = wins / len(moves_30s)
    
    return {
        "avg_move_30s": round(avg_move, 2),
        "win_rate": round(win_rate * 100, 1),
        "sample_size": len(moves_30s),
        "best": round(max(moves_30s), 2),
        "worst": round(min(moves_30s), 2),
    }
```

### 7.5 Frontend — Prediction Badge

```javascript
// If prediction data available, show below the main badge
if (ice.prediction) {
    const pred = ice.prediction;
    const predText = `+${pred.avg_move_30s} / ${pred.win_rate}% (n=${pred.sample_size})`;
    ctx.font = '8px monospace';
    ctx.fillStyle = pred.win_rate > 60 ? '#00e676' : '#ffab00';
    ctx.fillText(predText, x, y + ds + 30);
}
```

---

## PART 8: OPTIONS CHAIN FUSION

### 8.1 NQ Equivalent Price Column

Map each QQQ strikes to NQ equivalent using the ratio:
```
nq_price = strike_price × (NQ_current / QQQ_current)

Example:
  NQ = 25200, QQQ = 503.50
  ratio = 25200 / 503.50 = 50.05
  
  QQQ strike 600 → NQ equivalent = 600 × 50.05 = 30,030
  QQQ strike 500 → NQ equivalent = 500 × 50.05 = 25,025
  QQQ strike 503 → NQ equivalent = 503 × 50.05 = 25,175
```

Display as new column "NQ$" in the options chain.

### 8.2 Put/Call Ratio Column

```
pc_ratio = put_volume / max(call_volume, 1)

Display:
  < 0.5  → "🟢" (very bullish — more calls than puts)
  0.5-0.8 → "🟢" (bullish)
  0.8-1.2 → "─" (neutral)
  1.2-2.0 → "🔴" (bearish)
  > 2.0  → "🔴" (very bearish — more puts than calls)
```

### 8.3 GEX (Gamma Exposure) Column

```
FORMULA:
  GEX = gamma × open_interest × spot_price² × contract_multiplier × 0.01

For QQQ options:
  contract_multiplier = 100 (each option = 100 shares)
  
  GEX per strike = gamma × OI × (QQQ_price)² × 100 × 0.01

Approximation using IV (when gamma not directly available):
  gamma ≈ (1 / (IV × spot × √(DTE/365))) × N(d1)
  where N(d1) is the standard normal PDF at d1
  
Simplified for 0DTE:
  gamma ≈ 1 / (IV × spot × √(1/365))
```

### 8.4 Row Highlighting on Iceberg Match

```javascript
// When rendering options chain rows:
for (const strike of strikes) {
    const nqEquiv = strike * nqQqqRatio;
    const hasIceberg = activeIcebergs.some(ice => 
        Math.abs(ice.price - nqEquiv) < tickSize * 5
    );
    
    if (hasIceberg) {
        // Highlight entire row with green/red glow
        row.style.background = hasIceberg.side === 'b'
            ? 'rgba(0, 230, 118, 0.15)'
            : 'rgba(255, 23, 68, 0.15)';
        
        // Add iceberg icon
        row.querySelector('.ice-col').textContent = '🧊';
    }
}
```

### 8.5 Gamma Wall Lines on Chart

```javascript
// Draw horizontal lines at strikes with highest absolute GEX
const topGexStrikes = gexData
    .sort((a, b) => Math.abs(b.gex) - Math.abs(a.gex))
    .slice(0, 5);

for (const strike of topGexStrikes) {
    const nqPrice = strike.strike * nqQqqRatio;
    const y = priceToY(nqPrice);
    
    ctx.strokeStyle = strike.gex > 0
        ? 'rgba(0, 150, 255, 0.4)'   // blue for positive gamma (support)
        : 'rgba(255, 100, 0, 0.4)';   // orange for negative gamma (resistance)
    ctx.lineWidth = 1.5;
    ctx.setLineDash([8, 4]);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(chartWidth, y);
    ctx.stroke();
    ctx.setLineDash([]);
    
    // Label
    ctx.font = '9px monospace';
    ctx.fillText(`GEX ${strike.gex > 0 ? '+' : ''}${(strike.gex/1e6).toFixed(1)}M`, 
                 chartWidth - 100, y - 3);
}
```

### 8.6 Gamma-Backed Iceberg Flag

```python
def _check_gamma_backing(symbol, iceberg_price, options_data):
    """Check if iceberg is at a high-GEX strike."""
    if not options_data:
        return False, 0
    
    nq_qqq_ratio = get_nq_qqq_ratio()
    qqq_equiv = iceberg_price / nq_qqq_ratio
    
    # Find nearest strike
    nearest_strike = min(options_data.keys(), 
                         key=lambda s: abs(float(s) - qqq_equiv))
    distance = abs(float(nearest_strike) - qqq_equiv)
    
    if distance > 1.0:  # more than $1 from nearest strike
        return False, 0
    
    strike_data = options_data[nearest_strike]
    gex = strike_data.get("gex", 0)
    
    if abs(gex) > 500000:  # $500K+ GEX
        return True, gex
    
    return False, 0
```

Frontend badge addition:
```javascript
if (ice.gamma_backed) {
    badgeText += ' 🛡️';  // shield = gamma-backed, very strong
}
```

### 8.7 Fusion Signal Alert

When an iceberg is detected at a high-GEX level, emit a special signal:
```python
fusion_signal = {
    "type": "gamma_iceberg_fusion",
    "iceberg": iceberg_result,
    "gex": gex_value,
    "strike": nearest_strike,
    "nq_price": iceberg_price,
    "verdict": "wall_will_hold" if gex > 0 else "wall_may_break",
    "message": f"GAMMA-BACKED {'BUY' if side == 'b' else 'SELL'} WALL at {iceberg_price}"
               f" | GEX: ${gex/1e6:.1f}M | Strike {nearest_strike}"
               f" | {'MM support = wall holds' if gex > 0 else 'Negative gamma = volatile'}"
}
```

---

## PART 9: RAM BUDGET AND PERFORMANCE

### 9.1 Per-Feature RAM Breakdown

```
FEATURE                          DATA STRUCTURE            RAM
─────────────────────────────────────────────────────────────
v3 (LIVE):
  _TRADE_SIZE_HISTORY            500 ints/symbol           ~4 KB
  _ICE_PRICE_HISTORY             200 tuples/symbol         ~3.2 KB
  _ICE_TRACKER                   varies                    ~10 KB

DOM Cross-Validation:
  _DOM_SNAPSHOT_PREV/CURR        ~20 price levels          ~2 KB
  _DOM_REFILL_COUNT              counter dict              ~1 KB

Inter-Fill Timing:
  (no new state)                 computed inline           ~0

Completion Countdown:
  (no new state)                 computed from existing    ~0

Drifting Iceberg:
  _DRIFT_TRACKER                 100 fills × 2 sides      ~5 KB
  _DOM_BAND_DEPTH                summary dict              ~0.5 KB

Level Memory Heatmap:
  _ICE_LEVEL_MEMORY              ~200 levels/symbol        ~5 MB

Post-Iceberg Prediction:
  _ICE_OUTCOMES                  100 outcomes/symbol       ~8 MB
  _ICE_PENDING                   ~10 pending               ~1 KB

Options Fusion:
  (uses existing options data)   computed inline           ~0

─────────────────────────────────────────────────────────────
TOTAL NEW RAM:                                             ~13 MB
─────────────────────────────────────────────────────────────

Current system: 15 GB
After all features: ~15.013 GB
Headroom to 20 GB: ~5 GB free

VERDICT: ALL FEATURES FIT EASILY ✅
```

### 9.2 CPU Impact

All computations are O(n) where n = number of fills in window (typically < 50).
No sorting beyond Python's built-in sort. No matrix operations.
No network calls. No file I/O.

Estimated per-trade overhead: < 50 microseconds total for ALL features.
At 100 trades/second peak: ~5ms total CPU per second. Negligible.

### 9.3 Cleanup

Prune old data every 60 seconds:
```python
def _cleanup_ice_state(symbol, current_ts):
    """Remove stale data to prevent unbounded growth."""
    # Prune fill tracker (oldest window)
    max_window = _ICE_WINDOWS[-1][0]
    cutoff = current_ts - max_window
    for price_str in list(_ICE_TRACKER[symbol].keys()):
        fills = _ICE_TRACKER[symbol][price_str]
        while fills and fills[0][0] < cutoff:
            fills.pop(0)
        if not fills:
            del _ICE_TRACKER[symbol][price_str]
    
    # Prune drift tracker
    for side in ["b", "s"]:
        tracker = _DRIFT_TRACKER[symbol][side]
        while tracker and tracker[0][0] < current_ts - _DRIFT_WINDOW_SEC:
            tracker.popleft()
    
    # Prune level memory (remove levels older than 4 hours)
    for price_str in list(_ICE_LEVEL_MEMORY[symbol].keys()):
        mem = _ICE_LEVEL_MEMORY[symbol][price_str]
        if current_ts - mem["last_ts"] > 14400:  # 4 hours
            del _ICE_LEVEL_MEMORY[symbol][price_str]
```

---

## PART 10: BUILD ORDER AND DEPENDENCIES

### Priority Ranked Build Order

```
PHASE 1: ACCURACY (no new frontend, backend only)
  ├── #1 DOM Cross-Validation         15 min   depends: existing DOM data
  ├── #2 Inter-Fill Timing            10 min   depends: nothing
  └── #3 Completion Countdown         10 min   depends: nothing

PHASE 2: INTELLIGENCE (backend + frontend)  
  ├── #4 Drifting Iceberg (3 layers)  30 min   depends: #1 for DOM layer
  └── #5 Level Memory Heatmap         20 min   depends: nothing

PHASE 3: PREDICTION (backend + frontend)
  └── #6 Post-Iceberg Prediction      30 min   depends: #3 for completion

PHASE 4: OPTIONS FUSION (backend + frontend)
  ├── #7 NQ$ + P/C + GEX columns     15 min   depends: options data
  ├── #8 Gamma wall lines             10 min   depends: #7
  └── #9 Gamma-backed iceberg flag     5 min   depends: #7 + #8
```

### Dependency Graph

```
                    ┌─────────────┐
                    │ DOM Cross-  │
                    │ Validation  │──────┐
                    └──────┬──────┘      │
                           │             │
                    ┌──────▼──────┐      │
                    │ Inter-Fill  │      │
                    │ Timing      │      │
                    └──────┬──────┘      │
                           │             │
                    ┌──────▼──────┐  ┌───▼──────────┐
                    │ Completion  │  │ Drifting      │
                    │ Countdown   │  │ Iceberg       │
                    └──────┬──────┘  │ (3 layers)    │
                           │         └───────────────┘
                    ┌──────▼──────┐
                    │ Post-Ice    │
                    │ Prediction  │
                    └─────────────┘
                    
        ┌──────────────┐
        │ Level Memory │  (independent)
        │ Heatmap      │
        └──────────────┘
        
        ┌──────────────┐
        │ Options      │
        │ Fusion (A+B) │  (independent)
        └──────────────┘
```

### Integration Points

All features integrate through the same return dict from `_detect_iceberg()`:
```python
return {
    # v3 (existing):
    "clips", "est_total", "est_hidden", "avg_clip", "cv",
    "confidence", "side", "zone", "zone_levels",
    "decay", "slope", "absorbing", "size_rank",
    "urgency", "pressure", "fill_pct", "est_remaining_sec",
    
    # NEW — DOM cross-validation:
    "dom_confirmed",      # "confirmed"/"likely"/"possible"/"unconfirmed"
    "dom_refills",        # count of DOM-confirmed refills
    
    # NEW — Inter-fill timing:
    "gap_cv",             # CV of inter-fill gaps
    "timing",             # "algo_confirmed"/"algo_likely"/"mixed"/"random"
    
    # NEW — Completion countdown:
    "depletes_in_sec",    # estimated seconds until empty
    "state",              # "fresh"/"active"/"depleting"/"critical"/"gone"
    
    # NEW — Level memory:
    "level_ice_count",    # how many icebergs at this level historically
    "level_avg_size",     # average historical iceberg size here
    
    # NEW — Prediction:
    "prediction",         # {"avg_move_30s": +6.2, "win_rate": 85, "n": 23}
    
    # NEW — Options fusion:
    "gamma_backed",       # True if at high-GEX strike
    "gex_value",          # GEX at this level in dollars
}
```

### Frontend Badge Final Format

```
Full badge with all features:

  ◆  45/~520est                    ← visible / hidden estimate
  Z:BUY WALL⬇ ✓DOM 🛡️            ← zone + pressure + decay + DOM + gamma
  ████████████░░░ 72%              ← fill progress
  Depletes ~12s                    ← countdown
  +6.2 / 85% win (n=23)           ← prediction
  ICE x7 at this level            ← level memory
```

---

## END OF MASTER PROMPT

Total features to build: 9
Total estimated build time: ~2.5 hours
Total additional RAM: ~13 MB
Files to modify: l2_worker.py, volume_bubbles.js, options chain renderer

This document contains EVERY formula, EVERY data structure, EVERY edge case,
and EVERY integration point needed to build the complete elite-level
iceberg detection system. Nothing is missing.

Build it exactly as specified above.

---

## APPENDIX A: MISSING SECTIONS (Gap Fixes)

These sections fill gaps identified in the TOC that had no body content.

---

### A.1 DOM Cross-Validation — Edge Cases (was 2.5)

```
EDGE CASE 1: DOM update arrives LATE (stale snapshot)
  Problem: DOM snapshot is 200ms old, trade happened 50ms ago.
  The "after" snapshot may not yet reflect the refill.
  
  Solution: Allow a 1-snapshot grace period. If the current snapshot
  doesn't show a refill, check the NEXT snapshot too. Store pending
  validations:
  
  _DOM_PENDING_VALIDATION[symbol][price_str] = {
      "trade_vol": volume,
      "dom_before": dom_before,
      "ts": timestamp,
      "ttl": 2  # check next 2 DOM updates
  }
  
  On each DOM update, resolve pending validations.

EDGE CASE 2: Multiple trades at same price between DOM updates
  Problem: 3 trades of 5 contracts each print at 25200 between
  two DOM snapshots. Total fill = 15, but we only see one
  "before→after" change.
  
  Solution: Aggregate fills between DOM updates:
  
  _DOM_FILLS_PENDING[symbol][price_str] += volume  # accumulate
  
  When DOM updates:
    total_fills = _DOM_FILLS_PENDING[symbol][price_str]
    expected = max(0, dom_before - total_fills)
    refill = dom_after - expected
    _DOM_FILLS_PENDING[symbol][price_str] = 0  # reset

EDGE CASE 3: New limit orders from OTHER participants
  Problem: Someone else places a new limit order at 25200 between
  DOM snapshots. This looks like a "refill" but it is not the iceberg.
  
  Solution: Cross-reference with tape. If a refill is detected
  but there was NOT a corresponding fill at that price in the same
  window, it is a new order, not a refill. Only count refills that
  occur WITHIN 500ms of a fill at the same price.

EDGE CASE 4: Price level disappears entirely then reappears
  Problem: dom_before = 15, trade fills all 15, dom_after = 0.
  Next DOM update: dom = 12 (iceberg refilled after a gap).
  
  Solution: Track "recently emptied" levels:
  
  _DOM_RECENTLY_EMPTIED[symbol][price_str] = {
      "ts": timestamp,
      "last_fill_vol": volume,
  }
  
  If a level reappears within 2 seconds of being emptied,
  AND there was a fill at that price, count as iceberg refill.

EDGE CASE 5: Partial fills (trade volume < showing size)
  Problem: DOM shows 15, trade prints 3, DOM shows 12.
  Expected: 15 - 3 = 12. Actual: 12. Refill = 0.
  But the iceberg might have refilled 3 and then 3 more were taken.
  
  Solution: For partial fills, only flag as refill if
  dom_after > expected_remaining by a meaningful amount (> 1 contract).
  Don't flag partial fills where dom_after == expected as icebergs.
```

---

### A.2 DOM Cross-Validation — Test Scenarios (was 2.9)

```
TEST 1: Basic iceberg (should CONFIRM)
  Setup: DOM bid=20 at 25200
  Action: Trade 15 at 25200
  DOM after: bid=18 at 25200
  Expected: refill = 18 - max(0, 20-15) = 18 - 5 = 13
  Result: dom_confidence = "confirmed" (13/15 = 0.87 > 0.8) ✅

TEST 2: Normal fill (should NOT confirm)
  Setup: DOM bid=20 at 25200
  Action: Trade 15 at 25200
  DOM after: bid=5 at 25200
  Expected: refill = 5 - max(0, 20-15) = 5 - 5 = 0
  Result: dom_confidence = "unconfirmed" ✅

TEST 3: Full consumption (should NOT confirm)
  Setup: DOM bid=10 at 25200
  Action: Trade 10 at 25200
  DOM after: bid=0 at 25200
  Expected: refill = 0 - max(0, 10-10) = 0 - 0 = 0
  Result: dom_confidence = "unconfirmed" ✅

TEST 4: Varying show size iceberg (should CONFIRM)
  Setup: DOM bid=12 at 25200
  Action: Trade 10 at 25200
  DOM after: bid=8 at 25200
  Expected: refill = 8 - max(0, 12-10) = 8 - 2 = 6
  Result: dom_confidence = "likely" (6/10 = 0.6, > 0.3) ✅

TEST 5: Small refill (should mark POSSIBLE)
  Setup: DOM bid=20 at 25200
  Action: Trade 15 at 25200
  DOM after: bid=6 at 25200
  Expected: refill = 6 - max(0, 20-15) = 6 - 5 = 1
  Result: dom_confidence = "possible" (1/15 = 0.07, > 0.0) ✅

TEST 6: New order from someone else (should handle)
  Setup: DOM bid=20 at 25200
  Action: NO trade at 25200 (trade was at 25199.75)
  DOM after: bid=30 at 25200
  Expected: No fill at this price → new limit order, NOT iceberg
  Result: Skip validation (no fill to cross-reference) ✅

TEST 7: Multiple fills between DOM updates
  Setup: DOM bid=50 at 25200
  Action: Trade 10 + Trade 8 + Trade 12 = 30 total at 25200
  DOM after: bid=40 at 25200
  Expected: refill = 40 - max(0, 50-30) = 40 - 20 = 20
  Result: dom_confidence = "likely" (20/30 = 0.67) ✅
```

---

### A.3 Inter-Fill Timing — Test Scenarios (was 3.6)

```
TEST 1: Perfect algo timing (should mark algo_confirmed)
  Fills at: t=0, t=2.0, t=4.0, t=6.0, t=8.0
  Gaps: [2.0, 2.0, 2.0, 2.0]
  Gap CV: 0 / 2.0 = 0.0
  Result: timing = "algo_confirmed" ✅

TEST 2: Realistic algo with jitter (should mark algo_confirmed)
  Fills at: t=0, t=1.8, t=3.5, t=5.2, t=7.1, t=8.9
  Gaps: [1.8, 1.7, 1.7, 1.9, 1.8]
  Mean: 1.78, Stddev: 0.07
  Gap CV: 0.07 / 1.78 = 0.039
  Result: timing = "algo_confirmed" ✅

TEST 3: Random fills (should mark random)
  Fills at: t=0, t=0.05, t=12.3, t=12.4, t=58.1
  Gaps: [0.05, 12.25, 0.1, 45.7]
  Mean: 14.53, Stddev: 19.2
  Gap CV: 19.2 / 14.53 = 1.32
  Result: timing = "random" ✅

TEST 4: Mixed timing (should mark mixed)
  Fills at: t=0, t=1.5, t=5.0, t=6.2, t=8.0
  Gaps: [1.5, 3.5, 1.2, 1.8]
  Mean: 2.0, Stddev: 0.93
  Gap CV: 0.93 / 2.0 = 0.465
  Result: timing = "mixed" (between 0.3 and 0.6... wait, 0.465 < 0.6)
  Actually: timing = "algo_likely" ✅

TEST 5: Only 2 fills (insufficient data)
  Fills at: t=0, t=3.0
  Gaps: [3.0]
  Result: timing = "insufficient" (need >= 3 fills) ✅
```

---

### A.4 Completion Countdown — Wall Gone Detection (was missing from 4.3)

```python
# ── Wall Gone Detection State ──
# {symbol: {price_str: {"last_refill_ts": float, "was_active": bool}}}
_ICE_WALL_STATE: dict = defaultdict(lambda: defaultdict(lambda: {
    "last_refill_ts": 0, "was_active": False, "gone_announced": False
}))

_ICE_GONE_TIMEOUT = 3.0  # seconds without refill = wall gone

def _check_wall_gone(symbol, current_ts):
    """Check if any active icebergs have stopped refilling."""
    alerts = []
    
    for price_str, state in _ICE_WALL_STATE[symbol].items():
        if not state["was_active"]:
            continue
        
        time_since_refill = current_ts - state["last_refill_ts"]
        
        if time_since_refill >= _ICE_GONE_TIMEOUT and not state["gone_announced"]:
            state["gone_announced"] = True
            state["was_active"] = False
            
            alerts.append({
                "type": "wall_gone",
                "price": price_str,
                "ts": current_ts,
                "message": f"WALL GONE at {price_str} — no refill for {_ICE_GONE_TIMEOUT}s",
                "signal": "entry_trigger",  # this is when you enter the trade
            })
    
    return alerts

def _update_wall_state(symbol, price_str, timestamp):
    """Called whenever an iceberg fill is detected."""
    state = _ICE_WALL_STATE[symbol][price_str]
    state["last_refill_ts"] = timestamp
    state["was_active"] = True
    state["gone_announced"] = False
```

Frontend rendering for "WALL GONE":
```javascript
// When wall_gone alert received:
if (ice.state === 'gone') {
    // Green checkmark badge
    ctx.font = BUBBLE_CONFIG.FONT_BADGE;
    ctx.fillStyle = '#00e676';
    ctx.fillText('WALL GONE ✅', x, y + ds + 8);
    
    // Flash effect: bright pulse for 5 seconds then fade out
    const timeSinceGone = (performance.now() - ice.gone_ts) / 1000;
    if (timeSinceGone < 5) {
        const flashAlpha = 0.8 * (1 - timeSinceGone / 5);
        const flashGrad = ctx.createRadialGradient(x, y, 0, x, y, ds * 3);
        flashGrad.addColorStop(0, `rgba(0, 230, 118, ${flashAlpha})`);
        flashGrad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = flashGrad;
        ctx.beginPath();
        ctx.arc(x, y, ds * 3, 0, Math.PI * 2);
        ctx.fill();
    }
}
```

---

### A.5 Drifting Iceberg — Frontend Band Overlay (was 5.7)

```javascript
// ════════════════════════════════════════════════════════════════
// DRIFTING ICEBERG: Draw band overlay on chart
// ════════════════════════════════════════════════════════════════

function drawDriftingIceberg(ctx, drift, priceToY, chartWidth) {
    if (!drift || drift.type !== 'drifting') return;
    
    const yTop = priceToY(drift.band_high);
    const yBottom = priceToY(drift.band_low);
    const bandHeight = yBottom - yTop;
    
    // Side color: green for buy, red for sell
    const baseColor = drift.side === 'b'
        ? [0, 230, 118]    // green
        : [255, 23, 68];   // red
    
    // Semi-transparent band across the full chart width
    const alpha = drift.drift_confidence === 'confirmed' ? 0.12
        : drift.drift_confidence === 'likely' ? 0.08
        : 0.04;
    
    ctx.fillStyle = `rgba(${baseColor.join(',')}, ${alpha})`;
    ctx.fillRect(0, yTop, chartWidth, bandHeight);
    
    // Dashed border on top and bottom of band
    ctx.strokeStyle = `rgba(${baseColor.join(',')}, ${alpha * 3})`;
    ctx.lineWidth = 1;
    ctx.setLineDash([6, 3]);
    ctx.beginPath();
    ctx.moveTo(0, yTop);
    ctx.lineTo(chartWidth, yTop);
    ctx.moveTo(0, yBottom);
    ctx.lineTo(chartWidth, yBottom);
    ctx.stroke();
    ctx.setLineDash([]);
    
    // Label at right edge of band
    const labelY = yTop + bandHeight / 2;
    ctx.font = '10px monospace';
    ctx.textAlign = 'right';
    ctx.fillStyle = `rgba(${baseColor.join(',')}, 0.9)`;
    
    const sideLabel = drift.side === 'b' ? 'STEALTH BUY' : 'STEALTH SELL';
    const confLabel = drift.drift_confidence === 'confirmed' ? '✓✓'
        : drift.drift_confidence === 'likely' ? '✓' : '?';
    
    ctx.fillText(
        `${sideLabel} ${confLabel} | ${drift.fills} fills @ ${drift.prices_hit} prices | ~${drift.total_vol} vol`,
        chartWidth - 10,
        labelY
    );
    
    // Draw small dots at each fill price within the band
    if (drift.fill_prices) {
        for (const fp of drift.fill_prices) {
            const dotY = priceToY(fp.price);
            ctx.fillStyle = `rgba(${baseColor.join(',')}, 0.6)`;
            ctx.beginPath();
            ctx.arc(chartWidth - 20, dotY, 2, 0, Math.PI * 2);
            ctx.fill();
        }
    }
    
    ctx.textAlign = 'center';  // reset
}
```

Visual result:
```
Chart with drifting iceberg band:

  25205 ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   (dashed border)
  █████████████████████████████████████████████   
  ██ translucent green band across full width ██  STEALTH BUY ✓✓ | 6 fills @ 5 prices | ~60 vol
  █████████████████████████████████████████████   
  25195 ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   (dashed border)
  
  Small dots at each fill price within the band.
  Band opacity increases with confidence level.
```

---

### A.6 Level Memory — Session Persistence (was 6.7)

```python
import json
import os

_ICE_LEVEL_FILE = "data/ice_level_memory.json"

def _save_level_memory():
    """Save level memory to disk for cross-session persistence."""
    data = {}
    for symbol, levels in _ICE_LEVEL_MEMORY.items():
        data[symbol] = {}
        for price_str, mem in levels.items():
            data[symbol][price_str] = {
                "count": mem["count"],
                "total_vol": mem["total_vol"],
                "last_side": mem["last_side"],
                "last_ts": mem["last_ts"],
                "avg_size": mem["avg_size"],
            }
    
    os.makedirs(os.path.dirname(_ICE_LEVEL_FILE), exist_ok=True)
    with open(_ICE_LEVEL_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _load_level_memory():
    """Load level memory from disk on startup."""
    if not os.path.exists(_ICE_LEVEL_FILE):
        return
    
    try:
        with open(_ICE_LEVEL_FILE, "r") as f:
            data = json.load(f)
        
        for symbol, levels in data.items():
            for price_str, mem in levels.items():
                _ICE_LEVEL_MEMORY[symbol][price_str].update(mem)
        
        print(f"[ICE] Loaded level memory: {sum(len(v) for v in data.values())} levels")
    except Exception as e:
        print(f"[ICE] Failed to load level memory: {e}")

# Save every 5 minutes (called from main loop cleanup)
_LEVEL_MEMORY_LAST_SAVE = 0
_LEVEL_MEMORY_SAVE_INTERVAL = 300  # 5 minutes

def _maybe_save_level_memory(current_ts):
    global _LEVEL_MEMORY_LAST_SAVE
    if current_ts - _LEVEL_MEMORY_LAST_SAVE >= _LEVEL_MEMORY_SAVE_INTERVAL:
        _save_level_memory()
        _LEVEL_MEMORY_LAST_SAVE = current_ts
```

Startup integration:
```python
# In the worker startup / initialization:
_load_level_memory()  # restore from last session
```

File format (data/ice_level_memory.json):
```json
{
  "NQ": {
    "25200.00": {
      "count": 7,
      "total_vol": 2450,
      "last_side": "b",
      "last_ts": 1710732000.0,
      "avg_size": 350.0
    },
    "25250.00": {
      "count": 3,
      "total_vol": 2400,
      "last_side": "s",
      "last_ts": 1710731500.0,
      "avg_size": 800.0
    }
  }
}
```

---

### A.7 Post-Iceberg Prediction — Minimum Sample Size (was 7.7)

```
MINIMUM SAMPLE SIZE RULES:

The prediction system requires a minimum number of completed outcomes
before it starts showing predictions. This prevents misleading statistics
from tiny sample sizes.

Thresholds:
  n < 5     → No prediction shown at all
                "Insufficient data" — badge is blank
  
  n = 5-9   → Show prediction with LOW confidence warning
                Badge: "+4.2 / 72% (n=7) ⚠️"
                The ⚠️ warns trader that sample is small
  
  n = 10-19 → Show prediction with MEDIUM confidence  
                Badge: "+5.8 / 81% (n=14)"
                No warning, but no "reliable" label either
  
  n >= 20   → Show prediction with HIGH confidence
                Badge: "+6.2 / 85% (n=23) ✓"
                The ✓ indicates statistically meaningful sample

Statistical note:
  With n=5, a 100% win rate could easily be luck (p=0.03 one-tail).
  With n=20, an 85% win rate is statistically significant (p<0.001).
  We use n=20 as the threshold for "reliable" because:
    - Binomial test: 85% win rate at n=20 → p=0.0002 (very unlikely by chance)
    - Provides enough data to see both winning and losing scenarios
    - Balances speed (don't wait too long) with reliability

Code:
```python
def _get_prediction(symbol, side):
    outcomes = [o for o in _ICE_OUTCOMES[symbol] if o["side"] == side]
    
    if len(outcomes) < 5:
        return None  # not enough data, show nothing
    
    moves_30s = [o["outcome_30s"] for o in outcomes
                 if o["outcome_30s"] is not None]
    
    if not moves_30s:
        return None
    
    avg_move = sum(moves_30s) / len(moves_30s)
    wins = sum(1 for m in moves_30s if m > 0)
    win_rate = wins / len(moves_30s)
    n = len(moves_30s)
    
    # Confidence tier
    if n >= 20:
        pred_confidence = "high"
    elif n >= 10:
        pred_confidence = "medium"
    else:
        pred_confidence = "low"
    
    return {
        "avg_move_30s": round(avg_move, 2),
        "win_rate": round(win_rate * 100, 1),
        "sample_size": n,
        "pred_confidence": pred_confidence,
        "best": round(max(moves_30s), 2),
        "worst": round(min(moves_30s), 2),
        "median": round(sorted(moves_30s)[n // 2], 2),
    }
```

Frontend badge rendering:
```javascript
if (ice.prediction) {
    const pred = ice.prediction;
    const sign = pred.avg_move_30s >= 0 ? '+' : '';
    let predText = `${sign}${pred.avg_move_30s} / ${pred.win_rate}% (n=${pred.sample_size})`;
    
    // Add confidence indicator
    if (pred.pred_confidence === 'high') {
        predText += ' ✓';
    } else if (pred.pred_confidence === 'low') {
        predText += ' ⚠️';
    }
    
    ctx.font = '8px monospace';
    ctx.fillStyle = pred.win_rate > 65
        ? '#00e676'   // green = profitable
        : pred.win_rate > 50
        ? '#ffab00'   // yellow = marginal
        : '#ff1744';  // red = losing (should stop trading this pattern)
    ctx.fillText(predText, x, y + ds + 30);
}
```

---

### A.8 Opposition Volume / Absorption Ratio (was missing from Part 1)

These fields are in the v3 return dict but were not documented in Part 1:

```
OPPOSITION VOLUME:
  The total volume that traded AGAINST the iceberg during the window.
  If the iceberg is buying (side="b"), opposition_vol = total sell volume
  at or near the iceberg price during the same window.

MATH:
  opposition_vol = sum of all trades on the OPPOSITE side within
                   the iceberg's zone during the detection window
  
  For a buy iceberg at 25200 (zone: 25199.50-25200.50):
    All sells in zone during window: [5, 8, 12, 3, 7] = 35
    opposition_vol = 35

ABSORPTION RATIO:
  How much of the opposing flow the iceberg has absorbed.
  
  absorption_ratio = opposition_vol / max(visible_total, 1)
  
  Example:
    visible_total = 44 (what the iceberg has bought)
    opposition_vol = 35 (what sellers threw at it)
    absorption_ratio = 35 / 44 = 0.80
    
    80% = the iceberg absorbed most of the selling pressure
    If absorption_ratio > 1.0 → more opposition than iceberg fills
                                 (wall may be overwhelmed)

USAGE IN PRESSURE SIGNAL:
  absorption_ratio < 0.5  → iceberg easily absorbing (low opposition)
  absorption_ratio 0.5-1.0 → iceberg being tested (moderate opposition)
  absorption_ratio > 1.0  → iceberg being overwhelmed (may break)

CODE:
```python
# Collect opposition trades during iceberg window
opp_side = "s" if fill_sides[0] == "b" else "b"
opposition_vol = 0

for offset in range(-_ICE_ZONE_TICKS, _ICE_ZONE_TICKS + 1):
    adj_price = round(price_f + offset * tick_size, 2)
    adj_key = str(adj_price)
    adj_fills = _ICE_TRACKER[symbol].get(adj_key, [])
    for ts, vol, s in adj_fills:
        if s == opp_side and ts >= window_cutoff:
            opposition_vol += vol

absorption_ratio = opposition_vol / max(visible_total, 1)
```

Added to return dict:
```python
return {
    ...
    "opposition_vol": opposition_vol,
    "absorption_ratio": round(absorption_ratio, 2),
    ...
}
```

---

## END OF APPENDIX — ALL 7 GAPS FILLED

Summary of additions:
  A.1: DOM edge cases (stale snapshots, multiple fills, new orders, empty→reappear, partial fills)
  A.2: DOM test scenarios (7 test cases with expected inputs/outputs)
  A.3: Inter-fill timing test scenarios (5 test cases)
  A.4: Wall Gone detection (state machine + frontend flash)
  A.5: Drifting iceberg frontend band overlay (full JavaScript rendering)
  A.6: Level memory session persistence (JSON save/load + auto-save every 5 min)
  A.7: Post-iceberg prediction minimum sample size (statistical justification + tiered confidence)
  A.8: Opposition volume and absorption ratio (full math + code)

Total document is now complete. Zero gaps remaining.
