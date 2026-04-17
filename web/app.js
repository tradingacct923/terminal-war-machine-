// ── Debug log gate ────────────────────────────────────────────────────────────
// Silence console.log/info/debug in prod unless user opts in:
//   localStorage.setItem('altaris_debug', '1'); location.reload();
// Preserves console.warn/error so genuine problems stay visible.
(function _gateDebugLogs() {
    try {
        window._ALTARIS_DEBUG = localStorage.getItem('altaris_debug') === '1';
    } catch (_) { window._ALTARIS_DEBUG = false; }
    if (!window._ALTARIS_DEBUG) {
        const noop = () => {};
        console.log = noop;
        console.info = noop;
        console.debug = noop;
    }
})();

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
let _l2FetchInFlight = false;    // guard against abort cascade from competing fetches
let _l2TapeAll = [];   // accumulated trades, newest first
let _useCanvasLadder = false;  // true when Canvas 2D ladder pane is active
let _l2KineticCursor = 0;     // tracks which trades have already been fed to KineticText

// ── Wall / Max Pain overlay state ──
let _ocRefreshTimer = null; // options chain auto-refresh

const L2_SYMBOLS = ['NQ'];
const L2_TICK_SIZES = { NQ: 0.25, GC: 0.10 };

// ── Socket.IO connection for real-time candle/trade push ──
// ── Cached enriched data (bp, signals) — updated at 5Hz by candle_enriched ──
// Module-scope so _l2SwitchSymbol can clear on symbol change.
let _cachedBp = null;
let _cachedEnriched = null;
let _lastCandleOHLC = null;  // { time, open, high, low, close } — for DOM mid sync

