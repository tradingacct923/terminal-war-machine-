# Altaris Terminal — Project Rules

> **$300M NQ market maker terminal.** Every number on screen traceable to raw exchange data.
> No guessing. No retail cosmetics. No hardcoded thresholds.

## Architecture Overview

| Layer | File | Role |
|---|---|---|
| **Server** | `server.py` | Flask + Socket.IO, auth, API endpoints |
| **L2 Worker** | `l2_worker.py` | TopStepX WS → detection engine (Python) |
| **App Controller** | `web/app.js` | Data pipeline, feature routing, L2 rendering |
| **Chart Engine** | `web/features/chart_core.js` | Multi-instance LightweightCharts manager |
| **Layout System** | `web/layout_integration.js` | Pane grid, feature mount/unmount, presets |
| **Volume Bubbles** | `web/volume_bubbles.js` | Canvas 2D bubble renderer + DOM heatmap |
| **Depth Ladder** | `web/depth_ladder.js` | Canvas 2D order book ladder |
| **ThermalFlare** | `web/features/thermal_flare.js` | GEX/DEX exposure overlay |
| **WallLines** | `web/features/wall_lines.js` | Call/Put wall + Gamma Flip price lines |

## Critical Architecture Patterns

### Multi-Instance ChartCore
`ChartCore` manages an `_instances[]` array. Each layout pane gets its own LightweightCharts instance tagged with a `feature` key.

```
ChartCore.init(container, symbol, featureKey)
→ Creates instance tagged with featureKey
→ Pushes to _instances[]
→ Emits 'chart:ready' with { feature, candleSeries, ... }
```

**NEVER use global singletons** (`_l2CandleSeries`, `_l2CandleChart`) for rendering decisions. Always iterate `ChartCore.getInstances()` and check `inst.feature`.

### Feature Key System
Every pane instance is tagged with exactly ONE feature key:

| Key | Shows | Candlesticks | Overlays |
|---|---|:---:|---|
| `chart` | Full trading chart | ✅ Visible | Bubbles, WallLines, ThermalFlare, OptionsChain |
| `heatmap` | DOM liquidity depth map | ❌ Transparent | Heatmap canvas overlay |
| `gex` | Gamma exposure | ❌ Transparent | ThermalFlare (gamma mode) |
| `dex` | Delta exposure | ❌ Transparent | ThermalFlare (delta mode) |
| `ivskew` | IV volatility surface | ❌ Transparent | Skew curve overlay |
| `ocheat` | Option chain heatmap | ❌ Transparent | OI/volume heat grid |
| `ladder` | Order book depth | N/A | Canvas 2D depth ladder |
| `eqbook` | QQQ equity L2 trade tape | N/A | HTML trade list |
| `opscr` | Options unusual activity | N/A | Screener table |
| `alpha` | Alpha engine dashboard | N/A | Standalone JS module |

### Data Broadcasting Rules
In `app.js` data loops, ALWAYS route by feature:

```javascript
// ✅ CORRECT — route by feature
ChartCore.getInstances().forEach(inst => {
    if (inst.feature === 'chart') { /* bubbles, walls */ }
    if (inst.feature === 'heatmap') { /* heatmap overlay */ }
});

// ❌ WRONG — legacy global
if (_l2CandleSeries) _l2CandleSeries.update(...);
```

### Plugin Attachment Rules
In the `chart:ready` handler:
- **WallLines** → `chart` only
- **OptionsChain** → `chart` only  
- **ThermalFlare** → `chart`, `gex`, `dex`
- **Heatmap canvas** → `heatmap` only
- **Volume Bubbles** → `chart` only

### Layout Presets (9 total)

| Layout | Slots | Grid | Default Features |
|---|:---:|---|---|
| Single | 1 | 1×1 | `chart` |
| Execution | 2 | 2×1 | `chart`, `ladder` |
| Scalp | 2 | 2×1 | `ladder`, `chart` |
| Flow | 3 | 3×1 | `chart`, `gex`, `dex` |
| DOM | 3 | 3×1 | `heatmap`, `ladder`, `eqbook` |
| Intel | 3 | 2×2 | `chart`, `heatmap`, `dex` |
| Hedge | 3 | 3×1 | `chart`, `gex`, `ivskew` |
| Recon | 6 | 3×2 | `chart`, `heatmap`, `ladder`, `eqbook`, `gex`, `dex` |
| Maker | 3 | 3×1 | `heatmap`, `ladder`, `eqbook` |
| God Mode | 5 | 3×2 | `chart`×2, `heatmap`, `alpha`, `ladder` |

## Rendering Rules

### Candlestick Visibility
Only `chart` feature shows visible candles. All others use `transparent` colors to maintain the price axis for `priceToCoordinate()` mapping without visual noise.

### Canvas Renderers
- `renderDepthLadder()` — called synchronously from `_l2RenderDOM()`, no rAF throttle
- `renderDomHeatmap2D()` — called from `_forceRenderHeatmap()`, gated to heatmap instances
- Both use their OWN coordinate systems — they do NOT depend on `_l2CandleSeries.priceToCoordinate()`

### Guard Pattern
When checking if chart infrastructure exists, use:
```javascript
// ✅ CORRECT
if (typeof ChartCore === 'undefined' || ChartCore.getInstances().length === 0) return;

// ❌ WRONG — breaks in layouts without 'chart' pane
if (!_l2CandleSeries) return;
```

## Ports & Environment
- **Dev**: `localhost:3001` (`./run_dev.sh`)
- **Prod**: `localhost:3000` / `kaaliweb.uk` — NEVER modify without explicit instruction
- **Data**: TopStepX WebSocket → REST polling fallback at 500ms

## Strict Rules
1. **ZERO hardcoded thresholds** — use σ-adaptive or remove
2. **NEVER restart dev server** without explicit user approval
3. **NEVER modify production** without explicit instruction
4. **Fix completely in one shot** — read actual code before writing
5. **Trace every number** to raw exchange data or proven math
6. **Test layout switches** — always verify FLOW ↔ DOM ↔ GOD MODE transitions
