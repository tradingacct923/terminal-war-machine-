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
    });

    // ── l2_update: Full L2 state via WebSocket (replaces /api/l2 REST poll) ──
    window.AltarisEvents.on('data:l2:update', (data) => {
        if (!data) return;
        window._l2WsActive = true;
        window._l2WsLastTs = Date.now();
        _l2Render(data);
    });
}

// ── Timezone helper: UTC epoch → Eastern Time epoch ──
// LWC treats the time field as UTC, so we offset it to ET
// to make the x-axis show ET hours. Handles EST/EDT automatically.
function _utcToET(utcEpoch) {
    // FIX: Lightweight Charts natively requires raw UTC Unix Seconds for intraday.
    // Manually offsetting this shifts the candles out of the visible screen area.
    // Convert ET epoch seconds to UTC epoch seconds for chart consumption.
    // Data from API is in Eastern Time; Lightweight Charts expects UTC.
    const offsetHours = 4; // EDT offset (UTC-4)
    return utcEpoch + offsetHours * 3600;
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
            _l2TapeAll.push(entry);  // push to end (O(1)) — render from tail
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

                    // WallLines — only on chart panes
                    if (typeof WallLines !== 'undefined') {
                        WallLines.attachToSeries(data.candleSeries);
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

    // ── Symbol buttons (old tab layout) — delegate to unified handler ──
    document.querySelectorAll('#l2-chart-symbols .l2-tf-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#l2-chart-symbols .l2-tf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _l2SwitchSymbol(btn.dataset.sym);
        });
    });

    // ── Timeframe buttons (old tab layout) — delegate to unified handler ──
    document.querySelectorAll('#l2-chart-tfs .l2-tf-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#l2-chart-tfs .l2-tf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _l2SwitchTimeframe(btn.dataset.tf);
        });
    });

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
        if (typeof ChartCore === 'undefined' || typeof renderDomHeatmap2D !== 'function') {
            // Defer rendering until ChartCore becomes available – retry on next animation frame.
            requestAnimationFrame(() => window._forceRenderHeatmap());
            return;
        }
        // Guard against missing chart instances.
        const instances = ChartCore.getInstances();
        if (!instances || instances.length === 0) {
            requestAnimationFrame(() => window._forceRenderHeatmap());
            return;
        }
        instances.forEach(inst => {
            if (inst.feature === 'heatmap' && inst.heatmapCanvas && st.hasDom) {
                // Use candleSeries if present, otherwise fall back to chart's priceScale
                const priceToY = price => {
                    if (inst.candleSeries && typeof inst.candleSeries.priceToCoordinate === 'function') {
                        return inst.candleSeries.priceToCoordinate(price);
                    }
                    // Fallback: use chart's priceScale (always available)
                    const ps = inst.chart && inst.chart.priceScale && typeof inst.chart.priceScale().priceToCoordinate === 'function'
                        ? inst.chart.priceScale()
                        : null;
                    return ps ? ps.priceToCoordinate(price) : null;
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
        const newTradeCount = _l2TapeAll.length - (_l2KineticCursor || 0);
        if (newTradeCount > 0) {
            const newTrades = _l2TapeAll.slice(0, newTradeCount);
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
                    _activeChartFeature = featureKey;
                    window._activeChartFeature = featureKey;
                    
                    const wrap = document.createElement('div');
                    wrap.id = 't-l2-candle-chart-' + paneIdx;
                    wrap.dataset.feature = featureKey;
                    wrap.style.cssText = 'width:100%;height:100%';
                    slotEl.appendChild(wrap);
                    
                    _l2InitCandleChart(wrap, featureKey);
                } else if (featureKey === 'gex' || featureKey === 'dex') {
                    // ── GEX / DEX Bar Chart Pane ──
                    // Creates a dedicated Chart.js bar chart showing per-strike exposure
                    const canvasId = 'pane-' + featureKey + '-bar-' + paneIdx;
                    slotEl.innerHTML = `
                      <div style="width:100%;height:100%;overflow-y:auto;background:#070a14;padding:4px 0">
                        <div style="padding:4px 8px;font-family:'JetBrains Mono',monospace;font-size:.6rem;font-weight:700;letter-spacing:.1em;color:${featureKey === 'gex' ? 'rgba(0,220,150,.7)' : 'rgba(0,180,255,.7)'};text-transform:uppercase">
                          ${featureKey === 'gex' ? 'γ GEX — GAMMA EXPOSURE BY STRIKE' : 'Δ DEX — DELTA EXPOSURE BY STRIKE'}
                        </div>
                        <div style="width:100%;height:calc(100% - 22px);position:relative">
                          <canvas id="${canvasId}"></canvas>
                        </div>
                      </div>`;

                    // Fetch and render immediately, then refresh every 30s
                    const _renderPaneBar = () => {
                        authFetch('/api/data').then(r => r.json()).then(data => {
                            if (!data || data.error) return;
                            const barData = featureKey === 'gex' ? data.gex_bar : data.dex_bar;
                            const spot = data.spot;
                            if (!barData) return;
                            const cPos = featureKey === 'gex' ? 'rgba(0,220,150,.88)' : 'rgba(0,180,255,.88)';
                            const cNeg = featureKey === 'gex' ? 'rgba(255,50,90,.88)' : 'rgba(255,160,20,.88)';
                            if (typeof buildNetBar === 'function') {
                                buildNetBar(canvasId, barData, spot, cPos, cNeg);
                            }
                        }).catch(e => console.warn('[Pane ' + featureKey + '] fetch error:', e));
                    };
                    // Wait for Chart.js to be available
                    const _waitAndRender = () => {
                        if (typeof Chart !== 'undefined' && typeof buildNetBar === 'function') {
                            _renderPaneBar();
                        } else {
                            setTimeout(_waitAndRender, 300);
                        }
                    };
                    _waitAndRender();
                    // Store interval for cleanup
                    slotEl._paneRefreshTimer = setInterval(_renderPaneBar, 30000);

                } else if (featureKey === 'ivskew') {
                    // ── IV Skew / Vol Surface Pane ──
                    const surfaceId = 'pane-iv-surface-' + paneIdx;
                    const skewId = 'pane-iv-skew-' + paneIdx;
                    slotEl.innerHTML = `
                      <div style="width:100%;height:100%;display:flex;flex-direction:column;background:#070a14">
                        <div style="padding:4px 8px;font-family:'JetBrains Mono',monospace;font-size:.6rem;font-weight:700;letter-spacing:.1em;color:rgba(100,165,250,.7);text-transform:uppercase">
                          🌊 IV SKEW — VOLATILITY SURFACE
                        </div>
                        <div id="${surfaceId}" style="flex:1;min-height:0"></div>
                        <div style="height:140px;padding:0 4px;flex-shrink:0;position:relative">
                          <canvas id="${skewId}"></canvas>
                        </div>
                      </div>`;

                    const _renderIVPane = () => {
                        // Fetch vol surface data from /api/volatility
                        authFetch('/api/volatility').then(r => r.json()).then(data => {
                            if (!data || data.error || !data.surface?.length) return;
                            // Render Plotly 3D surface into the pane div
                            const surfEl = document.getElementById(surfaceId);
                            if (!surfEl || typeof Plotly === 'undefined') return;

                            const surface = data.surface;
                            const spot = data.spot || 0;
                            const strikes = data.strikes;
                            const x = strikes.map(s => ((s / spot) * 100).toFixed(1));
                            const y = data.expirations.map(e => e.dte);
                            const z = surface.map(e => e.ivs.map(v => {
                                const pct = +(v * 100).toFixed(2);
                                return (pct > 1 && pct <= 75) ? pct : 20; // clamp bad values
                            }));

                            Plotly.react(surfaceId, [{
                                type: 'surface', x, y, z,
                                colorscale: [[0,'rgb(10,10,60)'],[0.25,'rgb(30,70,200)'],[0.5,'rgb(60,180,220)'],[0.75,'rgb(255,200,60)'],[1,'rgb(255,60,60)']],
                                opacity: 0.92,
                                contours: { z: { show: true, usecolormap: true, project: { z: false } } },
                                showscale: false,
                            }], {
                                paper_bgcolor: '#070a14', plot_bgcolor: '#070a14',
                                margin: { l: 0, r: 0, t: 10, b: 0 },
                                scene: {
                                    bgcolor: '#070a14',
                                    xaxis: { title: 'Moneyness %', gridcolor: 'rgba(50,70,120,.3)', tickfont: { size: 9, color: '#4a6a9a' } },
                                    yaxis: { title: 'DTE', gridcolor: 'rgba(50,70,120,.3)', tickfont: { size: 9, color: '#4a6a9a' } },
                                    zaxis: { title: 'IV %', gridcolor: 'rgba(50,70,120,.3)', tickfont: { size: 9, color: '#4a6a9a' } },
                                    camera: { eye: { x: -1.5, y: -2.0, z: 0.8 } },
                                },
                                autosize: true,
                            }, { displayModeBar: false, responsive: true });

                            // Render 2D IV skew line (nearest expiry)
                            if (typeof Chart !== 'undefined') {
                                const nearest = surface[0];
                                const ivPct = nearest.ivs.map(v => +(v * 100).toFixed(2));
                                const skewCanvas = document.getElementById(skewId);
                                if (!skewCanvas) return;
                                const existingChart = Chart.getChart(skewCanvas);
                                if (existingChart) existingChart.destroy();
                                new Chart(skewCanvas.getContext('2d'), {
                                    type: 'line',
                                    data: {
                                        labels: strikes.map(s => '$' + s),
                                        datasets: [{
                                            label: nearest.label + ' IV',
                                            data: ivPct,
                                            borderColor: 'rgba(100,165,250,.9)',
                                            backgroundColor: 'rgba(100,165,250,.08)',
                                            borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: true,
                                        }]
                                    },
                                    options: {
                                        responsive: true, maintainAspectRatio: false,
                                        plugins: { legend: { display: false } },
                                        scales: {
                                            x: { ticks: { color: '#3a5070', font: { size: 8 }, maxTicksLimit: 10 }, grid: { color: 'rgba(255,255,255,.03)' } },
                                            y: { ticks: { color: '#3a5070', font: { size: 8 }, callback: v => v + '%' }, grid: { color: 'rgba(255,255,255,.03)' } },
                                        }
                                    }
                                });
                            }
                        }).catch(e => console.warn('[Pane ivskew] fetch error:', e));
                    };
                    // Lazy-load Plotly then render
                    if (typeof window._ensurePlotly === 'function') {
                        window._ensurePlotly(_renderIVPane);
                    } else if (typeof Plotly !== 'undefined') {
                        _renderIVPane();
                    }
                    slotEl._paneRefreshTimer = setInterval(_renderIVPane, 60000);

                } else if (['alpha', 'opscr'].includes(featureKey)) {
                    _activeChartFeature = featureKey;
                    window._activeChartFeature = featureKey;
                    
                    const wrap = document.createElement('div');
                    wrap.id = 't-l2-candle-chart-' + paneIdx;
                    wrap.dataset.feature = featureKey;
                    wrap.style.cssText = 'width:100%;height:100%';
                    slotEl.appendChild(wrap);
                    
                    _l2InitCandleChart(wrap, featureKey);
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
                if (featureKey === 'chart' || featureKey === 'heatmap') {
                    if (typeof ChartCore !== 'undefined') {
                        // Destroy only the chart inside this specific slotEl
                        const chartDiv = slotEl.querySelector('[id^="t-l2-candle-chart"]');
                        if (chartDiv) ChartCore.destroy(chartDiv);
                    }
                }
                // Clean up GEX/DEX/IV Skew refresh timers and Chart.js instances
                if (featureKey === 'gex' || featureKey === 'dex' || featureKey === 'ivskew') {
                    if (slotEl._paneRefreshTimer) {
                        clearInterval(slotEl._paneRefreshTimer);
                        slotEl._paneRefreshTimer = null;
                    }
                    // Destroy any Chart.js charts in this pane
                    if (typeof Chart !== 'undefined') {
                        slotEl.querySelectorAll('canvas').forEach(c => {
                            const ch = Chart.getChart(c);
                            if (ch) ch.destroy();
                        });
                    }
                    // Destroy Plotly surfaces
                    if (typeof Plotly !== 'undefined') {
                        slotEl.querySelectorAll('[id^="pane-iv-surface"]').forEach(el => {
                            try { Plotly.purge(el); } catch(e) {}
                        });
                    }
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
            };

            AltarisLayout.triggerInitialMounts();
            _startL2Poll();
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
    setInterval(_termUpdateMetrics, 2000);

    // Removed duplicate event listener for #t-heatmap-settings-btn since it conflicts with volume_bubbles.js

    // Thermal Flare Settings — delegated to ThermalFlare module (tf- prefixed IDs)
    const tfBtn = document.getElementById('tf-settings-btn');
    if (tfBtn && typeof ThermalFlare !== 'undefined') {
        tfBtn.addEventListener('click', () => ThermalFlare.openSettings());
    }

    console.log('[Terminal] Super Chart mode initialized');
});