// ── Data Events Setup (delegated to DataFetch module) ──
function _setupDataEvents() {
    if (!window.AltarisEvents || window._dataEventsWired) return;
    window._dataEventsWired = true;

    // Candle OHLCV updates — fast path, no throttle, matches DOM speed
    // Also updates bubbleSeries with cached bp data to keep time-aligned
    window.AltarisEvents.on('data:candles:update', (data) => {
        if (typeof ChartCore === 'undefined' || ChartCore.getInstances().length === 0) return;
        if (data.symbol !== _l2ChartSymbol || data.tf !== _l2ChartTF) {
            // DIAG: log once per distinct mismatch to trace tf routing bugs
            if (!window._candleDropLog) window._candleDropLog = {};
            const _k = `${data.symbol}/${data.tf} (active: ${_l2ChartSymbol}/${_l2ChartTF})`;
            if (!window._candleDropLog[_k]) {
                window._candleDropLog[_k] = true;
                console.log('[CANDLE DROP]', _k);
            }
            return;
        }

        const et = _utcToET(data.time);
        if (_l2LastCandleTime && et < _l2LastCandleTime) {
            // DIAG: live update older than last stored — timestamp bug?
            if (!window._candleTimeDrop) window._candleTimeDrop = 0;
            if (++window._candleTimeDrop <= 3) {
                console.warn('[CANDLE TIME DROP]', 'et=', et, 'last=', _l2LastCandleTime, 'tf=', _l2ChartTF);
            }
            return;
        }
        // New candle boundary — clear stale bp from previous candle
        if (_l2LastCandleTime && et > _l2LastCandleTime) {
            _cachedBp = null;
            _cachedEnriched = null;
        }
        _lastCandleOHLC = { time: et, open: data.open, high: data.high, low: data.low, close: data.close };
        try {
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
                // Bubble update: same time as candle, using cached bp from enriched events
                if (inst.bubbleSeries && _cachedBp) {
                    inst.bubbleSeries.update({
                        time: et,
                        o: data.open, h: data.high, l: data.low, c: data.close, close: data.close,
                        bp: _cachedBp,
                        sweeps: _cachedEnriched?.sweeps || null,
                        delta_div: _cachedEnriched?.delta_div || null,
                        ignition: _cachedEnriched?.ignition || null,
                        spoofs: _cachedEnriched?.spoofs || null,
                        wall_gone: _cachedEnriched?.wall_gone || null,
                        absorption: _cachedEnriched?.absorption || null,
                        depth_deltas: _cachedEnriched?.depth_deltas || null
                    });
                }
            });
        } catch (e) {
            if (window._candleErrCount === undefined) window._candleErrCount = 0;
            if (++window._candleErrCount <= 5) console.warn('[CANDLE]', e.message);
        }
        _l2LastCandleTime = et;
    });

    // Candle enriched data — cache bp + signals at 5Hz, don't touch bubbleSeries directly
    window.AltarisEvents.on('data:candles:enriched', (data) => {
        if (data.symbol !== _l2ChartSymbol) return;
        // bp is timeframe-specific — only cache when tf matches
        if (data.tf === _l2ChartTF && data.bp) {
            _cachedBp = data.bp;
            _cachedEnriched = data;
        }
        // absorption + depth_deltas are per-symbol, not per-tf — always cache
        if (data.absorption) _cachedEnriched = { ...(_cachedEnriched || {}), absorption: data.absorption };
        if (data.depth_deltas) _cachedEnriched = { ...(_cachedEnriched || {}), depth_deltas: data.depth_deltas };
    });

    // Cache trade tick DOM refs
    let _tradeStrip = null, _tradeSpot = null;
    const _tradePriceEls = {};

    // Price display: only needs latest trade (emitted at 20Hz after batching)
    window.AltarisEvents.on('data:trades:update', (data) => {
        if (!_tradeStrip) _tradeStrip = document.getElementById('l2-symbol-prices');
        if (_tradeStrip) {
            if (!_tradePriceEls[data.symbol]) {
                const row = _tradeStrip.querySelector(`[data-sym="${data.symbol}"] .price`);
                if (row) _tradePriceEls[data.symbol] = row;
            }
            if (_tradePriceEls[data.symbol]) _tradePriceEls[data.symbol].textContent = data.price.toFixed(2);
        }
        if (data.symbol === _l2ChartSymbol) {
            if (!_tradeSpot) _tradeSpot = document.getElementById('t-spot');
            if (_tradeSpot) _tradeSpot.textContent = data.price.toFixed(2);
        }
    });
    // Tape + physics: consume full batch (all trades in 50ms window)
    window.AltarisEvents.on('data:trades:batch', (batch) => {
        if (typeof _l2RenderTape === 'function') {
            const grouped = {};
            for (const t of batch) {
                if (!grouped[t.symbol]) grouped[t.symbol] = [];
                grouped[t.symbol].push(t);
            }
            _l2RenderTape(grouped);
        }
    });

    // Cache zone metric DOM refs once (avoid getElementById every 5s)
    let _zoneEls = null;
    function _getZoneEls() {
        if (_zoneEls) return _zoneEls;
        _zoneEls = {
            cw: document.getElementById('t-cw'),
            pw: document.getElementById('t-pw'),
            mp: document.getElementById('t-mp'),
            flip: document.getElementById('t-flip'),
            dexL: document.getElementById('t-dex-long'),
            dexS: document.getElementById('t-dex-short'),
            ndex: document.getElementById('t-ndex'),
            netPrem: document.getElementById('t-net-prem'),
            theta: document.getElementById('t-net-theta'),
            ivrv: document.getElementById('t-ivrv'),
            // Greek-surface cells — moved from wall_lines /api/walls poll (which
            // doesn't include these fields) to the 5Hz zone_update handler.
            skew: document.getElementById('t-iv-skew'),
            term: document.getElementById('t-term'),
            speed: document.getElementById('t-speed'),
            conf: document.getElementById('t-confluence'),
            misp: document.getElementById('t-misprice'),
            flow: document.getElementById('t-flow'),
            ivSpread: document.getElementById('t-iv-spread'),
            volRegime: document.getElementById('t-vol-regime'),
            volRegimeConf: document.getElementById('t-vol-regime-conf'),
            volPrem: document.getElementById('t-vol-prem'),
            ivRank: document.getElementById('t-iv-rank'),
            asScore: document.getElementById('t-as-score'),
            sigCal: document.getElementById('t-sig-cal'),
            // 10 newly-surfaced backend fields (zone_update) — previously dark.
            volAlert: document.getElementById('t-vol-alert'),
            volDur: document.getElementById('t-vol-dur'),
            skewVel: document.getElementById('t-skew-vel'),
            ivVel: document.getElementById('t-iv-vel'),
            skew25d: document.getElementById('t-skew-25d'),
            oratsMid: document.getElementById('t-orats-mid'),
            oratsSmv: document.getElementById('t-orats-smv'),
            mmUnc: document.getElementById('t-mm-unc'),
            copulaRho: document.getElementById('t-copula-rho'),
        };
        return _zoneEls;
    }

    window.AltarisEvents.on('data:zone:update', (data) => {
        if (typeof ChartCore === 'undefined' || ChartCore.getInstances().length === 0) return;
        if (!data || data.error) return;

        if (typeof WallLines !== 'undefined') {
            WallLines.updateLive(data);
        }

        const z = _getZoneEls();

        if (z.cw && data.underlying_call_wall) z.cw.textContent = data.underlying_call_wall;
        if (z.pw && data.underlying_put_wall) z.pw.textContent = data.underlying_put_wall;
        if (z.mp && data.underlying_max_pain) z.mp.textContent = data.underlying_max_pain;
        if (z.flip && data.underlying_gamma_flip) z.flip.textContent = data.underlying_gamma_flip;
        if (z.dexL && data.dex_wall_long_qqq !== undefined) z.dexL.textContent = data.dex_wall_long_qqq;
        if (z.dexS && data.dex_wall_short_qqq !== undefined) z.dexS.textContent = data.dex_wall_short_qqq;
        if (z.ndex && data.total_dex !== undefined) z.ndex.textContent = (data.total_dex / 1e6).toFixed(2) + 'M';
        if (z.netPrem && data.net_premium_m !== undefined) z.netPrem.textContent = data.net_premium_m.toFixed(2) + 'M';
        if (z.theta && data.net_theta_m !== undefined) z.theta.textContent = data.net_theta_m.toFixed(2) + 'M';
        if (z.ivrv && data.mean_iv !== undefined) z.ivrv.textContent = data.mean_iv.toFixed(2) + '%';

        if (z.skew && data.iv_skew_label) z.skew.textContent = data.iv_skew_label;
        if (z.term && data.term_structure) z.term.textContent = data.term_structure;
        if (z.speed && data.speed_sign) z.speed.textContent = data.speed_sign;
        if (z.conf) z.conf.textContent = data.confluence_count != null ? `${data.confluence_count}` : '0';

        if (z.misp && data.avg_mispricing_pct !== undefined) {
            const mp = data.avg_mispricing_pct || 0;
            z.misp.textContent = mp > 0 ? `${mp.toFixed(1)}%` : '—';
            z.misp.style.color = mp > 10 ? '#ff3060' : mp > 5 ? '#ff9500' : '#ff6b35';
        }
        if (z.flow && data.mark_flow_direction) {
            const f = data.mark_flow_direction;
            if (f === 'CALL_ACCUMULATING')      { z.flow.textContent = '▲ CALLS'; z.flow.style.color = '#2ee88a'; }
            else if (f === 'PUT_ACCUMULATING')  { z.flow.textContent = '▼ PUTS';  z.flow.style.color = '#ff3060'; }
            else                                { z.flow.textContent = '◆ BAL';    z.flow.style.color = '#888'; }
        }
        if (z.ivSpread && data.iv_spread_label) {
            const lbl = data.iv_spread_label;
            z.ivSpread.textContent = lbl;
            const c = lbl === 'WIDE' ? '#ff3060' : lbl === 'NORMAL' ? '#ff9500' : lbl === 'TIGHT' ? '#4cd964' : '#888';
            z.ivSpread.style.color = c;
        }
        if (z.volRegime && data.vol_regime) {
            const regimeColors = { 'STRESSED': '#ff3060', 'ELEVATED': '#ff9500', 'NORMAL': '#a78bfa', 'COMPLACENT': '#4cd964', 'COMPRESSED': '#00dcff' };
            z.volRegime.textContent = data.vol_regime;
            z.volRegime.style.color = regimeColors[data.vol_regime] || '#888';
        }
        if (z.volPrem && data.vol_premium !== undefined) {
            const vp = data.vol_premium;
            const sign = vp >= 0 ? '+' : '';
            z.volPrem.textContent = `${sign}${vp.toFixed(1)}%`;
            z.volPrem.style.color = vp > 15 ? '#ff3060' : vp > 8 ? '#ff9500' : vp > 0 ? '#38bdf8' : '#4cd964';
        }
        if (z.ivRank && data.iv_rank !== undefined) {
            z.ivRank.textContent = `${data.iv_rank.toFixed(0)}`;
            z.ivRank.style.color = data.iv_rank > 80 ? '#ff3060' : data.iv_rank > 60 ? '#ff9500' : data.iv_rank < 20 ? '#4cd964' : '#888';
        }

        // HMM regime posterior confidence (0-100%)
        if (z.volRegimeConf && data.vol_regime_confidence !== undefined) {
            const c = data.vol_regime_confidence;
            z.volRegimeConf.textContent = `${c.toFixed(0)}%`;
            z.volRegimeConf.style.color = c > 85 ? '#4cd964' : c > 65 ? '#ff9500' : '#ff3060';
        }

        // Adverse selection composite score (0-100, higher = more informed flow)
        if (z.asScore && data.adverse_selection_score !== undefined) {
            const s = data.adverse_selection_score;
            z.asScore.textContent = s.toFixed(0);
            // High AS = tape is toxic for MM; low = safe to quote
            z.asScore.style.color = s > 70 ? '#ff3060' : s > 40 ? '#ff9500' : '#4cd964';
        }

        // Copula-calibrated joint signal confidence (50-99.99 percentile)
        if (z.sigCal && data.copula_joint !== undefined) {
            const c = data.copula_joint;
            z.sigCal.textContent = `${c.toFixed(1)}`;
            // Higher percentile = rarer composite signal. Coloured by extremity.
            z.sigCal.style.color = c > 95 ? '#ff3060' : c > 85 ? '#ff9500' : c > 70 ? '#ffd700' : '#888';
        }

        // Vol alert: NONE/SHOCK/STRESS/SKEW_SHIFT etc.
        // Always show NONE explicitly (not em-dash) so user knows pipeline is live.
        if (z.volAlert) {
            const a = String(data.vol_alert || 'NONE');
            z.volAlert.textContent = a;
            z.volAlert.style.color = a === 'NONE' ? '#666' :
                (a.includes('SHOCK') || a.includes('STRESS')) ? '#ff3060' : '#ff9500';
        }

        // Duration in current HMM regime (seconds → compact Xs/Xm/Xh)
        if (z.volDur && data.vol_regime_duration !== undefined) {
            const s = Number(data.vol_regime_duration) || 0;
            z.volDur.textContent = s < 60 ? `${s.toFixed(0)}s` :
                s < 3600 ? `${(s / 60).toFixed(0)}m` : `${(s / 3600).toFixed(1)}h`;
        }

        // Skew velocity (Δ/min) — leading indicator, sign-colored
        if (z.skewVel && data.skew_velocity !== undefined) {
            const v = Number(data.skew_velocity) || 0;
            const sign = v >= 0 ? '+' : '';
            z.skewVel.textContent = `${sign}${v.toFixed(2)}`;
            z.skewVel.style.color = Math.abs(v) < 0.01 ? '#888' : v > 0 ? '#4cd964' : '#ff3060';
        }

        // IV velocity (Δ/min)
        if (z.ivVel && data.iv_velocity !== undefined) {
            const v = Number(data.iv_velocity) || 0;
            const sign = v >= 0 ? '+' : '';
            z.ivVel.textContent = `${sign}${v.toFixed(2)}`;
            z.ivVel.style.color = Math.abs(v) < 0.01 ? '#888' : v > 0 ? '#4cd964' : '#ff3060';
        }

        // 25-delta risk reversal (IV pts, positive = put skew premium)
        if (z.skew25d && data.skew_25d !== undefined) {
            const v = Number(data.skew_25d) || 0;
            const sign = v >= 0 ? '+' : '';
            z.skew25d.textContent = `${sign}${v.toFixed(2)}`;
            z.skew25d.style.color = v > 3 ? '#ff3060' : v > 1 ? '#ff9500' : v < -1 ? '#4cd964' : '#888';
        }

        // ORATS ATM mid IV
        if (z.oratsMid && data.orats_mid_iv !== undefined) {
            const v = Number(data.orats_mid_iv) || 0;
            z.oratsMid.textContent = `${v.toFixed(1)}%`;
        }

        // ORATS smoothed-mid vol
        if (z.oratsSmv && data.orats_smv_vol !== undefined) {
            const v = Number(data.orats_smv_vol) || 0;
            z.oratsSmv.textContent = `${v.toFixed(1)}%`;
        }

        // Market-maker uncertainty (0-3 normalized bid-ask band)
        if (z.mmUnc && data.mm_uncertainty !== undefined) {
            const v = Number(data.mm_uncertainty) || 0;
            z.mmUnc.textContent = v.toFixed(2);
            z.mmUnc.style.color = v > 2 ? '#ff3060' : v > 1 ? '#ff9500' : '#888';
        }

        // Copula-specific correlation estimate
        if (z.copulaRho && data.copula_rho_bar !== undefined) {
            const v = Number(data.copula_rho_bar) || 0;
            z.copulaRho.textContent = v.toFixed(2);
            z.copulaRho.style.color = v > 0.7 ? '#ff3060' : v > 0.4 ? '#ff9500' : '#38bdf8';
        }

        if (data.dex_profile && typeof ThermalFlare !== 'undefined') {
            ThermalFlare.updateData(data.dex_profile);
        }
    });

    window.AltarisEvents.on('data:tape:alert', (alert) => {
        const key = `${alert.price}_${alert.timestamp}`;
        alert._expiry = Date.now() + 15000;
        _tapeAlerts.set(key, alert);
        // Hard cap — drop oldest when over limit
        if (_tapeAlerts.size > 300) {
            const keys = [..._tapeAlerts.keys()].slice(0, 100);
            keys.forEach(k => _tapeAlerts.delete(k));
        }
    });
    // Batch expiry sweep every 5s instead of per-alert setTimeout (prevents timeout queue leak)
    setInterval(() => {
        const now = Date.now();
        for (const [k, a] of _tapeAlerts) {
            if (a._expiry && now > a._expiry) _tapeAlerts.delete(k);
        }
    }, 5000);

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
        const dir = isLong ? '[BUY]' : '[SELL]';
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
        // Cap to 50 signal types to prevent unbounded growth
        const eKeys = Object.keys(window._latestEdgeSignals);
        if (eKeys.length > 50) {
            for (const k of eKeys.slice(0, eKeys.length - 50)) delete window._latestEdgeSignals[k];
        }
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

    // ── l2_update: Full L2 state via WebSocket ──
    // Throttled to 10Hz (100ms) — DOM ladder at 20Hz vs 10Hz is visually identical.
    // Signals still processed immediately; only the expensive DOM render is throttled.
    let _l2RenderLast = 0, _l2RenderQueued = null, _l2RenderTid = 0;
    const _L2_RENDER_MS = 100; // 10Hz
    window.AltarisEvents.on('data:l2:update', (data) => {
        if (!data) return;
        const symDom = (data.dom || {})[_l2ChartSymbol];
        if (!symDom || (Object.keys(symDom.bids || {}).length === 0 && Object.keys(symDom.asks || {}).length === 0)) {
            window._l2WsActive = true;
            window._l2WsLastTs = Date.now();
            if (data.imbalance !== undefined) _l2RenderImbalance(data);
            if (data.signals) _l2RenderSignals(data.signals);
            return;
        }
        window._l2WsActive = true;
        window._l2WsLastTs = Date.now();
        // Throttle expensive DOM render to 10Hz
        const now = performance.now();
        if (now - _l2RenderLast >= _L2_RENDER_MS) {
            _l2RenderLast = now;
            _l2Render(data);
        } else {
            _l2RenderQueued = data;
            if (!_l2RenderTid) {
                _l2RenderTid = setTimeout(() => {
                    _l2RenderTid = 0;
                    _l2RenderLast = performance.now();
                    if (_l2RenderQueued) { _l2Render(_l2RenderQueued); _l2RenderQueued = null; }
                }, _L2_RENDER_MS - (now - _l2RenderLast));
            }
        }
        if (symDom && symDom.mid_price) {
            const spotEl = document.getElementById('t-spot');
            if (spotEl) spotEl.textContent = symDom.mid_price.toFixed(2);

            // ── Candle-to-DOM sync: update chart candle close at L2 speed ──
            // Between trades, the chart freezes while the ladder keeps ticking.
            // This syncs the active candle's close to the DOM mid so chart is
            // as responsive as the ladder. No new objects — reuses _lastCandleOHLC.
            if (_lastCandleOHLC && typeof ChartCore !== 'undefined') {
                const mid = symDom.mid_price;
                _lastCandleOHLC.close = mid;
                if (mid > _lastCandleOHLC.high) _lastCandleOHLC.high = mid;
                if (mid < _lastCandleOHLC.low) _lastCandleOHLC.low = mid;
                try {
                    const insts = ChartCore.getInstances();
                    for (let i = 0; i < insts.length; i++) {
                        insts[i].candleSeries.update(_lastCandleOHLC);
                    }
                } catch(e) { /* silent — chart may not be mounted yet */ }
            }
        }
    });

    // ── candle_history: WS push of full candle history (with bp) on connect ──
    // Server pushes this immediately on Socket.IO connect (server.py handle_connect)
    // and on subscribe. Without this handler, bp data was silently dropped and
    // volume bubbles would never render until REST polling accumulated enough data.
    window.AltarisEvents.on('data:candles:history', (data) => {
        console.log('[CANDLE HISTORY]', data?.symbol, data?.tf, 'count:', data?.candles?.length, 'active:', _l2ChartSymbol, _l2ChartTF);
        if (!data || !data.candles || data.candles.length === 0) return;
        // Leak-fix: ALWAYS buffer keyed by sym+tf so a history that arrives
        // for a pane that's still mounting (or a brief tf mismatch during
        // layout switch) can be replayed once the chart is ready. Previously
        // mismatches were silently dropped → blank chart.
        const _histKey = `${data.symbol}|${data.tf}`;
        window._pendingCandleHistoryMap = window._pendingCandleHistoryMap || {};
        window._pendingCandleHistoryMap[_histKey] = data;
        if (data.symbol !== _l2ChartSymbol || data.tf !== _l2ChartTF) {
            // Not for the currently selected chart — buffered for later switch-back.
            console.warn('[CANDLE HISTORY BUFFER] tf/symbol mismatch, keeping for later', _histKey);
            return;
        }
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
                    time: _utcToET(c.time),
                    o: c.open, h: c.high, l: c.low, c: c.close, close: c.close,
                    bp: c.bp || null,
                    sweeps: c.sweeps || null,
                    delta_div: c.delta_div || null, ignition: c.ignition || null,
                    spoofs: c.spoofs || null,
                    wall_gone: c.wall_gone || null,
                    absorption: c.absorption || null, depth_deltas: c.depth_deltas || null
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
                // Scroll to right edge showing latest candles, not fitContent which compresses everything
                inst.chart.timeScale().scrollToPosition(5, false);
                setTimeout(() => {
                    try { inst.chart.timeScale().scrollToPosition(5, false); } catch(e) {}
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
        _l2LastCandleTime = _utcToET(candles[candles.length - 1].time);

        // Dismiss welcome screen + hide loading overlay on data arrival
        if (window._dismissWelcome) window._dismissWelcome();
        _l2HideOverlay();
    });
}

// ── Timezone helper ──
// Server sends raw UTC epoch seconds. This shifts them to ET for LWC display.
// IMPORTANT: ALL timestamps stored in _l2LastCandleTime, _l2SeamTime, etc.
// MUST go through _utcToET. Raw UTC will cause silent comparison mismatches
// (candle_update rejected because et < _l2LastCandleTime).
function _utcToET(utcEpoch) {
    return utcEpoch - 4 * 3600; // EDT = UTC-4
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
    // Build DOM nodes once, then update in-place (avoid innerHTML rebuild at 20Hz)
    if (!row._imbCards || row._imbCards.length !== L2_SYMBOLS.length) {
        row.innerHTML = L2_SYMBOLS.map(sym => `<div class="l2-imb-card" data-sym="${sym}">
          <div class="l2-imb-label"><span class="imb-sym">${sym}</span> <span class="imb-mid" style="color:var(--text);font-size:.72rem">—</span></div>
          <div class="l2-imb-bar-wrap"><div class="l2-imb-bar"></div></div>
          <div class="l2-imb-val"><span class="imb-pct">—</span><span class="l2-imb-side imb-side">—</span></div>
        </div>`).join('');
        row._imbCards = L2_SYMBOLS.map((sym, i) => {
            const card = row.children[i];
            return { midEl: card.querySelector('.imb-mid'), barEl: card.querySelector('.l2-imb-bar'), pctEl: card.querySelector('.imb-pct'), sideEl: card.querySelector('.imb-side') };
        });
    }
    L2_SYMBOLS.forEach((sym, i) => {
        const c = row._imbCards[i];
        const snap = dom[sym] || {};
        const imb = snap.imbalance != null ? snap.imbalance : (data.imbalance || {})[sym];
        const midP = mid[sym] || 0;
        const pct = imb != null ? Math.abs(imb) * 50 : 0;
        const isBid = imb != null && imb > 0;
        const barClr = imb == null ? '#555' : (isBid ? 'var(--green)' : 'var(--red)');
        c.midEl.textContent = midP > 0 ? midP.toFixed(2) : '—';
        c.barEl.style.width = pct + '%';
        c.barEl.style.background = barClr;
        c.barEl.style.transformOrigin = 'left';
        if (isBid) { c.barEl.style.right = '50%'; c.barEl.style.left = 'auto'; c.barEl.style.transform = 'scaleX(-1)'; }
        else { c.barEl.style.left = '50%'; c.barEl.style.right = ''; c.barEl.style.transform = ''; }
        c.pctEl.textContent = imb != null ? (imb * 100).toFixed(1) + '%' : '—';
        c.sideEl.textContent = imb == null ? '—' : (isBid ? 'BID HVY' : 'ASK HVY');
        c.sideEl.style.color = barClr;
    });
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
                    ctx.fillText('WAITING FOR DOM DATA', cssW / 2, cssH / 2 - 10);
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
                // Void offsetWidth to retrigger animation (single sync reflow, no double-rAF)
                void row.el.offsetWidth;
                row.el.classList.add(flashClass);
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

    // 4. MM pull events (raw counts)
    const mmB = data.mm_bid_pulls || 0;
    const mmA = data.mm_ask_pulls || 0;
    const mmSD = data.mm_smart_dumb || 0;
    if (mmB + mmA > 0) {
        let mmText = `MM B${mmB} A${mmA}`;
        if (mmSD > 0) mmText += ' !';
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
            if (_l2TapeAll.length > 2000) _l2TapeAll.splice(0, _l2TapeAll.length - 1500);
            // Feed delta accumulator
            const side = t.side || (t.spin > 0 ? 'buy' : 'sell');
            const vol = t.volume || t.v || 1;
            _deltaHistory.push({ side, vol });
        }
    }
    // Cap array size (trim from front = oldest trades)
    if (_l2TapeAll.length > 300) _l2TapeAll.splice(0, _l2TapeAll.length - 300);
    if (_deltaHistory.length > _DELTA_WINDOW) _deltaHistory.splice(0, _deltaHistory.length - _DELTA_WINDOW);
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

                // Replay buffered candle_history if it arrived before chart was ready.
                // Leak-fix: prefer the sym+tf-matched buffer over the legacy single slot,
                // and clear the entry only AFTER the emit resolves (next microtask) so a
                // rapid 2nd layout-switch can't orphan the replay.
                const _activeKey = `${_l2ChartSymbol}|${_l2ChartTF}`;
                const _bufMap = window._pendingCandleHistoryMap || {};
                const _pending = _bufMap[_activeKey] || window._pendingCandleHistory;
                if (_pending) {
                    setTimeout(() => {
                        try {
                            if (window.AltarisEvents) window.AltarisEvents.emit('data:candles:history', _pending);
                        } finally {
                            delete _bufMap[_activeKey];
                            if (window._pendingCandleHistory === _pending) window._pendingCandleHistory = null;
                        }
                        // Re-arm the chart's initial fit now that data is in place.
                        try {
                            ChartCore.getInstances().forEach(inst => {
                                if (typeof inst._tryInitialFit === 'function') inst._tryInitialFit();
                            });
                        } catch(e) {}
                    }, 100);
                }

                // Direct REST fetch if no candle data loaded yet
                // This is the primary data load — don't rely solely on Socket.IO candle_history
                if (!_l2CandleDataCache) {
                    setTimeout(() => {
                        if (!_l2CandleDataCache) {
                            _l2FetchCandles(true).then(() => {
                                ChartCore.getInstances().forEach(inst => {
                                    if (inst.feature !== 'heatmap') inst.chart.timeScale().fitContent();
                                });
                            }).catch(() => {});
                        }
                    }, 500);
                }
            });
            // Scroll: skip ALL overlay redraws. Global _chartScrolling flag handles it.
            // Overlays re-render 200ms after scroll stops via their own dirty flags.
            window.AltarisEvents.on('chart:scroll', () => {
                // Nothing — all overlays check window._chartScrolling and skip.
                // After scroll stops (200ms), they'll render on next data update.
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
    _l2FetchInFlight = false;

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
    // 4c. Clear cached bp so old symbol's bubbles don't leak into new chart
    _cachedBp = null;
    _cachedEnriched = null;
    _lastCandleOHLC = null;

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
    _l2FetchInFlight = false;

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

    // 4. Set new timeframe + reset all state
    _l2ChartTF = tf;
    _l2LastCandleTime = 0;
    _l2FetchVersion++;
    _cachedBp = null;
    _cachedEnriched = null;
    _lastCandleOHLC = null;

    // 5. Show loading overlay
    _l2ShowOverlay(`Loading ${_l2ChartSymbol} ${tf}...`, false);

    // 6. Tell server about new tf (for live candle_update filtering)
    if (typeof DataFetch !== 'undefined') DataFetch.subscribe(_l2ChartSymbol, _l2ChartTF);

    // 7. Fetch candle history via REST (reliable, no race conditions)
    _l2FetchCandles(true).then(() => _l2HideOverlay()).catch(() => _l2HideOverlay());

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

    // Abort any previous in-flight fetch — but ONLY for explicit symbol/tf switches,
    // NOT for safety-net retries (which would kill a perfectly good in-flight request)
    if (fullRedraw && attempt === 0) {
        // Always abort previous fetch on explicit tf/symbol switch
        if (_l2FetchController) _l2FetchController.abort();
        _l2FetchController = new AbortController();
        _l2FetchInFlight = false;
    }

    _l2FetchInFlight = true;
    // _l2LastCandleTime is stored in ET (UTC-4). Backend expects UTC for ?since —
    // convert back by adding 4*3600 so the server filters on real UTC epoch.
    const since = (!fullRedraw && _l2LastCandleTime > 0) ? (_l2LastCandleTime + 4 * 3600) : 0;
    const signal = _l2FetchController ? _l2FetchController.signal : null;
    console.log('[L2 FETCH]', _l2ChartSymbol, _l2ChartTF, 'fullRedraw=', fullRedraw, 'since=', since);
    
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
                    msg.innerHTML = 'NO CANDLE DATA<br><span style="font-size:.7rem;opacity:.6">Waiting for L2 feed or market may be closed</span>';
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
                                sweeps: c.sweeps || null,
                                delta_div: c.delta_div || null, ignition: c.ignition || null,
                                spoofs: c.spoofs || null,
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
                                    time: et,
                                    o: c.open, h: c.high, l: c.low, c: c.close, close: c.close,
                                    bp: c.bp || null,
                                    sweeps: c.sweeps || null,
                                    delta_div: c.delta_div || null, ignition: c.ignition || null,
                                    spoofs: c.spoofs || null,
                                    wall_gone: c.wall_gone || null,
                                    absorption: c.absorption || null, depth_deltas: c.depth_deltas || null
                                });
                            }
                        });
                    }
                }
            }

            // Track the newest candle timestamp (ET-converted for comparison with _utcToET)
            _l2LastCandleTime = _utcToET(candles[candles.length - 1].time);
            _l2FetchInFlight = false;
            _l2HideOverlay(); // clear any error overlay
        })
        .catch(err => {
            _l2FetchInFlight = false;
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
                _l2ShowOverlay('Failed to load chart data -- click a symbol to retry', true);
            }
        });
}

