---
description: Coding standards and rules for the War Machine trading terminal
---

# War Machine Trading Terminal — Coding Standards

## Design Philosophy
Hedge fund-grade terminal. Clean, institutional, premium. No noise.

## Volume Bubble Logic (volume_bubbles.js)

### How Bubbles Work
- Backend sends `bp` (bubble profile) per candle: `{price: [buyVol, sellVol]}`
- ALL thresholds are σ-adaptive using **log-transform StdDev** — ZERO fixed contract numbers
- Log-transform prevents one whale print from breaking the scale
- Thresholds recalculate every frame based on visible candles

### σ Thresholds (from code — no fixed numbers)
- `1.0σ` = absorption context level
- `1.5σ` = unusual (top ~7% of prints) — minimum to render a bubble
- `3.0σ` = extreme outlier / institutional (top ~0.1%)
- `0.5σ` = minimum volume for high-dominance bypass (90%+ one-sided)

### What Makes a Bubble Appear (conviction × consistency × one-sidedness)
- **NOT just big prints** — a 20-lot at 95% buy dominance clustered at a defended price is more tradeable than a random 300-lot
- Bubbles show **conviction patterns**, not raw size:
  1. 🟣 **Absorption** (purple) — both sides 35%+ at same price, 1σ+ vol. Battle happening.
  2. 🟢🔴 **Aggression** (green/red) — 80%+ one-sided dominance, 1.5σ+ vol. Conviction showing.
  3. 🔵 **Cluster lines** — same price hit 3+ candles, volume accelerating. Defended level.
  4. **Institutional glow** — 3σ+ prints get a glow ring (adaptive, not fixed contracts)

### Opacity = σ² Exponential Curve
- Below 1.5σ → don't render at all (hard cutoff, not dim)
- 1.5σ → visible
- 2σ → stands out
- 3σ → pops
- 4σ → maximum presence

### Radius = σ-Based Scaling
- `sigmaRatio = (sigmaDistance / 4)^1.5`
- 3px minimum → 24px maximum
- Size tells the story — no text labels needed on chart

## Detection Logic (l2_worker.py)

### Iceberg v4 — 30+ fields, 6 elite features
- Zone detection (±2 ticks), adaptive clip floor (50% rolling average)
- Tiered windows: 5s=high, 15s=medium, 60s=low
- CV < 0.35 for clip consistency
- Elite: DOM cross-validation, inter-fill timing, completion countdown
- Fill exhaustion (linear regression slope), absorption, size rank (σ distance)
- Level memory, post-iceberg predictions (+10/30/60s outcome tracking)

### Drifting Iceberg — 3-Layer Detection
- **Layer 1 (Behavioral):** Same-side fills across ALL prices, clip CV < 0.40, 5+ fills, 3+ prices, 30s window
- **Layer 2 (DOM Depth):** Total depth barely drops despite fills. depth_leak_ratio > 0.5 = confirmed
- **Layer 3 (Timing):** Inter-fill gap CV < 0.3 = algo regularity
- Composite score 0-6. ≥4=confirmed, ≥2=likely, <2=possible

### Other Detections
- **Sweep:** 200ms window, 3+ levels, 30+ vol
- **Spoof:** DOM diff, 100+ size, vanishes within 3s, 2+ occurrences
- **Ignition:** 8 trades/2s, small clips, reversal check at 30s
- **Delta Divergence:** 20-candle lookback, price vs cumulative delta
- **Wall Gone:** 3s without iceberg refill

## Coding Rules
- ALL thresholds use σ (standard deviation) — NEVER fixed contract numbers
- Fix completely in one shot — check actual backend data before writing frontend filters
- When user raises an issue or improvement, IMPLEMENT IT — don't ask permission
- Always question your own thresholds: "does this cutoff make sense for the signal type?"
- Answer with CLARITY — read the actual code before answering, never guess or make up numbers
- When in doubt, make it cleaner and more minimal

## Architecture
- Backend: `l2_worker.py` (Python) — all detection logic
- Frontend: `volume_bubbles.js` (JavaScript) — Canvas 2D rendering
- Server: `server.py` (Python) — WebSocket pipeline
- Process manager: PM2 (`pm2 restart war-machine`)

// turbo-all
