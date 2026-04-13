// ── Auth ──────────────────────────────────────────────────────────────────────
// authFetch is defined by features/data_fetch.js as window.authFetch
// DO NOT re-declare here — function declarations hoist and overwrite window.authFetch,
// causing infinite recursion. All callers (loadL2, dashboard_charts) resolve to
// window.authFetch automatically via global scope lookup.


// ── Header greeting with time-of-day ─────────────────────────────────────────
(function setHeaderGreeting() {
    const h = new Date().getHours();
    const greeting = h < 12 ? 'Good Morning' : h < 18 ? 'Good Afternoon' : 'Good Evening';
    const el = document.getElementById('greeting-text');
    if (el) el.textContent = `${greeting}.`;

    const dateEl = document.getElementById('greeting-date');
    if (dateEl) {
        dateEl.textContent = new Date().toLocaleDateString('en-US', {
            weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
        });
    }
})();

function doLogout() {
    // No-op — auth removed
}


// ── Welcome transition screen ────────────────────────────────────────────────
(function setupWelcome() {
    const ws = document.getElementById('welcome-screen');
    if (!ws) return;

    // Set user name
    const email = localStorage.getItem('greeks-user') || '';
    const name = email ? email.split('@')[0].replace(/[._]/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase()) : 'Member';
    const nameEl = document.getElementById('ws-name');
    if (nameEl) nameEl.textContent = name;

    // Animate progress bar
    const bar = document.getElementById('ws-progress-bar');
    const status = document.getElementById('ws-status');
    const steps = [
        [15, 'DECRYPTING MARKET STREAM...'],
        [35, 'LOADING OPTIONS CHAIN DATA...'],
        [55, 'COMPUTING GREEK EXPOSURES...'],
        [75, 'BUILDING HEATMAP MATRICES...'],
        [90, 'INITIALIZING DASHBOARD...'],
    ];
    let step = 0;
    const iv = setInterval(() => {
        if (step < steps.length) {
            if (bar) bar.style.width = steps[step][0] + '%';
            // Typewriter effect for status text
            if (status) {
                const txt = steps[step][1];
                status.textContent = '';
                let ci = 0;
                const typeIt = () => {
                    if (ci < txt.length) { status.textContent += txt[ci]; ci++; setTimeout(typeIt, 18); }
                };
                typeIt();
            }
            step++;
        }
    }, 600);

    // Dismiss welcome screen — called after first data load
    window._dismissWelcome = function () {
        clearInterval(iv);
        if (bar) bar.style.width = '100%';
        if (status) status.textContent = 'DASHBOARD READY';
        setTimeout(() => {
            ws.classList.add('ws-hide');
            setTimeout(() => { ws.style.display = 'none'; }, 800);
        }, 400);
    };

    // Fallback: auto-dismiss after 8s in case data never comes
    setTimeout(() => { if (window._dismissWelcome) window._dismissWelcome(); }, 8000);
})();

// ── Sidebar navigation REMOVED — terminal mode only ─────────────────────────
// setupSidebar() deleted: no sidebar DOM exists.
// User info is now handled by the terminal toolbar.


// ── Config ────────────────────────────────────────────────────────────────────
// API_URL, ANOMALY_URL, SETTINGS_URL, VOL_URL are now in features/dashboard_charts.js
// _refreshTimer, _barTimer, _barMs are now in features/dashboard_charts.js (loads sync before this defer script)

// ── Cached number formatter (avoids per-call Intl.NumberFormat allocation) ──
const _numFmt = new Intl.NumberFormat('en-US');

// ── Plotly lazy loader (3.5MB — loaded on-demand when GEX/DEX/IV panes first mount) ──
let _plotlyLoading = false;
let _plotlyCallbacks = [];
window._ensurePlotly = function(cb) {
    if (typeof Plotly !== 'undefined') { if (cb) cb(); return; }
    if (cb) _plotlyCallbacks.push(cb);
    if (_plotlyLoading) return;
    _plotlyLoading = true;
    const s = document.createElement('script');
    s.src = 'https://cdn.plot.ly/plotly-2.27.0.min.js';
    s.defer = true;
    s.onload = () => { _plotlyCallbacks.forEach(fn => fn()); _plotlyCallbacks = []; };
    document.head.appendChild(s);
};

// ── Level 2 Dashboard ─────────────────────────────────────────────────────────

let _l2PollTimer = null;
let _l2CandleChart = null; // Legacy reference
let _l2CandleSeries = null; // Legacy reference
let _l2HeatmapVisible = true;      // Legacy compat — ThermalFlare module now owns this
let _l2CandleDataCache = null;      // Cached {ohlc, vol} for backfilling new chart instances

// ── Visual Layer Separation ──
// 'chart' = clean candles + walls + bubbles + Thermal Flare (options GEX/DEX overlay)
// 'heatmap' = candles + NQ DOM depth blocks (no bubbles, no thermal flare)
let _activeChartFeature = 'chart';
window._activeChartFeature = 'chart';
let _l2PendingContainer = null; // Container element for next chart init

// ── Thermal Flare rendering is now in features/thermal_flare.js ──
// Legacy compat — _drawDexHeatmap delegates to ThermalFlare module
function _drawDexHeatmap() {
    if (typeof ThermalFlare !== 'undefined') ThermalFlare.render();
}
function _debugInjectHeatmap() {
    if (typeof ThermalFlare !== 'undefined') ThermalFlare.debugInject();
}

let _l2VolumeSeries = null;
let _l2BubbleSeries = null;  // Custom series for volume bubbles
let _l2ChartSymbol = 'NQ';
let _l2ChartTF = '1m';
let _l2ChartInitialized = false;
let _l2CandlePollTimer = null;
let _l2SeamTime = 0;  // timestamp of the first candle with live bubble data
let _l2FetchController = null;   // AbortController for in-flight fetch cancellation
let _l2FetchVersion = 0;         // bumped on each switch to discard stale responses
let _l2TapeAll = [];   // accumulated trades, newest first
let _useCanvasLadder = false;  // true when Canvas 2D ladder pane is active
let _l2KineticCursor = 0;     // tracks which trades have already been fed to KineticText

// ── Wall / Max Pain overlay state ──
let _ocRefreshTimer = null; // options chain auto-refresh

const L2_SYMBOLS = ['NQ', 'GC'];
const L2_TICK_SIZES = { NQ: 0.25, GC: 0.10 };

// ── Socket.IO connection for real-time candle/trade push ──
// ── Data Events Setup (delegated to DataFetch module) ──
function _setupDataEvents() {
    if (!window.AltarisEvents || window._dataEventsWired) return;
    window._dataEventsWired = true;

    window.AltarisEvents.on('data:candles:update', (data) => {
        if (typeof ChartCore === 'undefined' || ChartCore.getInstances().length === 0) return;
        if (data.symbol !== _l2ChartSymbol || data.tf !== _l2ChartTF) return;

        const et = _utcToET(data.time);
        try {
            if (typeof ChartCore !== 'undefined') {
                ChartCore.getInstances().forEach(inst => {
                    inst.candleSeries.update({
                        time: et, open: data.open, high: data.high, low: data.low, close: data.close
                    });
                    if (inst.volumeSeries) {
                        inst.volumeSeries.update({
                            time: et, value: data.volume || 0,
                            color: data.close >= data.open ? 'rgba(38,166,154,.25)' : 'rgba(239,83,80,.25)'
                        });
                    }
                    if (inst.bubbleSeries && data.bp) {
                        inst.bubbleSeries.update({
                            time: et, close: data.close, bp: data.bp,
                            icebergs: data.icebergs || null, sweeps: data.sweeps || null,
                            delta_div: data.delta_div || null, ignition: data.ignition || null,
                            spoofs: data.spoofs || null, drifting_iceberg: data.drifting_iceberg || null,
                            wall_gone: data.wall_gone || null
                        });
                    }
                });
            }
        } catch (e) {}
        _l2LastCandleTime = data.time;
    });

    window.AltarisEvents.on('data:trades:update', (data) => {
        const strip = document.getElementById('l2-symbol-prices');
        if (strip) {
            const priceEl = strip.querySelector(`[data-sym="${data.symbol}"] .price`);
            if (priceEl) priceEl.textContent = data.price.toFixed(2);
        }
        if (data.symbol === _l2ChartSymbol) {
            const spotEl = document.getElementById('t-spot');
            if (spotEl) spotEl.textContent = data.price.toFixed(2);
        }
        // Push the live trade into the tape buffer directly!
        if (typeof _l2RenderTape === 'function') {
            _l2RenderTape({ [data.symbol]: [data] });
        }
    });

    window.AltarisEvents.on('data:zone:update', (data) => {
        if (typeof ChartCore === 'undefined' || ChartCore.getInstances().length === 0) return;
        if (!data || data.error) return;

        if (typeof WallLines !== 'undefined') {
            WallLines.updateLive(data);
        }

        const setCW = document.getElementById('t-cw');
        const setPW = document.getElementById('t-pw');
        const setMP = document.getElementById('t-mp');
        const setFlip = document.getElementById('t-flip');
        const setDexL = document.getElementById('t-dex-long');
        const setDexS = document.getElementById('t-dex-short');
        const setNDex = document.getElementById('t-ndex');
        const setNetPrem = document.getElementById('t-net-prem');
        const setTheta = document.getElementById('t-net-theta');
        const setIvrv = document.getElementById('t-ivrv');

        if (setCW && data.underlying_call_wall) setCW.textContent = data.underlying_call_wall;
        if (setPW && data.underlying_put_wall) setPW.textContent = data.underlying_put_wall;
        if (setMP && data.underlying_max_pain) setMP.textContent = data.underlying_max_pain;
        if (setFlip && data.underlying_gamma_flip) setFlip.textContent = data.underlying_gamma_flip;
        if (setDexL && data.dex_wall_long_qqq !== undefined) setDexL.textContent = data.dex_wall_long_qqq;
        if (setDexS && data.dex_wall_short_qqq !== undefined) setDexS.textContent = data.dex_wall_short_qqq;
        if (setNDex && data.total_dex !== undefined) setNDex.textContent = (data.total_dex / 1e6).toFixed(2) + 'M';
        if (setNetPrem && data.net_premium_m !== undefined) setNetPrem.textContent = data.net_premium_m.toFixed(2) + 'M';
        if (setTheta && data.net_theta_m !== undefined) setTheta.textContent = data.net_theta_m.toFixed(2) + 'M';
        if (setIvrv && data.mean_iv !== undefined) setIvrv.textContent = data.mean_iv.toFixed(2) + '%';

        if (data.dex_profile && typeof ThermalFlare !== 'undefined') {
            ThermalFlare.updateData(data.dex_profile);
        }
    });

    window.AltarisEvents.on('data:tape:alert', (alert) => {
        // Match key: price + timestamp (ms) — same as used in _l2RenderTape
        const key = `${alert.price}_${alert.timestamp}`;
        _tapeAlerts.set(key, alert);
        // Cap size to prevent memory growth during high-freq iceberging
        if (_tapeAlerts.size > 500) {
            const keys = [..._tapeAlerts.keys()].slice(0, 100);
            keys.forEach(k => _tapeAlerts.delete(k));
        }
        // Auto-expire after 15s to prevent memory leak
        setTimeout(() => _tapeAlerts.delete(key), 15000);
    });

    // ── spot_update: Live NQ/QQQ/SPY/VIX spot from Schwab WS streamer ──
    window.AltarisEvents.on('data:spot:update', (data) => {
        if (!data || !data.ticker) return;
        // Update toolbar spot price if this is the chart symbol
        if (data.ticker === _l2ChartSymbol || data.ticker === 'NQ') {
            const spotEl = document.getElementById('t-spot');
            if (spotEl) spotEl.textContent = data.spot.toFixed(2);
        }
        // Update the l2 symbol strip
        const strip = document.getElementById('l2-symbol-prices');
        if (strip) {
            const priceEl = strip.querySelector(`[data-sym="${data.ticker}"] .price`);
            if (priceEl) priceEl.textContent = data.spot.toFixed(2);
        }
    });

    // ── edge_signal: Cross-asset conviction signal from EdgeDetector ──
    window.AltarisEvents.on('data:edge:signal', (signal) => {
        if (!signal) return;
        const isLong = (signal.type || '').includes('LONG');
        const dir = isLong ? '🟢' : '🔴';
        const conf = signal.confidence_pctl ? `P${signal.confidence_pctl.toFixed(0)}` : '';
        console.log(`[EDGE] ${dir} ${signal.type} ${conf}`);
        // Show toast for high-conviction signals
        if (typeof AltarisToast !== 'undefined' && signal.confidence_pctl >= 70) {
            const msg = `${dir} ${signal.type} ${conf} ${signal.symbol || ''}`;
            AltarisToast[isLong ? 'success' : 'warn'](msg);
        }
        // Feed signal to _l2RenderSignals if signals grid exists
        if (window._latestEdgeSignals === undefined) window._latestEdgeSignals = {};
        window._latestEdgeSignals[signal.type] = signal;
    });

    // ── eq_book_update: QQQ NASDAQ L2 book depth from Schwab ──
    window.AltarisEvents.on('data:eqbook:update', (data) => {
        if (!data || !data.bids || !data.asks) return;
        // Store latest book data globally for ladder/book renderers
        window._latestEqBook = data;
    });

    // ── screener_option_update: Unusual options activity ──
    window.AltarisEvents.on('data:screener:update', (data) => {
        if (!data) return;
        window._latestScreenerData = data;
        _renderScreenerAlerts(data);
    });

    // ── l2_update: Full L2 state via WebSocket (replaces /api/l2 REST poll) ──
    window.AltarisEvents.on('data:l2:update', (data) => {
        if (!data) return;
        window._l2WsActive = true;
        window._l2WsLastTs = Date.now();
        _l2Render(data);
    });

    // ── candle_history: WS push of full candle history (with bp) on connect ──
    // Server pushes this immediately on Socket.IO connect (server.py handle_connect)
    // and on subscribe. Without this handler, bp data was silently dropped and
    // volume bubbles would never render until REST polling accumulated enough data.
    window.AltarisEvents.on('data:candles:history', (data) => {
        if (!data || !data.candles || data.candles.length === 0) return;
        if (data.symbol !== _l2ChartSymbol || data.tf !== _l2ChartTF) return;
        // Buffer if chart not ready yet — replay from chart:ready handler
        if (typeof ChartCore === 'undefined' || ChartCore.getInstances().length === 0) {
            window._pendingCandleHistory = data;
            return;
        }

        const candles = data.candles;
        const ohlc = candles.map(c => ({
            time: _utcToET(c.time), open: c.open, high: c.high, low: c.low, close: c.close
        }));
        const vol = candles.map(c => ({
            time: _utcToET(c.time), value: c.volume || 0,
            color: c.close >= c.open ? 'rgba(38,166,154,.25)' : 'rgba(239,83,80,.25)'
        }));

        const instances = ChartCore.getInstances();
        instances.forEach(inst => {
            inst.candleSeries.setData(ohlc);
            if (inst.volumeSeries) inst.volumeSeries.setData(vol);

            if (inst.feature === 'chart' && inst.bubbleSeries) {
                const bubbleData = candles.map(c => ({
                    time: _utcToET(c.time), close: c.close, bp: c.bp || null,
                    icebergs: c.icebergs || null, sweeps: c.sweeps || null,
                    delta_div: c.delta_div || null, ignition: c.ignition || null,
                    spoofs: c.spoofs || null, drifting_iceberg: c.drifting_iceberg || null,
                    wall_gone: c.wall_gone || null
                }));
                inst.bubbleSeries.setData(bubbleData);
            }

            if (inst.feature === 'heatmap') {
                const _hChart = inst.chart;
                setTimeout(() => {
                    _hChart.timeScale().applyOptions({ barSpacing: 20, rightOffset: 3 });
                    _hChart.timeScale().scrollToPosition(-3, false);
                    _hChart.priceScale('right').applyOptions({
                        scaleMargins: { top: 0.05, bottom: 0.05 },
                        autoScale: true,
                    });
                }, 100);
            } else {
                inst.chart.timeScale().fitContent();
                // Deferred re-fit in case layout hasn't fully settled
                setTimeout(() => {
                    try { inst.chart.timeScale().fitContent(); } catch(e) {}
                }, 250);
            }
        });

        // ── Seam marker: first candle with bp = live data start ──
        _l2SeamTime = 0;
        for (const c of candles) {
            if (c.bp && Object.keys(c.bp).length > 0) {
                _l2SeamTime = _utcToET(c.time);
                break;
            }
        }
        if (_l2SeamTime > 0) {
            ChartCore.getInstances().forEach(inst => {
                inst.candleSeries.setMarkers([{
                    time: _l2SeamTime, position: 'belowBar', color: 'rgba(124,90,247,.8)',
                    shape: 'arrowUp', text: 'LIVE ▸'
                }]);
            });
        }

        // Cache for layout switches + update poll cursor
        _l2CandleDataCache = { ohlc, vol };
        _l2LastCandleTime = candles[candles.length - 1].time;

        // Dismiss welcome screen on first data
        if (window._dismissWelcome) window._dismissWelcome();
    });
}