// Cache L2 render DOM refs
let _l2RenderEls = null;
function _getL2RenderEls() {
    if (_l2RenderEls) return _l2RenderEls;
    _l2RenderEls = {
        dot: document.getElementById('l2-status-dot'),
        txt: document.getElementById('l2-status-text'),
        strip: document.getElementById('l2-symbol-prices'),
        sMid: document.getElementById('s-mid'),
        sSpread: document.getElementById('s-spread'),
        sImbal: document.getElementById('s-imbal'),
        sBidAsk: document.getElementById('s-bidask'),
    };
    return _l2RenderEls;
}

function _l2Render(data) {
    const el = _getL2RenderEls();
    const conn = data.connected;
    if (el.dot) el.dot.className = 'l2-dot' + (conn ? ' live' : '');
    if (el.txt) el.txt.textContent = conn ? 'LIVE' : 'DISCONNECTED';

    if (el.strip) {
        const mid = data.mid_prices || {};
        el.strip.innerHTML = L2_SYMBOLS.map(s =>
            `<div class="l2-sym-price"><span class="l2-sym-label">${s}</span><span>${mid[s] ? mid[s].toFixed(2) : '—'}</span></div>`
        ).join('');
    }

    // ── Status Bar live data ──
    const symDom = (data.dom || {})[_l2ChartSymbol] || {};
    const sMid = el.sMid;
    const sSpread = el.sSpread;
    const sImbal = el.sImbal;
    const sBidAsk = el.sBidAsk;
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
    // Live clock (EST)
    const tTsEl = document.getElementById('t-timestamp');
    if (tTsEl) {
        const now = new Date();
        const hh = now.getHours().toString().padStart(2, '0');
        const mm = now.getMinutes().toString().padStart(2, '0');
        const ss = now.getSeconds().toString().padStart(2, '0');
        tTsEl.textContent = `${hh}:${mm}:${ss} ET`;
    }
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
        ['vp-settings-panel', 'hm-settings-panel', 'tf-settings-panel', 'master-settings-panel'].forEach(id => {
            if (id === exceptId) return;
            const el = document.getElementById(id);
            if (el) { if (id === 'tf-settings-panel') el.remove(); else el.style.display = 'none'; }
        });
    };

    // ── Toolbar: Volume Profile toggle ──
    const vpToggle = document.getElementById('t-vp-toggle');
    if (vpToggle) {
        vpToggle.addEventListener('click', () => {
            if (typeof VolumeProfileOverlay === 'undefined') return;
            const vis = VolumeProfileOverlay.toggleVisibility();
            vpToggle.classList.toggle('active', vis);
        });
    }

    // ── Master Settings Panel ──
    const masterBtn = document.getElementById('master-settings-btn');
    if (masterBtn) {
        masterBtn.addEventListener('click', () => {
            const panel = document.getElementById('master-settings-panel');
            if (!panel) return;
            const opening = panel.style.display === 'none';
            if (opening) {
                window.closeAllSettingsPanels('master-settings-panel');
                _buildMasterSettings(panel);
            }
            panel.style.display = opening ? '' : 'none';
        });
    }

    function _buildMasterSettings(panel) {
        if (panel.dataset.built) return;
        panel.dataset.built = '1';
        panel.innerHTML = `
            <div class="vp-panel-header">
                <span class="vp-panel-title">Settings</span>
                <button class="vp-panel-close" id="ms-close">\u2715</button>
            </div>

            <!-- Accordion sections -->
            <div class="ms-body">

                <!-- CHART OVERLAYS -->
                <div class="ms-section" data-open="true">
                    <div class="ms-section-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                        <span class="ms-arrow">\u25B8</span> Chart Overlays
                    </div>
                    <div class="ms-section-body">
                        <div class="vp-field">
                            <label class="vp-checkbox"><input type="checkbox" id="ms-ov-bubbles" checked> Trade Bubbles</label>
                        </div>
                        <div class="vp-field">
                            <label class="vp-checkbox"><input type="checkbox" id="ms-ov-flare" checked> DEX Thermal Flare</label>
                        </div>
                        <div class="vp-field">
                            <label class="vp-checkbox"><input type="checkbox" id="ms-vp" checked> Volume Profile</label>
                        </div>
                        <div class="vp-field">
                            <label class="vp-checkbox"><input type="checkbox" id="ms-walls" checked> Options Walls</label>
                        </div>
                        <div class="vp-separator"></div>
                        <div class="vp-field">
                            <label class="vp-checkbox"><input type="checkbox" id="ms-metrics"> Metrics Ribbon</label>
                        </div>
                    </div>
                </div>

                <!-- VOLUME PROFILE -->
                <div class="ms-section" data-open="false">
                    <div class="ms-section-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                        <span class="ms-arrow">\u25B8</span> Volume Profile
                    </div>
                    <div class="ms-section-body">
                        <button class="ms-open-btn" id="ms-vp-open">Open VP Settings \u2192</button>
                    </div>
                </div>

                <!-- DRAWING TOOLS -->
                <div class="ms-section" data-open="false">
                    <div class="ms-section-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                        <span class="ms-arrow">\u25B8</span> Drawing Tools
                    </div>
                    <div class="ms-section-body">
                        <div class="ms-draw-grid">
                            <button class="ms-draw-btn" data-draw="draw_hline">\u2500 H-Line</button>
                            <button class="ms-draw-btn" data-draw="draw_vline">\u2502 V-Line</button>
                            <button class="ms-draw-btn" data-draw="draw_box">\u25A0 Box</button>
                            <button class="ms-draw-btn ms-draw-del" data-draw="delete">\u2715 Delete</button>
                        </div>
                        <div class="vp-separator"></div>
                        <div class="vp-field">
                            <span class="vp-field-label">Color</span>
                            <div class="ms-color-dots">
                                <div class="ms-dot active" data-color="#E0A800" style="background:#E0A800"></div>
                                <div class="ms-dot" data-color="#26A69A" style="background:#26A69A"></div>
                                <div class="ms-dot" data-color="#EF5350" style="background:#EF5350"></div>
                                <div class="ms-dot" data-color="#7a8ba8" style="background:#7a8ba8"></div>
                                <div class="ms-dot" data-color="#ffffff" style="background:#ffffff"></div>
                            </div>
                        </div>
                        <div class="vp-separator"></div>
                        <div class="vp-field">
                            <span class="vp-field-label">Label</span>
                            <input type="text" id="ms-draw-label" class="vp-num-input" style="width:100px;text-align:left" placeholder="S/R, POI...">
                        </div>
                        <div class="vp-field">
                            <label class="vp-checkbox"><input type="checkbox" id="ms-draw-extend"> Extend Right</label>
                        </div>
                        <div class="vp-separator"></div>
                        <div class="vp-field">
                            <button class="ms-clear-btn" id="ms-clear-drawings">Clear All</button>
                        </div>
                    </div>
                </div>

                <!-- HEATMAP -->
                <div class="ms-section" data-open="false">
                    <div class="ms-section-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                        <span class="ms-arrow">\u25B8</span> DOM Heatmap
                    </div>
                    <div class="ms-section-body">
                        <button class="ms-open-btn" id="ms-hm-open">Open Heatmap Settings \u2192</button>
                    </div>
                </div>

            </div>
        `;

        // Wire close
        document.getElementById('ms-close').addEventListener('click', () => { panel.style.display = 'none'; });

        // Chart overlay config helper — finds the active chart container's _overlayConfig
        function _setOverlay(key, enabled) {
            document.querySelectorAll('[data-slot]').forEach(slot => {
                const wrap = slot.querySelector('.chart-wrap, [class*="chart"]');
                if (wrap && wrap._overlayConfig) wrap._overlayConfig[key] = enabled;
                // Also check direct children
                slot.querySelectorAll('*').forEach(el => {
                    if (el._overlayConfig) el._overlayConfig[key] = enabled;
                });
            });
        }

        // Chart overlay toggles
        const ovMap = {
            'ms-ov-bubbles': 'bubbles',
            'ms-ov-flare': 'flare',
        };
        for (const [id, key] of Object.entries(ovMap)) {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', (e) => _setOverlay(key, e.target.checked));
        }

        document.getElementById('ms-walls').addEventListener('change', (e) => {
            _setOverlay('walls', e.target.checked);
            if (typeof WallLines !== 'undefined') WallLines.toggle();
            const wb = document.getElementById('t-walls-toggle');
            if (wb) wb.classList.toggle('active', e.target.checked);
        });
        document.getElementById('ms-vp').addEventListener('change', (e) => {
            _setOverlay('vp', e.target.checked);
            if (typeof VolumeProfileOverlay !== 'undefined') {
                const vis = VolumeProfileOverlay.toggleVisibility();
                const vb = document.getElementById('t-vp-toggle');
                if (vb) vb.classList.toggle('active', vis);
            }
        });
        document.getElementById('ms-metrics').addEventListener('change', (e) => {
            const terminal = document.getElementById('terminal');
            if (terminal) terminal.classList.toggle('metrics-collapsed', !e.target.checked);
            const mb = document.getElementById('t-metrics-toggle');
            if (mb) mb.classList.toggle('active', e.target.checked);
        });

        // VP settings deep link
        document.getElementById('ms-vp-open').addEventListener('click', () => {
            panel.style.display = 'none';
            if (typeof VolumeProfileOverlay !== 'undefined') VolumeProfileOverlay.openSettings();
        });

        // Drawing tools
        panel.querySelectorAll('.ms-draw-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const mode = btn.dataset.draw;
                if (typeof DrawingTools !== 'undefined') {
                    DrawingTools.setMode(mode);
                    panel.querySelectorAll('.ms-draw-btn').forEach(b => b.classList.remove('active'));
                    if (mode !== 'delete') btn.classList.add('active');
                }
            });
        });
        panel.querySelectorAll('.ms-dot').forEach(dot => {
            dot.addEventListener('click', () => {
                panel.querySelectorAll('.ms-dot').forEach(d => d.classList.remove('active'));
                dot.classList.add('active');
                if (typeof DrawingTools !== 'undefined') DrawingTools.setColor(dot.dataset.color);
            });
        });
        document.getElementById('ms-clear-drawings').addEventListener('click', () => {
            if (typeof DrawingTools !== 'undefined' && confirm('Clear all drawings?')) DrawingTools.clearAll();
        });

        // Label input — apply to selected drawing on Enter or blur
        const labelInput = document.getElementById('ms-draw-label');
        if (labelInput) {
            const applyLabel = () => {
                if (typeof DrawingTools !== 'undefined') DrawingTools.setLabel(labelInput.value);
            };
            labelInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') applyLabel(); });
            labelInput.addEventListener('blur', applyLabel);
        }

        // Extend toggle — apply to selected hline
        const extendCheck = document.getElementById('ms-draw-extend');
        if (extendCheck) {
            extendCheck.addEventListener('change', () => {
                if (typeof DrawingTools !== 'undefined') DrawingTools.toggleExtend();
            });
        }

        // Heatmap settings deep link
        document.getElementById('ms-hm-open').addEventListener('click', () => {
            panel.style.display = 'none';
            const hmPanel = document.getElementById('hm-settings-panel');
            if (hmPanel) hmPanel.style.display = hmPanel.style.display === 'none' ? '' : 'none';
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

                    // Overlay config stored on container — controlled from master settings panel
                    // (BUB/FLR/ICE/VP/LVL toolbar removed — now in Settings > Chart Overlays)

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
                            // Standalone render loop — throttled to 20fps + pauses during chart scroll
                            let _pfRAF;
                            let _pfDirty = true;
                            let _pfLastFrame = 0;
                            let _pfScrollPaused = false;
                            let _pfScrollTimer = 0;
                            if (window.AltarisEvents) {
                                const _pfScrollHandler = () => {
                                    _pfScrollPaused = true;
                                    clearTimeout(_pfScrollTimer);
                                    _pfScrollTimer = setTimeout(() => { _pfScrollPaused = false; _pfDirty = true; }, 300);
                                };
                                window.AltarisEvents.on('chart:scroll', _pfScrollHandler);
                                slotEl._pfScrollHandler = _pfScrollHandler;
                            }
                            const _pfLoop = (now) => {
                                if (!document.body.contains(pCanvas)) return;
                                // Skip entirely during scroll — fluid sim is decorative, not critical
                                if (_pfScrollPaused) {
                                    _pfRAF = requestAnimationFrame(_pfLoop);
                                    return;
                                }
                                // Throttle to 20fps (50ms)
                                if (now - _pfLastFrame >= 50) {
                                    _pfLastFrame = now;
                                    if (PressureField._ready && _pfDirty) {
                                        _pfDirty = false;
                                        PressureField.update(0.05);
                                        PressureField.render();
                                    }
                                }
                                _pfRAF = requestAnimationFrame(_pfLoop);
                            };
                            _pfRAF = requestAnimationFrame(_pfLoop);
                            slotEl._pfMarkDirty = () => { _pfDirty = true; };
                            slotEl._pfRAF = _pfRAF;

                            // Wire L2 DOM data → obstacle texture
                            const _pfL2Handler = (data) => {
                                if (!PressureField._ready || !document.body.contains(pCanvas)) return;
                                const symData = (data.dom || {})[_l2ChartSymbol] || {};
                                const bids = symData.bids || {};
                                const asks = symData.asks || {};
                                const mid = symData.mid_price || 0;
                                if (!mid) return;
                                const tick = 0.25;
                                const levels = 40;
                                const visiblePrices = [];
                                for (let i = -levels; i <= levels; i++) {
                                    visiblePrices.push(mid + i * tick);
                                }
                                let maxDepth = 1;
                                for (const k of Object.keys(bids)) { if (bids[k] > maxDepth) maxDepth = bids[k]; }
                                for (const k of Object.keys(asks)) { if (asks[k] > maxDepth) maxDepth = asks[k]; }
                                PressureField.updateObstacles(bids, asks, visiblePrices, maxDepth);
                                _pfDirty = true; // mark for next render frame
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
                                _pfDirty = true;
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
                                    _kDirty = true;
                                };
                                window.AltarisEvents.on('data:trades:update', _kTradeHandler);
                                slotEl._kTradeHandler = _kTradeHandler;

                                let _kRAF;
                                let _kDirty = true;
                                const _kLoop = () => {
                                    if (!document.body.contains(kCanvas)) return;
                                    if (_kDirty) {
                                        _kDirty = false;
                                        _drawKineticFallback(kCanvas, _kHeat);
                                    }
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
                } else if (featureKey === 'vpintel') {
                    if (typeof VPIntelPane !== 'undefined') VPIntelPane.init(slotEl);
                } else if (featureKey === 'flow') {
                    if (typeof FlowPane !== 'undefined') FlowPane.init(slotEl);
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
                        // Fix #10 — selector-based lookup was missing instances when
                        // the layout engine tore down slot DOM before unmount fired.
                        // Walk the instance array and destroy any instance whose
                        // container is this slot, inside this slot, or already
                        // detached. Catches orphans that previously leaked and
                        // caused "blank chart with only VP lines" symptom.
                        const stale = ChartCore.getInstances().filter(inst => {
                            const c = inst.container;
                            if (!c) return true;
                            if (!c.isConnected) return true;
                            if (c === slotEl) return true;
                            if (slotEl && slotEl.contains && slotEl.contains(c)) return true;
                            return false;
                        });
                        stale.forEach(inst => { try { ChartCore.destroy(inst.container); } catch(e) {} });
                    }
                }
                if (featureKey === 'pressure') {
                    if (slotEl._pfRAF) cancelAnimationFrame(slotEl._pfRAF);
                    if (slotEl._pfScrollHandler) window.AltarisEvents.off('chart:scroll', slotEl._pfScrollHandler);
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
                if (featureKey === 'vpintel' && typeof VPIntelPane !== 'undefined') VPIntelPane.destroy(slotEl);
                if (featureKey === 'flow' && typeof FlowPane !== 'undefined') FlowPane.destroy();
            };

            AltarisLayout.triggerInitialMounts();
            _startL2Poll();

            // Safety net: if candles haven't loaded, force REST fetch
            // Two passes: fast (2s) catches slow WS, slow (6s) catches everything
            const _safetyFetch = (label) => {
                if (!_l2CandleDataCache && typeof ChartCore !== 'undefined' && ChartCore.getInstances().length > 0) {
                    console.warn(`[Safety] ${label} — no candle data, force fetching...`);
                    _l2FetchCandles(true).then(() => {
                        ChartCore.getInstances().forEach(inst => inst.chart.timeScale().fitContent());
                    });
                }
            };
            setTimeout(() => _safetyFetch('2s check'), 2000);
            setTimeout(() => _safetyFetch('6s check'), 6000);
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
    // Pause when tab hidden to avoid wasted CPU
    if (window._termMetricsTimer) clearInterval(window._termMetricsTimer);
    window._termMetricsTimer = setInterval(_termUpdateMetrics, 2000);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            if (window._termMetricsTimer) { clearInterval(window._termMetricsTimer); window._termMetricsTimer = null; }
        } else {
            if (!window._termMetricsTimer) window._termMetricsTimer = setInterval(_termUpdateMetrics, 2000);
        }
    });

    // Removed duplicate event listener for #t-heatmap-settings-btn since it conflicts with volume_bubbles.js

    // Thermal Flare Settings — delegated to ThermalFlare module (tf- prefixed IDs)
    const tfBtn = document.getElementById('tf-settings-btn');
    if (tfBtn && typeof ThermalFlare !== 'undefined') {
        tfBtn.addEventListener('click', () => ThermalFlare.openSettings());
    }

    console.log('[Terminal] Super Chart mode initialized');
});
