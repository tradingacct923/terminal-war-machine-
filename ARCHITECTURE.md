# Altaris Terminal — Architecture Reference

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    ALTARIS TERMINAL                          │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │  CHART   │  │ HEATMAP  │  │  LADDER  │  │ EQ BOOK  │   │
│  │ candles  │  │ depth    │  │ DOM      │  │ tape     │   │
│  │ bubbles  │  │ overlay  │  │ bid/ask  │  │ trades   │   │
│  │ walls    │  │          │  │ canvas   │  │          │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
│       │              │              │              │         │
│  ┌────┴──────────────┴──────────────┴──────────────┴────┐   │
│  │              ChartCore._instances[]                   │   │
│  │         (tagged with feature key per pane)            │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                         │                                    │
│  ┌──────────────────────┴───────────────────────────────┐   │
│  │                    app.js                             │   │
│  │    Data Pipeline: fetch → broadcast → render          │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                         │                                    │
│  ┌──────────────────────┴───────────────────────────────┐   │
│  │        AltarisLayout (layout_integration.js)          │   │
│  │    Grid engine: slots, feature mount/unmount          │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────┐     ┌──────────────────────┐
│   server.py (Flask)     │────▶│   l2_worker.py       │
│   Port 3001 (dev)       │     │   TopStepX WS        │
│   Auth, REST API,       │     │   Detection engine   │
│   Socket.IO push        │     │   σ-adaptive logic   │
└─────────────────────────┘     └──────────────────────┘
```

## File Map

### `/web/` — Frontend

| File | Size | Purpose |
|---|---|---|
| `app.js` | 61KB | Main controller — data pipeline, feature routing, L2 rendering, event wiring |
| `volume_bubbles.js` | 120KB | Volume bubble renderer + DOM heatmap 2D + σ-engine |
| `depth_ladder.js` | 17KB | Canvas 2D order book ladder (bid/ask bars + price levels) |
| `layout_integration.js` | 39KB | Layout grid engine — presets, feature mount/unmount, pane dropdowns |
| `layout_engine.css` | 11KB | CSS Grid engine for multi-pane layouts |
| `style.css` | 134KB | Main stylesheet — terminal dark theme |
| `kinetic_text.js` | 39KB | WebGL text physics engine (KineticText) |
| `kinetic_hud.js` | 16KB | WebGL HUD overlay for ladder |
| `pressure_field.js` | 41KB | Physics pressure visualization |
| `sigma_engine.js` | 6KB | σ-adaptive threshold engine |
| `index.html` | 36KB | Main terminal HTML shell |

### `/web/features/` — Modular Feature Plugins

| File | Purpose |
|---|---|
| `chart_core.js` | Multi-instance LightweightCharts manager (THE core engine) |
| `thermal_flare.js` | GEX/DEX exposure glow overlay |
| `wall_lines.js` | Call Wall / Put Wall / Gamma Flip price lines |
| `options_chain.js` | Options chain data integration |
| `alpha_dashboard.js` | Alpha signals dashboard |
| `dashboard_charts.js` | Legacy dashboard charts (bar/candle polling) |
| `data_fetch.js` | Socket.IO data fetching abstraction |

## Layout System

### Preset Layouts (12 total)

| Layout | Slots | Grid | Default Features |
|---|:---:|---|---|
| **Single** | 1 | 1×1 | `chart` |
| **Execution** | 2 | 2×1 | `chart`, `ladder` |
| **Scalp** | 2 | 2×1 | `ladder`, `chart` |
| **Flow** | 3 | 3×1 | `chart`, `gex`, `dex` |
| **DOM** | 3 | 3×1 | `heatmap`, `ladder`, `eqbook` |
| **Intel** | 3 | 2×2 | `chart`, `heatmap`, `ivskew` |
| **OC Desk** | 4 | 2×2 | `chart`, `oclvl`, `ocheat`, `ocliq` |
| **Hedge** | 3 | 3×1 | `chart`, `oclvl`, `ivskew` |
| **OC Scan** | 2 | 2×1 | `ocheat`, `ocliq` |
| **Recon** | 6 | 3×2 | `chart`, `heatmap`, `ladder`, `eqbook`, `oclvl`, `opscr` |
| **Maker** | 3 | 3×1 | `heatmap`, `ladder`, `eqbook` |
| **God Mode** | 5 | 3×2 | `chart`×2, `heatmap`, `alpha`, `ladder` |

### Mount/Unmount Lifecycle

```
User selects layout → AltarisLayout.setLayout('dom')
  → For each slot:
    1. onFeatureUnmount(oldPane, oldFeature, slotEl)
       → ChartCore.destroy(chartDiv)    // removes LW chart
       → _useCanvasLadder = false       // resets ladder state
    2. onFeatureMount(newPane, newFeature, slotEl)
       → Creates DOM container
       → ChartCore.init(container, symbol, featureKey)
       → Emits 'chart:ready' event
       → Plugins attach based on feature key
```

## Data Pipeline

### Real-Time Data Flow

```
TopStepX WS → l2_worker.py → server.py → Socket.IO → app.js
                                        ↘ REST /api/l2 (500ms fallback)
```

### Data Routing in app.js

```
_l2Render(data)
  ├── _l2RenderImbalance(data)      → Bid/ask imbalance bars
  ├── _l2RenderDOM(data.dom)        → Ladder (canvas) OR HTML DOM
  ├── _l2RenderTape(data.trades)    → EQ Book trade tape
  ├── _l2RenderSignals(data.signals)→ Detection alerts
  └── _forceRenderHeatmap()         → DOM depth heatmap overlay

_l2FetchCandles(fullRedraw)
  → fetch /api/data → parse candles
  → ChartCore.getInstances().forEach(inst => {
      inst.candleSeries.setData(candles)     // ALL instances
      inst.volumeSeries.setData(volume)      // ALL instances
      if (inst.feature === 'chart')
        inst.bubbleSeries.setData(bubbles)   // CHART only
    })
```

### Event System

| Event | Fires When | Used By |
|---|---|---|
| `chart:ready` | ChartCore.init() completes | Plugin attachment (WallLines, ThermalFlare) |
| `chart:scroll` | User scrolls/zooms chart | ThermalFlare re-render, heatmap re-render |
| `chart:resize` | Container resizes | Chart + overlay dimension sync |
| `data:candles:update` | Socket.IO candle push | Real-time candle updates to all instances |
| `data:trades:update` | Socket.IO trade push | Price ticker updates |
| `data:zone:update` | Socket.IO zone push | WallLines level updates |

## Known Gotchas

1. **Legacy globals** (`_l2CandleSeries`, `_l2CandleChart`) — exist for backward compat but MUST NOT be used as guards. Use `ChartCore.getInstances()` instead.
2. **Canvas renderers** (ladder, heatmap) — must NOT use rAF throttle wrappers. They're called every 500ms from the poll loop; throttling causes race conditions.
3. **ThermalFlare is a singleton** — calling `.init()` multiple times resets its internal state. Multi-instance ThermalFlare requires refactoring if needed.
4. **WallLines duplicate labels** — `updateLive()` can create duplicate price lines if called multiple times without cleanup. Known cosmetic bug.
5. **`_l2ChartSymbol`** defaults to `'NQ'` — all DOM data keying uses `data.dom[_l2ChartSymbol]`.