// ── Timezone helper ──
// Server sends raw UTC epoch seconds. LWC uses them as-is.
// ET display is handled by _applyETLabelFormatter in chart_core.js.
function _utcToET(utcEpoch) {
    return utcEpoch;
}

const SIG_META = {
    shannon_entropy:     { label: 'Shannon Entropy',     unit: 'bits', hi: 8,    good: 'chaos', color: '#7c5af7' },
    ising_magnetization: { label: 'Ising Magnetization', unit: '',     hi: 1,    good: 'trend', color: '#28c4f8' },
    reynolds_number:     { label: 'Reynolds Number',     unit: '',     hi: 5000, good: 'flow',  color: '#1fd17a' },
    lppl_sornette:       { label: 'LPPL Bubble',         unit: '',     hi: 1,    good: 'risk',  color: '#e8435a' },
    powerlaw_tail:       { label: 'Power-Law α',         unit: '',     hi: 6,    good: 'tail',  color: '#e6b430' },
    transfer_entropy:    { label: 'Transfer Entropy',    unit: 'bits', hi: 4,    good: 'cause', color: '#f07828' },
    percolation_threshold: { label: 'Percolation θ',    unit: '',     hi: 1,    good: 'connect',color: '#9b7ef8' },
    mutual_information:  { label: 'Mutual Info',         unit: 'bits', hi: 3,    good: 'corr',  color: '#b06fff' },
};

function _l2FmtTime(ts) {
    if (!ts) return '—';
    try {
        const d = new Date(ts);
        const hh = d.getHours().toString().padStart(2,'0');
        const mm = d.getMinutes().toString().padStart(2,'0');
        const ss = d.getSeconds().toString().padStart(2,'0');
        return `${hh}:${mm}:${ss}`;
    } catch { return '—'; }
}

function _l2RenderImbalance(data) {
    const row = document.getElementById('l2-imbalance-row');
    if (!row) return;
    const dom = data.dom || {};
    const mid = data.mid_prices || {};
    row.innerHTML = L2_SYMBOLS.map(sym => {
        const snap = dom[sym] || {};
        const imb = snap.imbalance != null ? snap.imbalance : (data.imbalance || {})[sym];
        const midP = mid[sym] || 0;
        const pct = imb != null ? Math.abs(imb) * 50 : 0; // 0..50% from center
        const isBid = imb != null && imb > 0;
        const barClr = imb == null ? '#555' : (isBid ? 'var(--green)' : 'var(--red)');
        const side = imb == null ? '—' : (isBid ? 'BID HVY' : 'ASK HVY');
        const imbTxt = imb != null ? (imb * 100).toFixed(1) + '%' : '—';
        const midTxt = midP > 0 ? midP.toFixed(2) : '—';
        return `<div class="l2-imb-card">
          <div class="l2-imb-label">${sym} <span style="color:var(--text);font-size:.72rem">${midTxt}</span></div>
          <div class="l2-imb-bar-wrap">
            <div class="l2-imb-bar" style="
              width:${pct}%;
              background:${barClr};
              transform-origin:left;
              ${isBid ? 'right:50%;left:auto;transform:scaleX(-1)' : 'left:50%'};
            "></div>
          </div>
          <div class="l2-imb-val">
            <span>${imbTxt}</span>
            <span class="l2-imb-side" style="color:${barClr}">${side}</span>
          </div>
        </div>`;
    }).join('');
}

let _domNodesCreated = false;
let _domMemory = {}; 
let _autoCenterDOM = true;

function _l2RenderDOM(dom) {
    const symData = dom ? (dom[_l2ChartSymbol] || {}) : {};

    // ── Canvas 2D Ladder path: pipe data to renderDepthLadder ──
    if (_useCanvasLadder) {
        const ladderCanvas = document.getElementById('dom-ladder-canvas');
        if (ladderCanvas && typeof renderDepthLadder === 'function') {
            // Compute midPrice with multiple fallbacks
            const bestBid = symData.best_bid || 0;
            const bestAsk = symData.best_ask || 0;
            let midPrice = symData.mid_price || 0;
            if (!midPrice && bestBid && bestAsk) midPrice = (bestBid + bestAsk) / 2;
            // Fallback: use heatmap data cache
            if (!midPrice && window._latestHeatmapData) midPrice = window._latestHeatmapData.mid_price || 0;
            // Fallback: use any active chart instance's last price
            if (!midPrice && typeof ChartCore !== 'undefined') {
                const inst = ChartCore.getInstances()[0];
                if (inst && inst.candleSeries) {
                    try {
                        const lr = inst.chart.timeScale().getVisibleLogicalRange();
                        // Can't easily get price — skip
                    } catch(e) {}
                }
            }
            
            if (midPrice && (Object.keys(symData.bids || {}).length > 0 || Object.keys(symData.asks || {}).length > 0)) {
                renderDepthLadder(ladderCanvas, null, symData, midPrice);
            } else {
                // No DOM data — show waiting message
                const ctx = ladderCanvas.getContext('2d');
                if (ctx) {
                    const dpr = window.devicePixelRatio || 1;
                    const cssW = ladderCanvas.clientWidth;
                    const cssH = ladderCanvas.clientHeight;
                    if (ladderCanvas.width !== Math.round(cssW * dpr)) {
                        ladderCanvas.width = Math.round(cssW * dpr);
                        ladderCanvas.height = Math.round(cssH * dpr);
                    }
                    ctx.save();
                    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
                    // Dark background
                    const bg = ctx.createLinearGradient(0, 0, 0, cssH);
                    bg.addColorStop(0, 'rgba(6, 9, 20, 0.98)');
                    bg.addColorStop(1, 'rgba(6, 8, 18, 0.98)');
                    ctx.fillStyle = bg;
                    ctx.fillRect(0, 0, cssW, cssH);
                    // Message
                    ctx.fillStyle = 'rgba(140, 160, 200, 0.4)';
                    ctx.font = '12px "JetBrains Mono", monospace';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText('⏸ WAITING FOR DOM DATA', cssW / 2, cssH / 2 - 10);
                    ctx.font = '9px "JetBrains Mono", monospace';
                    ctx.fillStyle = 'rgba(140, 160, 200, 0.25)';
                    ctx.fillText('Market may be closed', cssW / 2, cssH / 2 + 12);
                    ctx.restore();
                }
            }
        }
        return; // skip HTML DOM rendering
    }

    // ── HTML DOM fallback path ──
    const body = document.getElementById('l2-dom-body');
    const stats = document.getElementById('l2-dom-stats');
    if (!body) return;
    
    const bids = symData.bids || {};
    const asks = symData.asks || {};
    const bestBid = symData.best_bid || 0;
    const bestAsk = symData.best_ask || 0;
    
    if (stats) stats.textContent = `bids: ${Object.keys(bids).length}  asks: ${Object.keys(asks).length}  spread: ${bestAsk && bestBid ? (bestAsk - bestBid).toFixed(2) : '—'}`;

    const bidPrices = Object.keys(bids).map(Number).sort((a,b) => b - a).slice(0, 20);
    const askPrices = Object.keys(asks).map(Number).sort((a,b) => a - b).slice(0, 20);
    const maxBid = bidPrices.reduce((m, p) => Math.max(m, bids[p] || 0), 1);
    const maxAsk = askPrices.reduce((m, p) => Math.max(m, asks[p] || 0), 1);
    
    const askReversed = [...askPrices].reverse();
    
    // 1. Initialize permanent nodes if first run
    if (!_domNodesCreated || body.children.length === 0) {
        body.innerHTML = ''; // clear
        let newRows = [];
        
        // Asks
        for (let i = 0; i < 20; i++) {
            const div = document.createElement('div');
            div.className = 'l2-dom-row';
            div.innerHTML = `
              <span class="l2-dom-bid l2-dom-vol"></span>
              <span class="l2-dom-price" style="color:var(--red)">—</span>
              <span class="l2-dom-ask l2-dom-vol" style="color:var(--red)">
                <div class="l2-dom-bar ask-bar" style="width:0%"></div>
                <span class="vol-text"></span>
              </span>
            `;
            body.appendChild(div);
            newRows.push({ el: div, type: 'ask', priceEl: div.querySelector('.l2-dom-price'), barEl: div.querySelector('.ask-bar'), txtEl: div.querySelector('.vol-text') });
        }
        
        // Mid
        const midDiv = document.createElement('div');
        midDiv.className = 'l2-dom-row l2-at-market';
        midDiv.innerHTML = `
          <span class="l2-dom-bid" style="font-size:.55rem;color:var(--cyan)">BID</span>
          <span class="l2-dom-price">— MID —</span>
          <span class="l2-dom-ask" style="font-size:.55rem;color:var(--cyan)">ASK</span>
        `;
        body.appendChild(midDiv);
        newRows.push({ el: midDiv, type: 'mid' });
        
        // Bids
        for (let i = 0; i < 20; i++) {
            const div = document.createElement('div');
            div.className = 'l2-dom-row';
            div.innerHTML = `
              <span class="l2-dom-bid l2-dom-vol" style="color:var(--green)">
                <div class="l2-dom-bar bid-bar" style="width:0%"></div>
                <span class="vol-text"></span>
              </span>
              <span class="l2-dom-price" style="color:var(--green)">—</span>
              <span class="l2-dom-ask l2-dom-vol"></span>
            `;
            body.appendChild(div);
            newRows.push({ el: div, type: 'bid', priceEl: div.querySelector('.l2-dom-price'), barEl: div.querySelector('.bid-bar'), txtEl: div.querySelector('.vol-text') });
        }
        
        _domNodesCreated = newRows;
    }
    
    // 2. Update nodes in place
    let askIdx = 0;
    let bidIdx = 0;
    
    const _flashClasses = ['flash-add-ask', 'flash-add-bid', 'flash-pull'];
    const applyDelta = (row, volStr, p, type) => {
        let vol = parseInt(volStr.replace(/,/g, '')) || 0;
        let oldVol = _domMemory[p] || 0;
        if (oldVol > 0 && vol !== oldVol) {
            let diff = vol - oldVol;
            let pct = diff / oldVol;
            let flashClass = null;
            if (pct > 0.25) {
                flashClass = type === 'ask' ? 'flash-add-ask' : 'flash-add-bid';
            } else if (pct < -0.25) {
                flashClass = 'flash-pull';
            }
            if (flashClass) {
                row.el.classList.remove(..._flashClasses);
                // Double-rAF to retrigger animation without synchronous reflow
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        row.el.classList.add(flashClass);
                    });
                });
                // Clean up class after animation ends so future flashes can retrigger
                row.el.addEventListener('animationend', function _cleanup() {
                    row.el.classList.remove(..._flashClasses);
                    row.el.removeEventListener('animationend', _cleanup);
                });
            }
        }
        _domMemory[p] = vol;
    };
    
    _domNodesCreated.forEach(row => {
        if (row.type === 'ask') {
            let p = askReversed[askIdx];
            if (p) {
                const vol = asks[p] || 0;
                const barW = (vol / maxAsk * 100).toFixed(1);
                const vStr = vol.toLocaleString();
                
                row.priceEl.textContent = p.toFixed(2);
                row.txtEl.textContent = vStr;
                row.barEl.style.width = `${barW}%`;
                
                if (askIdx === askReversed.length - 1 && vol > (bids[bestBid]||1)*3) {
                    row.txtEl.style.color = '#ffdc00';
                    row.txtEl.style.textShadow = '0 0 5px rgba(255,220,0,0.4)';
                } else {
                    row.txtEl.style.color = '';
                    row.txtEl.style.textShadow = '';
                }

                applyDelta(row, vStr, p, 'ask');
            } else {
                row.priceEl.textContent = '—';
                row.txtEl.textContent = '';
                row.barEl.style.width = '0%';
            }
            askIdx++;
        } 
        else if (row.type === 'bid') {
            let p = bidPrices[bidIdx];
            if (p) {
                const vol = bids[p] || 0;
                const barW = (vol / maxBid * 100).toFixed(1);
                const vStr = vol.toLocaleString();
                
                row.priceEl.textContent = p.toFixed(2);
                row.txtEl.textContent = vStr;
                row.barEl.style.width = `${barW}%`;
                
                if (bidIdx === 0 && vol > (asks[bestAsk]||1)*3) {
                    row.txtEl.style.color = '#ffdc00';
                    row.txtEl.style.textShadow = '0 0 5px rgba(255,220,0,0.4)';
                } else {
                    row.txtEl.style.color = '';
                    row.txtEl.style.textShadow = '';
                }

                applyDelta(row, vStr, p, 'bid');
            } else {
                row.priceEl.textContent = '—';
                row.txtEl.textContent = '';
                row.barEl.style.width = '0%';
            }
            bidIdx++;
        }
    });

    const autoCenterToggle = document.getElementById('hms-auto-center');
    _autoCenterDOM = autoCenterToggle ? autoCenterToggle.checked : true;

    if (_autoCenterDOM && body.scrollHeight > body.clientHeight) {
        const midRow = _domNodesCreated.find(r => r.type === 'mid')?.el;
        if (midRow) {
            body.scrollTop = midRow.offsetTop - (body.clientHeight / 2) + (midRow.clientHeight / 2);
        }
    }
}

