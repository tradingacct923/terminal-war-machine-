/**
 * Volume Profile Overlay — MotiveWave-style VP on chart canvas
 *
 * Modes: Prior Day, Rolling 4H, Custom Pin
 * Data: /api/vprofile (TopStepX 1m candle bp data)
 * Renders: delta-colored bars (buy/sell), POC, VAH, VAL
 * Attaches as a LightweightCharts series primitive (draws every frame)
 */
(function() {
'use strict';

// ── Config (all user-adjustable via settings panel) ──
const VP_CONFIG = {
    BAR_WIDTH_PCT: 0.24,
    BAR_ALPHA: 0.55,
    BAR_ALPHA_POC: 0.80,
    MIN_BAR_PX: 3,
    POC_LINE_WIDTH: 2,
    POC_LINE_DASH: [],          // solid line for POC
    VAH_VAL_WIDTH: 0.75,
    VAH_VAL_DASH: [2, 3],
    LABEL_FONT: '9px "JetBrains Mono", "SF Mono", monospace',
    REFRESH_INTERVAL: 3000,
    SIDE: 'right',
    MARGIN: 6,
    ROW_COUNT: 0,
    VA_PCT: 0.70,
    EXTEND_LINES: true,
    BUY_COLOR: [40, 180, 240],       // steel blue
    SELL_COLOR: [180, 80, 140],      // muted magenta
    POC_COLOR: '#e8b830',            // warm amber gold
    VA_COLOR: 'rgba(80,140,220,0.5)',
};

// ── Per-Profile Visual Styles ──
// Each mode gets distinct side, opacity, and color treatment
const PROFILE_STYLES = {
    prior_day: {
        side: 'right',
        barAlpha: 0.22,
        barAlphaPOC: 0.40,
        buyColor: [60, 140, 180],
        sellColor: [160, 90, 130],
        pocColor: 'rgba(232,184,48,0.35)',
        vaColor: 'rgba(80,140,220,0.2)',
        label: 'PD',
        barWidthPct: 0.16,
    },
    session: {
        side: 'right',
        barAlpha: 0.65,
        barAlphaPOC: 0.88,
        buyColor: [40, 180, 240],
        sellColor: [180, 80, 140],
        pocColor: '#e8b830',
        vaColor: 'rgba(80,140,220,0.5)',
        label: 'DEV',
        barWidthPct: 0.24,
    },
    rolling_4h: {
        side: 'right',
        barAlpha: 0.55,
        barAlphaPOC: 0.80,
        buyColor: [40, 180, 240],
        sellColor: [180, 80, 140],
        pocColor: '#e8b830',
        vaColor: 'rgba(80,140,220,0.45)',
        label: '4H',
        barWidthPct: 0.22,
    },
    rolling_1h: {
        side: 'right',
        barAlpha: 0.50,
        barAlphaPOC: 0.75,
        buyColor: [40, 180, 240],
        sellColor: [180, 80, 140],
        pocColor: '#e8b830',
        vaColor: 'rgba(80,140,220,0.4)',
        label: '1H',
        barWidthPct: 0.20,
    },
    rolling_2h: {
        side: 'right',
        barAlpha: 0.52,
        barAlphaPOC: 0.78,
        buyColor: [40, 180, 240],
        sellColor: [180, 80, 140],
        pocColor: '#e8b830',
        vaColor: 'rgba(80,140,220,0.42)',
        label: '2H',
        barWidthPct: 0.20,
    },
    '2day': {
        side: 'right',
        barAlpha: 0.25,
        barAlphaPOC: 0.45,
        buyColor: [55, 150, 200],
        sellColor: [165, 85, 135],
        pocColor: 'rgba(232,184,48,0.4)',
        vaColor: 'rgba(80,140,220,0.25)',
        label: '2D',
        barWidthPct: 0.16,
    },
    weekly: {
        side: 'right',
        barAlpha: 0.18,
        barAlphaPOC: 0.35,
        buyColor: [50, 130, 175],
        sellColor: [155, 80, 125],
        pocColor: 'rgba(232,184,48,0.3)',
        vaColor: 'rgba(80,140,220,0.15)',
        label: 'WK',
        barWidthPct: 0.14,
    },
    custom: {
        side: 'right',
        barAlpha: 0.55,
        barAlphaPOC: 0.80,
        buyColor: [40, 180, 240],
        sellColor: [180, 80, 140],
        pocColor: '#e8b830',
        vaColor: 'rgba(80,140,220,0.5)',
        label: 'CUST',
        barWidthPct: 0.22,
    },
};

// ── State ──
let _profiles = {};
let _activeProfiles = ['prior_day', 'session'];
let _customRange = { from_ts: 0, to_ts: 0 };
let _pollTimer = null;
// Per-instance: array of { chart, series, primitive, container }
let _vpInstances = [];
let _overlayVisible = true;
let _showPOCLine = true;
let _showVALines = true;
let _showVAShade = true;
let _showHVNLVN = true;
let _deltaMode = false;
let _extendPOC = true;
let _extendVAH = true;
let _extendVAL = true;
let _sessionType = 'all'; // 'all' | 'rth' | 'eth'
let _volumeFilter = 'total'; // 'total' | 'buy' | 'sell'
let _minLevelVol = 0; // trade-size filter (0=all, 10=large, 25=institutional)
let _stepSize = '1h'; // step profile ('', '15m', '30m', '1h', '2h')
let _stepExtendPOC = false;  // extend step POC lines to right edge
let _stepShowVA = true;      // show VA shade per step
let _stepBarOpacity = 0.45;  // step bar opacity
let _showAbsorption = true;   // show absorption strength badges (FORTRESS/SOLID/HELD)
let _showExhaustion = true;   // show exhaustion arrows (▼ weakening, ▲ strengthening)
let _showNakedPocs = true;    // show naked POC horizontal dashed lines from prior sessions
let _showDevLines  = true;    // show developing POC/VAH/VAL (session) as horizontal lines
let _intelPaneActive = false; // when VP Intel pane is mounted, chart overlay shows lines only
let _nakedPocs = []; // [{price, session_date, age_days}]
let _devPocPath = []; // [{time, poc}]
let _devPocCurrent = null;
let _showLiquidity = true; // show DOM depth overlay on VP
let _latestDOM = { bids: {}, asks: {} }; // live DOM snapshot for liquidity heatmap
let _prevDOM = { bids: {}, asks: {} };   // previous snapshot for depth delta
let _depthDeltas = {};                    // {priceStr: delta} — positive = loading, negative = pulling
let _bidTotal = 0;                        // sum of all bid depth
let _askTotal = 0;                        // sum of all ask depth
let _lastDOMDeltaTs = 0;

// Listen for DOM updates — deferred until AltarisEvents exists
let _domListenerWired = false;
function _wireDOMListener() {
    if (_domListenerWired) return;
    if (typeof AltarisEvents === 'undefined' && typeof window.AltarisEvents === 'undefined') return;
    _domListenerWired = true;
    const _ev = window.AltarisEvents || AltarisEvents;
    _ev.on('data:l2:update', (data) => {
        const dom = (data?.dom || {})['NQ'] || (data?.dom || {})[Object.keys(data?.dom || {})[0]];
        if (dom && dom.bids && dom.asks) {
            // Compute depth deltas every 2s (not every frame)
            const now = Date.now();
            if (now - _lastDOMDeltaTs > 2000) {
                _lastDOMDeltaTs = now;
                _depthDeltas = {};
                // Bid deltas
                for (const [ps, sz] of Object.entries(dom.bids)) {
                    const prev = _prevDOM.bids[ps] || 0;
                    const delta = sz - prev;
                    if (Math.abs(delta) >= 2) _depthDeltas[ps] = delta;
                }
                // Ask deltas
                for (const [ps, sz] of Object.entries(dom.asks)) {
                    const prev = _prevDOM.asks[ps] || 0;
                    const delta = sz - prev;
                    if (Math.abs(delta) >= 2) _depthDeltas[ps] = (_depthDeltas[ps] || 0) - delta; // negative = asks loading
                }
                _prevDOM = { bids: { ...dom.bids }, asks: { ...dom.asks } };
            }
            // Bid/ask totals
            _bidTotal = 0; _askTotal = 0;
            for (const sz of Object.values(dom.bids)) _bidTotal += sz;
            for (const sz of Object.values(dom.asks)) _askTotal += sz;
            _latestDOM = { bids: { ...dom.bids }, asks: { ...dom.asks } };
        }
    });
}
// Try immediately, retry after 1s if AltarisEvents not ready yet
_wireDOMListener();
setTimeout(_wireDOMListener, 1000);
setTimeout(_wireDOMListener, 3000);

function _rgba(rgb, a) { return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`; }

// ── Hex to RGB helper ──
function _hexToRgb(hex) {
    const r = parseInt(hex.slice(1,3), 16);
    const g = parseInt(hex.slice(3,5), 16);
    const b = parseInt(hex.slice(5,7), 16);
    return [r, g, b];
}
function _rgbToHex(rgb) {
    return '#' + rgb.map(c => Math.max(0, Math.min(255, c)).toString(16).padStart(2, '0')).join('');
}

// ── Fetch ──
async function _fetchProfile(mode, symbol = 'NQ') {
    let url = `/api/vprofile?symbol=${symbol}&mode=${mode}`;
    if (VP_CONFIG.ROW_COUNT > 0) url += `&row_count=${VP_CONFIG.ROW_COUNT}`;
    if (VP_CONFIG.VA_PCT !== 0.70) url += `&va_pct=${VP_CONFIG.VA_PCT}`;
    if (_sessionType !== 'all') url += `&session_type=${_sessionType}`;
    if (_volumeFilter !== 'total') url += `&vol_filter=${_volumeFilter}`;
    if (_minLevelVol > 0) url += `&min_level_vol=${_minLevelVol}`;
    if (_stepSize && mode === 'session') url += `&step=${_stepSize}`;
    if (mode === 'custom' && _customRange.from_ts > 0 && _customRange.to_ts > 0) {
        url += `&from_ts=${_customRange.from_ts}&to_ts=${_customRange.to_ts}`;
    }
    try {
        const resp = await window.authFetch(url);
        if (!resp.ok) return null;
        const data = await resp.json();
        _profiles[mode] = { data, lastFetch: Date.now() };
        return data;
    } catch (e) {
        console.warn('[VP] Fetch error:', e);
        return null;
    }
}

let _pollInFlight = false;
async function _pollProfiles() {
    if (_pollInFlight) return;
    _pollInFlight = true;
    try {
        const symbol = window._activeSymbol || 'NQ';
        const fetches = _activeProfiles.map(mode => _fetchProfile(mode, symbol));
        // PD POC comes from prior_day profile data (already fetched above)
        // Dev POC fetch (data available for programmatic use)
        fetches.push(
            window.authFetch(`/api/vprofile/dev-poc?symbol=${symbol}&interval=5`)
                .then(r => r.json()).then(d => {
                    _devPocPath = d.poc_path || [];
                    _devPocCurrent = d.current_poc;
                }).catch(() => {})
        );
        // Naked POCs fetch (prior-session POCs not yet revisited)
        fetches.push(
            window.authFetch(`/api/vprofile/naked-pocs?symbol=${symbol}`)
                .then(r => r.json()).then(d => {
                    _nakedPocs = Array.isArray(d) ? d : (d.naked_pocs || d.pocs || []);
                }).catch(() => {})
        );
        await Promise.all(fetches);
    } finally {
        _pollInFlight = false;
    }
}

// ── Bucket levels for clean rendering ──
function _bucketLevels(levels, priceConverter, minBarPx) {
    if (levels.length === 0) return [];
    const sorted = [...levels].sort((a, b) => a.price - b.price);
    const tickSize = sorted.length >= 2 ? sorted[1].price - sorted[0].price : 0.25;
    const y1 = priceConverter(sorted[0].price);
    const y2 = priceConverter(sorted[0].price + tickSize);
    const pxPerTick = (y1 != null && y2 != null) ? Math.abs(y2 - y1) : 1;

    if (pxPerTick >= minBarPx) {
        return sorted.map(lv => ({ ...lv, barH: pxPerTick }));
    }

    const ticksPerBucket = Math.ceil(minBarPx / pxPerTick);
    const bucketSize = tickSize * ticksPerBucket;
    const bucketPxH = pxPerTick * ticksPerBucket;
    const buckets = {};
    for (const lv of sorted) {
        const bk = Math.floor(lv.price / bucketSize) * bucketSize;
        if (!buckets[bk]) buckets[bk] = { price: bk + bucketSize / 2, buy: 0, sell: 0, total: 0 };
        buckets[bk].buy += lv.buy;
        buckets[bk].sell += lv.sell;
        buckets[bk].total += lv.buy + lv.sell;
        // Propagate KDE fields: max density, highest conviction
        if (lv.kde !== undefined) buckets[bk].kde = Math.max(buckets[bk].kde || 0, lv.kde);
        if (lv.hvn !== undefined && lv.hvn !== null) buckets[bk].hvn = Math.max(buckets[bk].hvn || 0, lv.hvn);
        if (lv.lvn !== undefined && lv.lvn !== null) buckets[bk].lvn = Math.max(buckets[bk].lvn || 0, lv.lvn);
        if (lv.abs_ratio !== undefined) buckets[bk].abs_ratio = Math.max(buckets[bk].abs_ratio || 0, lv.abs_ratio);
        if (lv.exh !== undefined) {
            if (buckets[bk].exh === undefined) buckets[bk].exh = lv.exh;
            else buckets[bk].exh = Math.min(buckets[bk].exh, lv.exh);
        }
        if (lv.refill_class) {
            const _ro = {instant: 0, fast: 1, slow: 2, gone: 3};
            if (!buckets[bk].refill_class || _ro[lv.refill_class] < _ro[buckets[bk].refill_class])
                buckets[bk].refill_class = lv.refill_class;
        }
    }
    return Object.values(buckets).sort((a, b) => a.price - b.price).map(b => ({ ...b, barH: bucketPxH }));
}

// ── Render (Market Maker Grade) ──
// Design: sigma-weighted bars with delta core, HVN/LVN conviction scoring
function _renderProfile(ctx, mediaSize, priceConverter, mode, data, timeScale) {
    if (!data || !data.levels || data.levels.length === 0) return;

    const style = PROFILE_STYLES[mode] || PROFILE_STYLES.rolling_4h;
    const buyColor = style.buyColor;
    const sellColor = style.sellColor;
    const pocColor = style.pocColor;
    const vaColor = style.vaColor;
    const _hasSteps = _stepSize && _profiles['session']?.data?.step_profiles?.length > 0;
    const _stepDim = (mode === 'session' && _hasSteps) ? 0.65 : 1.0;
    const barAlpha = style.barAlpha;
    const barAlphaPOC = style.barAlphaPOC;

    // ── Time-anchored positioning ──
    const _etOff = 4 * 3600;
    let timeAnchored = false;
    let anchorX1 = 0, anchorX2 = mediaSize.width, anchorW = mediaSize.width;
    try {
        if (timeScale && data.from_ts && data.to_ts) {
            const tx1 = timeScale.timeToCoordinate(data.from_ts - _etOff);
            const tx2 = timeScale.timeToCoordinate(data.to_ts - _etOff);
            if (tx1 != null && tx2 != null && !isNaN(tx1) && !isNaN(tx2) && Math.abs(tx2 - tx1) > 10) {
                anchorX1 = Math.min(tx1, tx2);
                anchorX2 = Math.max(tx1, tx2);
                anchorW = anchorX2 - anchorX1;
                timeAnchored = true;
            }
        }
    } catch(e) { /* timeScale not ready */ }

    const isLeft = timeAnchored ? true : (style.side === 'left');
    const _stepWidthDim = (!timeAnchored && mode === 'session' && _hasSteps) ? 0.6 : 1.0;
    const maxBarW = (timeAnchored ? anchorW * 0.90 : mediaSize.width * (style.barWidthPct || 0.24)) * _stepWidthDim;
    const baseX = timeAnchored ? anchorX1 : (isLeft ? VP_CONFIG.MARGIN : mediaSize.width - VP_CONFIG.MARGIN);

    const buckets = _bucketLevels(data.levels, priceConverter, VP_CONFIG.MIN_BAR_PX);
    if (buckets.length === 0) return;
    const maxVol = Math.max(...buckets.map(b => b.total));
    if (maxVol === 0) return;

    // KDE data comes from server — no client-side statistical computation needed.
    // bk.kde = normalized density [0,1], bk.hvn = prominence conviction [0,1], bk.lvn = prominence conviction [0,1]
    const hasKDE = buckets.some(b => b.kde !== undefined);

    const vahPrice = data.vah || 0;
    const valPrice = data.val || 0;

    // ── Pass 1: LVN air-pocket zones — subtle indicator, not full-width band ──
    // Reduced to bar area only so candles remain visible
    // Skipped when VP Intel pane owns per-price LVN rendering (avoids duplicate lines)
    if (_showHVNLVN && !_intelPaneActive) {
        for (const bk of buckets) {
            if (!bk.lvn) continue;
            const y = priceConverter(bk.price);
            if (y == null || isNaN(y)) continue;
            const conviction = Math.min(bk.lvn, 1);
            const barH = bk.barH;
            // Subtle line at LVN level within profile's time range
            ctx.strokeStyle = `rgba(255,160,40,${0.1 + conviction * 0.15})`;
            ctx.lineWidth = 0.5;
            ctx.setLineDash([2, 4]);
            ctx.beginPath();
            ctx.moveTo(timeAnchored ? anchorX1 : 0, y);
            ctx.lineTo(timeAnchored ? anchorX2 : mediaSize.width, y);
            ctx.stroke();
            ctx.setLineDash([]);
        }
    }

    // When VP Intel pane is active, skip all bars/badges/arrows/zones/depth
    // Only POC/VAH/VAL/VA shade lines render on chart overlay
    // Safety: auto-reset stale flag if VP Intel pane no longer mounted in any slot
    if (_intelPaneActive && !document.querySelector('.feat-sel-item[data-feat="vpintel"].current')) {
        _intelPaneActive = false;
    }
    const _skipBars = _intelPaneActive;

    // ── Pass 2: Bars with sigma-weighted coloring ──
    let lastBadgeY = -Infinity;
    let bIdx = 0;
    if (_skipBars) { /* skip entire bar+badge+arrow loop when VP Intel active */ }
    else for (const bk of buckets) {
        const y = priceConverter(bk.price);
        if (y == null || isNaN(y)) { bIdx++; continue; }
        if (y < -bk.barH || y > mediaSize.height + bk.barH) { bIdx++; continue; }

        const isPOC = data.poc >= bk.price - bk.barH && data.poc <= bk.price + bk.barH;
        const isHVN = !!bk.hvn;
        const isLVN = !!bk.lvn;
        const inVA = bk.price >= valPrice && bk.price <= vahPrice;

        const volRatio = bk.total / maxVol;

        // ── Alpha: KDE density-driven brightness ──
        // kde is [0,1] from server (0 = lowest density, 1 = highest)
        // Falls back to volRatio if KDE not available
        const density = hasKDE ? (bk.kde || 0) : volRatio;
        const kdeAlpha = 0.12 + density * 0.78;  // range [0.12, 0.90]
        const vaBoost = inVA ? 0.06 : 0;
        const alpha = Math.min(0.95, (isPOC ? Math.max(kdeAlpha, barAlphaPOC) : kdeAlpha * barAlpha / 0.55) + vaBoost) * _stepDim;

        const gap = 1;
        const barH = Math.max(bk.barH - gap, 1);
        const totalW = volRatio * maxBarW;
        const barY = Math.round(y - barH / 2) + 0.5;
        const barX = timeAnchored ? anchorX1 : (isLeft ? baseX : baseX - totalW);
        const r = Math.min(2, barH / 2);

        // ── Delta core rendering ──
        // Outer shell = total volume (muted), inner core = net delta (vivid)
        const delta = bk.buy - bk.sell;
        const deltaRatio = bk.total > 0 ? Math.abs(delta) / bk.total : 0;
        const deltaPositive = delta >= 0;

        if (_deltaMode) {
            // Pure delta mode — solid fill (no gradients)
            const deltaW = (Math.abs(delta) / maxVol) * maxBarW;
            if (deltaW > 0.5) {
                const dColor = deltaPositive ? [0, 200, 120] : [220, 50, 70];
                const dX = isLeft ? baseX : baseX - deltaW;
                ctx.fillStyle = _rgba(dColor, alpha * 0.7);
                ctx.fillRect(dX, barY, deltaW, barH);
            }
        } else {
            // ── Composite bar: shell + delta core (solid fills, zero gradients) ──
            const dominantColor = deltaPositive ? buyColor : sellColor;
            const blendT = deltaRatio;

            // Shell bar (full width, muted)
            if (totalW > 0.5) {
                const shellAlpha = alpha * (0.2 + blendT * 0.15);
                ctx.fillStyle = _rgba(dominantColor, shellAlpha);
                ctx.fillRect(barX, barY, totalW, barH);
            }

            // Delta core (inner bar showing net aggression)
            const coreW = totalW * deltaRatio;
            if (coreW > 1) {
                const coreAlpha = alpha * (0.5 + blendT * 0.4);
                const coreColor = deltaPositive ? [30, 210, 150] : [240, 60, 80];
                const coreX = isLeft ? baseX : baseX - coreW;
                ctx.fillStyle = _rgba(coreColor, coreAlpha);
                ctx.fillRect(coreX, barY, coreW, barH);
            }

            // Edge cap (bright tip at the price-axis side)
            if (totalW > 4) {
                const tipW = Math.min(3, totalW * 0.08);
                const tipX = isLeft ? baseX : baseX - tipW;
                ctx.fillStyle = _rgba(dominantColor, alpha * 0.8);
                ctx.fillRect(tipX, barY, tipW, barH);
            }
        }

        // ── POC: bright border (no shadowBlur — too expensive) ──
        if (isPOC) {
            ctx.strokeStyle = pocColor;
            ctx.lineWidth = 1.5;
            ctx.strokeRect(barX, barY, totalW, barH);
        }

        // ── HVN: badge only, no full-width glow bands (they hide candles) ──
        if (isHVN) {
            const conviction = Math.min(bk.hvn, 1);
            ctx.save();
            // Conviction badge with background pill
            const mX = isLeft ? barX + totalW + 6 : barX - 50;
            const badgeText = conviction >= 0.5 ? `HVN ${Math.round(conviction * 100)}%` : 'HVN';
            ctx.font = 'bold 8px "JetBrains Mono", monospace';
            const tm = ctx.measureText(badgeText);
            // Pill background
            ctx.fillStyle = 'rgba(0,0,0,0.7)';
            ctx.beginPath();
            ctx.rect(mX - 2, y - 7, tm.width + 6, 14);
            ctx.fill();
            // Pill border
            ctx.strokeStyle = `rgba(40,220,255,${0.3 + conviction * 0.5})`;
            ctx.lineWidth = 0.75;
            ctx.beginPath();
            ctx.rect(mX - 2, y - 7, tm.width + 6, 14);
            ctx.stroke();
            // Text
            ctx.fillStyle = conviction >= 0.7 ? 'rgba(40,240,255,0.95)' : 'rgba(40,200,240,0.7)';
            ctx.textAlign = 'left';
            ctx.fillText(badgeText, mX + 1, y + 3);
            ctx.restore();
        }

        // ── LVN: badge only, no full-width bands ──
        if (isLVN) {
            const conviction = Math.min(bk.lvn, 1);
            ctx.save();
            // Badge with pill
            const lX = isLeft ? barX + totalW + 6 : barX - 46;
            const lvnText = `AIR ${Math.round(conviction * 100)}%`;
            ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const tm = ctx.measureText(lvnText);
            ctx.fillStyle = 'rgba(0,0,0,0.7)';
            ctx.beginPath();
            ctx.rect(lX - 2, y - 6, tm.width + 6, 12);
            ctx.fill();
            ctx.strokeStyle = `rgba(255,180,60,${0.3 + conviction * 0.4})`;
            ctx.lineWidth = 0.75;
            ctx.beginPath();
            ctx.rect(lX - 2, y - 6, tm.width + 6, 12);
            ctx.stroke();
            ctx.fillStyle = `rgba(255,200,60,${0.6 + conviction * 0.35})`;
            ctx.textAlign = 'left';
            ctx.fillText(lvnText, lX + 1, y + 2);
            ctx.restore();
        }

        // ── Contract numbers on bars ──
        // Show volume + delta on significant levels (above median KDE density)
        // POC/HVN/LVN get enhanced treatment
        if (totalW > 8 && barH >= 3) {
            const showNumbers = hasKDE ? (density >= 0.4 || isPOC || isHVN || isLVN) : (volRatio >= 0.3);
            if (showNumbers) {
                const vol = Math.round(bk.total);
                const delta = Math.round((bk.buy || 0) - (bk.sell || 0));
                const deltaStr = delta >= 0 ? `+${delta}` : `${delta}`;

                // Position: inside the bar if wide enough, otherwise outside
                const isSpecial = isPOC || isHVN || isLVN;
                const fontSize = isSpecial ? 8 : 7;
                ctx.save();
                ctx.font = `${isSpecial ? 'bold ' : ''}${fontSize}px "JetBrains Mono", monospace`;

                // Volume text
                const volText = vol >= 1000 ? `${(vol/1000).toFixed(1)}k` : `${vol}`;
                const fullText = `${volText} ${deltaStr}`;
                const tm = ctx.measureText(fullText);

                // Position inside the bar near the price axis
                let textX, textAlign;
                if (isLeft) {
                    textX = baseX + 3;
                    textAlign = 'left';
                } else {
                    textX = baseX - 3;
                    textAlign = 'right';
                }

                ctx.textAlign = textAlign;
                ctx.textBaseline = 'middle';

                // Only render if text fits or is a key level
                if (tm.width < totalW - 4 || isSpecial) {
                    // Background for readability
                    const bgX = textAlign === 'right' ? textX - tm.width - 2 : textX - 1;
                    ctx.fillStyle = 'rgba(0,0,0,0.5)';
                    ctx.fillRect(bgX, y - fontSize / 2 - 1, tm.width + 3, fontSize + 2);

                    // Volume number
                    const volColor = isPOC ? 'rgba(232,184,48,0.95)' :
                                     isHVN ? 'rgba(40,220,255,0.9)' :
                                     isLVN ? 'rgba(255,200,60,0.85)' :
                                     `rgba(200,210,230,${0.4 + density * 0.5})`;
                    ctx.fillStyle = volColor;
                    const volTm = ctx.measureText(volText + ' ');
                    ctx.fillText(volText, textX, y);

                    // Delta number (colored)
                    const deltaX = textAlign === 'right' ? textX - volTm.width : textX + volTm.width;
                    ctx.fillStyle = delta > 0 ? 'rgba(30,210,150,0.85)' :
                                    delta < 0 ? 'rgba(240,60,80,0.85)' :
                                    'rgba(160,170,190,0.5)';
                    ctx.fillText(deltaStr, deltaX, y);
                }
                ctx.restore();
            }
        }

        // ── Absorption badge + Exhaustion arrow per bar ──
        const absR = bk.abs_ratio || 0;
        const exh = bk.exh;
        if ((_showAbsorption || _showExhaustion) && absR > 0 && totalW > 6) {
            // Classify absorption strength
            let absLabel = '', absColor = '';
            if (absR >= 50) { absLabel = 'FORTRESS'; absColor = 'rgba(40,255,180,0.9)'; }
            else if (absR >= 30) { absLabel = 'SOLID'; absColor = 'rgba(40,220,160,0.7)'; }
            else if (absR >= 15) { absLabel = 'HELD'; absColor = 'rgba(140,200,220,0.5)'; }
            // Only show badge for significant levels
            let absBadgeW = 0;
            if (absLabel && _showAbsorption && Math.abs(y - lastBadgeY) >= 14) {
                ctx.save();
                ctx.font = 'bold 7px "JetBrains Mono", monospace';
                const absTm = ctx.measureText(absLabel);
                absBadgeW = absTm.width + 6;
                const absBx = barX + totalW + 4;
                // Pill background with colored border (matches LVN style)
                ctx.fillStyle = 'rgba(0,0,0,0.75)';
                ctx.beginPath();
                ctx.fillRect(absBx - 2, y - 6, absBadgeW, 12);
                ctx.strokeStyle = absColor;
                ctx.lineWidth = 0.75;
                ctx.strokeRect(absBx - 2, y - 6, absBadgeW, 12);
                ctx.fillStyle = absColor;
                ctx.textAlign = 'left';
                ctx.textBaseline = 'middle';
                ctx.fillText(absLabel, absBx + 1, y);
                ctx.restore();
                lastBadgeY = y;
            }
            // Exhaustion arrow (declining volume = level weakening)
            if (_showExhaustion && exh !== undefined && Math.abs(exh) > 0.25) {
                const arrowX = barX + totalW + (absBadgeW > 0 ? absBadgeW + 8 : 4);
                const isWeak = exh < 0;
                const intensity = Math.min(0.9, 0.4 + Math.abs(exh) * 0.5);
                ctx.save();
                // Background pill
                ctx.fillStyle = isWeak ? 'rgba(60,0,0,0.5)' : 'rgba(0,40,20,0.5)';
                ctx.beginPath();
                ctx.fillRect(arrowX - 2, y - 6, 12, 12);
                // Arrow — larger and bolder
                ctx.fillStyle = isWeak
                    ? `rgba(255,80,80,${intensity})`
                    : `rgba(40,255,140,${intensity})`;
                ctx.beginPath();
                if (isWeak) {
                    // Down arrow ▼
                    ctx.moveTo(arrowX, y - 3);
                    ctx.lineTo(arrowX + 8, y - 3);
                    ctx.lineTo(arrowX + 4, y + 4);
                } else {
                    // Up arrow ▲
                    ctx.moveTo(arrowX, y + 3);
                    ctx.lineTo(arrowX + 8, y + 3);
                    ctx.lineTo(arrowX + 4, y - 4);
                }
                ctx.closePath();
                ctx.fill();
                ctx.restore();
            }
        }

        bIdx++;
    }

    // ── Pass 2.5: Absorption Zone Bands ──
    // Percentile-based: no hardcoded thresholds
    if (_showAbsorption && !_skipBars) {
        const _bkAbs = buckets.filter(b => (b.abs_ratio || 0) > 0).map(b => b.abs_ratio).sort((a, b) => a - b);
        const _bkAbsP70 = _bkAbs.length > 0 ? _bkAbs[Math.floor(_bkAbs.length * 0.70)] : 999;
        const zones = [];
        let curZone = null;
        const tick = buckets.length >= 2 ? Math.abs(buckets[1].price - buckets[0].price) : 0.25;
        for (const bk of buckets) {
            if ((bk.abs_ratio || 0) >= _bkAbsP70) {
                if (curZone && bk.price - curZone.hi <= tick * 3) {
                    curZone.hi = bk.price;
                    curZone.maxAbs = Math.max(curZone.maxAbs, bk.abs_ratio);
                    curZone.minExh = Math.min(curZone.minExh, bk.exh || 0);
                    curZone.bestRefill = bk.refill_class && (!curZone.bestRefill || {instant:0,fast:1,slow:2,gone:3}[bk.refill_class] < {instant:0,fast:1,slow:2,gone:3}[curZone.bestRefill]) ? bk.refill_class : curZone.bestRefill;
                    curZone.levels++;
                } else {
                    if (curZone && curZone.levels >= 2) zones.push(curZone);
                    curZone = { lo: bk.price, hi: bk.price, maxAbs: bk.abs_ratio, minExh: bk.exh || 0, bestRefill: bk.refill_class || null, levels: 1 };
                }
            } else {
                if (curZone && curZone.levels >= 2) zones.push(curZone);
                curZone = null;
            }
        }
        if (curZone && curZone.levels >= 2) zones.push(curZone);

        // Render zone bands
        for (const z of zones) {
            const yLo = priceConverter(z.lo);
            const yHi = priceConverter(z.hi);
            if (yLo == null || yHi == null || isNaN(yLo) || isNaN(yHi)) continue;
            const top = Math.min(yLo, yHi);
            const h = Math.max(Math.abs(yLo - yHi), 4);
            // Rank by absorption within zones: top = FORTRESS, mid = SOLID, rest = HELD
            const _zSorted = [...zones].sort((a, b) => b.maxAbs - a.maxAbs);
            const _zIdx = _zSorted.indexOf(z);
            const zLabel = _zIdx < Math.ceil(_zSorted.length * 0.33) ? 'FORTRESS' : _zIdx < Math.ceil(_zSorted.length * 0.66) ? 'SOLID' : 'HELD';
            const zColor = z.maxAbs >= 50 ? [40,255,180] : z.maxAbs >= 30 ? [40,220,160] : [140,200,220];

            // Zone band
            ctx.fillStyle = _rgba(zColor, 0.06);
            const x1 = timeAnchored ? anchorX1 : 0;
            const x2 = timeAnchored ? anchorX2 : mediaSize.width;
            ctx.fillRect(x1, top, x2 - x1, h);

            // Zone border
            ctx.strokeStyle = _rgba(zColor, 0.25);
            ctx.lineWidth = 0.5;
            ctx.strokeRect(x1, top, x2 - x1, h);

            // Zone label
            ctx.save();
            ctx.font = 'bold 8px "JetBrains Mono", monospace';
            const rangeLabel = `${zLabel} ${z.lo.toFixed(0)}-${z.hi.toFixed(0)}`;
            const labelX = x2 - ctx.measureText(rangeLabel).width - 8;
            const labelY = top + h / 2;
            ctx.fillStyle = 'rgba(0,0,0,0.7)';
            ctx.fillRect(labelX - 3, labelY - 7, ctx.measureText(rangeLabel).width + 8, 14);
            ctx.fillStyle = _rgba(zColor, 0.9);
            ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(rangeLabel, labelX, labelY);

            // Refill dot
            if (z.bestRefill) {
                const dotColor = z.bestRefill === 'instant' ? 'rgba(40,255,140,0.9)' : z.bestRefill === 'fast' ? 'rgba(255,220,40,0.8)' : 'rgba(255,80,80,0.7)';
                ctx.fillStyle = dotColor;
                ctx.beginPath(); ctx.arc(labelX - 8, labelY, 3, 0, Math.PI * 2); ctx.fill();
            }

            // Exhaustion arrow on zone
            if (_showExhaustion && z.minExh < -0.25) {
                ctx.fillStyle = `rgba(255,80,80,${Math.min(0.9, 0.4 + Math.abs(z.minExh) * 0.5)})`;
                ctx.beginPath(); ctx.moveTo(labelX - 16, labelY - 3); ctx.lineTo(labelX - 10, labelY - 3); ctx.lineTo(labelX - 13, labelY + 3); ctx.closePath(); ctx.fill();
            }
            ctx.restore();
        }
    }

    // ── Pass 3: Liquidity Heatmap — DOM depth overlay ──
    // Skipped when VP Intel pane owns per-price-level rendering
    try {
    if (_showLiquidity && !_intelPaneActive && _latestDOM.bids && Object.keys(_latestDOM.bids).length > 0) {
        // Find max depth for scaling
        let maxDepth = 0;
        for (const sz of Object.values(_latestDOM.bids)) maxDepth = Math.max(maxDepth, sz);
        for (const sz of Object.values(_latestDOM.asks)) maxDepth = Math.max(maxDepth, sz);
        if (maxDepth > 0) {
            const depthMaxW = maxBarW * 0.4; // depth bars are 40% of max VP bar width
            const depthOffset = timeAnchored ? anchorX2 - depthMaxW - 2 : (isLeft ? baseX + maxBarW + 4 : baseX - maxBarW - depthMaxW - 4);
            // Bid depth (cyan)
            for (const [priceStr, sz] of Object.entries(_latestDOM.bids)) {
                const price = parseFloat(priceStr);
                const y = priceConverter(price);
                if (y == null || isNaN(y) || y < 0 || y > mediaSize.height) continue;
                const w = (sz / maxDepth) * depthMaxW;
                if (w < 0.5) continue;
                ctx.fillStyle = `rgba(0,180,220,${0.15 + (sz / maxDepth) * 0.25})`;
                ctx.fillRect(depthOffset, y - 1, w, 2);
            }
            // Ask depth (magenta)
            for (const [priceStr, sz] of Object.entries(_latestDOM.asks)) {
                const price = parseFloat(priceStr);
                const y = priceConverter(price);
                if (y == null || isNaN(y) || y < 0 || y > mediaSize.height) continue;
                const w = (sz / maxDepth) * depthMaxW;
                if (w < 0.5) continue;
                ctx.fillStyle = `rgba(220,80,140,${0.15 + (sz / maxDepth) * 0.25})`;
                ctx.fillRect(depthOffset, y - 1, w, 2);
            }
        }
    }

    } catch(e) { /* liquidity render error */ }

    // ── Pass 4: Depth Delta Arrows (▲+12, ▼-6) ──
    // Skipped when VP Intel pane owns per-price-level rendering
    if (_showLiquidity && !_intelPaneActive && Object.keys(_depthDeltas).length > 0) {
        ctx.save();
        ctx.font = 'bold 7px "JetBrains Mono", monospace';
        ctx.textBaseline = 'middle';
        const _arrowX = timeAnchored ? anchorX2 - 40 : mediaSize.width - 45;
        for (const [ps, delta] of Object.entries(_depthDeltas)) {
            const price = parseFloat(ps);
            const y = priceConverter(price);
            if (y == null || isNaN(y) || y < 0 || y > mediaSize.height) continue;
            if (Math.abs(delta) < 3) continue;
            const isLoad = delta > 0;
            const int_ = Math.min(0.9, 0.3 + Math.abs(delta) * 0.03);
            ctx.fillStyle = isLoad ? `rgba(40,255,140,${int_})` : `rgba(255,80,80,${int_})`;
            ctx.textAlign = 'left';
            ctx.fillText(isLoad ? `▲+${Math.abs(delta)}` : `▼-${Math.abs(delta)}`, _arrowX, y);
        }
        ctx.restore();
    }

    // ── Pass 5: WALL Badge (resting depth significantly above average) ──
    // Skipped when VP Intel pane owns per-price-level rendering
    if (_showLiquidity && !_intelPaneActive && _latestDOM.bids && Object.keys(_latestDOM.bids).length > 0) {
        // Dynamic threshold: 3x average depth, minimum 8 contracts
        let _totalDepth = 0, _depthN = 0;
        for (const sz of Object.values(_latestDOM.bids)) { _totalDepth += sz; _depthN++; }
        for (const sz of Object.values(_latestDOM.asks)) { _totalDepth += sz; _depthN++; }
        const _avgDepth = _depthN > 0 ? _totalDepth / _depthN : 5;
        const _wallThr = Math.max(8, _avgDepth * 3);

        ctx.save();
        ctx.font = 'bold 7px "JetBrains Mono", monospace';
        ctx.textBaseline = 'middle';
        for (const bk of buckets) {
            const ps = bk.price.toFixed(2);
            const depthHere = (_latestDOM.bids[ps] || 0) + (_latestDOM.asks[ps] || 0);
            if (depthHere >= _wallThr) {
                const y = priceConverter(bk.price);
                if (y == null || isNaN(y) || y < 0 || y > mediaSize.height) continue;
                const wx = timeAnchored ? anchorX1 + 4 : VP_CONFIG.MARGIN + 4;
                ctx.fillStyle = 'rgba(0,0,0,0.7)';
                ctx.fillRect(wx - 2, y - 6, 30, 12);
                ctx.fillStyle = 'rgba(255,200,40,0.9)';
                ctx.textAlign = 'left';
                ctx.fillText('WALL', wx, y);
            }
        }
        ctx.restore();
    }

    // ── Pass 6: Bid/Ask Total Bar (bottom) ──
    // Skipped when VP Intel pane owns per-price-level rendering
    if (_showLiquidity && !_intelPaneActive && (_bidTotal > 0 || _askTotal > 0)) {
        ctx.save();
        const btY = mediaSize.height - 18;
        const btMaxW = timeAnchored ? anchorW * 0.8 : mediaSize.width * 0.2;
        const btLeft = timeAnchored ? anchorX1 : mediaSize.width - btMaxW - VP_CONFIG.MARGIN;
        const btMax = Math.max(_bidTotal, _askTotal, 1);
        ctx.fillStyle = 'rgba(0,180,220,0.35)';
        ctx.fillRect(btLeft, btY, (_bidTotal / btMax) * btMaxW, 6);
        ctx.fillStyle = 'rgba(220,80,140,0.35)';
        ctx.fillRect(btLeft, btY + 8, (_askTotal / btMax) * btMaxW, 6);
        ctx.font = '8px "JetBrains Mono", monospace';
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'left';
        ctx.fillStyle = 'rgba(0,200,240,0.8)';
        ctx.fillText(`BID ${_bidTotal.toLocaleString()}`, btLeft + (_bidTotal / btMax) * btMaxW + 4, btY + 3);
        ctx.fillStyle = 'rgba(240,100,160,0.8)';
        ctx.fillText(`ASK ${_askTotal.toLocaleString()}`, btLeft + (_askTotal / btMax) * btMaxW + 4, btY + 11);
        const ratio = _bidTotal / Math.max(_askTotal, 1);
        ctx.fillStyle = ratio > 1.2 ? 'rgba(40,255,140,0.7)' : ratio < 0.8 ? 'rgba(255,80,80,0.7)' : 'rgba(160,170,200,0.5)';
        ctx.textAlign = 'right';
        ctx.fillText(`${ratio.toFixed(2)}x`, btLeft + btMaxW, btY + 7);
        ctx.restore();
    }

    // ── Session boundary marker ──
    if (timeAnchored) {
        ctx.save();
        ctx.strokeStyle = 'rgba(100,120,160,0.25)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(anchorX1, 0); ctx.lineTo(anchorX1, mediaSize.height); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(anchorX2, 0); ctx.lineTo(anchorX2, mediaSize.height); ctx.stroke();
        ctx.setLineDash([]);
        // Profile label at top
        ctx.font = 'bold 9px "JetBrains Mono", monospace';
        ctx.fillStyle = 'rgba(140,160,200,0.6)';
        ctx.textAlign = 'left';
        ctx.fillText(style.label || mode.toUpperCase(), anchorX1 + 4, 14);
        ctx.restore();
    }

    // ── Lines ──
    // Time-anchored: lines span the profile's time range (or extended to right edge)
    const lineStartBase = timeAnchored ? anchorX1 : baseX;
    const lineEndBase = timeAnchored ? anchorX2 : (isLeft ? baseX + maxBarW : baseX);

    // Dedup check: if DEV (session) level ≈ PD level within 2 ticks (0.5 pts on NQ),
    // mutate the DEV label to "DEV/PD X" and the PD pass below will skip that level.
    // Session mode only — DEV labels come from session data.
    const _pdDataForDedup = (mode === 'session') ? (_profiles['prior_day']?.data || null) : null;
    const _dupTol = 0.5;
    const _dupPOC = _pdDataForDedup && _pdDataForDedup.poc != null && data.poc != null &&
                    Math.abs(_pdDataForDedup.poc - data.poc) < _dupTol;
    const _dupVAH = _pdDataForDedup && _pdDataForDedup.vah != null && data.vah != null &&
                    Math.abs(_pdDataForDedup.vah - data.vah) < _dupTol;
    const _dupVAL = _pdDataForDedup && _pdDataForDedup.val != null && data.val != null &&
                    Math.abs(_pdDataForDedup.val - data.val) < _dupTol;

    // When VP Intel pane owns POC/VAH/VAL rendering, fully skip chart lines
    // (user requested: remove duplicates from chart when VP Intel has them).
    const _chartDim = 1.0;

    // POC line with KDE density-based thickness
    // Session mode POC = developing POC — respect _showDevLines toggle
    if (_showPOCLine && !_intelPaneActive && (mode !== 'session' || _showDevLines)) {
        const pocY = priceConverter(data.poc);
        if (pocY != null && !isNaN(pocY)) {
            const pocBucket = buckets.find(b => data.poc >= b.price - b.barH && data.poc <= b.price + b.barH);
            const pocDensity = pocBucket && pocBucket.kde !== undefined ? pocBucket.kde : 1;
            const pocConviction = Math.min(pocDensity, 1);

            const pocStart = _extendPOC ? 0 : lineStartBase;
            const pocEnd = _extendPOC ? mediaSize.width : lineEndBase;
            ctx.save();
            ctx.globalAlpha = _chartDim;
            ctx.shadowColor = pocColor;
            ctx.strokeStyle = pocColor;
            ctx.lineWidth = VP_CONFIG.POC_LINE_WIDTH + pocConviction * 1;
            ctx.setLineDash(VP_CONFIG.POC_LINE_DASH);
            ctx.beginPath(); ctx.moveTo(pocStart, pocY); ctx.lineTo(pocEnd, pocY); ctx.stroke();
            ctx.setLineDash([]);
            ctx.font = VP_CONFIG.LABEL_FONT;
            const lblPrefix = _dupPOC ? `${style.label}/PD` : style.label;
            const label = `${lblPrefix} POC ${data.poc.toFixed(2)}`;
            const tm = ctx.measureText(label);
            const pW = tm.width + 8, pH = 14;
            const pX = isLeft ? VP_CONFIG.MARGIN : mediaSize.width - VP_CONFIG.MARGIN - pW;
            ctx.fillStyle = 'rgba(0,0,0,0.85)';
            _roundRect(ctx, pX, pocY - pH - 2, pW, pH, 3); ctx.fill();
            ctx.strokeStyle = pocColor; ctx.lineWidth = 0.5;
            _roundRect(ctx, pX, pocY - pH - 2, pW, pH, 3); ctx.stroke();
            ctx.fillStyle = pocColor; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(label, pX + 4, pocY - pH / 2 - 2 + pH / 2);
            ctx.restore();
        }
    }

    // VAH / VAL — skipped when VP Intel pane owns rendering
    if (_showVALines && !_intelPaneActive && (mode !== 'session' || _showDevLines)) {
        for (const [level, lbl, dup] of [[data.vah, 'VAH', _dupVAH], [data.val, 'VAL', _dupVAL]]) {
            if (level == null) continue;
            const ly = priceConverter(level);
            if (ly == null || isNaN(ly)) continue;
            const extendThis = lbl === 'VAH' ? _extendVAH : _extendVAL;
            const vaStart = extendThis ? 0 : lineStartBase;
            const vaEnd = extendThis ? mediaSize.width : lineEndBase;
            ctx.save();
            ctx.globalAlpha = _chartDim;
            ctx.strokeStyle = vaColor; ctx.lineWidth = VP_CONFIG.VAH_VAL_WIDTH;
            ctx.setLineDash(VP_CONFIG.VAH_VAL_DASH);
            ctx.beginPath(); ctx.moveTo(vaStart, ly); ctx.lineTo(vaEnd, ly); ctx.stroke();
            ctx.setLineDash([]);
            ctx.font = VP_CONFIG.LABEL_FONT;
            const lblPrefix = dup ? `${style.label}/PD` : style.label;
            const txt = `${lblPrefix} ${lbl} ${level.toFixed(2)}`;
            const tm2 = ctx.measureText(txt);
            const w2 = tm2.width + 8, h2 = 13;
            const x2 = isLeft ? VP_CONFIG.MARGIN : mediaSize.width - VP_CONFIG.MARGIN - w2;
            const y2 = lbl === 'VAH' ? ly - h2 - 2 : ly + 3;
            ctx.fillStyle = 'rgba(0,0,0,0.80)';
            _roundRect(ctx, x2, y2, w2, h2, 3); ctx.fill();
            ctx.fillStyle = vaColor; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(txt, x2 + 4, y2 + h2 / 2);
            ctx.restore();
        }
    }

    // VA shade — skipped when VP Intel pane owns per-price VA rendering
    if (_showVAShade && !_intelPaneActive) {
        const vahY = priceConverter(data.vah);
        const valY = priceConverter(data.val);
        if (vahY != null && valY != null && !isNaN(vahY) && !isNaN(valY)) {
            const shadeStart = (_extendVAH || _extendVAL) ? 0 : lineStartBase;
            const shadeEnd = (_extendVAH || _extendVAL) ? mediaSize.width : lineEndBase;
            ctx.save();
            // Gradient shade — stronger at edges, fading toward center
            const vaH = Math.abs(valY - vahY);
            const vaTop = Math.min(vahY, valY);
            const grad = ctx.createLinearGradient(0, vaTop, 0, vaTop + vaH);
            grad.addColorStop(0, 'rgba(80,140,220,0.04)');
            grad.addColorStop(0.5, 'rgba(80,140,220,0.015)');
            grad.addColorStop(1, 'rgba(80,140,220,0.04)');
            ctx.fillStyle = grad;
            ctx.fillRect(shadeStart, vaTop, shadeEnd - shadeStart, vaH);
            ctx.restore();
        }
    }

    // Step profiles are rendered on the main canvas (need timeScale for x coords)

}

function _roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y, x + w, y + r, r); ctx.lineTo(x + w, y + h - r);
    ctx.arcTo(x + w, y + h, x + w - r, y + h, r); ctx.lineTo(x + r, y + h);
    ctx.arcTo(x, y + h, x, y + h - r, r); ctx.lineTo(x, y + r);
    ctx.arcTo(x, y, x + r, y, r); ctx.closePath();
}

// ── TradingView SVP HD-style Settings Panel ──
function _buildSettingsPanel() {
    const panel = document.getElementById('vp-settings-panel');
    if (!panel) return;
    panel.innerHTML = `
        <div class="vp-panel-header">
            <span class="vp-panel-title">SVP HD</span>
            <button class="vp-panel-close" id="vp-settings-close">\u2715</button>
        </div>
        <div class="vp-tabs">
            <button class="vp-tab active" data-tab="inputs">Inputs</button>
            <button class="vp-tab" data-tab="style">Style</button>
            <button class="vp-tab" data-tab="visibility">Visibility</button>
        </div>

        <!-- TAB: INPUTS -->
        <div class="vp-tab-content active" data-tab="inputs">
            <div class="vp-field">
                <span class="vp-field-label">Sessions</span>
                <select id="vps-session-type" class="vp-select">
                    <option value="all" selected>All</option>
                    <option value="rth">Regular (9:30-16:00)</option>
                    <option value="eth">Extended</option>
                </select>
            </div>
            <div class="vp-field">
                <span class="vp-field-label">Custom session</span>
                <div class="vp-time-range">
                    <input type="time" id="vps-custom-from" value="09:30" class="vp-time-input">
                    <span class="vp-time-sep">\u2014</span>
                    <input type="time" id="vps-custom-to" value="16:00" class="vp-time-input">
                </div>
            </div>
            <div class="vp-field">
                <span class="vp-field-label">Volume</span>
                <select id="vps-vol-filter" class="vp-select">
                    <option value="total" selected>Total</option>
                    <option value="buy">Buy</option>
                    <option value="sell">Sell</option>
                </select>
            </div>
            <div class="vp-field">
                <span class="vp-field-label">Value Area Volume</span>
                <div class="vp-field-value">
                    <input type="number" min="50" max="90" value="70" id="vps-va-pct" class="vp-num-input">
                </div>
            </div>
            <div class="vp-field">
                <span class="vp-field-label">Row Count</span>
                <select id="vps-row-count" class="vp-select">
                    <option value="0" selected>Auto</option>
                    <option value="50">50</option>
                    <option value="100">100</option>
                    <option value="200">200</option>
                    <option value="500">500</option>
                </select>
            </div>
            <div class="vp-separator"></div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-extend-poc" checked> Extend POC Right</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-extend-vah" checked> Extend VAH Right</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-extend-val" checked> Extend VAL Right</label>
            </div>
            <div class="vp-field">
                <label>Trade Size Filter</label>
                <select id="vps-trade-size">
                    <option value="0">All Trades</option>
                    <option value="10">Large (10+ lots)</option>
                    <option value="25">Institutional (25+)</option>
                    <option value="50">Block (50+)</option>
                </select>
            </div>
            <div class="vp-field">
                <label>Step Profile</label>
                <select id="vps-step">
                    <option value="">None</option>
                    <option value="15m">15 min</option>
                    <option value="30m">30 min</option>
                    <option value="1h">1 hour</option>
                    <option value="2h">2 hours</option>
                </select>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-step-extend-poc"> Extend Step POC</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-step-va" checked> Step VA Shade</label>
            </div>
            <div class="vp-field">
                <label>Step Opacity</label>
                <input type="range" id="vps-step-opacity" min="10" max="80" value="45" style="width:100px">
                <span id="vps-step-opacity-val">45%</span>
            </div>
        </div>

        <!-- TAB: STYLE -->
        <div class="vp-tab-content" data-tab="style">
            <div class="vp-field">
                <span class="vp-field-label">Profiles</span>
            </div>
            <div class="vp-profile-grid">
                <label class="vp-checkbox"><input type="checkbox" id="vps-prior-day" checked> Prior Day</label>
                <label class="vp-checkbox"><input type="checkbox" id="vps-session" checked> Session</label>
                <label class="vp-checkbox"><input type="checkbox" id="vps-rolling-1h"> 1 Hour</label>
                <label class="vp-checkbox"><input type="checkbox" id="vps-rolling-2h"> 2 Hour</label>
                <label class="vp-checkbox"><input type="checkbox" id="vps-rolling-4h"> 4 Hour</label>
                <label class="vp-checkbox"><input type="checkbox" id="vps-2day"> 2 Day</label>
                <label class="vp-checkbox"><input type="checkbox" id="vps-weekly"> 1 Week</label>
                <label class="vp-checkbox"><input type="checkbox" id="vps-custom-mode"> Custom</label>
            </div>
            <div class="vp-separator"></div>
            <div class="vp-field">
                <span class="vp-field-label">Bar Width</span>
                <div class="vp-slider-row">
                    <input type="range" min="10" max="50" value="25" id="vps-bar-width" class="vp-slider">
                    <span id="vps-bar-width-val" class="vp-slider-val">25%</span>
                </div>
            </div>
            <div class="vp-field">
                <span class="vp-field-label">Opacity</span>
                <div class="vp-slider-row">
                    <input type="range" min="20" max="90" value="55" id="vps-opacity" class="vp-slider">
                    <span id="vps-opacity-val" class="vp-slider-val">55%</span>
                </div>
            </div>
            <div class="vp-field">
                <span class="vp-field-label">Side</span>
                <div class="vp-btn-group">
                    <button class="vp-side-btn active" id="vps-side-left">Left</button>
                    <button class="vp-side-btn" id="vps-side-right">Right</button>
                </div>
            </div>
            <div class="vp-separator"></div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-delta-mode"> Delta Mode</label>
            </div>
            <div class="vp-separator"></div>
            <div class="vp-field">
                <span class="vp-field-label">Colors</span>
            </div>
            <div class="vp-color-row">
                <span class="vp-color-label">Buy</span>
                <input type="color" id="vps-buy-color" value="#00e6e6" class="vp-color-pick">
                <span class="vp-color-label">Sell</span>
                <input type="color" id="vps-sell-color" value="#ff6496" class="vp-color-pick">
                <span class="vp-color-label">POC</span>
                <input type="color" id="vps-poc-color" value="#ffd700" class="vp-color-pick">
            </div>
        </div>

        <!-- TAB: VISIBILITY -->
        <div class="vp-tab-content" data-tab="visibility">
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-poc-line" checked> POC Line</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-va-lines" checked> VAH / VAL Lines</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-va-shade" checked> Value Area Shade</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-hvn-lvn" checked> HVN / LVN Markers</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-liquidity" checked> Liquidity Heatmap (DOM Depth)</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-absorption" checked> Absorption Strength</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-exhaustion" checked> Exhaustion Arrows</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-naked-pocs" checked> Naked POCs (dashed purple)</label>
            </div>
            <div class="vp-field">
                <label class="vp-checkbox"><input type="checkbox" id="vps-dev-lines" checked> Developing POC / VAH / VAL</label>
            </div>
            <div class="vp-field">
            </div>
        </div>

        <div class="vp-panel-footer">TopStepX NQ 1m \u00b7 Poll 10s</div>
    `;

    // ── Wire tab switching ──
    panel.querySelectorAll('.vp-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            panel.querySelectorAll('.vp-tab').forEach(t => t.classList.remove('active'));
            panel.querySelectorAll('.vp-tab-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            panel.querySelector(`.vp-tab-content[data-tab="${tab.dataset.tab}"]`).classList.add('active');
        });
    });

    // Wire close
    document.getElementById('vp-settings-close').addEventListener('click', () => { panel.style.display = 'none'; });

    // ── INPUTS TAB ──
    // Session type
    document.getElementById('vps-session-type').addEventListener('change', (e) => {
        _sessionType = e.target.value;
        _pollProfiles();
    });
    // Custom time range
    const customFrom = document.getElementById('vps-custom-from');
    const customTo = document.getElementById('vps-custom-to');
    function _applyCustomTime() {
        const now = new Date();
        const [fh, fm] = customFrom.value.split(':').map(Number);
        const [th, tm] = customTo.value.split(':').map(Number);
        const fromDate = new Date(now); fromDate.setHours(fh, fm, 0, 0);
        const toDate = new Date(now); toDate.setHours(th, tm, 0, 0);
        _customRange.from_ts = Math.floor(fromDate.getTime() / 1000);
        _customRange.to_ts = Math.floor(toDate.getTime() / 1000);
    }
    customFrom.addEventListener('change', _applyCustomTime);
    customTo.addEventListener('change', _applyCustomTime);

    // Volume filter
    document.getElementById('vps-vol-filter').addEventListener('change', (e) => {
        _volumeFilter = e.target.value;
        _pollProfiles();
    });

    // Value area
    const vaPctInput = document.getElementById('vps-va-pct');
    vaPctInput.addEventListener('change', () => {
        VP_CONFIG.VA_PCT = Math.max(50, Math.min(90, parseInt(vaPctInput.value) || 70)) / 100;
        vaPctInput.value = Math.round(VP_CONFIG.VA_PCT * 100);
        _pollProfiles();
    });

    // Row count
    document.getElementById('vps-row-count').addEventListener('change', (e) => {
        VP_CONFIG.ROW_COUNT = parseInt(e.target.value);
        _pollProfiles();
    });

    // Extend per-line
    document.getElementById('vps-extend-poc').addEventListener('change', (e) => { _extendPOC = e.target.checked; });
    document.getElementById('vps-extend-vah').addEventListener('change', (e) => { _extendVAH = e.target.checked; });
    document.getElementById('vps-extend-val').addEventListener('change', (e) => { _extendVAL = e.target.checked; });

    // Trade size filter
    const _elTradeSize = document.getElementById('vps-trade-size');
    if (_elTradeSize) _elTradeSize.addEventListener('change', (e) => {
        _minLevelVol = parseInt(e.target.value);
        _pollProfiles();
    });

    // Step profile
    const _elStep = document.getElementById('vps-step');
    if (_elStep) _elStep.addEventListener('change', (e) => {
        _stepSize = e.target.value;
        _pollProfiles();
    });

    // Step extend POC
    const _elStepExtPoc = document.getElementById('vps-step-extend-poc');
    if (_elStepExtPoc) _elStepExtPoc.addEventListener('change', (e) => { _stepExtendPOC = e.target.checked; });

    // Step VA shade
    const _elStepVA = document.getElementById('vps-step-va');
    if (_elStepVA) _elStepVA.addEventListener('change', (e) => { _stepShowVA = e.target.checked; });

    // Step opacity slider
    const _elStepOp = document.getElementById('vps-step-opacity');
    const _elStepOpVal = document.getElementById('vps-step-opacity-val');
    if (_elStepOp) _elStepOp.addEventListener('input', () => {
        _stepBarOpacity = parseInt(_elStepOp.value) / 100;
        if (_elStepOpVal) _elStepOpVal.textContent = _elStepOp.value + '%';
    });

    // ── STYLE TAB ──
    // Profiles
    const profileMap = {
        'vps-prior-day': 'prior_day', 'vps-session': 'session',
        'vps-rolling-1h': 'rolling_1h', 'vps-rolling-2h': 'rolling_2h',
        'vps-rolling-4h': 'rolling_4h', 'vps-2day': '2day',
        'vps-weekly': 'weekly', 'vps-custom-mode': 'custom',
    };
    for (const [id, mode] of Object.entries(profileMap)) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.addEventListener('change', (e) => {
            if (e.target.checked) {
                if (!_activeProfiles.includes(mode)) _activeProfiles.push(mode);
                _fetchProfile(mode);
            } else {
                _activeProfiles = _activeProfiles.filter(m => m !== mode);
            }
        });
    }

    // Helper: sync slider value to all active PROFILE_STYLES entries
    function _syncToStyles(prop, val) {
        for (const mode of _activeProfiles) {
            if (PROFILE_STYLES[mode]) PROFILE_STYLES[mode][prop] = val;
        }
    }

    // Bar width
    const bwSlider = document.getElementById('vps-bar-width');
    const bwVal = document.getElementById('vps-bar-width-val');
    bwSlider.addEventListener('input', () => {
        const v = parseInt(bwSlider.value) / 100;
        VP_CONFIG.BAR_WIDTH_PCT = v;
        _syncToStyles('barWidthPct', v);
        bwVal.textContent = bwSlider.value + '%';
    });

    // Opacity
    const opSlider = document.getElementById('vps-opacity');
    const opVal = document.getElementById('vps-opacity-val');
    opSlider.addEventListener('input', () => {
        const v = parseInt(opSlider.value) / 100;
        VP_CONFIG.BAR_ALPHA = v;
        VP_CONFIG.BAR_ALPHA_POC = Math.min(v + 0.25, 0.95);
        _syncToStyles('barAlpha', v);
        _syncToStyles('barAlphaPOC', Math.min(v + 0.25, 0.95));
        opVal.textContent = opSlider.value + '%';
    });

    // Side
    document.getElementById('vps-side-left').addEventListener('click', () => {
        VP_CONFIG.SIDE = 'left';
        _syncToStyles('side', 'left');
        document.getElementById('vps-side-left').classList.add('active');
        document.getElementById('vps-side-right').classList.remove('active');
    });
    document.getElementById('vps-side-right').addEventListener('click', () => {
        VP_CONFIG.SIDE = 'right';
        _syncToStyles('side', 'right');
        document.getElementById('vps-side-right').classList.add('active');
        document.getElementById('vps-side-left').classList.remove('active');
    });

    // Delta mode
    document.getElementById('vps-delta-mode').addEventListener('change', (e) => { _deltaMode = e.target.checked; });

    // Colors
    document.getElementById('vps-buy-color').addEventListener('input', (e) => {
        const rgb = _hexToRgb(e.target.value);
        VP_CONFIG.BUY_COLOR = rgb;
        _syncToStyles('buyColor', rgb);
    });
    document.getElementById('vps-sell-color').addEventListener('input', (e) => {
        const rgb = _hexToRgb(e.target.value);
        VP_CONFIG.SELL_COLOR = rgb;
        _syncToStyles('sellColor', rgb);
    });
    document.getElementById('vps-poc-color').addEventListener('input', (e) => {
        VP_CONFIG.POC_COLOR = e.target.value;
        _syncToStyles('pocColor', e.target.value);
    });

    // ── VISIBILITY TAB ──
    document.getElementById('vps-poc-line').addEventListener('change', (e) => { _showPOCLine = e.target.checked; });
    document.getElementById('vps-va-lines').addEventListener('change', (e) => { _showVALines = e.target.checked; });
    document.getElementById('vps-va-shade').addEventListener('change', (e) => { _showVAShade = e.target.checked; });
    document.getElementById('vps-hvn-lvn').addEventListener('change', (e) => { _showHVNLVN = e.target.checked; });
    const _elLiq = document.getElementById('vps-liquidity');
    if (_elLiq) _elLiq.addEventListener('change', (e) => { _showLiquidity = e.target.checked; });
    const _elAbs = document.getElementById('vps-absorption');
    if (_elAbs) _elAbs.addEventListener('change', (e) => { _showAbsorption = e.target.checked; });
    const _elExh = document.getElementById('vps-exhaustion');
    if (_elExh) _elExh.addEventListener('change', (e) => { _showExhaustion = e.target.checked; });
    const _elNaked = document.getElementById('vps-naked-pocs');
    if (_elNaked) _elNaked.addEventListener('change', (e) => { _showNakedPocs = e.target.checked; });
    const _elDev = document.getElementById('vps-dev-lines');
    if (_elDev) _elDev.addEventListener('change', (e) => { _showDevLines = e.target.checked; });
}

function _syncToolbarButtons() {
    document.querySelectorAll('#t-vp-modes .t-btn').forEach(btn => {
        btn.classList.toggle('active', _activeProfiles.includes(btn.dataset.vp));
    });
}

function _syncPanelFromState() {
    const el = (id) => document.getElementById(id);
    // Style tab: profiles
    if (el('vps-prior-day')) el('vps-prior-day').checked = _activeProfiles.includes('prior_day');
    if (el('vps-session')) el('vps-session').checked = _activeProfiles.includes('session');
    if (el('vps-rolling-1h')) el('vps-rolling-1h').checked = _activeProfiles.includes('rolling_1h');
    if (el('vps-rolling-2h')) el('vps-rolling-2h').checked = _activeProfiles.includes('rolling_2h');
    if (el('vps-rolling-4h')) el('vps-rolling-4h').checked = _activeProfiles.includes('rolling_4h');
    if (el('vps-2day')) el('vps-2day').checked = _activeProfiles.includes('2day');
    if (el('vps-weekly')) el('vps-weekly').checked = _activeProfiles.includes('weekly');
    if (el('vps-custom-mode')) el('vps-custom-mode').checked = _activeProfiles.includes('custom');
    if (el('vps-delta-mode')) el('vps-delta-mode').checked = _deltaMode;
    // Visibility tab
    if (el('vps-poc-line')) el('vps-poc-line').checked = _showPOCLine;
    if (el('vps-va-lines')) el('vps-va-lines').checked = _showVALines;
    if (el('vps-va-shade')) el('vps-va-shade').checked = _showVAShade;
    if (el('vps-hvn-lvn')) el('vps-hvn-lvn').checked = _showHVNLVN;
    if (el('vps-liquidity')) el('vps-liquidity').checked = _showLiquidity;
    if (el('vps-absorption')) el('vps-absorption').checked = _showAbsorption;
    if (el('vps-exhaustion')) el('vps-exhaustion').checked = _showExhaustion;
    if (el('vps-naked-pocs')) el('vps-naked-pocs').checked = _showNakedPocs;
    if (el('vps-dev-lines')) el('vps-dev-lines').checked = _showDevLines;
    // Inputs tab
    if (el('vps-extend-poc')) el('vps-extend-poc').checked = _extendPOC;
    if (el('vps-extend-vah')) el('vps-extend-vah').checked = _extendVAH;
    if (el('vps-extend-val')) el('vps-extend-val').checked = _extendVAL;
    if (el('vps-session-type')) el('vps-session-type').value = _sessionType;
    if (el('vps-vol-filter')) el('vps-vol-filter').value = _volumeFilter;
    if (el('vps-row-count')) el('vps-row-count').value = String(VP_CONFIG.ROW_COUNT);
    if (el('vps-trade-size')) el('vps-trade-size').value = String(_minLevelVol);
    if (el('vps-step')) el('vps-step').value = _stepSize;
    if (el('vps-step-extend-poc')) el('vps-step-extend-poc').checked = _stepExtendPOC;
    if (el('vps-step-va')) el('vps-step-va').checked = _stepShowVA;
    if (el('vps-step-opacity')) el('vps-step-opacity').value = Math.round(_stepBarOpacity * 100);
    if (el('vps-step-opacity-val')) el('vps-step-opacity-val').textContent = Math.round(_stepBarOpacity * 100) + '%';
    if (el('vps-va-pct')) el('vps-va-pct').value = Math.round(VP_CONFIG.VA_PCT * 100);
    // Style tab: sliders + colors
    if (el('vps-buy-color')) el('vps-buy-color').value = _rgbToHex(VP_CONFIG.BUY_COLOR);
    if (el('vps-sell-color')) el('vps-sell-color').value = _rgbToHex(VP_CONFIG.SELL_COLOR);
    if (el('vps-poc-color')) el('vps-poc-color').value = VP_CONFIG.POC_COLOR;
}

// ── Series Primitive (per-instance) — offscreen canvas caching ──
// VP only re-renders when data changes (every 10s poll) or price range changes.
// During active scroll: skip draw entirely (VP is static context, not price action).
let _vpOffscreen = null;
let _vpOffscreenCtx = null;
let _vpCacheKey = '';
let _vpLastCoord = null;

// Scroll listener removed — VP uses offscreen canvas blit (O(1) on cache hit)

function _createPrimitive(seriesRef, containerRef, chartRef) {
    const vpRenderer = {
        draw(target) {
            if (!_overlayVisible) return;
            if (containerRef && containerRef._overlayConfig && !containerRef._overlayConfig.vp) return;
            if (!seriesRef) return;
            // VP uses offscreen canvas blit — no need to skip during scroll
            try {
                target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
                    // Build cache key from data timestamps + quantized price range
                    // Quantize Y to nearest 4px to avoid re-render on every sub-pixel scroll
                    const _refPrice = _profiles[_activeProfiles[0]]?.data?.poc || 0;
                    const vr = seriesRef.priceToCoordinate ? (seriesRef.priceToCoordinate(_refPrice) | 0) : 0;
                    const profileKeys = _activeProfiles.map(m => {
                        const c = _profiles[m];
                        return c ? `${m}:${c.lastFetch || 0}` : '';
                    }).join('|');
                    // Include viewport position for scroll invalidation
                    let _anchorX = 0;
                    try {
                        const _firstFrom = _profiles[_activeProfiles[0]]?.data?.from_ts || 0;
                        if (chartRef && _firstFrom) _anchorX = chartRef.timeScale().timeToCoordinate(_firstFrom - 4*3600) || 0;
                    } catch(e) {}
                    const cacheKey = `${profileKeys}:${mediaSize.width}:${mediaSize.height}:${vr}:${Math.round(_anchorX)}`;

                    if (cacheKey !== _vpCacheKey) {
                        // Data or view changed — re-render to offscreen canvas
                        if (!_vpOffscreen || _vpOffscreen.width !== mediaSize.width || _vpOffscreen.height !== mediaSize.height) {
                            if (typeof OffscreenCanvas !== 'undefined') {
                                _vpOffscreen = new OffscreenCanvas(mediaSize.width, mediaSize.height);
                            } else {
                                _vpOffscreen = document.createElement('canvas');
                                _vpOffscreen.width = mediaSize.width;
                                _vpOffscreen.height = mediaSize.height;
                            }
                            _vpOffscreenCtx = _vpOffscreen.getContext('2d');
                        }
                        _vpOffscreenCtx.clearRect(0, 0, mediaSize.width, mediaSize.height);

                        for (const mode of _activeProfiles) {
                            const cached = _profiles[mode];
                            if (cached && cached.data) {
                                const priceConverter = (price) => {
                                    try { return seriesRef.priceToCoordinate(price); } catch(e) { return null; }
                                };
                                const _ts = chartRef ? chartRef.timeScale() : null;
                                _renderProfile(_vpOffscreenCtx, mediaSize, priceConverter, mode, cached.data, _ts);
                            }
                        }
                        _vpCacheKey = cacheKey;
                    }

                    // Blit cached VP to main canvas (O(1) — just a drawImage)
                    if (_vpOffscreen) {
                        ctx.drawImage(_vpOffscreen, 0, 0);
                    }

                    // ── Developing POC line — disabled (too noisy visually) ──
                    // Data still available at /api/vprofile/dev-poc for programmatic use
                    if (false && _devPocPath.length > 0) {
                        ctx.save();
                        const timeScale = chartRef ? chartRef.timeScale() : null;
                        if (timeScale) {
                            // Draw POC migration path
                            ctx.strokeStyle = 'rgba(232,184,48,0.7)';
                            ctx.lineWidth = 1.5;
                            ctx.setLineDash([]);
                            ctx.beginPath();
                            let started = false;
                            let lastX = 0, lastY = 0;
                            const _etOff2 = 4 * 3600;
                            for (const pt of _devPocPath) {
                                const x = timeScale.timeToCoordinate(pt.time - _etOff2);
                                const y = seriesRef.priceToCoordinate(pt.poc);
                                if (x == null || y == null || isNaN(x) || isNaN(y)) continue;
                                if (!started) { ctx.moveTo(x, y); started = true; }
                                else {
                                    // Stepped line: horizontal to new x, then vertical to new y
                                    ctx.lineTo(x, lastY);
                                    ctx.lineTo(x, y);
                                }
                                lastX = x; lastY = y;
                            }
                            // Extend to right edge at current POC
                            if (started && _devPocCurrent) {
                                const curY = seriesRef.priceToCoordinate(_devPocCurrent);
                                if (curY != null && !isNaN(curY)) {
                                    ctx.lineTo(mediaSize.width, curY);
                                }
                            }
                            if (started) ctx.stroke();

                            // Label at right edge
                            if (_devPocCurrent) {
                                const curY = seriesRef.priceToCoordinate(_devPocCurrent);
                                if (curY != null && !isNaN(curY)) {
                                    ctx.font = 'bold 8px "JetBrains Mono", monospace';
                                    ctx.fillStyle = 'rgba(232,184,48,0.9)';
                                    ctx.textAlign = 'right';
                                    ctx.textBaseline = 'middle';
                                    ctx.fillText(`dPOC ${_devPocCurrent.toFixed(2)}`, mediaSize.width - 6, curY - 8);
                                }
                            }
                        }
                        ctx.restore();
                    }

                    // ── Step Profiles: full mini-histograms per time block ──
                    // Gated OFF when VP Intel pane is mounted — those step columns
                    // are redundant with the VP Intel profile sidebar.
                    const _stepData = _profiles['session']?.data?.step_profiles;
                    const _etOff = 4 * 3600; // EDT offset for timestamp alignment
                    if (_stepData && _stepData.length > 0 && chartRef && !_intelPaneActive) {
                        const timeScale = chartRef.timeScale();
                        ctx.save();
                        for (const sp of _stepData) {
                            if (!sp.levels || sp.levels.length === 0) continue;
                            const x1 = timeScale.timeToCoordinate(sp.from_ts - _etOff);
                            const x2 = timeScale.timeToCoordinate(sp.to_ts - _etOff);
                            if (x1 == null || x2 == null || isNaN(x1) || isNaN(x2)) continue;
                            const stepW = Math.abs(x2 - x1);
                            const stepLeft = Math.min(x1, x2);
                            if (stepW < 4) continue;

                            const maxVol = Math.max(...sp.levels.map(l => l.total));
                            if (maxVol <= 0) continue;

                            // Tick height in pixels
                            const sorted = [...sp.levels].sort((a, b) => a.price - b.price);
                            const tickPx = sorted.length >= 2
                                ? Math.abs((seriesRef.priceToCoordinate(sorted[1].price) || 0) - (seriesRef.priceToCoordinate(sorted[0].price) || 0))
                                : 2;
                            const barH = Math.max(tickPx - 0.5, 2.5);

                            // Step boundary lines — left and right
                            ctx.strokeStyle = 'rgba(100,120,160,0.35)';
                            ctx.lineWidth = 1;
                            ctx.setLineDash([3, 3]);
                            const stepRight = stepLeft + stepW;
                            ctx.beginPath(); ctx.moveTo(stepLeft, 0); ctx.lineTo(stepLeft, mediaSize.height); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(stepRight, 0); ctx.lineTo(stepRight, mediaSize.height); ctx.stroke();
                            ctx.setLineDash([]);
                            // Time label at top
                            if (stepW > 40) {
                                const _fmtHM = (ts) => { const d = new Date(ts * 1000); return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false }); };
                                ctx.font = '7px "JetBrains Mono", monospace';
                                ctx.fillStyle = 'rgba(140,160,200,0.5)';
                                ctx.textAlign = 'center';
                                ctx.fillText(_fmtHM(sp.from_ts), stepLeft + stepW / 2, 12);
                            }

                            // Draw bars — grow RIGHT from left edge (QuantTower-style)
                            const _sAlpha = _stepBarOpacity;
                            for (const lv of sp.levels) {
                                const y = seriesRef.priceToCoordinate(lv.price);
                                if (y == null || isNaN(y) || y < -barH || y > mediaSize.height + barH) continue;

                                const volRatio = lv.total / maxVol;
                                const barW = volRatio * stepW * 0.92;
                                if (barW < 0.5) continue;

                                const isPOC = Math.abs(lv.price - sp.poc) < 0.01;
                                const inVA = lv.price >= sp.val && lv.price <= sp.vah;
                                const delta = (lv.buy || 0) - (lv.sell || 0);
                                const isBuy = delta >= 0;

                                const barAlpha = isPOC ? _sAlpha * 1.8 : (inVA ? _sAlpha : _sAlpha * 0.6);
                                const color = isBuy ? [0, 220, 180] : [255, 120, 50];
                                ctx.fillStyle = _rgba(color, barAlpha);

                                const barX = stepLeft;
                                const barY = Math.round(y - barH / 2);
                                ctx.fillRect(barX, barY, barW, barH);

                                // Subtle outline for definition
                                if (barW > 3 && barH > 2) {
                                    ctx.strokeStyle = _rgba(color, barAlpha * 0.5);
                                    ctx.lineWidth = 0.5;
                                    ctx.strokeRect(barX, barY, barW, barH);
                                }

                                // Delta core (vivid inner bar, grows from left)
                                if (lv.total > 0) {
                                    const deltaRatio = Math.abs(delta) / lv.total;
                                    const coreW = barW * deltaRatio;
                                    if (coreW > 1) {
                                        const coreColor = isBuy ? [0, 240, 200] : [255, 100, 30];
                                        ctx.fillStyle = _rgba(coreColor, barAlpha * 1.4);
                                        ctx.fillRect(stepLeft, barY, coreW, barH);
                                    }
                                }
                            }

                            // Step VA shade
                            if (_stepShowVA) {
                                const vahY = seriesRef.priceToCoordinate(sp.vah);
                                const valY = seriesRef.priceToCoordinate(sp.val);
                                if (vahY != null && valY != null && !isNaN(vahY) && !isNaN(valY)) {
                                    const top = Math.min(vahY, valY);
                                    const h = Math.abs(valY - vahY);
                                    ctx.fillStyle = 'rgba(80,140,220,0.04)';
                                    ctx.fillRect(stepLeft, top, stepW, h);
                                }
                            }

                            // POC line — within step or extended to right edge
                            const pocY = seriesRef.priceToCoordinate(sp.poc);
                            if (pocY != null && !isNaN(pocY)) {
                                ctx.strokeStyle = 'rgba(232,184,48,0.6)';
                                ctx.lineWidth = 1;
                                ctx.beginPath();
                                ctx.moveTo(stepLeft, pocY);
                                ctx.lineTo(_stepExtendPOC ? mediaSize.width : stepLeft + stepW, pocY);
                                ctx.stroke();
                                // POC label pill
                                if (stepW > 60) {
                                    ctx.font = 'bold 7px "JetBrains Mono", monospace';
                                    const spLabel = `POC ${sp.poc.toFixed(2)}`;
                                    const spTm = ctx.measureText(spLabel);
                                    const spLW = spTm.width + 6, spLH = 11;
                                    const spLX = stepLeft + 2, spLY = pocY - spLH - 1;
                                    ctx.fillStyle = 'rgba(0,0,0,0.75)';
                                    _roundRect(ctx, spLX, spLY, spLW, spLH, 2); ctx.fill();
                                    ctx.strokeStyle = 'rgba(232,184,48,0.5)'; ctx.lineWidth = 0.5;
                                    _roundRect(ctx, spLX, spLY, spLW, spLH, 2); ctx.stroke();
                                    ctx.fillStyle = 'rgba(232,184,48,0.85)';
                                    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
                                    ctx.fillText(spLabel, spLX + 3, spLY + spLH / 2);
                                }
                            }

                            // VAH/VAL lines within step
                            for (const [level, color, lbl] of [[sp.vah, 'rgba(80,160,240,0.3)', 'VAH'], [sp.val, 'rgba(80,160,240,0.3)', 'VAL']]) {
                                const ly = seriesRef.priceToCoordinate(level);
                                if (ly == null || isNaN(ly)) continue;
                                ctx.strokeStyle = color;
                                ctx.lineWidth = 0.5;
                                ctx.setLineDash([2, 3]);
                                ctx.beginPath(); ctx.moveTo(stepLeft, ly); ctx.lineTo(stepLeft + stepW, ly); ctx.stroke();
                                ctx.setLineDash([]);
                                // VAH/VAL label
                                if (stepW > 60) {
                                    const vaLabel = `${lbl} ${level.toFixed(2)}`;
                                    ctx.font = '7px "JetBrains Mono", monospace';
                                    const vaTm = ctx.measureText(vaLabel);
                                    const vaLW = vaTm.width + 4, vaLH = 10;
                                    const vaLX = stepLeft + 2;
                                    const vaLY = lbl === 'VAH' ? ly - vaLH - 1 : ly + 2;
                                    ctx.fillStyle = 'rgba(0,0,0,0.65)';
                                    ctx.fillRect(vaLX, vaLY, vaLW, vaLH);
                                    ctx.fillStyle = 'rgba(80,160,240,0.7)';
                                    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
                                    ctx.fillText(vaLabel, vaLX + 2, vaLY + vaLH / 2);
                                }
                            }
                        }
                        ctx.restore();
                    }

                    // ── Prior Day POC/VAH/VAL — LEFT column (so DEV stays in far right) ──
                    // Skip levels that duplicate DEV (already merged into DEV/PD label).
                    // Fade by distance from session POC (current engaged zone):
                    //   <20pts → full, 20-50pts → 0.6, >50pts → 0.3
                    const _pdData = _profiles['prior_day']?.data;
                    const _sessData = _profiles['session']?.data;
                    const DEV_COL_WIDTH = 90;  // Width reserved for DEV column on far right
                    const _dupTolPD = 0.5;
                    const _pdRefPrice = (_sessData && _sessData.poc != null) ? _sessData.poc : null;
                    const _pdFadeFor = (price) => {
                        if (_pdRefPrice == null || price == null) return 1.0;
                        const dd = Math.abs(price - _pdRefPrice);
                        return dd > 50 ? 0.3 : dd > 20 ? 0.6 : 1.0;
                    };

                    if (_pdData && _pdData.poc && _showPOCLine) {
                        const dupPOC = _sessData && _sessData.poc != null &&
                                        Math.abs(_pdData.poc - _sessData.poc) < _dupTolPD;
                        if (!dupPOC) {
                            const pdPocY = seriesRef.priceToCoordinate(_pdData.poc);
                            if (pdPocY != null && !isNaN(pdPocY) && pdPocY >= -50 && pdPocY <= mediaSize.height + 50) {
                                const fade = _pdFadeFor(_pdData.poc);
                                ctx.save();
                                ctx.strokeStyle = `rgba(255,180,40,${0.6 * fade})`;
                                ctx.lineWidth = 1.5;
                                ctx.setLineDash([8, 4]);
                                ctx.beginPath();
                                ctx.moveTo(0, pdPocY);
                                ctx.lineTo(mediaSize.width, pdPocY);
                                ctx.stroke();
                                ctx.setLineDash([]);
                                // Label pill — LEFT of DEV column
                                ctx.font = 'bold 9px "JetBrains Mono", monospace';
                                const pdLabel = `PD POC ${_pdData.poc.toFixed(2)}`;
                                const pdTm = ctx.measureText(pdLabel);
                                const pdW = pdTm.width + 10;
                                const pdX = mediaSize.width - DEV_COL_WIDTH - pdW - 8;
                                ctx.fillStyle = `rgba(0,0,0,${0.8 * fade})`;
                                ctx.fillRect(pdX, pdPocY - 8, pdW, 16);
                                ctx.fillStyle = `rgba(255,180,40,${0.9 * fade})`;
                                ctx.textAlign = 'left';
                                ctx.textBaseline = 'middle';
                                ctx.fillText(pdLabel, pdX + 4, pdPocY);
                                ctx.restore();
                            }
                        }
                        // Prior day VAH/VAL lines
                        if (_pdData.vah && _pdData.val) {
                            ctx.save();
                            ctx.lineWidth = 0.75;
                            ctx.setLineDash([4, 4]);
                            for (const [price, lbl, devLev] of [
                                [_pdData.vah, 'PD VAH', _sessData?.vah],
                                [_pdData.val, 'PD VAL', _sessData?.val]
                            ]) {
                                if (devLev != null && Math.abs(price - devLev) < _dupTolPD) continue;
                                const ly = seriesRef.priceToCoordinate(price);
                                if (ly == null || isNaN(ly) || ly < -50 || ly > mediaSize.height + 50) continue;
                                const fade = _pdFadeFor(price);
                                ctx.strokeStyle = `rgba(255,180,40,${0.3 * fade})`;
                                ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(mediaSize.width, ly); ctx.stroke();
                                // Label — LEFT of DEV column
                                ctx.font = '8px "JetBrains Mono", monospace';
                                const lblTxt = `${lbl} ${price.toFixed(2)}`;
                                const lblTm = ctx.measureText(lblTxt);
                                const lblW = lblTm.width + 8;
                                const lblX = mediaSize.width - DEV_COL_WIDTH - lblW - 8;
                                ctx.fillStyle = `rgba(0,0,0,${0.75 * fade})`;
                                ctx.fillRect(lblX, ly - 6, lblW, 12);
                                ctx.fillStyle = `rgba(255,180,40,${0.65 * fade})`;
                                ctx.textAlign = 'left';
                                ctx.textBaseline = 'middle';
                                ctx.fillText(lblTxt, lblX + 4, ly);
                            }
                            ctx.setLineDash([]);
                            ctx.restore();
                        }
                    }

                    // ── Naked POCs (prior-session POCs not yet revisited) ──
                    // Skipped when VP Intel pane owns naked-POC rendering
                    if (_showNakedPocs && !_intelPaneActive && _nakedPocs && _nakedPocs.length > 0) {
                        ctx.save();
                        ctx.font = 'bold 8px "JetBrains Mono", monospace';
                        ctx.textBaseline = 'middle';
                        for (const np of _nakedPocs) {
                            if (!np || np.price == null) continue;
                            const ny = seriesRef.priceToCoordinate(np.price);
                            if (ny == null || isNaN(ny) || ny < -50 || ny > mediaSize.height + 50) continue;
                            ctx.strokeStyle = 'rgba(200,160,255,0.55)';
                            ctx.lineWidth = 0.75;
                            ctx.setLineDash([3, 3]);
                            ctx.beginPath();
                            ctx.moveTo(0, ny);
                            ctx.lineTo(mediaSize.width, ny);
                            ctx.stroke();
                            ctx.setLineDash([]);
                            // Age badge
                            const npTxt = `nPOC ${np.price.toFixed(2)}${np.age_days != null ? ` · ${np.age_days}d` : ''}`;
                            const npTm = ctx.measureText(npTxt);
                            ctx.fillStyle = 'rgba(0,0,0,0.78)';
                            ctx.fillRect(mediaSize.width - npTm.width - 14, ny - 7, npTm.width + 10, 14);
                            ctx.fillStyle = 'rgba(210,180,255,0.9)';
                            ctx.textAlign = 'right';
                            ctx.fillText(npTxt, mediaSize.width - 8, ny);
                        }
                        ctx.restore();
                    }
                });
            } catch(e) { /* VP draw error — silent */ }
        },
    };
    const paneView = {
        zOrder() { return 'bottom'; },
        renderer() { return vpRenderer; },
    };
    return {
        updateAllViews() {},
        paneViews() { return [paneView]; },
    };
}

// ── Public API ──
window.VolumeProfileOverlay = {
    attach(chart, candleSeries, container) {
        // Prune orphaned instances (container removed from DOM by layout switch)
        _vpInstances = _vpInstances.filter(i => document.contains(i.container));
        // Avoid duplicate attachment for same container
        if (container && _vpInstances.find(i => i.container === container)) return;
        const primitive = _createPrimitive(candleSeries, container, chart);
        candleSeries.attachPrimitive(primitive);
        _vpInstances.push({ chart, series: candleSeries, primitive, container });
        // Start shared polling if not already running
        if (!_pollTimer) {
            _pollProfiles();
            _pollTimer = setInterval(_pollProfiles, VP_CONFIG.REFRESH_INTERVAL);
        }
        if (_vpInstances.length === 1) {
            setTimeout(() => _buildSettingsPanel(), 100);
        }
    },
    detachInstance(container) {
        const idx = _vpInstances.findIndex(i => i.container === container);
        if (idx > -1) {
            const inst = _vpInstances[idx];
            try { if (inst.series && inst.primitive) inst.series.detachPrimitive(inst.primitive); } catch(e) {}
            _vpInstances.splice(idx, 1);
        }
        if (_vpInstances.length === 0 && _pollTimer) {
            clearInterval(_pollTimer); _pollTimer = null;
        }
    },
    detach() {
        for (const inst of _vpInstances) {
            try { if (inst.series && inst.primitive) inst.series.detachPrimitive(inst.primitive); } catch(e) {}
        }
        _vpInstances = [];
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
        _profiles = {};
    },
    draw(ctx, mediaSize, priceConverter) {
        if (!_overlayVisible) return;
        for (const mode of _activeProfiles) {
            const cached = _profiles[mode];
            if (cached && cached.data) _renderProfile(ctx, mediaSize, priceConverter, mode, cached.data);
        }
    },
    setActiveProfiles(modes) { _activeProfiles = modes; _pollProfiles(); _syncPanelFromState(); },
    toggleVisibility() { _overlayVisible = !_overlayVisible; return _overlayVisible; },
    isVisible() { return _overlayVisible; },
    setCustomRange(from_ts, to_ts) { _customRange = { from_ts, to_ts }; if (_activeProfiles.includes('custom')) _fetchProfile('custom'); },
    refresh() { _pollProfiles(); },
    getData(mode) { const c = _profiles[mode || 'prior_day']; return c ? c.data : null; },
    getProfiles() { return _profiles; },
    getDOM() { return { dom: _latestDOM, deltas: _depthDeltas, bidTotal: _bidTotal, askTotal: _askTotal }; },
    getNakedPocs() { return _nakedPocs; },
    getDevPoc() { return _devPocCurrent; },
    getDevPocPath() { return _devPocPath; },
    getActiveProfiles() { return [..._activeProfiles]; },
    setIntelPaneActive(v) {
        // Dedup disabled — chart overlays (POC/VAH/VAL/HVN/LVN/liquidity/step/naked POC)
        // stay visible even when VP Intel pane is mounted. User wants them on the chart too.
        _intelPaneActive = false;
        if (v && !_pollTimer) {
            _wireDOMListener();
            _pollProfiles();
            _pollTimer = setInterval(_pollProfiles, VP_CONFIG.REFRESH_INTERVAL);
        }
    },
    isIntelActive() {
        // Safety: auto-reset stale flag if VP Intel pane is not actually mounted.
        // Prevents all Phase 2 gates (FORTRESS labels, zone bands, etc.) from
        // getting permanently hidden if a destroy path didn't fire cleanly.
        if (_intelPaneActive) {
            const _mounted = document.querySelector('[data-feat="vpintel"].current')
                           || document.querySelector('canvas[data-vpintel="1"]')
                           || (window.App && window.App._paneFeature && window.App._paneFeature.includes('vpintel'));
            if (!_mounted) _intelPaneActive = false;
        }
        return _intelPaneActive;
    },
    openSettings() {
        const panel = document.getElementById('vp-settings-panel');
        if (!panel) return;
        const opening = panel.style.display === 'none';
        if (opening && window.closeAllSettingsPanels) window.closeAllSettingsPanels('vp-settings-panel');
        _syncPanelFromState();
        panel.style.display = opening ? '' : 'none';
    },
    CONFIG: VP_CONFIG,
};

})();
