---
description: Coding standards and rules for the War Machine trading terminal
---

# War Machine Trading Terminal — Coding Standards

## RULE #1: ZERO GUESSING. PROVEN DATA ONLY.

This is a $300M NQ market maker terminal competing against other market makers.
**Every single number on screen must be traceable to raw exchange data or computed via proven math.**
If it cannot be proven, it does not go on screen. Period.

### BANNED — Do not even bring to the table:

| Category | Examples | Why |
|---|---|---|
| Hardcoded thresholds | `MIN_VOLUME = 100`, `MIN_TRADES = 15` | Made-up numbers. Use σ-adaptive. |
| Extrapolated values | `est_hidden`, `fill_pct`, `depletes_in_sec` | Guesses dressed as data. |
| Arbitrary labels | "whale", "institutional", "professional" | Arbitrary size buckets. |
| Retail cosmetics | Emojis (🐋🏛👔), vague words ("STEALTH", "TRAP", "FRESH") | Not professional. |
| Unvalidated detections | Any signal that hasn't been backtested or validated | Noise, not signal. |

### ALLOWED — Only these types of values:

| Grade | Type | Example | Status |
|---|---|---|---|
| A | Raw exchange data | Trade price, volume, CME aggressor side, timestamp | ✅ Always show |
| B | Computed from A-grade | CV, delta, notional $, VWAP, absorption ratio | ✅ Always show |
| C | σ-adaptive threshold | `threshold = rolling_mean + 2σ` from live data | ✅ Show with confidence |
| D | Hardcoded threshold | `MIN_TRADES = 15` | ❌ BANNED |
| F | Guess/extrapolation | `est_hidden`, `depletes_in_sec` | ❌ BANNED |

### Before writing ANY code, ask:
1. Where does every number come from? Trace to raw data or remove.
2. Is any threshold hardcoded? Make it σ-adaptive or remove.
3. Would a quant at Citadel trust this? If not, don't show it.
4. Can this be backtested? If not, it's not ready.

## Volume Bubble Logic (volume_bubbles.js)

### How Bubbles Work
- Backend sends `bp` (bubble profile) per candle: `{price: [buyVol, sellVol]}`
- ALL thresholds are σ-adaptive using **log-transform StdDev** — ZERO fixed contract numbers
- Thresholds recalculate every frame based on visible candles

### σ Thresholds (from code — no fixed numbers)
- `1.5σ` = minimum to render (top ~7% of prints)
- `3.0σ` = extreme outlier / institutional (top ~0.1%)
- Opacity = σ² exponential curve. Below 1.5σ → don't render.
- Radius = `(sigmaDistance / 4)^1.5`, 3px min → 24px max

## Detection Logic (l2_worker.py)

### ALL detection thresholds MUST be σ-adaptive
- Iceberg: clip consistency via CV, detection window tiered (5s/15s/60s)
- Sweep: σ-adaptive volume threshold from rolling trade distribution
- Spoof: σ-adaptive size threshold from rolling DOM depth distribution
- Ignition: σ-adaptive trade count from rolling arrival rate distribution
- Divergence: σ-adaptive price move from rolling ATR

### What IS proven (keep):
- CV (coefficient of variation) — pure math
- DOM cross-validation — checks real book
- Linear regression slope on fill sizes — pure math
- Absorption ratio — real opposing volume
- Notional $ — `volume × price × $20` (exact)
- Cumulative delta — sum of real trades
- Level memory — count of events at same price

### What is NOT proven (remove or make σ-adaptive):
- Any `MIN_*` or `MAX_*` constant with a hardcoded number
- Any `est_*` field
- Any countdown or depletion timer
- Any label that categorizes by arbitrary size buckets

## Coding Rules
- **NEVER ship hardcoded thresholds** — σ-adaptive or remove
- Fix completely in one shot — check actual backend data before writing frontend
- Answer with CLARITY — read actual code, never guess or make up numbers
- When in doubt, make it cleaner and more minimal
- **Deploy: changes stay in files. Do NOT restart server or load to localhost without explicit user approval**
- **Production site (kaaliweb.uk) is NEVER modified without explicit instruction**

## Architecture
- Backend: `l2_worker.py` (Python) — all detection logic
- Frontend: `volume_bubbles.js` (JavaScript) — Canvas 2D rendering
- Server: `server.py` (Python) — WebSocket pipeline
- Dev port: 3001, Production port: 3000

// turbo-all