let _l2TapePrevLen = 0;  // tape dedup tracker
const _TAPE_MAX_ROWS = 80;
let _tapeNodesCreated = false;

// ── Tape Alert Buffer (fed from Python EdgeDetector via Socket.IO) ──
// Keys: `${price}_${timestamp}` → alert object with tier, pctl, regime, source
const _tapeAlerts = new Map();

// ── Delta Accumulator (rolling buy vs sell volume) ──
const _DELTA_WINDOW = 300;  // track last 300 trades
let _deltaHistory = [];     // { side: 'buy'|'sell', vol: number }
let _deltaStripEl = null;
let _deltaLabelEl = null;

function _updateDeltaStrip() {
    if (!_deltaStripEl) {
        _deltaStripEl = document.getElementById('tape-delta-fill');
        _deltaLabelEl = document.getElementById('tape-delta-labels');
    }
    if (!_deltaStripEl) return;

    let buyVol = 0, sellVol = 0;
    for (const d of _deltaHistory) {
        if (d.side === 'buy') buyVol += d.vol;
        else sellVol += d.vol;
    }
    const total = buyVol + sellVol;
    if (total === 0) return;

    const buyPct = (buyVol / total) * 100;
    const sellPct = 100 - buyPct;

    if (buyPct >= sellPct) {
        _deltaStripEl.style.width = `${buyPct}%`;
        _deltaStripEl.style.left = '0';
        _deltaStripEl.style.right = 'auto';
        _deltaStripEl.className = 'tape-delta-strip-fill';
    } else {
        _deltaStripEl.style.width = `${sellPct}%`;
        _deltaStripEl.style.left = 'auto';
        _deltaStripEl.style.right = '0';
        _deltaStripEl.className = 'tape-delta-strip-fill sell-dominant';
    }

    if (_deltaLabelEl) {
        _deltaLabelEl.innerHTML = `<span class="buy-pct">${Math.round(buyPct)}% BUY</span><span class="sell-pct">${Math.round(sellPct)}% SELL</span>`;
    }
}

// ── EQ Context Strip (Raw Cross-Asset Signals) ──
let _eqCtxCached = false;
let _eqCtxEls = {};

const _REGIME_LABELS = {
    'crash_tail_risk': 'CRASH',
    'short_gamma_volatile': 'SHORT γ',
    'transition': 'TRANS',
    'long_gamma_stable': 'LONG γ',
    'pin_mean_revert': 'PINNED',
};

function _updateEqContext(data) {
    if (!_eqCtxCached) {
        _eqCtxEls = {
            regime: document.getElementById('eq-ctx-regime'),
            hawkes: document.getElementById('eq-ctx-hawkes'),
            cp:     document.getElementById('eq-ctx-cp'),
            ice:    document.getElementById('eq-ctx-ice'),
            mm:     document.getElementById('eq-ctx-mm'),
        };
        if (!_eqCtxEls.regime) return;  // EQ Book pane not mounted
        _eqCtxCached = true;
    }

    // 1. Regime badge
    const regime = data.regime || 'transition';
    const regimeLabel = _REGIME_LABELS[regime] || regime.toUpperCase();
    _eqCtxEls.regime.textContent = regimeLabel;
    _eqCtxEls.regime.className = 'eq-ctx-badge regime regime-' + regime.replace(/_/g, '-');

    // 2. Hawkes λ percentile
    const hp = data.hawkes_pctl || 50;
    _eqCtxEls.hawkes.textContent = `λ P${hp}`;
    _eqCtxEls.hawkes.className = 'eq-ctx-badge hawkes' +
        (hp >= 90 ? ' ctx-hot' : hp >= 75 ? ' ctx-warm' : '');

    // 3. C/P ratio
    if (data.cp_ratio !== null && data.cp_ratio !== undefined) {
        _eqCtxEls.cp.textContent = `C/P ${data.cp_ratio}`;
        _eqCtxEls.cp.className = 'eq-ctx-badge cp' +
            (data.cp_ratio >= 2.0 ? ' ctx-call-heavy' :
             data.cp_ratio <= 0.5 ? ' ctx-put-heavy' : '');
    } else {
        _eqCtxEls.cp.textContent = 'C/P —';
        _eqCtxEls.cp.className = 'eq-ctx-badge cp';
    }

    // 4. Iceberg counts (raw)
    const iceL = data.ice_long || 0;
    const iceS = data.ice_short || 0;
    if (iceL + iceS > 0) {
        _eqCtxEls.ice.innerHTML = `ICE <span class="ice-bull">${iceL}▲</span><span class="ice-bear">${iceS}▼</span>`;
        _eqCtxEls.ice.className = 'eq-ctx-badge ice ctx-active';
    } else {
        _eqCtxEls.ice.textContent = 'ICE —';
        _eqCtxEls.ice.className = 'eq-ctx-badge ice';
    }

    // 5. MM pull events (raw counts)
    const mmB = data.mm_bid_pulls || 0;
    const mmA = data.mm_ask_pulls || 0;
    const mmSD = data.mm_smart_dumb || 0;
    if (mmB + mmA > 0) {
        let mmText = `MM B${mmB} A${mmA}`;
        if (mmSD > 0) mmText += ' ⚠';
        _eqCtxEls.mm.textContent = mmText;
        _eqCtxEls.mm.className = 'eq-ctx-badge mm ctx-active' +
            (mmSD > 0 ? ' ctx-diverge' : '');
    } else {
        _eqCtxEls.mm.textContent = 'MM —';
        _eqCtxEls.mm.className = 'eq-ctx-badge mm';
    }
}

// Wire up eq_context event
if (window.AltarisEvents) {
    window.AltarisEvents.on('data:eq:context', _updateEqContext);
}

function _l2RenderTape(trades) {
    const body = document.getElementById('l2-tape-body');
    const cnt  = document.getElementById('l2-tape-count');
    if (!body) return;
    let hasNewTrades = false;
    // Merge new trades into our accumulated list, newest first
    // Only process trades for the actively focused chart symbol to prevent scale thrashing
    const sym = _l2ChartSymbol;
    if (trades[sym]) {
        for (const t of [...trades[sym]].reverse()) {
            hasNewTrades = true;
            const entry = { ...t, sym };
            _l2TapeAll.push(entry);
            if (_l2TapeAll.length > 2000) _l2TapeAll = _l2TapeAll.slice(-1500);
            // Feed delta accumulator
            const side = t.side || (t.spin > 0 ? 'buy' : 'sell');
            const vol = t.volume || t.v || 1;
            _deltaHistory.push({ side, vol });
        }
    }
    // Cap array size (trim from front = oldest trades)
    if (_l2TapeAll.length > 300) _l2TapeAll = _l2TapeAll.slice(-300);
    if (_deltaHistory.length > _DELTA_WINDOW) _deltaHistory = _deltaHistory.slice(-_DELTA_WINDOW);
    if (cnt) cnt.textContent = `${_l2TapeAll.length} prints`;
    // Feed latest trade to ladder for last-trade marker
    if (_l2TapeAll.length > 0) {
        const lt = _l2TapeAll[_l2TapeAll.length - 1];  // newest is at the end
        window._lastTradeForLadder = { price: lt.price, side: lt.side || (lt.spin > 0 ? 'buy' : 'sell') };
    }
    // Skip if no new trades arrived
    if (!hasNewTrades) return;

    // Update delta strip
    _updateDeltaStrip();

    // 1. Initialize permanent nodes once
    if (!_tapeNodesCreated || body.children.length === 0) {
        body.innerHTML = '';
        let nodes = [];
        for (let i = 0; i < _TAPE_MAX_ROWS; i++) {
            const div = document.createElement('div');
            div.className = 'l2-tape-row';
            div.style.display = 'none';
            div.innerHTML = `<span class="tape-ts"></span><span class="tape-price"></span><span class="l2-tape-vol"></span><span class="l2-tape-side"></span>`;
            body.appendChild(div);
            nodes.push({
                el: div,
                tsEl: div.querySelector('.tape-ts'),
                priceEl: div.querySelector('.tape-price'),
                volEl: div.querySelector('.l2-tape-vol'),
                sideEl: div.querySelector('.l2-tape-side')
            });
        }
        _tapeNodesCreated = nodes;
    }

    // 2. Update nodes in place — TWO-LAYER glow system
    //    Layer 1: Python EdgeDetector tape_alert (highest trust, enriched)
    //    Layer 2: JS SigmaEngine.classifyTrade() (instant, local fallback)
    const visible = _l2TapeAll.slice(-_TAPE_MAX_ROWS).reverse();  // newest first for display
    for (let i = 0; i < _TAPE_MAX_ROWS; i++) {
        const row = _tapeNodesCreated[i];
        const t = visible[i];
        if (t) {
            const side = t.side || (t.spin > 0 ? 'buy' : 'sell');
            const vol = t.volume || t.v || 0;
            row.el.style.display = '';
            row.tsEl.textContent = _l2FmtTime(t.timestamp);
            row.priceEl.textContent = t.price != null ? t.price.toFixed(2) : '—';
            row.sideEl.textContent = side.toUpperCase();

            // ── Layer 1: Check EdgeDetector alert buffer ──
            const tsMs = typeof t.timestamp === 'number' && t.timestamp < 1e12
                ? Math.round(t.timestamp * 1000) : Math.round(t.timestamp || 0);
            let alert = null;
            for (const [key, a] of _tapeAlerts) {
                if (Math.abs(a.price - (t.price || 0)) < 0.01 && Math.abs(a.timestamp - tsMs) < 500) {
                    alert = a;
                    break;
                }
            }

            // ── Layer 2: Local SigmaEngine classification (fallback) ──
            let localTier = 'noise';
            let localPctl = 50;
            if (!alert && typeof SigmaEngine !== 'undefined' && vol > 0) {
                const classification = SigmaEngine.classifyTrade(vol, side);
                localTier = classification.tier;
                localPctl = classification.pctl;
            }

            // ── Build CSS class list ──
            let cls = `l2-tape-row ${side}`;
            const effectiveTier = alert ? alert.tier : localTier;
            const effectivePctl = alert ? alert.pctl : localPctl;

            if (effectiveTier !== 'noise') {
                cls += ' tape-glow';
                cls += ` tape-${effectiveTier}`;
                if (alert && alert.source === 'nq_detection') {
                    cls += ' tape-nq-verified';
                }
            }
            row.el.className = cls;

            // Volume display: add percentile badge for institutional+ prints
            if (effectiveTier === 'whale' || effectiveTier === 'inst') {
                row.volEl.innerHTML = `${vol.toLocaleString()} <span class="tape-pctl-badge">P${Math.round(effectivePctl)}</span>`;
            } else {
                row.volEl.textContent = vol > 0 ? vol.toLocaleString() : '—';
            }
        } else {
            row.el.style.display = 'none';
        }
    }
}

