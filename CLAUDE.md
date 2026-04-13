# Altaris Terminal

Real-time NQ futures + QQQ options dealer hedging terminal. Runs on `localhost:3001`.

## Architecture Overview

### Data Flow
```
TopStepX (NQ/GC futures L2) → l2_worker.py → Socket.IO → web UI
Schwab (QQQ options/equities)→ schwab_bridge.py → Socket.IO → web UI
                              → server.py (REST /api/*) → web UI
```

### Primary Data Sources
- **TopStepX**: NQ/GC futures Level 2 (DOM, trades, mid price). Primary NQ price source. 24/5.
- **Schwab Streamer**: QQQ options (200 contracts, Greeks/IV/marks), QQQ/SPY L2 book (NASDAQ/NYSE), equity spots (QQQ/SPY/VIX/$NDX.X), NQ/ES Level 1 (secondary).
- **Tradier ORATS**: IV calibration data (5-min poll via `iv_calibrator.py`).

### Backend (`background_engine/`)
| File | Role |
|------|------|
| `l2_worker.py` | Core L2 engine. DOM processing, iceberg detection, VPIN, Kalman OFI, Hawkes branching, candle aggregation. Receives TopStepX data. ~4500 lines. |
| `schwab_bridge.py` | Schwab streamer integration. Options GEX/DEX computation, zone_update emit (every 5s), dealer_session_flow, equity tape, book microstructure. ~1400 lines. |
| `topstepx_connector.py` | WebSocket connector to TopStepX futures API. Feeds l2_worker. |
| `main.py` | Legacy background engine (percolation/entropy frameworks). |

### Connectors (`connectors/`)
| File | Role |
|------|------|
| `schwab_auth.py` | OAuth2 token management for Schwab API |
| `schwab_streamer.py` | WebSocket client for Schwab streaming data |
| `data_schwab.py` | REST API client for Schwab (chains, quotes) |
| `edge_detector.py` | Cross-asset signal detection (icebergs, sweeps, GEX zones) |
| `flow_classifier.py` | Classifies option flow (institutional vs retail) |
| `greek_surface.py` | Higher-order Greeks (vanna, charm, speed, zomma) |
| `vol_surface.py` | Vol regime detection (NORMAL/ELEVATED/EXTREME/COMPRESSED) |
| `iv_calibrator.py` | Tradier ORATS IV calibration polling |
| `mm_tracker.py` | Market maker activity tracking |
| `dte0_squeeze.py` | 0DTE gamma squeeze detector |
| `vpin_engine.py` | Volume-synchronized PIN computation |

### Server (`server.py`)
Flask + Socket.IO server. ~1700 lines. Key endpoints:
- `/api/data` — GEX zone data (from Schwab bridge)
- `/api/l2` — L2 state snapshot (from l2_worker)
- `/api/chain` — Options chain (Schwab REST)
- `/api/spot` — Live spot prices
- `/api/walls` — Put/call wall levels
- Socket.IO events: `candle_update`, `trade_tick`, `zone_update`, `l2_update`, `spot_update`, `edge_signal`, `book_microstructure`, `equity_tape`, `option_mark_update`, `dealer_session_flow`, `screener_option_update`, `eq_book_update`, `eq_context`

### Frontend (`web/`)

**Layout System**: `layout_integration.js` defines FEATURES (feature key → label/description) and LAYOUTS (preset configurations). `app.js` handles mount/unmount lifecycle. Each pane module uses IIFE pattern with `init(slotEl)`, `destroy()`.

**Core UI files:**
| File | What it does |
|------|-------------|
| `app.js` | Main app controller, pane mount/unmount, event routing (~1900 lines) |
| `index.html` | Shell HTML with layout dropdown, script tags |
| `style.css` | All styling (~3500 lines) |
| `layout_integration.js` | FEATURES registry, LAYOUTS presets, feature dropdowns |
| `layout_engine.css` | CSS grid layouts for 1-6 pane configurations |

**Feature panes (`web/features/` + `web/`):**
| File | Feature Key | Data Event |
|------|------------|------------|
| `features/chart_core.js` | `chart` | `candle_update`, `candle_history` |
| `features/data_fetch.js` | — | Socket.IO connection + all event routing |
| `features/wall_lines.js` | `gex` | `/api/data` REST |
| `features/dashboard_charts.js` | `dashboard` | Multiple REST endpoints |
| `features/options_chain.js` | `chain` | `/api/chain` REST |
| `features/thermal_flare.js` | `flare` | `trade_tick` |
| `features/drawing_tools.js` | — | Chart drawing overlay |
| `features/alpha_dashboard.js` | `alpha` | Multiple signals |
| `volume_bubbles.js` | `bubbles` | `trade_tick` |
| `v2_dom_heatmap.js` | `heatmap` | `l2_update` |
| `v2_iceberg_sweep.js` | `iceberg` | `trade_tick` + `l2_update` |
| `v2_integration.js` | — | V2 feature integration layer |
| `v2_sigma_engine.js` | `sigma` | Multiple events |
| `depth_ladder.js` | `ladder` | `l2_update` |
| `sigma_engine.js` | — | Signal processing engine |
| `book_microstructure_hud.js` | `bookms` | `book_microstructure` |
| `pressure_field.js` | `pressure` | `l2_update` + `trade_tick` |
| `kinetic_hud.js` | `kinetic` | Multiple events |
| `equity_tape_pane.js` | `eqtape` | `equity_tape` |
| `dealer_flow_pane.js` | `dealer` | `dealer_session_flow` |
| `cross_divergence_pane.js` | `xdiv` | `book_microstructure` |
| `vol_surface_pane.js` | `volsurf` | `zone_update` + `option_mark_update` |
| `options_flow_pane.js` | `optflow` | `option_mark_update` |

### Physics/Math Frameworks (`frameworks/`)
Statistical mechanics models applied to market microstructure:
- Shannon entropy, Ising magnetization, Reynolds number, LPPL (Sornette), power-law tails, percolation threshold, mutual information, transfer entropy

## Running
```bash
source venv/bin/activate
PORT=3001 python server.py
# Open http://localhost:3001
```

## Key Conventions
- Pane modules: IIFE with `init(slotEl)`, `destroy()`, data handler methods
- Socket.IO events routed through `data_fetch.js` → `AltarisEvents` bus
- NQ price: always use TopStepX L2 mid as primary (`_get_nq_mid()` in schwab_bridge.py)
- GEX zone emit: every 5s when option data fresh, every 30s with stale option data + live NQ
- Auth: session token in `sessionStorage('greeks-auth')`, `X-Auth-Token` header

## Active Work Streams
- **Volume Bubbles**: Improving `volume_bubbles.js` visualization
- **Dealer Hedging Terminal**: 5 new panes (EQ TAPE, DEALER, X-DIV, VOL SURF, OPT FLOW) — recently completed
- **V2 Engine**: Advanced iceberg detection, DOM heatmap, sigma engine
