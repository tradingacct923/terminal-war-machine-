# Altaris Terminal

Real-time NQ futures + QQQ options dealer hedging terminal. Runs on `localhost:3001`.

## Architecture Overview

### Data Flow
```
TopStepX (NQ futures L2)     → l2_worker.py → Socket.IO → web UI
Schwab (QQQ options/equities)→ schwab_bridge.py → Socket.IO → web UI
                              → server.py (REST /api/*) → web UI
```

### Primary Data Sources
- **TopStepX**: NQ futures Level 2 (DOM, trades, mid price). Primary NQ price source. 24/5. NQ only (GC removed).
- **Schwab Streamer**: QQQ options (200 contracts, Greeks/IV/marks), QQQ/SPY L2 book (NASDAQ/NYSE), equity spots (QQQ/SPY/VIX/$NDX.X), NQ/ES Level 1 (secondary). Schwab candle_update filtered out for NQ/ES (TopStepX is sole NQ candle source).
- **Tradier ORATS**: IV calibration data (5-min poll via `iv_calibrator.py`).

### Backend (`background_engine/`)
| File | Role |
|------|------|
| `l2_worker.py` | Core L2 engine. DOM processing, sweep/absorption detection, VPIN, Kalman OFI, Hawkes branching, candle aggregation. Receives TopStepX data. Candle `bp` = `{priceStr: [buyVol, sellVol, fp, abs, bookSz]}`, `bp_large` = same but only trades ≥10 lots. Split emit: `candle_update` (fast OHLCV 20Hz) + `candle_enriched` (heavy payload 5Hz). ~4500 lines. |
| `schwab_bridge.py` | Schwab streamer integration. Options GEX/DEX computation, zone_update emit (every 5s), dealer_session_flow, equity tape, book microstructure. ~1400 lines. |
| `topstepx_connector.py` | WebSocket connector to TopStepX futures API. Feeds l2_worker. |
| `main.py` | Legacy background engine (percolation/entropy frameworks). |

### Connectors (`connectors/`)
| File | Role |
|------|------|
| `schwab_auth.py` | OAuth2 token management for Schwab API |
| `schwab_streamer.py` | WebSocket client for Schwab streaming data |
| `data_schwab.py` | REST API client for Schwab (chains, quotes) |
| `edge_detector.py` | Cross-asset signal detection (sweeps, GEX zones) |
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
- `/api/vprofile` — Volume profile (session/prior_day/rolling/step). Params: `mode`, `step` (15m/30m/1h), `min_level_vol`, `va_pct`
- `/api/vprofile/naked-pocs` — Prior session POCs not yet revisited by price
- `/api/vprofile/dev-poc` — Developing POC migration path for current session
- Socket.IO events: `candle_update`, `candle_enriched`, `trade_tick`, `zone_update`, `l2_update`, `spot_update`, `edge_signal`, `book_microstructure`, `equity_tape`, `option_mark_update`, `dealer_session_flow`, `screener_option_update`, `eq_book_update`, `eq_context`, `candle_history`

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
| `volume_profile_overlay.js` | — | `/api/vprofile` REST (10s poll). Naked POC, developing POC, step profiles, trade-size filter |

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

## Critical Rules

### Timezone
- `_utcToET()` in `app.js` subtracts 4hrs (EDT) from UTC epochs for LWC display
- **ALL candle timestamps** stored in `_l2LastCandleTime`, `_l2SeamTime`, etc. MUST go through `_utcToET()`
- Raw UTC stored alongside ET-converted values will silently drop all live candle updates (comparison mismatch)
- VP overlay timestamps (`from_ts`, `to_ts`, dev POC `time`) also need ET offset before `timeToCoordinate()`

### Performance
- **No `shadowBlur`** in per-row/per-bar render loops (GPU expensive). Use brighter fill colors instead
- **Dirty flags** on all rAF loops — skip render when no data changed
- **No `_chartScrolling` gate** on data handlers (`_throttleRAF`, `_throttleMs`). Only gate canvas overlays (heatmap, thermal flare, volume profile)
- **No infinite CSS animations** — use finite iteration counts
- Volume bubble cache: two-level (data signature for classification, viewport for x/y remap on scroll)

### Data Sources
- NQ candles come from TopStepX l2_worker ONLY — schwab_bridge filters out NQ/ES from `_on_chart_candle()`
- `candle_update` = fast OHLCV at 20Hz, `candle_enriched` = heavy payload (bp, depth_deltas, hawkes) at 5Hz
- `dom_snapshot` and `v2_signals` events removed — all DOM data flows via `l2_update`

## Active Work Streams
- **Volume Profile Pro**: Naked POC, developing POC, step profiles (QuantTower-style), trade-size filter
- **Volume Bubbles**: Per-bar rendering glued to candles, Kalman-adaptive thresholds
- **V2 Engine**: DOM heatmap, sigma engine