let _lastSignalsKey = '';
function _l2RenderSignals(signals) {
    const grid = document.getElementById('l2-signals-grid');
    if (!grid) return;
    // Dedup: signals only change every ~60s, skip redundant innerHTML rebuilds
    const sigKey = JSON.stringify(signals);
    if (sigKey === _lastSignalsKey) return;
    _lastSignalsKey = sigKey;
    if (!signals || Object.values(signals).every(v => v == null)) {
        grid.innerHTML = '<div style="color:var(--dim);padding:30px;text-align:center;grid-column:1/-1">⏳ Signals compute after 60s of live data + backfill...</div>';
        return;
    }
    grid.innerHTML = Object.entries(SIG_META).map(([key, meta]) => {
        const raw = signals[key];
        let val = '—', fill = 0, fillClr = meta.color;
        if (raw != null && typeof raw === 'object') {
            // complex signal obj: try common fields
            const v = raw.value ?? raw.signal ?? raw.score ?? raw.magnetization ?? raw.entropy ?? raw.reynolds ?? null;
            if (v != null) { val = typeof v === 'number' ? v.toFixed(4) : String(v).slice(0,10); fill = Math.min(100, Math.abs(v) / meta.hi * 100); }
        } else if (raw != null) {
            val = typeof raw === 'number' ? raw.toFixed(4) : String(raw).slice(0,10);
            fill = Math.min(100, Math.abs(parseFloat(raw) || 0) / meta.hi * 100);
        }
        return `<div class="l2-signal-card">
          <div class="l2-signal-name">${meta.label}</div>
          <div class="l2-signal-val" style="color:${meta.color}">${val}${meta.unit ? '<span style="font-size:.65rem;opacity:.6;margin-left:4px">'+meta.unit+'</span>': ''}</div>
          <div class="l2-signal-bar"><div class="l2-signal-fill" style="width:${fill}%;background:${fillClr}"></div></div>
        </div>`;
    }).join('');
}

// ── OC HEAT: Options Chain GEX Heatmap (Canvas2D) ──
function _drawOcHeat(canvas, ocData) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    if (cssW < 10 || cssH < 10) return;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    ctx.fillStyle = '#070a14';
    ctx.fillRect(0, 0, cssW, cssH);

    // Collect strikes and DTEs
    const allStrikes = new Set();
    const dteKeys = Object.keys(ocData).map(Number).sort((a, b) => a - b);
    for (const dte of dteKeys) {
        for (const entry of Object.values(ocData[dte])) allStrikes.add(entry.strike);
    }
    const strikes = Array.from(allStrikes).sort((a, b) => a - b);

    if (strikes.length < 2 || dteKeys.length < 1) {
        ctx.fillStyle = 'rgba(140,160,200,.3)';
        ctx.font = '11px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for options data...', cssW / 2, cssH / 2);
        ctx.restore();
        return;
    }

    // Bucket DTEs — only keep buckets with data
    const dteBucketDef = [{max:0,label:'0DTE'},{max:3,label:'1-3d'},{max:7,label:'4-7d'},{max:14,label:'8-14d'},{max:30,label:'15-30d'},{max:Infinity,label:'30d+'}];
    const dteBuckets = [];
    for (const def of dteBucketDef) {
        const lo = dteBuckets.length > 0 ? dteBucketDef[dteBuckets.length - 1].max : -1;
        const matching = dteKeys.filter(d => d <= def.max && d > lo);
        if (matching.length > 0) dteBuckets.push({ ...def, dtes: matching });
    }
    if (dteBuckets.length === 0) { ctx.restore(); return; }

    // Layout
    const headerH = 28;
    const padL = 46, padR = 12, padT = headerH + 4, padB = 22;
    const plotW = cssW - padL - padR;
    const plotH = cssH - padT - padB;
    const cellW = plotW / strikes.length;
    const cellH = Math.min(plotH / dteBuckets.length, 80); // cap row height
    const usedH = cellH * dteBuckets.length;
    const yOffset = padT + (plotH - usedH) / 2; // vertically center

    // Max |GEX| for normalization
    let maxGex = 0.001;
    for (const dte of dteKeys) {
        for (const entry of Object.values(ocData[dte])) {
            if (Math.abs(entry.gex) > maxGex) maxGex = Math.abs(entry.gex);
        }
    }

    // (spot marker uses ATM strike midpoint below)

    // ── Header bar ──
    ctx.fillStyle = 'rgba(255,255,255,.04)';
    ctx.fillRect(0, 0, cssW, headerH);
    ctx.fillStyle = 'rgba(180,190,220,.7)';
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText('GEX HEATMAP', 8, 18);
    ctx.fillStyle = 'rgba(140,160,200,.4)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText(`${strikes.length} strikes \u00b7 ${dteKeys.length} DTE`, 100, 18);
    // Legend
    ctx.textAlign = 'right';
    ctx.fillStyle = '#1fd17a';
    ctx.fillRect(cssW - 120, 10, 10, 10);
    ctx.fillStyle = 'rgba(180,190,220,.5)';
    ctx.fillText('+\u0393 Long', cssW - 66, 18);
    ctx.fillStyle = '#e03060';
    ctx.fillRect(cssW - 60, 10, 10, 10);
    ctx.fillStyle = 'rgba(180,190,220,.5)';
    ctx.fillText('\u2212\u0393 Short', cssW - 6, 18);

    // ── Draw cells ──
    for (let bi = 0; bi < dteBuckets.length; bi++) {
        const bucket = dteBuckets[bi];
        // Row separator line
        ctx.strokeStyle = 'rgba(255,255,255,.04)';
        ctx.beginPath();
        ctx.moveTo(padL, yOffset + bi * cellH);
        ctx.lineTo(cssW - padR, yOffset + bi * cellH);
        ctx.stroke();

        for (let si = 0; si < strikes.length; si++) {
            const K = strikes[si];
            let totalGex = 0;
            for (const dte of bucket.dtes) {
                const cEntry = ocData[dte] ? ocData[dte][K + '_C'] : null;
                const pEntry = ocData[dte] ? ocData[dte][K + '_P'] : null;
                if (cEntry) totalGex += cEntry.gex;
                if (pEntry) totalGex += pEntry.gex;
            }
            if (totalGex === 0) continue;

            const x = padL + si * cellW;
            const y = yOffset + bi * cellH;
            const norm = Math.min(Math.abs(totalGex) / maxGex, 1.0);
            const t = Math.pow(norm, 0.5);

            if (totalGex > 0) {
                const g = Math.round(80 + t * 175);
                ctx.fillStyle = `rgba(10, ${g}, ${Math.round(50 + t * 70)}, ${0.2 + t * 0.75})`;
            } else {
                const r = Math.round(80 + t * 175);
                ctx.fillStyle = `rgba(${r}, ${Math.round(15 + t * 35)}, ${Math.round(30 + t * 60)}, ${0.2 + t * 0.75})`;
            }
            ctx.fillRect(x + 0.5, y + 0.5, Math.max(cellW - 1, 1), cellH - 1);

            // Value label in roomy cells
            if (cellW > 28 && cellH > 16 && t > 0.15) {
                ctx.fillStyle = `rgba(255,255,255,${0.3 + t * 0.6})`;
                ctx.font = '7px "JetBrains Mono", monospace';
                ctx.textAlign = 'center';
                ctx.fillText(totalGex.toFixed(1), x + cellW / 2, y + cellH / 2 + 3);
            }
        }
    }

    // ── Y-axis: DTE labels ──
    ctx.fillStyle = 'rgba(160,175,210,.6)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    for (let bi = 0; bi < dteBuckets.length; bi++) {
        ctx.fillText(dteBuckets[bi].label, padL - 4, yOffset + bi * cellH + cellH / 2 + 3);
    }

    // ── X-axis: strike labels ──
    ctx.fillStyle = 'rgba(140,160,200,.45)';
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    const step = Math.max(1, Math.floor(strikes.length / 14));
    for (let si = 0; si < strikes.length; si += step) {
        ctx.fillText(strikes[si].toFixed(0), padL + si * cellW + cellW / 2, yOffset + usedH + 14);
    }

    // ── Spot price vertical marker ──
    if (strikes.length > 1) {
        // Find nearest strike to ATM (use middle of range as proxy)
        const midStrike = strikes[Math.floor(strikes.length / 2)];
        const spotIdx = strikes.findIndex(s => s >= midStrike);
        if (spotIdx >= 0) {
            const sx = padL + spotIdx * cellW + cellW / 2;
            ctx.strokeStyle = 'rgba(255,220,40,.35)';
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(sx, yOffset);
            ctx.lineTo(sx, yOffset + usedH);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = 'rgba(255,220,40,.5)';
            ctx.font = '7px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.fillText('ATM', sx, yOffset - 3);
        }
    }

    ctx.restore();
}

// ── KINETIC FALLBACK: Canvas2D trade heat ladder (when WebGL2 unavailable) ──
function _drawKineticFallback(canvas, heatMap) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    if (cssW < 10 || cssH < 10) return;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    ctx.fillStyle = '#070a14';
    ctx.fillRect(0, 0, cssW, cssH);

    const hmData = window._latestHeatmapData;
    const l2 = hmData ? hmData.domData : null;
    if (!l2 || !l2.mid_price) {
        ctx.fillStyle = 'rgba(140,160,200,.3)';
        ctx.font = '11px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for L2 data...', cssW / 2, cssH / 2);
        ctx.restore();
        return;
    }

    const mid = l2.mid_price;
    const bestBid = l2.best_bid || 0;
    const bestAsk = l2.best_ask || 0;
    const spread = bestAsk && bestBid ? (bestAsk - bestBid).toFixed(2) : '?';
    const bids = l2.bids || {};
    const asks = l2.asks || {};
    const tick = 0.25;
    const headerH = 26;
    const rowH = 17;
    const bodyH = cssH - headerH;
    const numRows = Math.floor(bodyH / rowH);
    const halfRows = Math.floor(numRows / 2);
    const now = Date.now();

    // Max depth & vol
    let maxDepth = 1, bidTotal = 0, askTotal = 0;
    for (const v of Object.values(bids)) { if (v > maxDepth) maxDepth = v; bidTotal += v; }
    for (const v of Object.values(asks)) { if (v > maxDepth) maxDepth = v; askTotal += v; }
    let maxVol = 1;
    for (const h of Object.values(heatMap)) if (h.vol > maxVol) maxVol = h.vol;

    // ── Header ──
    ctx.fillStyle = 'rgba(255,255,255,.04)';
    ctx.fillRect(0, 0, cssW, headerH);
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    ctx.fillStyle = 'rgba(180,190,220,.7)';
    ctx.textAlign = 'left';
    ctx.fillText('DEPTH LADDER', 8, 17);
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillStyle = 'rgba(255,220,40,.8)';
    ctx.fillText(`MID ${mid.toFixed(2)}`, 110, 17);
    ctx.fillStyle = 'rgba(140,160,200,.5)';
    ctx.fillText(`SPR ${spread}`, 210, 17);
    // Imbalance bar
    const total = bidTotal + askTotal || 1;
    const bidPct = bidTotal / total;
    const barX = cssW - 130, barW = 80, barY = 8, barH = 10;
    ctx.fillStyle = 'rgba(255,255,255,.06)';
    ctx.fillRect(barX, barY, barW, barH);
    ctx.fillStyle = 'rgba(0,200,120,.5)';
    ctx.fillRect(barX, barY, barW * bidPct, barH);
    ctx.fillStyle = 'rgba(220,40,80,.5)';
    ctx.fillRect(barX + barW * bidPct, barY, barW * (1 - bidPct), barH);
    ctx.fillStyle = 'rgba(180,190,220,.5)';
    ctx.font = '7px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText(`${(bidPct * 100).toFixed(0)}%`, barX - 2, barY + 8);
    ctx.textAlign = 'left';
    ctx.fillText(`${((1 - bidPct) * 100).toFixed(0)}%`, barX + barW + 2, barY + 8);

    // Column headers
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.fillStyle = 'rgba(0,200,120,.5)';
    ctx.textAlign = 'right';
    ctx.fillText('BID', cssW * 0.35 - 4, headerH + 10);
    ctx.fillStyle = 'rgba(160,170,200,.4)';
    ctx.textAlign = 'center';
    ctx.fillText('PRICE', cssW * 0.5, headerH + 10);
    ctx.fillStyle = 'rgba(220,50,80,.5)';
    ctx.textAlign = 'left';
    ctx.fillText('ASK', cssW * 0.65 + 4, headerH + 10);
    const ladderTop = headerH + 16;

    ctx.font = '10px "JetBrains Mono", monospace';
    for (let i = -halfRows; i <= halfRows; i++) {
        const price = Math.round((mid + i * tick) / tick) * tick;
        const y = ladderTop + (halfRows - i) * rowH;
        if (y < ladderTop || y > cssH) continue;

        const pk2 = price.toFixed(2);
        const pk1 = price.toFixed(1);
        const pk = price.toString();
        const bidSz = bids[pk2] || bids[pk1] || bids[pk] || 0;
        const askSz = asks[pk2] || asks[pk1] || asks[pk] || 0;

        // Alternating row background
        if (Math.abs(i) % 2 === 0) {
            ctx.fillStyle = 'rgba(255,255,255,.012)';
            ctx.fillRect(0, y - rowH / 2, cssW, rowH);
        }

        // Trade heat glow
        const heat = heatMap[pk2];
        if (heat && (now - heat.ts) < 5000) {
            const age = (now - heat.ts) / 5000;
            const hNorm = (heat.vol / maxVol) * (1 - age);
            if (hNorm > 0.01) {
                ctx.fillStyle = heat.side === 'bid'
                    ? `rgba(0, 220, 120, ${hNorm * 0.35})`
                    : `rgba(220, 40, 80, ${hNorm * 0.35})`;
                ctx.fillRect(0, y - rowH / 2, cssW, rowH);
            }
        }

        // Mid price highlight
        const isMid = Math.abs(price - mid) < tick * 0.6;
        if (isMid) {
            ctx.fillStyle = 'rgba(255, 220, 0, 0.08)';
            ctx.fillRect(0, y - rowH / 2, cssW, rowH);
            // Yellow border
            ctx.strokeStyle = 'rgba(255,220,40,.25)';
            ctx.strokeRect(0, y - rowH / 2, cssW, rowH);
        }

        // Bid bar
        if (bidSz > 0) {
            const norm = Math.min(bidSz / maxDepth, 1.0);
            const sqN = Math.sqrt(norm); // boost small values
            const barW = sqN * (cssW * 0.33);
            ctx.fillStyle = `rgba(0, 180, 100, ${0.12 + sqN * 0.4})`;
            ctx.fillRect(cssW * 0.35 - barW, y - rowH / 2 + 1, barW, rowH - 2);
            ctx.fillStyle = `rgba(0, 220, 130, ${0.5 + norm * 0.5})`;
            ctx.textAlign = 'right';
            ctx.fillText(bidSz.toString(), cssW * 0.35 - 4, y + 4);
        }

        // Price label
        ctx.fillStyle = isMid ? 'rgba(255, 220, 40, 0.95)' : 'rgba(160, 170, 200, 0.55)';
        ctx.textAlign = 'center';
        ctx.font = isMid ? 'bold 11px "JetBrains Mono", monospace' : '10px "JetBrains Mono", monospace';
        ctx.fillText(pk2, cssW * 0.5, y + 4);
        ctx.font = '10px "JetBrains Mono", monospace';

        // Ask bar
        if (askSz > 0) {
            const norm = Math.min(askSz / maxDepth, 1.0);
            const sqN = Math.sqrt(norm);
            const barW = sqN * (cssW * 0.33);
            ctx.fillStyle = `rgba(180, 30, 60, ${0.12 + sqN * 0.4})`;
            ctx.fillRect(cssW * 0.65, y - rowH / 2 + 1, barW, rowH - 2);
            ctx.fillStyle = `rgba(220, 50, 80, ${0.5 + norm * 0.5})`;
            ctx.textAlign = 'left';
            ctx.fillText(askSz.toString(), cssW * 0.65 + 4, y + 4);
        }
    }

    // Decay old heat entries
    for (const [k, h] of Object.entries(heatMap)) {
        if (now - h.ts > 10000) delete heatMap[k];
    }

    ctx.restore();
}

