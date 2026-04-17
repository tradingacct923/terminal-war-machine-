(function() {
    'use strict';

    const _instances = [];

// --------------------------------------------------------------
//  ET label formatter – visual only, data stays in UTC
// --------------------------------------------------------------
function _applyETLabelFormatter(chart) {
    // Candle times are already shifted to ET by _utcToET() in app.js
    // LWC displays them directly — no additional offset needed in labels
}
    
    window.ChartCore = {
        /**
         * @param {HTMLElement} container  — the DOM element to mount into
         * @param {string} initialSymbol   — e.g. 'NQ', 'GC'
         * @param {string} featureKey      — 'chart' | 'heatmap' | 'dex' | 'gex'
         */
        init(container, initialSymbol, featureKey) {
            featureKey = featureKey || 'chart';

            // Guard: if container has no dimensions yet, defer until it does
            if (!container.clientWidth || !container.clientHeight) {
                const _self = this;
                requestAnimationFrame(() => _self.init(container, initialSymbol, featureKey));
                return;
            }

            // Unmount existing chart in this container if any
            this.destroy(container);

            // Fix #10 — purge any orphan instances whose container was detached
            // from the DOM (happens when layout engine wipes a slot before the
            // unmount handler runs). These leak ResizeObservers, canvases, and
            // primitives that keep firing into the void.
            // Guard: only pass truthy containers to destroy() — a null container
            // would trigger the "destroy all" branch and nuke valid charts too.
            const _orphans = _instances.filter(i => i.container && !i.container.isConnected);
            _orphans.forEach(inst => { try { this.destroy(inst.container); } catch(e) {} });

            const chartW = container.clientWidth;
            const chartH = container.clientHeight;

            const _chart = LightweightCharts.createChart(container, {
                width: chartW,
                height: chartH,
                layout: {
                    background: { type: 'solid', color: 'transparent' },
                    textColor: 'rgba(140,160,200,.65)',
                    fontFamily: "'JetBrains Mono', 'SF Mono', monospace",
                    fontSize: 11,
                },
                grid: {
                    vertLines: { color: 'rgba(255,255,255,.025)', style: 1 },
                    horzLines: { color: 'rgba(255,255,255,.025)', style: 1 },
                },
                crosshair: {
                    mode: LightweightCharts.CrosshairMode.Normal,
                    vertLine: { color: 'rgba(124,90,247,.5)', width: 1, style: 2, labelBackgroundColor: 'rgba(124,90,247,.85)' },
                    horzLine: { color: 'rgba(124,90,247,.5)', width: 1, style: 2, labelBackgroundColor: 'rgba(124,90,247,.85)' },
                },
                rightPriceScale: {
                    borderColor: 'rgba(255,255,255,.06)',
                    scaleMargins: { top: 0.02, bottom: 0.18 },
                    textColor: 'rgba(140,160,200,.55)',
                    entireTextOnly: true,
                },
                timeScale: {
                    borderColor: 'rgba(255,255,255,.06)',
                    timeVisible: true,
                    secondsVisible: true,
                    rightOffset: 8,
                    barSpacing: 7,
                    minBarSpacing: 2,
                    fixLeftEdge: false,
                    fixRightEdge: false,
                },
                handleScroll: { mouseWheel: true, pressedMouseMove: true },
                handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
            });
    // Apply ET‑styled tick labels (visual only)
    _applyETLabelFormatter(_chart);

            // Heatmap-specific overrides: tighter price scale + wider bar spacing
            if (featureKey === 'heatmap') {
                _chart.priceScale('right').applyOptions({
                    scaleMargins: { top: 0.05, bottom: 0.05 },
                });
                _chart.timeScale().applyOptions({
                    barSpacing: 20,
                    rightOffset: 3,
                });
            }

            container.style.position = 'relative';

            // ── Per-pane overlay config (toggled by toolbar buttons) ──
            if (!container._overlayConfig) {
                container._overlayConfig = {
                    bubbles: true,
                    flare: true,
                    vp: true,
                    walls: true,
                };
            }

            // ── Series ──
            const L2_TICK_SIZES = { NQ: 0.25, GC: 0.10 };
            
            // Chart and alpha panes show visible candlesticks — heatmap uses transparent
            // candles (DOM overlay replaces them) to maintain the price axis for coordinate mapping
            const showCandles = (featureKey !== 'heatmap');

            const _candleSeries = _chart.addCandlestickSeries({
                upColor:         showCandles ? '#00E676' : 'transparent',
                downColor:       showCandles ? '#FF1744' : 'transparent',
                borderUpColor:   showCandles ? '#00C853' : 'transparent',
                borderDownColor: showCandles ? '#D50000' : 'transparent',
                wickUpColor:     showCandles ? '#00E676' : 'transparent',
                wickDownColor:   showCandles ? '#FF1744' : 'transparent',
                priceFormat: { type: 'price', precision: 2, minMove: L2_TICK_SIZES[initialSymbol] || 0.25 },
            });

            const _volumeSeries = _chart.addHistogramSeries({
                priceFormat: { type: 'volume' },
                priceScaleId: 'vol',
                visible: false,
            });
            _chart.priceScale('vol').applyOptions({
                scaleMargins: { top: 0.85, bottom: 0 },
                drawTicks: false,
                visible: false,
            });

            // ── Volume Bubbles: for all chart-based panes (not heatmap) ──
            let _bubbleSeries = null;
            if (featureKey !== 'heatmap' && typeof VolumeBubbleSeries !== 'undefined') {
                const _bubblePlugin = new VolumeBubbleSeries();
                // Store container ref on renderer for per-pane overlay config lookup
                _bubblePlugin._renderer._containerRef = container;
                _bubbleSeries = _chart.addCustomSeries(_bubblePlugin, {
                    priceScaleId: 'right',
                    lastValueVisible: false,
                    priceLineVisible: false,
                });
            }

            // ── Volume Profile Overlay: attach as primitive on candlestick series ──
            if (featureKey !== 'heatmap' && typeof VolumeProfileOverlay !== 'undefined') {
                VolumeProfileOverlay.attach(_chart, _candleSeries, container);
            }

            // ── Wall Lines: attach to candlestick series ──
            if (featureKey !== 'heatmap' && typeof WallLines !== 'undefined') {
                WallLines.attachToSeries(_candleSeries, container);
            }

            // ── Heatmap Canvas: ONLY for 'heatmap' feature ──
            // Must be placed INSIDE LWC's chart area div (the position:relative wrapper
            // around the main canvases) so it stacks above them. Appending to `container`
            // puts it outside LWC's table stacking context and it's hidden.
            let heatmapCanvas = null;
            if (featureKey === 'heatmap') {
                heatmapCanvas = document.createElement('canvas');
                heatmapCanvas.id = 'dom-heatmap-canvas-' + Date.now();
                heatmapCanvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:5;';
                // Find LWC's main chart area div (TABLE > TR > TD[1] > DIV with position:relative)
                const lwcEl = container.querySelector('.tv-lightweight-charts');
                const chartAreaDiv = lwcEl && lwcEl.querySelector('tr td:nth-child(2) > div');
                if (chartAreaDiv) {
                    chartAreaDiv.appendChild(heatmapCanvas);
                } else {
                    container.appendChild(heatmapCanvas); // fallback
                }

                const dpr = window.devicePixelRatio || 1;
                heatmapCanvas.width = (container.clientWidth || 900) * dpr;
                heatmapCanvas.height = (container.clientHeight || chartH) * dpr;
            }

            // ── WebGL Overlay Canvas: GPU-accelerated bubbles + VP bars + zones ──
            // Deferred: LWC creates its DOM async, so we wait a frame before attaching.
            let _webglCanvas = null;
            let _textCanvas = null;
            if (featureKey !== 'heatmap' && typeof WebGLOverlay !== 'undefined') {
                requestAnimationFrame(() => {
                    // Leak-fix: if this chart was destroyed before the rAF fired
                    // (rapid layout swap), skip attachment — otherwise we'd
                    // orphan canvases into a detached DOM subtree.
                    if (!container.isConnected || !_instances.some(i => i.chart === _chart)) {
                        // _chart exists only if pushed into _instances; if not found
                        // it's because destroy() already ran. Bail.
                        if (_chart && !_instances.some(i => i.chart === _chart)) return;
                    }
                    const lwcEl2 = container.querySelector('.tv-lightweight-charts');
                    const chartArea2 = lwcEl2 && lwcEl2.querySelector('tr td:nth-child(2) > div');
                    const parentEl = chartArea2 || container;
                    if (!parentEl || !parentEl.isConnected) return;

                    _webglCanvas = document.createElement('canvas');
                    _webglCanvas.id = 'webgl-overlay-' + Date.now();
                    _webglCanvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:6;';
                    parentEl.appendChild(_webglCanvas);

                    _textCanvas = document.createElement('canvas');
                    _textCanvas.id = 'text-overlay-' + Date.now();
                    _textCanvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:7;';
                    parentEl.appendChild(_textCanvas);

                    // Leak-fix: update the already-pushed instance object so destroy()
                    // can clean up the canvases we just created (they were attached
                    // AFTER the instance record was built).
                    const _liveInst = _instances.find(i => i.chart === _chart);
                    if (_liveInst) {
                        _liveInst.webglCanvas = _webglCanvas;
                        _liveInst.textCanvas = _textCanvas;
                    }

                    const cw = container.clientWidth || 900;
                    const ch = container.clientHeight || chartH;
                    if (WebGLOverlay.init(_webglCanvas)) {
                        WebGLOverlay.resize(cw, ch);
                        console.log('[WebGL] Overlay canvas attached and initialized');
                    } else {
                        console.warn('[WebGL] Init failed — Canvas 2D fallback active');
                    }
                    const dpr2 = window.devicePixelRatio || 1;
                    _textCanvas.width = Math.round(cw * dpr2);
                    _textCanvas.height = Math.round(ch * dpr2);
                });
            }

            // ── Resize Observer ──
            const _heatmapCanvas = heatmapCanvas; // closure ref
            // `_needsInitialFit` stays true until fitContent actually runs with
            // a real width AND data is present. Prevents "chart shows no
            // candles" when pane mounts hidden (width=0) or when RO fires
            // before setData lands.
            let _needsInitialFit = true;
            const _tryInitialFit = () => {
                if (!_needsInitialFit || !_chart) return;
                try {
                    const w = container.clientWidth;
                    const hasData = _candleSeries && _candleSeries.data && _candleSeries.data().length > 0;
                    if (w > 10 && hasData) {
                        _chart.timeScale().fitContent();
                        _needsInitialFit = false;
                    }
                } catch(e) {}
            };
            const ro = new ResizeObserver(entries => {
                for (const entry of entries) {
                    const { width, height } = entry.contentRect;
                    if (_chart && width > 0) {
                        _chart.applyOptions({ width, height: height || chartH });
                        if (_heatmapCanvas) {
                            const dpr = window.devicePixelRatio || 1;
                            _heatmapCanvas.width = width * dpr;
                            _heatmapCanvas.height = (height || chartH) * dpr;
                        }
                        if (_webglCanvas && typeof WebGLOverlay !== 'undefined' && WebGLOverlay.isReady()) {
                            WebGLOverlay.resize(width, height || chartH);
                        }
                        if (_textCanvas) {
                            const dpr = window.devicePixelRatio || 1;
                            _textCanvas.width = Math.round(width * dpr);
                            _textCanvas.height = Math.round((height || chartH) * dpr);
                        }
                        // Retry fit until it actually succeeds (width+data both ready)
                        if (_needsInitialFit) {
                            setTimeout(_tryInitialFit, 50);
                            setTimeout(_tryInitialFit, 250);
                            setTimeout(_tryInitialFit, 800);
                        }
                        if (window.AltarisEvents) {
                            window.AltarisEvents.emit('chart:resize', { width, height: height || chartH, container });
                        }
                    }
                }
            });
            ro.observe(container);

            // ── Scroll Event + Global scroll flag ──
            // ALL overlays check window._chartScrolling to skip rendering during scroll.
            // This is the single source of truth — no per-overlay scroll detection needed.
            _chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
                window._chartScrolling = true;
                clearTimeout(window._chartScrollTimer);
                window._chartScrollTimer = setTimeout(() => { window._chartScrolling = false; }, 80);
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('chart:scroll', { container });
                }
            });
            
            // Store instance with feature tag
            const instance = {
                id: 'chart-inst-' + Date.now(),
                feature: featureKey,
                container,
                chart: _chart,
                candleSeries: _candleSeries,
                volumeSeries: _volumeSeries,
                bubbleSeries: _bubbleSeries,
                heatmapCanvas,
                webglCanvas: _webglCanvas,
                textCanvas: _textCanvas,
                ro,
                // Leak-fix: expose fit retry so external data handlers
                // (candle_history replay) can re-arm after late data lands.
                _tryInitialFit: _tryInitialFit,
            };

            _instances.push(instance);

            // ── Emit Ready Event ──
            if (window.AltarisEvents) {
                window.AltarisEvents.emit('chart:ready', {
                     instanceId: instance.id,
                     feature: featureKey,
                     container,
                     chart: _chart,
                     candleSeries: _candleSeries,
                     volumeSeries: _volumeSeries,
                     bubbleSeries: _bubbleSeries,
                     heatmapCanvas,
                     chartH
                });
            }
        },

        getInstances() { return _instances; },

        destroy(container_or_all) {
            if (!container_or_all) {
                // Destroy all
                [..._instances].forEach(inst => this.destroy(inst.container));
                return;
            }
            const idx = _instances.findIndex(i => i.container === container_or_all);
            if (idx > -1) {
                const inst = _instances[idx];
                inst.ro.disconnect();
                // Detach per-instance VP and WallLines before removing chart
                if (typeof VolumeProfileOverlay !== 'undefined') {
                    VolumeProfileOverlay.detachInstance(inst.container);
                }
                if (typeof WallLines !== 'undefined') {
                    WallLines.detachInstance(inst.container);
                }
                inst.chart.remove();
                if (inst.heatmapCanvas && inst.heatmapCanvas.parentNode) {
                    inst.heatmapCanvas.parentNode.removeChild(inst.heatmapCanvas);
                }
                // Leak-fix: WebGL + text overlay canvases were attached via a
                // deferred rAF path and never removed on destroy. Orphaned
                // canvases accumulated on every layout switch, eventually
                // covering the real chart canvas → "no candles visible".
                if (inst.webglCanvas && inst.webglCanvas.parentNode) {
                    try {
                        if (typeof WebGLOverlay !== 'undefined' && WebGLOverlay.destroy) {
                            WebGLOverlay.destroy();
                        }
                    } catch(e) {}
                    inst.webglCanvas.parentNode.removeChild(inst.webglCanvas);
                }
                if (inst.textCanvas && inst.textCanvas.parentNode) {
                    inst.textCanvas.parentNode.removeChild(inst.textCanvas);
                }
                inst.webglCanvas = null;
                inst.textCanvas = null;
                inst.chart = null;
                inst.candleSeries = null;
                inst.volumeSeries = null;
                inst.bubbleSeries = null;
                _instances.splice(idx, 1);
            }
        }
    };
})();
