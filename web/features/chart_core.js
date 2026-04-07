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

            // Unmount existing chart in this container if any
            this.destroy(container);

            const chartH = container.clientHeight || 700;

            const _chart = LightweightCharts.createChart(container, {
                width: container.clientWidth,
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

            // ── Series ──
            const L2_TICK_SIZES = { NQ: 0.25, GC: 0.10 };
            
            // Only 'chart' panes show visible candlesticks — others use transparent
            // candles to maintain the price axis for coordinate mapping
            const showCandles = (featureKey === 'chart');
            
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

            // ── Volume Bubbles: ONLY for 'chart' feature ──
            let _bubbleSeries = null;
            if (featureKey === 'chart' && typeof VolumeBubbleSeries !== 'undefined') {
                _bubbleSeries = _chart.addCustomSeries(new VolumeBubbleSeries(), {
                    priceScaleId: 'right',
                    lastValueVisible: false,
                    priceLineVisible: false,
                });
            }

            // ── Heatmap Canvas: ONLY for 'heatmap' feature ──
            let heatmapCanvas = null;
            if (featureKey === 'heatmap') {
                heatmapCanvas = document.createElement('canvas');
                heatmapCanvas.id = 'dom-heatmap-canvas-' + Date.now();
                heatmapCanvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:10;';
                container.appendChild(heatmapCanvas);
                
                const dpr = window.devicePixelRatio || 1;
                heatmapCanvas.width = (container.clientWidth || 900) * dpr;
                heatmapCanvas.height = (container.clientHeight || chartH) * dpr;
            }

            // ── Resize Observer ──
            const _heatmapCanvas = heatmapCanvas; // closure ref
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
                inst.chart.remove();
                if (inst.heatmapCanvas && inst.heatmapCanvas.parentNode) {
                    inst.heatmapCanvas.parentNode.removeChild(inst.heatmapCanvas);
                }
                _instances.splice(idx, 1);
            }
        }
    };
})();