function _renderScreenerAlerts(data) {
    const grid = document.getElementById('l2-signals-grid');
    if (!grid) return;
    const alerts = data.alerts || [];
    if (alerts.length === 0) {
        grid.innerHTML = '<div style="color:rgba(140,160,200,.4);padding:30px;text-align:center;grid-column:1/-1;font-family:\'JetBrains Mono\',monospace;font-size:.75rem">No unusual options activity detected</div>';
        return;
    }
    grid.innerHTML = alerts.map(a => {
        const pct = a.percentChange || 0;
        const clr = pct >= 0 ? '#1fd17a' : '#e03060';
        const vol = (a.volume || 0).toLocaleString();
        const price = typeof a.lastPrice === 'number' ? a.lastPrice.toFixed(2) : '—';
        return `<div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:6px;padding:10px;font-family:'JetBrains Mono',monospace">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <span style="font-size:11px;font-weight:700;color:#e0e6f0">${a.symbol || '?'}</span>
                <span style="font-size:10px;font-weight:600;color:${clr}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span>
            </div>
            <div style="font-size:8px;color:rgba(140,160,200,.5);margin-bottom:6px;line-height:1.3">${a.description || ''}</div>
            <div style="display:flex;justify-content:space-between;font-size:9px">
                <span style="color:rgba(180,190,220,.7)">$${price}</span>
                <span style="color:rgba(140,160,200,.5)">${vol} vol</span>
            </div>
        </div>`;
    }).join('');
}

window._l2PendingElements = window._l2PendingElements || [];
function _l2InitCandleChart(container, featureKey) {
    if (!container) {
        // Fallback or onload trigger
        if (typeof LightweightCharts === 'undefined') return;
        
        let foundQueue = false;
        if (window._l2PendingElements.length > 0) {
            const queue = [...window._l2PendingElements];
            window._l2PendingElements = [];
            queue.forEach(item => _l2InitCandleChart(item.container, item.featureKey));
            foundQueue = true;
        }
        
        if (!foundQueue) {
            const fallback = _l2PendingContainer || document.getElementById('t-l2-candle-chart') || document.getElementById('l2-candle-chart');
            if (fallback) _l2InitCandleChart(fallback, fallback.dataset.feature || 'chart');
            _l2PendingContainer = null;
        }
        return;
    }
    
    featureKey = featureKey || container.dataset.feature || 'chart';
    
    // If Engine isn't loaded yet, queue it up for the onload event
    if (typeof LightweightCharts === 'undefined') {
        window._l2PendingElements.push({container, featureKey});
        return;
    }

    // Register event listener once
    if (!window._chartEventsWired) {
        window._chartEventsWired = true;
        if (window.AltarisEvents) {
            window.AltarisEvents.on('chart:ready', (data) => {
                const f = data.feature;

                // Legacy global pointers — only from 'chart' panes
                if (f === 'chart') {
                    _l2CandleChart = data.chart;
                    _l2CandleSeries = data.candleSeries;
                    _l2VolumeSeries = data.volumeSeries;
                    _l2BubbleSeries = data.bubbleSeries;

                    // WallLines init + first fetch (attachment now handled by ChartCore)
                    if (typeof WallLines !== 'undefined') {
                        WallLines.update();
                    }
                    // OptionsChain — only on chart panes
                    if (typeof OptionsChain !== 'undefined') {
                        OptionsChain.attachToSeries(data.candleSeries);
                    }
                }

                // ThermalFlare — on chart, gex, dex panes (these all show the exposure overlay)
                if ((f === 'chart' || f === 'gex' || f === 'dex') && typeof ThermalFlare !== 'undefined') {
                    ThermalFlare.init(data.container, data.chartH);
                    ThermalFlare.attachToSeries(data.candleSeries, data.container);
                }

                // Heatmap canvas pointer (for legacy code paths)
                if (data.feature === 'heatmap' && data.heatmapCanvas) {
                    window._domHeatmapCanvas = data.heatmapCanvas;
                }

                // Backfill new instances with cached candle data (fixes layout-switch blank charts)
                if (_l2CandleDataCache && data.candleSeries) {
                    data.candleSeries.setData(_l2CandleDataCache.ohlc);
                    if (data.volumeSeries) data.volumeSeries.setData(_l2CandleDataCache.vol);
                    
                    if (data.feature === 'heatmap') {
                        // Heatmap pane: defer zoom so LightweightCharts layout is complete
                        const _hChart = data.chart;
                        setTimeout(() => {
                            _hChart.timeScale().applyOptions({ barSpacing: 20, rightOffset: 3 });
                            _hChart.timeScale().scrollToPosition(-3, false);
                            _hChart.priceScale('right').applyOptions({
                                scaleMargins: { top: 0.05, bottom: 0.05 },
                                autoScale: true,
                            });
                        }, 100);
                    } else {
                        data.chart.timeScale().fitContent();
                    }
                }

                // Replay buffered candle_history if it arrived before chart was ready
                if (window._pendingCandleHistory) {
                    const pending = window._pendingCandleHistory;
                    window._pendingCandleHistory = null;
                    setTimeout(() => {
                        if (window.AltarisEvents) window.AltarisEvents.emit('data:candles:history', pending);
                    }, 100);
                }
            });
            window.AltarisEvents.on('chart:scroll', () => {
                if (typeof ThermalFlare !== 'undefined') requestAnimationFrame(() => ThermalFlare.render());
                if (typeof window._forceRenderHeatmap === 'function') requestAnimationFrame(() => window._forceRenderHeatmap());
            });
            window.AltarisEvents.on('chart:resize', () => {
                if (typeof window._forceRenderHeatmap === 'function') requestAnimationFrame(() => window._forceRenderHeatmap());
            });
        }
    }

    // Pass the feature key so ChartCore knows what overlays to create
    if (typeof ChartCore !== 'undefined') {
        ChartCore.init(container, _l2ChartSymbol, featureKey);
    }

    // ── Symbol buttons (old tab layout) — use event delegation to avoid listener stacking ──
    const symBar = document.getElementById('l2-chart-symbols');
    if (symBar && !symBar._delegated) {
        symBar._delegated = true;
        symBar.addEventListener('click', (e) => {
            const btn = e.target.closest('.l2-tf-btn');
            if (!btn) return;
            symBar.querySelectorAll('.l2-tf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _l2SwitchSymbol(btn.dataset.sym);
        });
    }

    // ── Timeframe buttons (old tab layout) — use event delegation to avoid listener stacking ──
    const tfBar = document.getElementById('l2-chart-tfs');
    if (tfBar && !tfBar._delegated) {
        tfBar._delegated = true;
        tfBar.addEventListener('click', (e) => {
            const btn = e.target.closest('.l2-tf-btn');
            if (!btn) return;
            tfBar.querySelectorAll('.l2-tf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _l2SwitchTimeframe(btn.dataset.tf);
        });
    }

    // Initial fetch — full history with setData()
    _l2FetchCandles(true);
    // Start chained polling (NOT setInterval — prevents request stacking)
    _l2ScheduleNextPoll();
}

// ── Delta tracking ──
// Tracks the timestamp of the newest candle we've received.
// Live polls send ?since=_l2LastCandleTime so the server returns only 1-3 candles.
let _l2LastCandleTime = 0;

// ── Chained setTimeout polling (FALLBACK — only used when Socket.IO unavailable) ──
// Socket.IO handles live candle push. This polls as backup every 3-5s.

function _l2ScheduleNextPoll() {
    // FIX: Completely disabled the REST fallback polling to prevent race conditions 
    // with the active Socket.IO streams. The Websocket handles 100% of delta updates.
    return;
}

// ── Loading / Error overlay helpers ──
function _l2ShowOverlay(msg, isError) {
    let overlay = document.getElementById('l2-chart-overlay');
    const container = document.getElementById('t-l2-candle-chart')
                   || document.getElementById('l2-candle-chart');
    if (!container) return;
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'l2-chart-overlay';
        overlay.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;display:flex;align-items:center;justify-content:center;z-index:50;pointer-events:none;font-family:"JetBrains Mono",monospace;font-size:12px;';
        container.style.position = 'relative';
        container.appendChild(overlay);
    }
    overlay.style.display = 'flex';
    overlay.style.color = isError ? 'rgba(224,48,96,0.9)' : 'rgba(140,160,200,0.8)';
    overlay.style.background = isError ? 'rgba(30,10,15,0.6)' : 'rgba(10,10,20,0.5)';
    overlay.innerHTML = msg;
}

function _l2HideOverlay() {
    const overlay = document.getElementById('l2-chart-overlay');
    if (overlay) overlay.style.display = 'none';
}

// ═══════════════════════════════════════════════════════════════════════════
// UNIFIED SWITCH FUNCTIONS — all symbol/timeframe switching goes through here
// ═══════════════════════════════════════════════════════════════════════════

function _l2SwitchSymbol(sym) {
    if (sym === _l2ChartSymbol) return;

    // 1. Cancel in-flight fetch
    if (_l2FetchController) { _l2FetchController.abort(); _l2FetchController = null; }

    // 2. Cancel poll timer
    if (_l2CandlePollTimer) { clearTimeout(_l2CandlePollTimer); _l2CandlePollTimer = null; }

    // 3. Clear chart data immediately (no stale candles)
    if (typeof ChartCore !== 'undefined') {
        ChartCore.getInstances().forEach(inst => {
            if (inst.candleSeries) inst.candleSeries.setData([]);
            if (inst.volumeSeries) inst.volumeSeries.setData([]);
            if (inst.bubbleSeries) inst.bubbleSeries.setData([]);
        });
    }

    // 4. Set new symbol + reset delta tracking
    _l2ChartSymbol = sym;
    if (typeof WallLines !== 'undefined') WallLines.setCurrentSymbol(sym);
    _l2LastCandleTime = 0;
    _l2FetchVersion++;

    // 4b. Clear DOM delta memory so stale prices don't cause false flashes
    _domMemory = {};

    // 5. Update price format for symbol-specific tick size
    const tickSize = L2_TICK_SIZES[sym] || 0.25;
    if (_l2CandleSeries) {
        _l2CandleSeries.applyOptions({
            priceFormat: { type: 'price', precision: 2, minMove: tickSize },
        });
    }
    if (_l2BubbleSeries) {
        try { _l2BubbleSeries.applyOptions({ priceFormat: { type: 'price', precision: 2, minMove: tickSize } }); } catch(e) {}
    }

    // 6. Show loading overlay
    _l2ShowOverlay(`Loading ${sym}...`, false);

    // 7. Tell Socket.IO server about new symbol
    if (typeof DataFetch !== 'undefined') DataFetch.subscribe(_l2ChartSymbol, _l2ChartTF);

    // 8. Fetch full candle history + restart poll on success
    _l2FetchCandles(true).then(() => {
        _l2HideOverlay();
        _l2ScheduleNextPoll();
    });

    // 9. Sync button active states across both button sets
    document.querySelectorAll('#t-symbols .t-btn, #l2-chart-symbols .l2-tf-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.sym === sym);
    });

    // 10. Restart 2D DOM history for new symbol
    if (typeof startDomHistory === 'function') {
        startDomHistory(sym);
    }
}

