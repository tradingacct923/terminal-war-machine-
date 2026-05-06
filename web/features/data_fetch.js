(function() {
    'use strict';

    window._sio = null;
    let _sioConnected = false;
    let _currentSymbol = 'NQ';
    let _currentTf = '1m';

    // ── Performance: throttle helper ──
    // During scroll: queue data but DON'T process until scroll stops.
    // This keeps the main thread free for LWC's own candle rendering.
    const _rafQueue = {};
    function _throttleRAF(key, fn) {
        return function(data) {
            _rafQueue[key] = data;
            if (_rafQueue[key + '_scheduled']) return;
            _rafQueue[key + '_scheduled'] = true;
            requestAnimationFrame(() => {
                _rafQueue[key + '_scheduled'] = false;
                const d = _rafQueue[key];
                if (d !== undefined) fn(d);
            });
        };
    }
    // Throttle by interval (ms) — for events that don't need 60fps
    function _throttleMs(fn, ms) {
        let last = 0, queued = null, tid = 0;
        return function(data) {
            const now = performance.now();
            if (now - last >= ms) {
                last = now;
                fn(data);
            } else {
                queued = data;
                if (!tid) {
                    tid = setTimeout(() => {
                        tid = 0;
                        last = performance.now();
                        if (queued !== null) { fn(queued); queued = null; }
                    }, ms - (now - last));
                }
            }
        };
    }

    window.authFetch = function(url, opts = {}) {
        const tok = sessionStorage.getItem('greeks-auth');
        if (tok) {
            opts.headers = { ...(opts.headers || {}), 'X-Auth-Token': tok };
        }
        return fetch(url, opts).then(res => {
            if (res.status === 401) {
                sessionStorage.removeItem('greeks-auth');
                window.location.href = '/login';
            }
            return res;
        });
    };

    window.DataFetch = {
        initSocket() {
            if (window._sio) return;
            if (typeof io === 'undefined') {
                console.warn('[Socket.IO] Client library not loaded, falling back to HTTP polling');
                return;
            }
            window._sio = io({ transports: ['websocket', 'polling'], reconnection: true, reconnectionDelay: 1000 });

            window._sio.on('connect', () => {
                _sioConnected = true;
                console.log('[Socket.IO] Connected — real-time data push active');
                if (typeof AltarisToast !== 'undefined') AltarisToast.success('Live data connected');
                this.subscribe(_currentSymbol, _currentTf);
            });

            window._sio.on('disconnect', () => {
                _sioConnected = false;
                console.warn('[Socket.IO] Disconnected — will reconnect');
                if (typeof AltarisToast !== 'undefined') AltarisToast.warn('Data stream disconnected — reconnecting…');
            });

            // Candle OHLCV updates: NO throttle — price accuracy matches DOM speed
            let _candleLatencySum = 0, _candleLatencyCount = 0;
            window._sio.on('candle_update', (data) => {
                // Server only sends active tf — pass through directly
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:candles:update', data);
                }
            });

            // Candle enriched data (bp, signals, depth) at 5Hz — heavier payload
            window._sio.on('candle_enriched', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:candles:enriched', data);
                }
            });

            // Trade ticks: batched to 50ms (20Hz) — reduces 100+ events/sec to 20 batched emits
            let _tradeLatencySum = 0, _tradeLatencyCount = 0;
            let _tradeBatch = [];
            let _tradeBatchTimer = 0;
            function _flushTradeBatch() {
                _tradeBatchTimer = 0;
                if (_tradeBatch.length === 0) return;
                const batch = _tradeBatch;
                _tradeBatch = [];
                if (window.AltarisEvents) {
                    // Emit last trade for price display (most recent = most relevant)
                    window.AltarisEvents.emit('data:trades:update', batch[batch.length - 1]);
                    // Emit full batch for tape/pressure/kinetic that want all trades
                    window.AltarisEvents.emit('data:trades:batch', batch);
                }
            }
            window._sio.on('trade_tick', (data) => {
                // Measure end-to-end latency (Python emit → JS receive)
                if (data._emit_ts) {
                    const latMs = (Date.now() / 1000 - data._emit_ts) * 1000;
                    _tradeLatencySum += latMs;
                    _tradeLatencyCount++;
                    if (_tradeLatencyCount % 100 === 0) {
                        const avg = (_tradeLatencySum / _tradeLatencyCount).toFixed(0);
                        console.log(`[LATENCY] trade_tick avg: ${avg}ms over ${_tradeLatencyCount} trades`);
                        _tradeLatencySum = 0; _tradeLatencyCount = 0;
                    }
                }
                _tradeBatch.push(data);
                if (!_tradeBatchTimer) {
                    _tradeBatchTimer = setTimeout(_flushTradeBatch, 50);
                }
            });

            window._sio.on('zone_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:zone:update', data);
                }
                if (typeof VolSurfacePane !== 'undefined') VolSurfacePane.onZoneUpdate(data);
            });

            window._sio.on('tape_alert', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:tape:alert', data);
                }
            });

            window._sio.on('regime_update', (data) => {
                if (typeof SigmaEngine !== 'undefined' && data && data.regime) {
                    SigmaEngine.setRegime(data.regime);
                }
            });

            window._sio.on('eq_context', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:eq:context', data);
                }
            });

            // ── spot_update: Live NQ/QQQ/SPY/VIX spot price from Schwab streamer ──
            // Throttle to 100ms (10Hz) — spot prices don't need 60fps updates
            window._sio.on('spot_update', _throttleMs((data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:spot:update', data);
                }
            }, 100));

            // ── edge_signal: Cross-asset conviction signals from EdgeDetector ──
            window._sio.on('edge_signal', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:edge:signal', data);
                }
            });

            // ── eq_book_update: QQQ NASDAQ L2 book depth from Schwab ──
            // Throttle to rAF — book depth visualization doesn't need >60fps
            window._sio.on('eq_book_update', _throttleRAF('eqbook', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:eqbook:update', data);
                }
            }));

            // ── screener_option_update: Unusual options activity from Schwab screener ──
            window._sio.on('screener_option_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:screener:update', data);
                }
            });

            // ── flow_update: Per-ticker signed Δ notional curves (0DT-Hero-style) ──
            window._sio.on('flow_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:flow:update', data);
                }
            });

            // ── flow_alert: AlertEngine signals (cross/divergence/spike/dump) ──
            window._sio.on('flow_alert', (alert) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:flow:alert', alert);
                }
            });

            // ── intel:sweep_alert: Multi-strike sweep detector push (Phase 1) ──
            // Source: connectors/sweep_detector.py — emits when 3+ adjacent
            // option strikes traded within 500ms, all aggressor-side same dir.
            // Consumed by web/sweep_pane.js via 'socket:intel:sweep_alert'.
            window._sio.on('intel:sweep_alert', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:sweep_alert', data);
                }
            });

            // ── intel:pin_update: Pin Convergence cache push (Phase 2) ──
            // Source: connectors/pin_convergence.compute_pin_state via
            // schwab_bridge._intel_compute_loop. 15s last hour / 60s otherwise.
            // Consumed by web/pin_pane.js via 'socket:intel:pin_update'.
            window._sio.on('intel:pin_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:pin_update', data);
                }
            });

            // ── intel:hedge_forecast: Hedge Forecaster push (Phase 3) ──
            // Source: connectors/hedge_forecaster.compute_forecast via
            // schwab_bridge._intel_compute_loop. 5s cadence during RTH.
            // Consumed by web/hedge_forecast_pane.js via 'socket:intel:hedge_forecast'.
            window._sio.on('intel:hedge_forecast', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:hedge_forecast', data);
                }
            });

            // ── intel:spx_qqq_divergence: SPX-vs-QQQ regime comparator (Phase 4) ──
            // Source: connectors/spx_qqq_divergence.compute_state via
            // schwab_bridge._intel_compute_loop. 10s cadence during RTH.
            // Consumed by web/spx_qqq_divergence_pane.js via
            // 'socket:intel:spx_qqq_divergence'.
            window._sio.on('intel:spx_qqq_divergence', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:spx_qqq_divergence', data);
                }
            });

            // ── intel:vix_term: VIX regime / cross-asset vol dashboard (Phase 5) ──
            // Source: connectors/vix_term_structure.compute_state via
            // schwab_bridge._intel_compute_loop. 10s cadence during RTH.
            // Consumed by web/vix_term_pane.js via 'socket:intel:vix_term'.
            window._sio.on('intel:vix_term', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:vix_term', data);
                }
            });

            // ── intel:wing_update: 0DTE Wing Tracker push (Phase 6) ──
            // Source: connectors/wing_tracker.compute_state via
            // schwab_bridge._intel_compute_loop. 5s cadence during RTH.
            // Wing prints arrive in real-time via _on_tradier_timesale; this
            // event delivers the periodic regime + aggregate snapshot.
            // Consumed by web/wing_tracker_pane.js via 'socket:intel:wing_update'.
            window._sio.on('intel:wing_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:wing_update', data);
                }
            });

            // ── intel:gamma_skyline: per-strike dealer Γ$ visualization (Phase 7) ──
            // Source: connectors/gamma_skyline.compute_state via
            // schwab_bridge._intel_compute_loop. 5s cadence during RTH.
            // Pure visualization push; consumed by web/gamma_skyline_pane.js
            // via 'socket:intel:gamma_skyline'.
            window._sio.on('intel:gamma_skyline', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:gamma_skyline', data);
                }
            });

            // ── intel:dealer_warehouse: per-strike commitment quality (Phase 8) ──
            // Source: connectors/dealer_warehouse.compute_state via
            // schwab_bridge._intel_compute_loop. 10s cadence during RTH.
            // Reads mm_attribution._capture (Schwab OPTIONS_BOOK posted/caught).
            // Pin Convergence consumes this via dealer_warehouse.get_warehouse_strength
            // to upgrade its `warehouse_strength` from oi_proxy → MEASURED.
            // Consumed by web/dealer_warehouse_pane.js via 'socket:intel:dealer_warehouse'.
            window._sio.on('intel:dealer_warehouse', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:dealer_warehouse', data);
                }
            });

            // ── intel:events: earnings + macro event calendar (Phase 10B) ──
            // Source: connectors/event_calendar.compute_state via
            // schwab_bridge._intel_compute_loop. 60min cadence; events change rarely.
            // Reads data/event_calendar.json (operator-maintained).
            // Consumed by web/events_pane.js via 'socket:intel:events'.
            window._sio.on('intel:events', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('socket:intel:events', data);
                }
            });

            // ── book_microstructure: QQQ NASDAQ L2 venue quality + QA imbalance ──
            // Emitted at 2Hz. Contains per-level venue taxonomy (HFT vs institutional),
            // quality-adjusted imbalance (filters phantom HFT depth), and BBO quality scores.
            window._sio.on('book_microstructure', (data) => {
                window._latestBookMs = data;
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:book:microstructure', data);
                }
                // Cross-market divergence pane
                if (typeof CrossDivergencePane !== 'undefined') CrossDivergencePane.onBookMs(data);
            });

            // ── equity_tape: Venue-tagged equity trades (MIC routing) ──
            // Throttle to 50ms — tape rows batch better than 1-by-1
            window._sio.on('equity_tape', _throttleMs((data) => {
                if (typeof EquityTapePane !== 'undefined') EquityTapePane.onTick(data);
            }, 50));

            // ── dealer_session_flow: Dealer hedge session stats ──
            window._sio.on('dealer_session_flow', (data) => {
                if (typeof DealerFlowPane !== 'undefined') DealerFlowPane.onDealerFlow(data);
            });

            // ── candle_history: Backfill candle data from l2_worker ──
            window._sio.on('candle_history', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:candles:history', data);
                }
            });

            // ── l2_update: Full L2 state push — NO scroll gate ──
            // L2 order book must always update immediately. Market makers need real-time DOM.
            window._sio.on('l2_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:l2:update', data);
                }
            });

            // ── mm_event_batch: MM Attribution structural events (50ms batched) ──
            window._sio.on('mm_event_batch', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:mm:event', data);
                }
            });
            // ── mm_contract_state: per-contract aggregate snapshot pushed
            //    at ~4Hz while a client is `watch`ing the sym. Replaces the
            //    old 1s REST poll from the MM Attribution pane.
            window._sio.on('mm_contract_state', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:mm:state', data);
                }
            });
        },

        subscribe(symbol, tf) {
            _currentSymbol = symbol;
            _currentTf = tf;
            if (window._sio && _sioConnected) {
                window._sio.emit('subscribe', { symbol, tf });
            }
        },

        isConnected() {
            return _sioConnected;
        },

        fetchCandles(symbol, tf, since = 0, signal = null) {
            let url = `/api/l2/candles?symbol=${symbol}&tf=${tf}`;
            if (since > 0) url += `&since=${since}`;
            
            const opts = signal ? { signal } : {};
            return window.authFetch(url, opts).then(r => r.json());
        }
    };
})();
