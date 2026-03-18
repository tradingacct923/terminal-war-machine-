# Iceberg Detection — Full Research & Architecture Doc

> Saved from conversation on Mar 18 2026.  
> All math, logic, planned features, and design decisions captured here.

---

## Table of Contents

1. [What's Been Built (v3 — LIVE)](#1-whats-been-built-v3--live)
2. [The Math Behind Every Feature](#2-the-math-behind-every-feature)
3. [Elite Features — Planned (Not Yet Built)](#3-elite-features--planned)
4. [Options Chain Fusion — Planned](#4-options-chain-fusion--planned)
5. [Drifting Iceberg Detection — Planned](#5-drifting-iceberg-detection--planned)
6. [RAM Budget](#6-ram-budget)
7. [Build Priority Order](#7-build-priority-order)

---

## 1. What's Been Built (v3 — LIVE)

**Commit:** `053b386`  
**Files:** `l2_worker.py`, `volume_bubbles.js`

### Backend: 11 Intelligence Fields

| Field | What It Tells You |
|---|---|
| `zone` / `zone_levels` | Multi-level iceberg across ±2 ticks |
| `fill_pct` | How much of the hidden order is filled (0-1) |
| `size_rank` | whale/institutional/professional/retail vs σ |
| `decay` / `slope` | Clips shrinking (exhausting) or growing (strengthening) |
| `absorbing` | Price stable despite fills = wall holding |
| `opposition_vol` / `absorption_ratio` | Volume hitting against the iceberg |
| `urgency` | 0-1 composite of fill speed + size + remaining |
| `est_remaining_sec` | Estimated seconds until iceberg depleted |
| `pressure` | Plain-English signal: bullish_wall / wall_breaking / etc |

### Frontend: Diamond Badge

```
        ◆  30/~360est        ← visible vol / estimated hidden
     Z:BUY WALL⬇             ← zone + pressure + decay arrow
     ████████░░░░             ← fill % progress bar
```

Color coding:
- 🟢 `#00e676` = bullish_wall / bearish_wall (absorbing)
- 🔵 `#00b0ff` = wall_fresh (just appeared)
- 🟡 `#ffab00` = wall_breaking (losing)
- 🟠 `#ff6d00` = wall_exhausted (almost done)

### Constants (l2_worker.py)

```python
_ICE_REFILL_COUNT     = 3       # min refills to trigger
_ICE_CV_THRESHOLD     = 0.35    # max coefficient of variation
_ICE_MIN_CLIP_FLOOR   = 1       # absolute minimum clip
_ICE_ZONE_TICKS       = 2       # ±2 ticks = same zone
_ICE_ABSORB_MAX_MOVE  = 2       # price stability threshold
_ICE_WINDOWS = [
    (5.0,  "high"),    # fast refill
    (15.0, "medium"),  # patient algo
    (60.0, "low"),     # very patient
]
```

---

## 2. The Math Behind Every Feature

### 2.1 Adaptive Min Clip Size

```
trade_history = [last 500 trades]
avg_trade = sum(trade_history) / len(trade_history)
min_clip = max(1, int(avg_trade × 0.5))

Night session example:  avg=2.1 → min_clip=1
NY Open example:        avg=18.5 → min_clip=9
```

### 2.2 Coefficient of Variation (CV)

```
vols = [10, 15, 10, 15, 12]
mean = 12.4
variance = Σ(v - mean)² / n = 5.04
stddev = √5.04 = 2.245
CV = stddev / mean = 2.245 / 12.4 = 0.181

0.181 < 0.35 → ICEBERG (consistent clips)
Random noise: CV > 1.0 → REJECTED
```

### 2.3 Zone Detection (±2 Ticks)

```
NQ tick = $0.25
Zone for 25200.00 = [25199.50 ... 25200.50]

Old: tracked per-price → missed multi-tick icebergs
New: groups all fills within ±2 ticks as one iceberg

Trade at 25200.00: 9 (buy)
Trade at 25200.25: 13 (buy)
Trade at 25200.00: 11 (buy)
Trade at 25199.75: 14 (buy)
→ Zone iceberg: 4 fills, CV=0.163 ✅
```

### 2.4 Fill Exhaustion (Linear Regression Slope)

```
Fills: [(t=0s, vol=15), (t=3s, vol=14), (t=7s, vol=12), (t=12s, vol=8)]

Normalize time to [0, 1]:
xs = [0, 0.25, 0.583, 1.0]
ys = [15, 14, 12, 8]

slope = Σ((x-x̄)(y-ȳ)) / Σ((x-x̄)²) = -3.959 / 0.563 = -7.03
norm_slope = -7.03 / mean(ys) = -0.574

-0.574 < -0.15 → "EXHAUSTING" ⬇ (clips shrinking)
> +0.15         → "STRENGTHENING" ⬆
else            → "HOLDING" ─
```

### 2.5 Absorption Detection

```
recent_prices during iceberg window
price_range = max(prices) - min(prices)
price_range_in_ticks = price_range / tick_size

≤ 2 ticks → absorbing = TRUE (wall holding)
> 2 ticks → absorbing = FALSE (price moving through)
```

### 2.6 Size Rank (σ Distance)

```
trade_history: mean=2.1, std=1.05
iceberg avg_clip = 12.0

sigma = (12.0 - 2.1) / 1.05 = 9.43σ

< 1σ  → "retail"
1-2σ  → "professional"
2-3σ  → "institutional"
3σ+   → "whale" 🐋
```

### 2.7 Urgency Score (0-1 Composite)

```
time_factor     = min(fill_rate / 2.0, 1.0)     × 0.4
size_factor     = min(avg_clip / avg_trade, 1.0) × 0.3
remaining_factor = (1.0 - fill_pct)              × 0.3

urgency = sum of weighted factors

Example: fill_rate=3.5, avg_clip=12, avg_trade=2.1, fill_pct=0.15
→ urgency = 0.955 (very urgent)
```

### 2.8 Pressure Signal (Decision Tree)

```python
if exhausting + fill_pct > 0.5:     → "wall_exhausted"   🟠
elif exhausting + not absorbing:    → "wall_breaking"     🟡
elif absorbing + fill_pct < 0.3:    → "bullish/bearish_wall" 🟢🔴
elif absorbing + fill_pct >= 0.3:   → "bullish/bearish_wall"
elif fill_pct < 0.2:               → "wall_fresh"        🔵
else:                               → "wall_active"
```

### 2.9 Estimated Hidden Size

```
visible_total = 44 contracts
fill_rate = 0.8 fills/sec
avg_clip = 11.0
est_duration = 90 seconds

remaining_fills = 0.8 × (90 - 5) = 68
est_hidden = 44 + (68 × 11.0) = 792 contracts

Display: "44/~792est"
fill_pct = 44 / 792 = 5.6%
```

---

## 3. Elite Features — Planned (Not Yet Built)

### 3.1 DOM Cross-Validation

**Goal:** Confirm icebergs from BOTH tape AND book.

```
Before trade:  DOM bid at 25200 = 12
Trade:         10 sold at 25200
Expected after: 12 - 10 = 2
Actual after:   8

refill_amount = 8 - 2 = +6 → HIDDEN LIQUIDITY REFILLED

Key: Doesn't check if size stays the SAME (icebergs vary show sizes).
     Checks if size is MORE than it should be. Simple subtraction.
     
refill = dom_after - (dom_before - trade_volume)
if refill > 0 → confirmed iceberg

Works even with varying show sizes:
  12 → fill 10 → shows 8  (refilled +6)
  8  → fill 7  → shows 15 (refilled +14)
  15 → fill 12 → shows 10 (refilled +7)
  All different sizes, but refill > 0 every time.
```

**Badge:** `"BUY WALL⬇ ✓DOM"` (DOM-confirmed)

### 3.2 Inter-Fill Timing Analysis

**Goal:** Separate algo-driven refills from random trades.

```
Algo timing:   gaps = [1.2s, 0.8s, 1.5s, 1.1s, 0.9s] → gap_cv = 0.22 (regular)
Random timing: gaps = [0.1s, 8.3s, 0.3s, 45s, 0.02s] → gap_cv = 2.1 (wild)

gap_cv < 0.5 → ALGO-DRIVEN timing (definitely iceberg)
gap_cv > 1.0 → RANDOM timing (probably coincidence)
```

### 3.3 Completion Countdown

**Goal:** Active countdown showing when iceberg depletes.

```
When fill_pct > 0.5:  show "DEPLETES IN ~Xs"
When fill_pct > 0.85: flash badge rapidly
When no refill for 3s: "WALL GONE ✅" → ENTRY SIGNAL
```

### 3.4 Level Memory Heatmap

**Goal:** Remember where icebergs appeared → "known institutional level."

```python
_ICE_LEVEL_HISTORY = {
    "25200.00": {"count": 7, "avg_size": 350, "last_side": "b"},
    "25250.00": {"count": 3, "avg_size": 800, "last_side": "s"},
}

When price approaches a known level:
→ "Approaching known institutional buy level"
→ Even BEFORE iceberg detection triggers
```

### 3.5 Post-Iceberg Price Prediction

**Goal:** Track what happens AFTER icebergs complete → statistical edge.

```python
_ICE_OUTCOMES = [
    {"side": "b", "price": 25200, "move_10s": +3, "move_30s": +8},
    {"side": "b", "price": 25180, "move_10s": +5, "move_30s": +12},
]

After 20+ samples:
  avg_move_after_buy_ice = +6.2 points in 30s
  win_rate = 85%

Badge: "BUY WALL⬇ | +6.2 avg / 85% win"
```

---

## 4. Options Chain Fusion — Planned

### Option A: Quick Columns

| New Column | What It Shows |
|---|---|
| **NQ$** | Strike mapped to NQ equivalent price |
| **P/C** | Put/Call volume ratio (< 0.7 bullish, > 1.3 bearish) |
| **GEX** | Gamma exposure = gamma × OI × spot² × 0.01 |
| **🧊** | Active iceberg detected at this strike's NQ level |

### Option B: Full Fusion

- **Gamma wall lines** on the price chart (horizontal at high-GEX strikes)
- **Iceberg + gamma correlation flag**: `"Z:BUY WALL⬇ 🛡️"` (shield = gamma-backed)
- **Row highlight** when iceberg detected at that strike
- **Fusion signal box:**

```
GAMMA-BACKED ICEBERG at 25200
Options: 24K calls at 603 → MM buying NQ
Tape:    520-lot buy wall → absorbing sells
GEX:     +$2.1M positive gamma
⚡ VERDICT: Wall will HOLD (MM support)
```

---

## 5. Drifting Iceberg Detection — Planned

**Problem:** Smart algos move the iceberg across multiple prices to avoid detection.

```
Fill at 25200.00 → move to 25197.50 → fill → move to 25202.25 → etc.
Our ±2 tick zone misses this. Each price sees only 1 fill.
```

### Layer 1: Behavioral Fingerprint (Ignore Price, Track Behavior)

```
All trades in 30 seconds, SAME SIDE:
  25200.00: BUY 10
  25197.50: BUY 12
  25202.25: BUY 9
  25195.00: BUY 11
  25201.00: BUY 10

Clips: [10, 12, 9, 11, 10] → CV = 0.10 → consistent = ALGO

6 buys with low CV at 5 different prices = drifting iceberg
```

### Layer 2: DOM Total Depth Anomaly

```
Track TOTAL bid depth across a band (e.g., 25195-25205):

Time 0: total_depth=200, fill=10, expected=190, actual=198
Time 1: total_depth=198, fill=12, expected=186, actual=195
Time 2: total_depth=195, fill=9,  expected=186, actual=193

Total fills: 31 contracts removed
Expected depth: 169
Actual depth:   193
Leak: 24 contracts appeared → hidden liquidity in the band

depth_leak_ratio = (193 - 169) / 31 = 0.77
> 0.5 → DRIFTING ICEBERG CONFIRMED
```

### Layer 3: Timing Regularity

```
Random buyers: gaps [0.1s, 45s, 0.02s, 12s] → CV > 1.0
One algo:      gaps [4.2s, 3.8s, 5.1s, 4.5s] → CV < 0.4

Regular timing across random prices = single entity
```

### What They CAN vs CANNOT Fake

| They Can Randomize | They CANNOT Hide |
|---|---|
| ✅ Clip sizes | ❌ That clips have low CV |
| ✅ Prices | ❌ That total DOM depth doesn't drop |
| ✅ Show sizes | ❌ That fills are all same side |
| ✅ Time jitter | ❌ That timing is regular (not Poisson) |
| ✅ Individual orders | ❌ Net flow direction |

---

## 6. RAM Budget

Current Mac Mini: 15 GB running, max 20 GB

| Feature | New RAM | Status |
|---|---:|---|
| **v3 iceberg (live)** | ~3.2 KB | ✅ BUILT |
| DOM cross-validation | ~0 MB | ⬜ PLANNED |
| Inter-fill timing | ~0 MB | ⬜ PLANNED |
| Completion countdown | ~0 MB | ⬜ PLANNED |
| Level memory heatmap | ~5 MB | ⬜ PLANNED |
| Post-iceberg prediction | ~10 MB | ⬜ PLANNED |
| Options columns (NQ$/P/C/GEX) | ~0 MB | ⬜ PLANNED |
| Gamma wall lines | ~0 MB | ⬜ PLANNED |
| Iceberg + gamma flag | ~0 MB | ⬜ PLANNED |
| Drifting iceberg (3 layers) | ~5 MB | ⬜ PLANNED |
| **TOTAL** | **~20 MB** | |

**Impact: 15 GB → 15.02 GB** (negligible)

---

## 7. Build Priority Order

| # | Feature | Impact | Time |
|:---:|---|:---:|:---:|
| 1 | DOM cross-validation | 🔥🔥🔥 | 15 min |
| 2 | Inter-fill timing | 🔥🔥 | 10 min |
| 3 | Completion countdown | 🔥🔥 | 10 min |
| 4 | Drifting iceberg (3 layers) | 🔥🔥🔥🔥 | 30 min |
| 5 | Options fusion (A+B) | 🔥🔥🔥 | 30 min |
| 6 | Level memory heatmap | 🔥🔥🔥 | 20 min |
| 7 | Post-iceberg prediction | 🔥🔥🔥🔥 | 30 min |

**Total: ~2.5 hours**

---

*End of research doc. All features designed, math documented, ready to build.*