function _l2SwitchTimeframe(tf) {
    if (tf === _l2ChartTF) return;

    // 1. Cancel in-flight fetch
    if (_l2FetchController) { _l2FetchController.abort(); _l2FetchController = null; }

    // 2. Cancel poll timer
    if (_l2CandlePollTimer) { clearTimeout(_l2CandlePollTimer); _l2CandlePollTimer = null; }

    // 3. Clear chart data immediately
    if (typeof ChartCore !== 'undefined') {
        ChartCore.getInstances().forEach(inst => {
            if (inst.candleSeries) inst.candleSeries.setData([]);
            if (inst.volumeSeries) inst.volumeSeries.setData([]);
            if (inst.bubbleSeries) inst.bubbleSeries.setData([]);
        });
    }

    // 4. Set new timeframe + reset delta tracking
    _l2ChartTF = tf;
    _l2LastCandleTime = 0;
    _l2FetchVersion++;

    // 5. Show loading overlay
    _l2ShowOverlay(`Loading ${_l2ChartSymbol} ${tf}...`, false);

    // 6. Tell Socket.IO server about new timeframe
    if (typeof DataFetch !== 'undefined') DataFetch.subscribe(_l2ChartSymbol, _l2ChartTF);

    // 7. Fetch full candle history + restart poll on success
    _l2FetchCandles(true).then(() => {
        _l2HideOverlay();
        _l2ScheduleNextPoll();
    });

    // 8. Sync button active states
    document.querySelectorAll('#t-timeframes .t-btn, #l2-chart-tfs .l2-tf-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.tf === tf);
    });
}

// ═══════════════════════════════════════════════════════════════════════════
// FETCH WITH RETRY + ABORT + ERROR HANDLING
// ═══════════════════════════════════════════════════════════════════════════

function _l2FetchCandles(fullRedraw, _retryCount) {
    if (typeof ChartCore === 'undefined' || ChartCore.getInstances().length === 0) return Promise.resolve();
    const attempt = _retryCount || 0;
    const MAX_RETRIES = 3;
    const myVersion = _l2FetchVersion; // capture to detect stale responses

    // Abort any previous in-flight fetch
    if (fullRedraw) {
        if (_l2FetchController) _l2FetchController.abort();
        _l2FetchController = new AbortController();
    }

    const since = (!fullRedraw && _l2LastCandleTime > 0) ? _l2LastCandleTime : 0;
    const signal = _l2FetchController ? _l2FetchController.signal : null;
    
    return DataFetch.fetchCandles(_l2ChartSymbol, _l2ChartTF, since, signal)
        .then(resp => {
            // Discard stale response if user switched symbol/tf during fetch
            if (myVersion !== _l2FetchVersion) return;

            const candles = resp.candles;
            if (!Array.isArray(candles) || candles.length === 0) {
                // Show "no data" watermark on the chart container
                const container = document.getElementById('t-l2-candle-chart')
                                || document.getElementById('l2-candle-chart');
                if (container && !container.querySelector('.l2-no-data')) {
                    const msg = document.createElement('div');
                    msg.className = 'l2-no-data';
                    msg.style.cssText = 'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);'
                        + 'color:rgba(140,160,200,.45);font-size:1.1rem;font-family:JetBrains Mono,monospace;'
                        + 'text-align:center;pointer-events:none;z-index:10;letter-spacing:.05em;';
                    msg.innerHTML = '⏸ NO CANDLE DATA<br><span style="font-size:.7rem;opacity:.6">Waiting for L2 feed or market may be closed</span>';
                    container.style.position = 'relative';
                    container.appendChild(msg);
                }
                return;
            }
            // Remove "no data" message if candles arrive
            const _ndEl = (document.getElementById('t-l2-candle-chart')
                        || document.getElementById('l2-candle-chart'));
            if (_ndEl) { const _nd = _ndEl.querySelector('.l2-no-data'); if (_nd) _nd.remove(); }

            if (fullRedraw) {
                // ── FULL HISTORY: setData() once ──
                const ohlc = candles.map(c => ({
                    time: _utcToET(c.time), open: c.open, high: c.high, low: c.low, close: c.close
                }));
                const vol = candles.map(c => ({
                    time: _utcToET(c.time), value: c.volume || 0,
                    color: c.close >= c.open ? 'rgba(38,166,154,.25)' : 'rgba(239,83,80,.25)'
                }));
                
                if (typeof ChartCore !== 'undefined') {
                    ChartCore.getInstances().forEach(inst => {
                        // All instances get candle + volume data
                        inst.candleSeries.setData(ohlc);
                        if (inst.volumeSeries) inst.volumeSeries.setData(vol);
                        
                        // Heatmap: zoom tight to recent candles so DOM depth cells are large
                        if (inst.feature === 'heatmap') {
                            // Defer zoom to next frame (LightweightCharts needs a tick after setData)
                            const _hChart = inst.chart;
                            const _totalBars = ohlc.length;
                            setTimeout(() => {
                                _hChart.timeScale().applyOptions({ barSpacing: 20, rightOffset: 3 });
                                _hChart.timeScale().scrollToPosition(-3, false);
                                _hChart.priceScale('right').applyOptions({
                                    scaleMargins: { top: 0.05, bottom: 0.05 },
                                    autoScale: true,
                                });
                            }, 100);
                        } else {
                            inst.chart.timeScale().fitContent();
                            // Deferred fitContent in case layout hasn't settled yet
                            setTimeout(() => inst.chart.timeScale().fitContent(), 200);
                        }

                        // ── VOLUME BUBBLE DATA — only for 'chart' feature ──
                        if (inst.feature === 'chart' && inst.bubbleSeries) {
                            const bubbleData = candles.map(c => ({
                                time: _utcToET(c.time), close: c.close, bp: c.bp || null,
                                icebergs: c.icebergs || null, sweeps: c.sweeps || null,
                                delta_div: c.delta_div || null, ignition: c.ignition || null,
                                spoofs: c.spoofs || null, drifting_iceberg: c.drifting_iceberg || null,
                                wall_gone: c.wall_gone || null
                            }));
                            inst.bubbleSeries.setData(bubbleData);
                        }
                    });
                    // Cache for backfilling future instances (layout switches)
                    _l2CandleDataCache = { ohlc, vol };
                }

                // ── LIVE DATA SEAM MARKER ──
                _l2SeamTime = 0;
                for (const c of candles) {
                    if (c.bp && Object.keys(c.bp).length > 0) {
                        _l2SeamTime = _utcToET(c.time);
                        break;
                    }
                }
                if (_l2SeamTime > 0) {
                    if (typeof ChartCore !== 'undefined') {
                        ChartCore.getInstances().forEach(inst => {
                            inst.candleSeries.setMarkers([{
                                time: _l2SeamTime, position: 'belowBar', color: 'rgba(124,90,247,.8)',
                                shape: 'arrowUp', text: 'LIVE ▸'
                            }]);
                        });
                    }
                } else {
                    if (typeof ChartCore !== 'undefined') {
                        ChartCore.getInstances().forEach(inst => {
                            inst.candleSeries.setMarkers([]);
                        });
                    }
                }

                // ── Fetch wall/max-pain overlays on full redraw ──
                if (typeof WallLines !== 'undefined') WallLines.update();
            } else {
                // ── DELTA UPDATE: update() only ──
                for (const c of candles) {
                    const et = _utcToET(c.time);
                    if (typeof ChartCore !== 'undefined') {
                        ChartCore.getInstances().forEach(inst => {
                            // All instances get candle + volume
                            inst.candleSeries.update({
                                time: et, open: c.open, high: c.high, low: c.low, close: c.close
                            });
                            if (inst.volumeSeries) {
                                inst.volumeSeries.update({
                                    time: et, value: c.volume || 0,
                                    color: c.close >= c.open ? 'rgba(38,166,154,.25)' : 'rgba(239,83,80,.25)'
                                });
                            }
                            // Bubbles only for 'chart' feature
                            if (inst.feature === 'chart' && inst.bubbleSeries) {
                                inst.bubbleSeries.update({
                                    time: et, close: c.close, bp: c.bp || null,
                                    icebergs: c.icebergs || null, sweeps: c.sweeps || null,
                                    delta_div: c.delta_div || null, ignition: c.ignition || null,
                                    spoofs: c.spoofs || null, drifting_iceberg: c.drifting_iceberg || null,
                                    wall_gone: c.wall_gone || null
                                });
                            }
                        });
                    }
                }
            }

            // Track the newest candle timestamp (raw UTC for ?since= param)
            _l2LastCandleTime = candles[candles.length - 1].time;
            _l2HideOverlay(); // clear any error overlay
        })
        .catch(err => {
            // Aborted fetches are normal during rapid switching — ignore silently
            if (err && err.name === 'AbortError') return;
            // Stale request — ignore
            if (myVersion !== _l2FetchVersion) return;

            console.warn(`[L2Chart] Fetch error (attempt ${attempt + 1}/${MAX_RETRIES}):`, err);

            if (attempt < MAX_RETRIES) {
                const delay = 1000 * (attempt + 1); // 1s, 2s, 3s backoff
                _l2ShowOverlay(`Connection error — retrying (${attempt + 1}/${MAX_RETRIES})...`, true);
                return new Promise(resolve => setTimeout(resolve, delay))
                    .then(() => _l2FetchCandles(fullRedraw, attempt + 1));
            } else {
                _l2ShowOverlay('⚠ Failed to load chart data — click a symbol to retry', true);
            }
        });
}

function _l2Render(data) {
    // Status dot
    const dot  = document.getElementById('l2-status-dot');
    const txt  = document.getElementById('l2-status-text');
    const conn = data.connected;
    if (dot) dot.className = 'l2-dot' + (conn ? ' live' : '');
    if (txt) txt.textContent = conn ? 'LIVE' : 'DISCONNECTED';

    // Symbol prices strip
    const strip = document.getElementById('l2-symbol-prices');
    if (strip) {
        const mid = data.mid_prices || {};
        strip.innerHTML = L2_SYMBOLS.map(s =>
            `<div class="l2-sym-price"><span class="l2-sym-label">${s}</span><span>${mid[s] ? mid[s].toFixed(2) : '—'}</span></div>`
        ).join('');
    }

    // ── Status Bar live data ──
    const symDom = (data.dom || {})[_l2ChartSymbol] || {};
    const sMid = document.getElementById('s-mid');
    const sSpread = document.getElementById('s-spread');
    const sImbal = document.getElementById('s-imbal');
    const sBidAsk = document.getElementById('s-bidask');
    if (sMid) {
        const mp = symDom.mid_price || (data.mid_prices || {})[_l2ChartSymbol] || 0;
        sMid.textContent = mp ? 'MID ' + mp.toFixed(2) : 'MID —';
    }
    if (sSpread) {
        const sp = symDom.spread || (symDom.best_ask && symDom.best_bid ? symDom.best_ask - symDom.best_bid : 0);
        sSpread.textContent = sp ? 'SPR ' + sp.toFixed(2) : 'SPR —';
    }
    if (sImbal) {
        const imb = symDom.imbalance || (data.imbalance || {})[_l2ChartSymbol] || 0;
        const pct = (imb * 100).toFixed(0);
        sImbal.textContent = 'IMB ' + (imb ? pct + '%' : '—');
        sImbal.className = 's-data' + (imb > 0.1 ? ' s-pos' : imb < -0.1 ? ' s-neg' : '');
    }
    if (sBidAsk) {
        const bt = symDom.bid_total || 0;
        const at = symDom.ask_total || 0;
        sBidAsk.textContent = bt || at ? 'B/A ' + bt + '/' + at : 'B/A —';
    }

    _l2RenderImbalance(data);
    _l2RenderDOM(data.dom);
    // NOTE: Tape is driven by real-time trade_tick WS events (Path A).
    // Do NOT replay data.trades here — it causes double-counting.
    _l2RenderSignals(data.signals);

    // ── DOM Heatmap 2D — renders as a narrow strip on the RIGHT edge of the chart ──
    const domData = (data.dom || {})[_l2ChartSymbol] || {};
    // Store latest state globally so we can rerender on scroll/resize without waiting for 500ms poll
    window._latestHeatmapData = {
        domData: domData,
        hasDom: (domData.bids || domData.asks),
        mid_price: domData.mid_price || 0,
    };
    window._latestHeatmapData.domData._absorption = (data.absorption || {})[_l2ChartSymbol] || {};
    
    window._forceRenderHeatmap = function() {
        const st = window._latestHeatmapData;
        // Ensure heatmap rendering only proceeds when ChartCore and the renderer are ready.
        if (!st) return;
        if (typeof ChartCore === 'undefined' || typeof renderDomHeatmap2D !== 'function') return;
        const instances = ChartCore.getInstances();
        if (!instances || instances.length === 0) return;
        instances.forEach(inst => {
            if (inst.feature === 'heatmap' && inst.heatmapCanvas && st.hasDom) {
                // Use candleSeries if present, otherwise fall back to chart's priceScale
                const priceToY = price => {
                    try {
                        if (inst.candleSeries && typeof inst.candleSeries.priceToCoordinate === 'function') {
                            return inst.candleSeries.priceToCoordinate(price);
                        }
                        const ps = inst.chart && inst.chart.priceScale && typeof inst.chart.priceScale().priceToCoordinate === 'function'
                            ? inst.chart.priceScale()
                            : null;
                        return ps ? ps.priceToCoordinate(price) : null;
                    } catch(e) { return null; }
                };
                renderDomHeatmap2D(inst.heatmapCanvas, priceToY, st.mid_price);
            }
        });
    };
    
    // Initial draw
    window._forceRenderHeatmap();

    // ── KineticText trade shock integration ──
    if (_useCanvasLadder && typeof KineticText !== 'undefined' && KineticText.programValid) {
        const domData = (data.dom || {})[_l2ChartSymbol] || {};
        // Only feed NEW trades (use cursor to avoid duplicate shocks)
        const cursor = _l2KineticCursor || 0;
        const newTradeCount = _l2TapeAll.length - cursor;
        if (newTradeCount > 0) {
            const newTrades = _l2TapeAll.slice(cursor);
            KineticText.processTrades(newTrades, domData.bids || {}, domData.asks || {});
            _l2KineticCursor = _l2TapeAll.length;
        }
    }
}

