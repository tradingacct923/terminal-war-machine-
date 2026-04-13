(function() {
    'use strict';

    const _instances = [];

// --------------------------------------------------------------
//  ET label formatter – visual only, data stays in UTC
// --------------------------------------------------------------
function _applyETLabelFormatter(chart) {
    const ET_OFFSET_MS = 4 * 60 * 60 * 1000; // UTC‑4 (EDT)
    chart.timeScale().applyOptions({
        tickMarkFormatter: time => {
            const utc = new Date(time * 1000);
            const et  = new Date(utc.getTime() - ET_OFFSET_MS);
            const hh = String(et.getHours()).padStart(2, '0');
            const mm = String(et.getMinutes()).padStart(2, '0');
            return `${hh}:${mm}`;
        }
    });
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
                    cumlDelta: true,
                    iceberg: true,
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
                upColor:         showCandles ? '#26A69A' : 'transparent',
                downColor:       showCandles ? '#EF5350' : 'transparent',
                borderUpColor:   showCandles ? '#26A69A' : 'transparent',
                borderDownColor: showCandles ? '#EF5350' : 'transparent',
                wickUpColor:     showCandles ? 'rgba(38,166,154,.7)' : 'transparent',
                wickDownColor:   showCandles ? 'rgba(239,83,80,.7)' : 'transparent',
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

            // ── Resize Observer ──
            const _heatmapCanvas = heatmapCanvas; // closure ref
            let _firstResize = true;
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
                        // On first resize, fit content so candles are visible
                        if (_firstResize) {
                            _firstResize = false;
                            setTimeout(() => {
                                try {
                                    _chart.timeScale().fitContent();
                                } catch(e) {}
                            }, 150);
                        }
                        if (window.AltarisEvents) {
                            window.AltarisEvents.emit('chart:resize', { width, height: height || chartH, container });
                        }
                    }
                }
            });
            ro.observe(container);

            // ── Scroll Event ──
            _chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
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
                ro
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
                _instances.splice(idx, 1);
            }
        }
    };
})();