// ── L2 DOM/Trade fallback polling (only fires when WebSocket is down) ──

function loadL2() {
    return authFetch('/api/l2')
        .then(r => r.json())
        .then(data => { if (data) _l2Render(data); })
        .catch(e => console.warn('L2 poll error:', e));
}

function _l2ScheduleDomPoll() {
    _l2PollTimer = setTimeout(() => {
        // Skip REST poll if WebSocket l2_update is actively pushing data
        const wsAge = Date.now() - (window._l2WsLastTs || 0);
        if (window._l2WsActive && wsAge < 2000) {
            // WS is alive — no need to poll REST
            _l2ScheduleDomPoll();
            return;
        }
        // WS is dead or stale — fall back to REST
        loadL2().finally(() => {
            _l2ScheduleDomPoll();
        });
    }, 5000);  // 5s fallback (WS handles real-time at 400ms)
}

function _startL2Poll() {
    // Initial DOM fetch
    loadL2();
    // Start chained DOM polling (replaces setInterval)
    _l2ScheduleDomPoll();
    // BUG 3 FIX: removed redundant _l2InitCandleChart() — it's already called once in DOMContentLoaded
    // Initialize Socket.IO for real-time push
    _setupDataEvents();
    if (typeof DataFetch !== 'undefined') DataFetch.initSocket();
    
    if (typeof startDomHistory === 'function') {
        startDomHistory(_l2ChartSymbol);
    }
}

function _stopL2Poll() {
    if (_l2PollTimer) { clearTimeout(_l2PollTimer); _l2PollTimer = null; }
    if (_l2CandlePollTimer) { clearTimeout(_l2CandlePollTimer); _l2CandlePollTimer = null; }
}

// ══════════════════════════════════════════════════════════════════════════════
// TERMINAL MODE — Auto-start + event bus + options chain
// ══════════════════════════════════════════════════════════════════════════════

// ── TerminalBus: lightweight pub/sub for cross-panel communication ──
const TerminalBus = {
    _listeners: {},
    on(event, fn) {
        (this._listeners[event] = this._listeners[event] || []).push(fn);
    },
    off(event, fn) {
        if (!this._listeners[event]) return;
        this._listeners[event] = this._listeners[event].filter(f => f !== fn);
    },
    emit(event, data) {
        (this._listeners[event] || []).forEach(fn => fn(data));
    },
};
window.TerminalBus = TerminalBus;

// ── Options Chain: Delegated to features/options_chain.js ──



// ── Bridge: push existing dashboard metrics into toolbar ──
let _metricsLastHash = '';
function _termUpdateMetrics() {
    const copy = (srcId, dstId) => {
        const src = document.getElementById(srcId);
        const dst = document.getElementById(dstId);
        if (src && dst && src.textContent !== '—') dst.textContent = src.textContent;
    };
    // Skip work if page is hidden (tab not active)
    if (document.hidden) return;
    // Quick hash to skip redundant DOM writes
    const spot = document.getElementById('ds-spot');
    const hash = spot ? spot.textContent : '';
    if (hash === _metricsLastHash) return;
    _metricsLastHash = hash;
    copy('ds-spot', 't-spot');
    copy('ds-cw', 't-cw');
    copy('ds-pw', 't-pw');
    copy('ds-mp', 't-mp');
    copy('ds-pcr', 't-pcr');
    copy('ds-ndex', 't-ndex');
    // Also update timestamp
    const tsEl = document.getElementById('timestamp');
    const tTsEl = document.getElementById('t-timestamp');
    if (tsEl && tTsEl) tTsEl.textContent = tsEl.textContent;
}

// ── Terminal Init ──
document.addEventListener('DOMContentLoaded', () => {
    const terminal = document.getElementById('terminal');
    if (!terminal) return;  // fallback: old layout mode

    // ── Toolbar: Symbol buttons ──
    // ── Toolbar: Symbol buttons — delegate to unified handler ──
    document.querySelectorAll('#t-symbols .t-btn').forEach(btn => {
        btn.addEventListener('click', () => _l2SwitchSymbol(btn.dataset.sym));
    });

    // ── Toolbar: Walls overlay toggle ──
    const wallsBtn = document.getElementById('t-walls-toggle');
    if (wallsBtn && typeof WallLines !== 'undefined') {
        wallsBtn.addEventListener('click', () => WallLines.toggle());
    }

    // ── Metrics Ribbon toggle ──
    const metricsBtn = document.getElementById('t-metrics-toggle');
    const metricsRibbon = document.getElementById('t-metrics-ribbon');
    if (metricsBtn && metricsRibbon) {
        metricsBtn.addEventListener('click', () => {
            const terminal = document.querySelector('.terminal');
            terminal.classList.toggle('metrics-collapsed');
            const collapsed = terminal.classList.contains('metrics-collapsed');
            metricsBtn.classList.toggle('active', !collapsed);
        });
    }

    // ── Settings Panel Mutual Exclusion ──
    window.closeAllSettingsPanels = function(exceptId) {
        ['vp-settings-panel', 'hm-settings-panel', 'tf-settings-panel'].forEach(id => {
            if (id === exceptId) return;
            const el = document.getElementById(id);
            if (el) { if (id === 'tf-settings-panel') el.remove(); else el.style.display = 'none'; }
        });
    };

    // ── Toolbar: Volume Profile toggles ──
    const vpToggle = document.getElementById('t-vp-toggle');
    if (vpToggle) {
        vpToggle.addEventListener('click', () => {
            if (typeof VolumeProfileOverlay === 'undefined') return;
            const vis = VolumeProfileOverlay.toggleVisibility();
            vpToggle.classList.toggle('active', vis);
        });
    }
    document.querySelectorAll('#t-vp-modes .t-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            if (typeof VolumeProfileOverlay === 'undefined') return;
            const mode = btn.dataset.vp;
            btn.classList.toggle('active');
            const active = [...document.querySelectorAll('#t-vp-modes .t-btn.active')].map(b => b.dataset.vp);
            VolumeProfileOverlay.setActiveProfiles(active);
        });
    });
    const vpSettingsBtn = document.getElementById('vp-settings-btn');
    if (vpSettingsBtn) {
        vpSettingsBtn.addEventListener('click', () => {
            if (typeof VolumeProfileOverlay !== 'undefined') VolumeProfileOverlay.openSettings();
        });
    }

    // ── Toolbar: Timeframe buttons — delegate to unified handler ──
    document.querySelectorAll('#t-timeframes .t-btn').forEach(btn => {
        btn.addEventListener('click', () => _l2SwitchTimeframe(btn.dataset.tf));
    });

    // ── Auto-start L2 chart (skip tab routing) ──
    // Use rAF to ensure CSS grid layout is fully computed before chart reads container dimensions
    requestAnimationFrame(() => {
        if (window.AltarisLayout) {
            // ── Feature Mount: create DOM containers for each pane feature ──
            AltarisLayout.onFeatureMount = (paneIdx, featureKey, slotEl) => {
                slotEl.innerHTML = ''; // Clear prior feature
                
                if (['chart', 'heatmap'].includes(featureKey)) {
                    // Track which feature controls the render mode
                    _activeChartFeature = featureKey === 'heatmap' ? 'heatmap' : 'chart';
                    window._activeChartFeature = _activeChartFeature;

                    const chartFK = featureKey === 'heatmap' ? 'heatmap' : 'chart';

                    const wrap = document.createElement('div');
                    wrap.id = 't-l2-candle-chart-' + paneIdx;
                    wrap.dataset.feature = chartFK;
                    wrap.style.cssText = 'width:100%;height:100%;position:relative';
                    slotEl.appendChild(wrap);

                    // ── Per-pane overlay toggle toolbar (chart only, not heatmap) ──
                    if (chartFK === 'chart') {
                        const toolbar = document.createElement('div');
                        toolbar.className = 'overlay-toolbar';
                        toolbar.innerHTML = [
                            ['bubbles', 'BUB', 'Trade Bubbles'],
                            ['flare',   'FLR', 'Options DEX Flare'],
                            ['cumlDelta','CΔ',  'Cumulative Delta'],
                            ['iceberg', 'ICE', 'Iceberg & Sweep'],
                            ['vp',      'VP',  'Volume Profile'],
                            ['walls',   'LVL', 'Options Levels'],
                        ].map(([key, label, title]) =>
                            `<button class="ov-btn active" data-ov="${key}" title="${title}">${label}</button>`
                        ).join('');
                        wrap.appendChild(toolbar);

                        // Wire toggle clicks — each button controls its overlay independently
                        toolbar.addEventListener('click', (e) => {
                            const btn = e.target.closest('.ov-btn');
                            if (!btn) return;
                            const key = btn.dataset.ov;
                            const cfg = wrap._overlayConfig;
                            if (!cfg) return;
                            cfg[key] = !cfg[key];
                            btn.classList.toggle('active', cfg[key]);
                            // Walls use price lines — need immediate clear/redraw
                            if (key === 'walls' && typeof WallLines !== 'undefined') {
                                WallLines.update();
                            }
                        });
                    }

                    // Defer init by one frame so browser lays out the container first
                    // (prevents 0-width chart creation which makes candles invisible)
                    requestAnimationFrame(() => _l2InitCandleChart(wrap, chartFK));
                } else if (featureKey === 'ladder') {
                    // Canvas 2D Depth Ladder + Kinetic Text WebGL overlay
                    slotEl.innerHTML = `
                      <div class="l2-dom-ladder" style="position:relative; height:100%; overflow:hidden">
                        <canvas id="dom-ladder-canvas" style="width:100%; height:100%; display:block"></canvas>
                        <canvas id="dom-pressure-canvas"></canvas>
                        <canvas id="dom-kinetic-canvas"></canvas>
                      </div>`;
                    _useCanvasLadder = true;
                    _domNodesCreated = false; // reset HTML DOM nodes
                    _domMemory = {};          // reset delta memory

                    // Auto-init KineticText WebGL on the overlay canvas
                    requestAnimationFrame(() => {
                        const pCanvas = document.getElementById('dom-pressure-canvas');
                        if (pCanvas) {
                            if (typeof window._initPressureField === 'function') {
                                window._initPressureField();
                                // Reset retry counter if it gave up earlier
                                if (typeof PressureField !== 'undefined' && !PressureField._ready) {
                                     PressureField._retryCount = 0;
                                }
                            } else if (typeof PressureField !== 'undefined') {
                                PressureField.init(pCanvas);
                            }
                        }
                        const kCanvas = document.getElementById('dom-kinetic-canvas');
                        if (kCanvas && typeof KineticText !== 'undefined' && !KineticText.programValid) {
                            KineticText.init(kCanvas);
                        }
                    });
                } else if (featureKey === 'eqbook') {
                    slotEl.innerHTML = `
                      <div class="l2-tape" style="height:100%; display:flex; flex-direction:column">
                        <div class="l2-tape-head" style="color:rgba(140,160,200,.6)">
                           <span>TIME</span><span>PRICE</span><span>SIZE</span><span>SIDE</span>
                        </div>
                        <div id="tape-delta-labels" class="tape-delta-label"><span class="buy-pct">—</span><span class="sell-pct">—</span></div>
                        <div class="tape-delta-strip"><div id="tape-delta-fill" class="tape-delta-strip-fill" style="width:50%"></div></div>
                        <div id="eq-context-strip" class="eq-context-strip">
                          <span id="eq-ctx-regime" class="eq-ctx-badge regime" title="Market regime (empirical)">—</span>
                          <span id="eq-ctx-hawkes" class="eq-ctx-badge hawkes" title="Hawkes λ percentile (tape velocity)">λ —</span>
                          <span id="eq-ctx-cp" class="eq-ctx-badge cp" title="QQQ Call/Put volume ratio (60s screener)">C/P —</span>
                          <span id="eq-ctx-ice" class="eq-ctx-badge ice" title="NQ iceberg/sweep detections (60s)">ICE —</span>
                          <span id="eq-ctx-mm" class="eq-ctx-badge mm" title="MM venue pull events (30s)">MM —</span>
                        </div>
                        <div id="l2-tape-body" class="l2-tape-body" style="flex:1; overflow-y:auto; overflow-x:hidden"></div>
                      </div>`;
                    _tapeNodesCreated = false; // force rebuild of permanent tape nodes
                    _l2TapePrevLen = 0;
                    _deltaStripEl = null;  // reset delta strip element cache
                    _deltaLabelEl = null;
                    _eqCtxCached = false;  // reset context strip element cache
                } else if (featureKey === 'opscr') {
                    slotEl.innerHTML = `<div id="l2-signals-grid" style="display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:12px; overflow-y:auto"></div>`;
                } else if (featureKey === 'alpha') {
                    if (typeof window.AlphaDashboard !== 'undefined') {
                        window.AlphaDashboard.init(slotEl);
                    } else {
                        slotEl.innerHTML = `<div style="color:var(--dim);padding:20px;">AlphaDashboard module not loaded.</div>`;
                    }
                } else if (featureKey === 'pressure') {
                    slotEl.innerHTML = `
                      <div style="position:relative;width:100%;height:100%;background:#070a14">
                        <canvas id="dom-pressure-canvas-${paneIdx}" style="width:100%;height:100%;display:block"></canvas>
                      </div>`;
                    requestAnimationFrame(() => {
                        const pCanvas = document.getElementById('dom-pressure-canvas-' + paneIdx);
                        if (pCanvas && typeof PressureField !== 'undefined') {
                            PressureField.init(pCanvas);
                            if (!PressureField._ready) {
                                // WebGL2 not available — show fallback
                                slotEl.querySelector('div').innerHTML = `
                                  <div style="display:flex;align-items:center;justify-content:center;height:100%;color:rgba(140,160,200,.4);font-family:'JetBrains Mono',monospace;font-size:.7rem">
                                    Pressure Field requires WebGL2 (open in Chrome with GPU)
                                  </div>`;
                                return;
                            }
                            // Standalone render loop
                            let _pfRAF;
                            const _pfLoop = () => {
                                if (!document.body.contains(pCanvas)) return; // unmounted
                                if (PressureField._ready) {
                                    PressureField.update(0.016);
                                    PressureField.render();
                                }
                                _pfRAF = requestAnimationFrame(_pfLoop);
                            };
                            _pfRAF = requestAnimationFrame(_pfLoop);
                            slotEl._pfRAF = _pfRAF;

                            // Wire L2 DOM data → obstacle texture
                            const _pfL2Handler = (data) => {
                                if (!PressureField._ready || !document.body.contains(pCanvas)) return;
                                const symData = (data.dom || {})[_l2ChartSymbol] || {};
                                const bids = symData.bids || {};
                                const asks = symData.asks || {};
                                const mid = symData.mid_price || 0;
                                if (!mid) return;
                                // Build visible price range: 40 levels centered on mid (0.25 tick)
                                const tick = 0.25;
                                const levels = 40;
                                const visiblePrices = [];
                                for (let i = -levels; i <= levels; i++) {
                                    visiblePrices.push(mid + i * tick);
                                }
                                // Compute max depth for normalization
                                let maxDepth = 1;
                                for (const k of Object.keys(bids)) { if (bids[k] > maxDepth) maxDepth = bids[k]; }
                                for (const k of Object.keys(asks)) { if (asks[k] > maxDepth) maxDepth = asks[k]; }
                                PressureField.updateObstacles(bids, asks, visiblePrices, maxDepth);
                            };
                            window.AltarisEvents.on('data:l2:update', _pfL2Handler);
                            slotEl._pfL2Handler = _pfL2Handler;

                            // Wire trade ticks → force injection
                            const _pfTradeHandler = (data) => {
                                if (!PressureField._ready || !document.body.contains(pCanvas)) return;
                                if (data.symbol !== _l2ChartSymbol) return;
                                const priceStr = parseFloat(data.price).toFixed(2);
                                const vol = data.vol || data.volume || data.size || 1;
                                const side = (data.side === 'sell' || data.side === 's') ? 'ask' : 'bid';
                                PressureField.injectForce(priceStr, vol, side);
                            };
                            window.AltarisEvents.on('data:trades:update', _pfTradeHandler);
                            slotEl._pfTradeHandler = _pfTradeHandler;
                        }
                    });
                } else if (featureKey === 'kinetic') {
                    slotEl.innerHTML = `
                      <div style="position:relative;width:100%;height:100%;background:#070a14">
                        <canvas id="dom-kinetic-canvas-${paneIdx}" style="width:100%;height:100%;display:block"></canvas>
                      </div>`;
                    requestAnimationFrame(() => {
                        const kCanvas = document.getElementById('dom-kinetic-canvas-' + paneIdx);
                        if (!kCanvas || typeof KineticText === 'undefined') return;
                        if (!KineticText.programValid) {
                            const ok = KineticText.init(kCanvas);
                            if (!ok) {
                                // WebGL2 not available — use Canvas2D trade ladder fallback
                                const _kHeat = {}; // { priceKey: { ts, vol, side } }
                                const _kTradeHandler = (data) => {
                                    if (!document.body.contains(kCanvas)) return;
                                    if (data.symbol !== _l2ChartSymbol) return;
                                    const pk = parseFloat(data.price).toFixed(2);
                                    const prev = _kHeat[pk] || { vol: 0 };
                                    _kHeat[pk] = { ts: Date.now(), vol: prev.vol + (data.vol || data.volume || 1), side: data.side === 'sell' || data.side === 's' ? 'ask' : 'bid' };
                                };
                                window.AltarisEvents.on('data:trades:update', _kTradeHandler);
                                slotEl._kTradeHandler = _kTradeHandler;

                                let _kRAF;
                                const _kLoop = () => {
                                    if (!document.body.contains(kCanvas)) return;
                                    _drawKineticFallback(kCanvas, _kHeat);
                                    _kRAF = requestAnimationFrame(_kLoop);
                                };
                                _kRAF = requestAnimationFrame(_kLoop);
                                slotEl._kRAF = _kRAF;
                            }
                        }
                    });
                } else if (featureKey === 'bookms') {
                    if (typeof BookMsHUD !== 'undefined') BookMsHUD.init(slotEl);
                } else if (featureKey === 'eqtape') {
                    if (typeof EquityTapePane !== 'undefined') EquityTapePane.init(slotEl);
                } else if (featureKey === 'dealer') {
                    if (typeof DealerFlowPane !== 'undefined') DealerFlowPane.init(slotEl);
                } else if (featureKey === 'xdiv') {
                    if (typeof CrossDivergencePane !== 'undefined') CrossDivergencePane.init(slotEl);
                } else if (featureKey === 'volsurf') {
                    if (typeof VolSurfacePane !== 'undefined') VolSurfacePane.init(slotEl);
                } else if (featureKey === 'optflow') {
                    if (typeof OptionsFlowPane !== 'undefined') OptionsFlowPane.init(slotEl);
                } else if (featureKey === 'ocheat') {
                    // Options Chain GEX Heatmap (Canvas2D)
                    slotEl.innerHTML = `<div style="width:100%;height:100%;background:#070a14;position:relative">
                        <canvas id="ocheat-canvas-${paneIdx}" style="width:100%;height:100%;display:block"></canvas>
                    </div>`;
                    const _ocData = {}; // { dte: { strike: { gex, iv, oi, side } } }
                    const _ocCanvas = document.getElementById('ocheat-canvas-' + paneIdx);
                    let _ocDirty = true;
                    const _ocHandler = (d) => {
                        if (!d || !d.strike || !d.iv) return;
                        const dte = d.dte || 0;
                        if (!_ocData[dte]) _ocData[dte] = {};
                        const key = d.strike + '_' + d.side;
                        _ocData[dte][key] = { strike: d.strike, side: d.side, gex: d.dollar_gex || 0, iv: d.iv, oi: d.oi || 0, vol: d.vol || 0 };
                        _ocDirty = true;
                    };
                    // Wire option_mark_update
                    if (window._sio) window._sio.on('option_mark_update', _ocHandler);
                    slotEl._ocHandler = _ocHandler;

                    // Render loop
                    let _ocRAF;
                    const _ocRender = () => {
                        if (!document.body.contains(_ocCanvas)) return;
                        if (_ocDirty) {
                            _ocDirty = false;
                            _drawOcHeat(_ocCanvas, _ocData);
                        }
                        _ocRAF = requestAnimationFrame(_ocRender);
                    };
                    _ocRAF = requestAnimationFrame(_ocRender);
                    slotEl._ocRAF = _ocRAF;
                } else if (featureKey.startsWith('oc')) {
                    // Option Chain Panel
                    const ocElem = document.getElementById('t-options');
                    if (ocElem) {
                        ocElem.style.display = 'flex';
                        slotEl.appendChild(ocElem);
                    }
                }
            };

            // ── Feature Unmount: reset state when a pane feature is removed ──
            AltarisLayout.onFeatureUnmount = (paneIdx, featureKey, slotEl) => {
                if (['chart', 'heatmap'].includes(featureKey)) {
                    if (typeof ChartCore !== 'undefined') {
                        const chartDiv = slotEl.querySelector('[id^="t-l2-candle-chart"]');
                        if (chartDiv) ChartCore.destroy(chartDiv);
                    }
                }
                if (featureKey === 'pressure') {
                    if (slotEl._pfRAF) cancelAnimationFrame(slotEl._pfRAF);
                    if (slotEl._pfL2Handler) window.AltarisEvents.off('data:l2:update', slotEl._pfL2Handler);
                    if (slotEl._pfTradeHandler) window.AltarisEvents.off('data:trades:update', slotEl._pfTradeHandler);
                    if (typeof PressureField !== 'undefined') PressureField.destroy();
                }
                if (featureKey === 'ocheat') {
                    if (slotEl._ocRAF) cancelAnimationFrame(slotEl._ocRAF);
                    if (slotEl._ocHandler && window._sio) window._sio.off('option_mark_update', slotEl._ocHandler);
                }
                if (featureKey === 'kinetic') {
                    if (typeof KineticText !== 'undefined' && KineticText.programValid) KineticText.destroy();
                    if (slotEl._kRAF) cancelAnimationFrame(slotEl._kRAF);
                    if (slotEl._kTradeHandler) window.AltarisEvents.off('data:trades:update', slotEl._kTradeHandler);
                }
                if (featureKey === 'alpha') {
                    if (typeof window.AlphaDashboard !== 'undefined') {
                        window.AlphaDashboard.destroyInstance(slotEl);
                    }
                }
                if (featureKey === 'heatmap' && _activeChartFeature === 'heatmap') {
                    _activeChartFeature = 'chart';
                    window._activeChartFeature = 'chart';
                }
                if (featureKey === 'ladder') {
                    _useCanvasLadder = false;
                    _domNodesCreated = false; // force HTML DOM to rebuild on next mount
                    if (typeof KineticText !== 'undefined' && KineticText.programValid) {
                        KineticText.destroy();
                    }
                    // Reset init flag so auto-init works on re-mount
                    if (typeof KineticText !== 'undefined') {
                        KineticText._initAttempted = false;
                    }
                    _l2KineticCursor = 0;
                }
                if (featureKey === 'eqbook') {
                    _tapeNodesCreated = false;
                    _l2TapePrevLen = 0;
                    _deltaStripEl = null;
                    _deltaLabelEl = null;
                    _eqCtxCached = false;
                }
                if (featureKey === 'bookms' && typeof BookMsHUD !== 'undefined') BookMsHUD.destroy();
                if (featureKey === 'eqtape' && typeof EquityTapePane !== 'undefined') EquityTapePane.destroy();
                if (featureKey === 'dealer' && typeof DealerFlowPane !== 'undefined') DealerFlowPane.destroy();
                if (featureKey === 'xdiv' && typeof CrossDivergencePane !== 'undefined') CrossDivergencePane.destroy();
                if (featureKey === 'volsurf' && typeof VolSurfacePane !== 'undefined') VolSurfacePane.destroy();
                if (featureKey === 'optflow' && typeof OptionsFlowPane !== 'undefined') OptionsFlowPane.destroy();
            };

            AltarisLayout.triggerInitialMounts();
            _startL2Poll();

            // Safety net: if candles haven't loaded after 4s, force a REST fetch
            setTimeout(() => {
                if (!_l2CandleDataCache && typeof ChartCore !== 'undefined' && ChartCore.getInstances().length > 0) {
                    console.warn('[Safety] No candle data after 4s — force fetching...');
                    _l2FetchCandles(true).then(() => {
                        ChartCore.getInstances().forEach(inst => inst.chart.timeScale().fitContent());
                    });
                }
            }, 4000);
        } else {
            // Legacy layout fallback
            _l2InitCandleChart();
            _startL2Poll();
        }
    });

    // ── Initialize Options Chain ──
    if (typeof OptionsChain !== 'undefined') OptionsChain.init();

    // ── Initialize WallLines background refresh ──
    if (typeof WallLines !== 'undefined') WallLines.init();

    // ── Metric bridge: update toolbar from old dash metrics ──
    if (window._termMetricsTimer) clearInterval(window._termMetricsTimer);
    window._termMetricsTimer = setInterval(_termUpdateMetrics, 2000);

    // Removed duplicate event listener for #t-heatmap-settings-btn since it conflicts with volume_bubbles.js

    // Thermal Flare Settings — delegated to ThermalFlare module (tf- prefixed IDs)
    const tfBtn = document.getElementById('tf-settings-btn');
    if (tfBtn && typeof ThermalFlare !== 'undefined') {
        tfBtn.addEventListener('click', () => ThermalFlare.openSettings());
    }

    console.log('[Terminal] Super Chart mode initialized');
});
