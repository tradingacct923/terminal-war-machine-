/**
 * 2D PASSIVE DOM HEATMAP v2 — Market-Maker Grade (Optimized)
 * High-performance extraction of the System 2 heatmap.
 * Wrapped in IIFE to avoid global name collisions with volume_bubbles.js
 */
(function() {
'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// HEATMAP SETTINGS
// ═══════════════════════════════════════════════════════════════════════════
const HM_DEFAULTS = {
    imbalance: true, bidColor: '#00ff96', askColor: '#ff4030', imbalanceOpacity: 75,
    wallColor: '#ffffff', wallBlend: 90,
    densityBoost: 100,
    midprice: true, midpriceColor: '#ffdc00', midpriceWidth: 2,
    microprice: true, micropriceColor: '#00dcff', micropriceWidth: 3,
    buyColor: '#00ff78', sellColor: '#ff3246',
    delta: true, deltaHeight: 40,
    persistence: true,
    velocity: true,
    depthMax: 0,
    otrLow: 3,
    otrHigh: 10,
    persistMid: 5,
    persistHigh: 20,
    velSigma: 10,
    ewmaAlpha: 5,
    flickerFilter: 0,
    bboBar: true,
    clusterTape: true,
};

const HeatmapSettings = { ...HM_DEFAULTS };

try {
    const saved = localStorage.getItem('heatmapSettings');
    if (saved) Object.assign(HeatmapSettings, JSON.parse(saved));
} catch (_) {}

if (window.AltarisEvents) {
    window.AltarisEvents.on('hms:updated', (next) => {
        try { Object.assign(HeatmapSettings, next || {}); } catch (_) {}
    });
}

// ═══════════════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════════════
function _hexToRgb(hex) {
    const n = parseInt(hex.replace('#', ''), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

const _rgbaCache = new Map();
function _cachedRgba(r, g, b, a) {
    // Quantize alpha to 2 decimal places to bound cache size
    const aq = Math.round(a * 100);
    const key = (r << 24) | (g << 16) | (b << 8) | aq;
    let str = _rgbaCache.get(key);
    if (!str) {
        str = `rgba(${r},${g},${b},${(aq/100).toFixed(2)})`;
        if (_rgbaCache.size > 20000) _rgbaCache.clear(); // safety
        _rgbaCache.set(key, str);
    }
    return str;
}

// ═══════════════════════════════════════════════════════════════════════════
// STATE MANAGER
// ═══════════════════════════════════════════════════════════════════════════
class HeatmapStateManager {
    constructor() {
        this.HISTORY_LIMIT = 600; // 60s at 10Hz — prevents 50-100MB leak over 2hrs
        this.CURRENT_COL_WIDTH = 12;

        this._snapshots = [];
        this._depthPersistence = new Map();
        this._prevSnapSizes = new Map();
        this._velocityFlash = new Map();
        this._fillAccum = new Map(); // OTR: fills accumulated incrementally
        this._otrScores = new Map();
        this._bboHistory = [];
        this._asymmetryHistory = [];
        
        // --- V2 Incremental Aggregation State ---
        this._ewmaInitialized = false;
        this._ewmaMean = 0;
        this._ewmaVar = 0;
        this._ewmaStdDev = 1;
        this._bookAsymmetry = 0.5;

        // Cumulative Delta: running sum of trades to avoid O(n) per frame
        this._runningDelta = 0;

        // Volume Profile Cache (price -> {bid, ask})
        this._volumeProfile = new Map();
        
        // P90 Trade Volumes: Running reservoir sampling or EWMA for max ref size
        this._tradeVolP90 = 5;
        this._tradeVolsLog = [];
    }

    _updateVolumeProfile(snapshot, isAdding) {
        const multiplier = isAdding ? 1 : -1;
        // Bids
        for (const [p, s] of Object.entries(snapshot.bids)) {
            const bp = (Math.floor(parseFloat(p) / snapshot._bucketSize) * snapshot._bucketSize).toFixed(2);
            let entry = this._volumeProfile.get(bp);
            if (!entry) { entry = { bid: 0, ask: 0 }; this._volumeProfile.set(bp, entry); }
            entry.bid += (s * multiplier);
            if (entry.bid < 0.01 && entry.ask < 0.01) this._volumeProfile.delete(bp);
        }
        // Asks
        for (const [p, s] of Object.entries(snapshot.asks)) {
            const bp = (Math.floor(parseFloat(p) / snapshot._bucketSize) * snapshot._bucketSize).toFixed(2);
            let entry = this._volumeProfile.get(bp);
            if (!entry) { entry = { bid: 0, ask: 0 }; this._volumeProfile.set(bp, entry); }
            entry.ask += (s * multiplier);
            if (entry.bid < 0.01 && entry.ask < 0.01) this._volumeProfile.delete(bp);
        }
    }

    // Phase 1: Institutional Data Prep (Ingestion Time)
    _prepSnapshot(rawSnap) {
        const tsDiff = rawSnap.ts - Date.now() / 1000;
        // If snapshot is impossibly far in the future or extremely old, sanitize to local clock
        const snapTs = (tsDiff > 600 || tsDiff < -86400) ? Date.now() / 1000 : rawSnap.ts;

        const snap = {
            ts: snapTs,
            bids: rawSnap.bids || {},
            asks: rawSnap.asks || {},
            trades: rawSnap.trades || [],
            absorption: rawSnap.absorption || null,
        };

        const bidEntries = Object.entries(snap.bids).map(([p, s]) => [parseFloat(p), s]);
        const askEntries = Object.entries(snap.asks).map(([p, s]) => [parseFloat(p), s]);

        bidEntries.sort((a, b) => b[0] - a[0]); // highest first
        askEntries.sort((a, b) => a[0] - b[0]); // lowest first

        snap._bidEntries = bidEntries;
        snap._askEntries = askEntries;

        snap._bestBid = bidEntries.length > 0 ? bidEntries[0][0] : null;
        snap._bestAsk = askEntries.length > 0 ? askEntries[0][0] : null;

        snap._midPrice = null;
        snap._micro = null;
        snap._spread = 0;

        if (snap._bestBid !== null && snap._bestAsk !== null) {
            snap._midPrice = (snap._bestBid + snap._bestAsk) / 2;
            snap._spread = snap._bestAsk - snap._bestBid;

            // Micro-price (VWAP of top 3 levels each side)
            let bidVolSq = 0, askVolSq = 0, bidPxVol = 0, askPxVol = 0;
            const depth = Math.min(3, Math.min(bidEntries.length, askEntries.length));
            for (let i = 0; i < depth; i++) {
                const bw = bidEntries[i][1] ** 2; // weight heavier sizes quadratically
                const aw = askEntries[i][1] ** 2;
                bidVolSq += bw; askVolSq += aw;
                bidPxVol += bidEntries[i][0] * bw;
                askPxVol += askEntries[i][0] * aw;
            }
            if (bidVolSq > 0 && askVolSq > 0) {
                const wBid = bidPxVol / bidVolSq;
                const wAsk = askPxVol / askVolSq;
                snap._micro = (wBid * askVolSq + wAsk * bidVolSq) / (bidVolSq + askVolSq);
            } else {
                snap._micro = snap._midPrice;
            }
        }

        // --- Incremental Updates ---
        
        // 1. Cumulative Delta
        let snapDelta = 0;
        let snapTradeVol = 0;
        for (const t of snap.trades) {
            const v = t.v || 1;
            if (t.s === 'b') snapDelta += v;
            else if (t.s === 's') snapDelta -= v;
            snapTradeVol += v;

            // Reservoir sampling for P90 trades max reference
            if (v > 0) {
                this._tradeVolsLog.push(v);
                if (this._tradeVolsLog.length > 500) this._tradeVolsLog.shift();
            }
        }
        this._runningDelta += snapDelta;
        snap._cumDelta = this._runningDelta;

        if (this._tradeVolsLog.length > 0) {
            // Lazy evaluate P90
            const sorted = [...this._tradeVolsLog].sort((a,b)=>a-b);
            this._tradeVolP90 = Math.max(sorted[Math.floor(0.90 * sorted.length)], 1);
        }

        // 2. Incremental OTR Fill accumulation
        for (const t of snap.trades) {
            const p = t.p;
            if (p) {
                const ps = p.toString();
                this._fillAccum.set(ps, (this._fillAccum.get(ps) || 0) + (t.v || 1));
            }
        }

        // Pre-compute array of primitive numbers for fast bounds checking
        const allKeys = [...Object.keys(snap.bids), ...Object.keys(snap.asks)];
        const priceArr = new Float64Array(allKeys.length);
        for (let i = 0; i < allKeys.length; i++) {
            priceArr[i] = parseFloat(allKeys[i]);
        }
        snap._priceArr = priceArr;

        return snap;
    }

    pushSnapshot(rawBody) {
        if (!rawBody || (!rawBody.bids && !rawBody.asks)) return;

        // Guess bucket tick size from data
        let bucketSize = 0.25;
        try {
            const someKeys = Object.keys(rawBody.bids);
            if (someKeys.length >= 2) {
                bucketSize = Math.abs(parseFloat(someKeys[0]) - parseFloat(someKeys[1]));
            } else if (someKeys.length === 1 && rawBody.asks && Object.keys(rawBody.asks).length > 0) {
                const askKeys = Object.keys(rawBody.asks);
                let diff = Math.abs(parseFloat(someKeys[0]) - parseFloat(askKeys[0]));
                if (diff < 1.0) bucketSize = diff;
            }
            if (bucketSize === 0 || isNaN(bucketSize)) bucketSize = 0.25;
            bucketSize = Math.round(bucketSize * 100) / 100;
        } catch (e) { bucketSize = 0.25; }

        rawBody._bucketSize = bucketSize;
        const snap = this._prepSnapshot(rawBody);

        this._snapshots.push(snap);
        
        // Add to aggregate volume profile
        this._updateVolumeProfile(snap, true);

        // Trim history
        if (this._snapshots.length > this.HISTORY_LIMIT) {
            const removed = this._snapshots.shift();
            // Remove from aggregate volume profile
            this._updateVolumeProfile(removed, false);
        }

        // Update Persistence & Velocity Memory (Incremental)
        const currentPrices = new Set();
        for (const [p, s] of Object.entries(snap.bids)) currentPrices.add({p, s});
        for (const [p, s] of Object.entries(snap.asks)) currentPrices.add({p, s});

        const activeStr = new Set();

        for (const {p, s} of currentPrices) {
            activeStr.add(p);
            // Persistence
            this._depthPersistence.set(p, (this._depthPersistence.get(p) || 0) + 1);
            
            // Velocity
            const prev = this._prevSnapSizes.get(p) || 0;
            if (s > prev + (prev * 0.5)) {
                this._velocityFlash.set(p, { type: 'add', ts: snap.ts, amt: s - prev, startH: s });
            } else if (prev > 0 && s < prev * 0.5) {
                this._velocityFlash.set(p, { type: 'pull', ts: snap.ts, amt: prev - s, startH: prev });
            }
            this._prevSnapSizes.set(p, s);
        }

        // Decay things
        for (const [p, count] of this._depthPersistence.entries()) {
            if (!activeStr.has(p)) {
                if (count > 1) this._depthPersistence.set(p, count - 1);
                else this._depthPersistence.delete(p);
                this._prevSnapSizes.delete(p);
            }
        }
        for (const [p, flash] of this._velocityFlash.entries()) {
            if (snap.ts - flash.ts > 1.5) this._velocityFlash.delete(p);
        }

        // Decay OTR fills so old trades don't permanently blind resting orders
        for (const [p, agg] of this._fillAccum.entries()) {
            if (agg > 0.1) this._fillAccum.set(p, agg * 0.95);
            else this._fillAccum.delete(p);
        }

        // Decay EWMA (Welford's algorithm)
        let _ewN = 0, _ewMean = 0, _ewM2 = 0;
        for (const p in snap.bids) {
            const s = snap.bids[p]; if (s <= 0) continue;
            _ewN++; const d = s - _ewMean; _ewMean += d / _ewN; _ewM2 += d * (s - _ewMean);
        }
        for (const p in snap.asks) {
            const s = snap.asks[p]; if (s <= 0) continue;
            _ewN++; const d = s - _ewMean; _ewMean += d / _ewN; _ewM2 += d * (s - _ewMean);
        }

        if (_ewN > 0) {
            const snapAvg = _ewMean;
            const snapVar = _ewM2 / _ewN;

            if (!this._ewmaInitialized) {
                this._ewmaMean = snapAvg;
                this._ewmaVar = snapVar;
                this._ewmaInitialized = true;
            } else {
                const a = HeatmapSettings.ewmaAlpha / 100;
                this._ewmaMean = a * snapAvg + (1 - a) * this._ewmaMean;
                this._ewmaVar = a * snapVar + (1 - a) * this._ewmaVar;
            }
            this._ewmaStdDev = Math.max(Math.sqrt(this._ewmaVar), 1);
        }

        // Book Asymmetry
        let totalBidDepth = 0, totalAskDepth = 0;
        for (const s of Object.values(snap.bids)) totalBidDepth += (s > 0 ? s : 0);
        for (const s of Object.values(snap.asks)) totalAskDepth += (s > 0 ? s : 0);
        const totalDepth = totalBidDepth + totalAskDepth;
        if (totalDepth > 0) {
            this._bookAsymmetry = totalBidDepth / totalDepth;
        }
        this._asymmetryHistory.push(this._bookAsymmetry);
        if (this._asymmetryHistory.length > 60) this._asymmetryHistory.shift();

        // OTR Scoring
        for (const [p, s] of Object.entries(snap.bids)) {
            const resting = s;
            if (resting <= 0) continue;
            const fills = this._fillAccum.get(p) || 0;
            const otr = resting / (fills + 1);
            this._otrScores.set(p, otr);
        }
        for (const [p, s] of Object.entries(snap.asks)) {
            const resting = s;
            if (resting <= 0) continue;
            const fills = this._fillAccum.get(p) || 0;
            const otr = resting / (fills + 1);
            this._otrScores.set(p, otr);
        }
        // Prune stale OTR entries for prices no longer in the book
        for (const p of this._otrScores.keys()) {
            if (!activeStr.has(p)) this._otrScores.delete(p);
        }
    }

    destroy() {
        this._snapshots = [];
        this._depthPersistence.clear();
        this._prevSnapSizes.clear();
        this._velocityFlash.clear();
        this._fillAccum.clear();
        this._otrScores.clear();
        this._bboHistory = [];
        this._asymmetryHistory = [];
        this._volumeProfile.clear();
        this._ewmaInitialized = false;
        _rgbaCache.clear();
    }
}

// Global state instance
const DOM2D_STATE = new HeatmapStateManager();

// ═══════════════════════════════════════════════════════════════════════════
// LIFECYCLE / CONNECTIONS
// ═══════════════════════════════════════════════════════════════════════════
let _domHistoryAbort = null;
let _domHistorySymbol = '';
function _fetchDomHistory(symbol) {
    if (!symbol) return;
    if (_domHistoryAbort) { try { _domHistoryAbort.abort(); } catch (_) {} }
    const ctrl = new AbortController();
    _domHistoryAbort = ctrl;
    _domHistorySymbol = symbol;
    fetch(`/api/l2/dom-history?symbol=${encodeURIComponent(symbol)}&limit=150`, { signal: ctrl.signal })
        .then(res => res.json())
        .then(data => {
            if (ctrl.signal.aborted || _domHistorySymbol !== symbol) return;
            if (!data.history || !Array.isArray(data.history)) return;
            DOM2D_STATE.destroy();
            data.history.forEach(rawBody => {
                DOM2D_STATE.pushSnapshot(rawBody);
            });
        })
        .catch(err => { if (err.name !== 'AbortError') console.error("DOM history fetch fail:", err); });
}

function startDomHistory(symbol) {
    // REST backfill: fetch historical snapshots. Live updates need a rewire
    // from the removed dom_snapshot event onto l2_update — heatmap is static
    // until that lands.
    DOM2D_STATE.destroy();
    _fetchDomHistory(symbol);
}

function stopDomHistory() {
    DOM2D_STATE.destroy();
    const tt = document.getElementById('dom2d-tooltip');
    if (tt) tt.style.display = 'none';
}

// ═══════════════════════════════════════════════════════════════════════════
// RENDER ENGINE
// ═══════════════════════════════════════════════════════════════════════════
function renderDomHeatmap2D(canvas, safePriceToY = null, midPrice = 0, chartComponentContext = null) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    if (DOM2D_STATE._snapshots.length === 0) return;

    // Derive midPrice from snapshot data if not provided or zero
    if (!midPrice || midPrice <= 0) {
        const latest = DOM2D_STATE._snapshots[DOM2D_STATE._snapshots.length - 1];
        if (latest && latest._midPrice) midPrice = latest._midPrice;
        if (!midPrice || midPrice <= 0) return; // No valid price reference — skip render
    }

    // ── DPR-aware canvas sizing ──
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    if (cssW < 10 || cssH < 10) return;

    // Sync canvas buffer to CSS size * DPR (avoids stale init dimensions)
    const bufW = Math.round(cssW * dpr);
    const bufH = Math.round(cssH * dpr);
    if (canvas.width !== bufW || canvas.height !== bufH) {
        canvas.width = bufW;
        canvas.height = bufH;
    }

    let _priceToY;
    let visMin = 0, visMax = Infinity;
    let topPrice = null, botPrice = null, topY = null, botY = null;

    // ── For standalone heatmap: use a tight ±15 NQ-point window around mid ──
    // This ensures ~5px per 0.25 tick on typical screen sizes (ideal for heatmap cells)
    const HEATMAP_HALF_RANGE = 15; // NQ points each side of mid

    if (!safePriceToY && chartComponentContext) {
        // Find visible bounds
        const sData = chartComponentContext.series.data();
        if (sData && sData.length > 0) {
            const visRange = chartComponentContext.series.timeScale().visibleLogicalRange();
            if (visRange) {
                let pmin = Infinity, pmax = 0;
                const start = Math.max(0, Math.floor(visRange.from));
                const end = Math.min(sData.length - 1, Math.ceil(visRange.to));
                for (let i = start; i <= end; i++) {
                    const d = sData[i];
                    if (!d) continue;
                    pmin = Math.min(pmin, d.low); pmax = Math.max(pmax, d.high);
                }
                if (pmin !== Infinity) { visMin = pmin * 0.95; visMax = pmax * 1.05; }
            }
        }

        const priceScale = chartComponentContext.series.priceScale();
        const height = chartComponentContext.series.priceScale().height();
        topY = 0; topPrice = priceScale.coordinateToPrice(topY);
        botY = height; botPrice = priceScale.coordinateToPrice(botY);

        _priceToY = (price) => {
            const h = priceScale.height();
            const tp = priceScale.coordinateToPrice(0);
            const bp = priceScale.coordinateToPrice(h);
            if (tp === null || bp === null || tp === bp) return null;
            return ((tp - price) / (tp - bp)) * h;
        };
    } else if (safePriceToY) {
        // Test if chart's price scale gives enough pixel density
        const p1 = safePriceToY(midPrice + 1);
        const p2 = safePriceToY(midPrice - 1);
        const pxPerPoint = (p1 !== null && p2 !== null && p1 !== p2) ? Math.abs(p2 - p1) / 2 : 0;

        // Need at least 3px per NQ point (0.75px per tick) for a usable heatmap.
        // If chart is zoomed out too far, use our own linear mapping instead.
        if (pxPerPoint >= 3) {
            topY = 0; botY = cssH - 12;
            topPrice = midPrice + ((cssH / 2) / pxPerPoint);
            botPrice = midPrice - ((cssH / 2) / pxPerPoint);
            visMin = botPrice * 0.95; visMax = topPrice * 1.05;
            _priceToY = safePriceToY;
        } else {
            // Chart zoomed out too far — use own tight mapping
            topPrice = midPrice + HEATMAP_HALF_RANGE;
            botPrice = midPrice - HEATMAP_HALF_RANGE;
            visMin = botPrice - 2; visMax = topPrice + 2;
            _priceToY = (price) => {
                return cssH - ((price - botPrice) / (topPrice - botPrice)) * cssH;
            };
        }
    } else {
        topPrice = midPrice + HEATMAP_HALF_RANGE;
        botPrice = midPrice - HEATMAP_HALF_RANGE;
        visMin = botPrice - 2; visMax = topPrice + 2;
        _priceToY = (price) => {
            return cssH - ((price - botPrice) / (topPrice - botPrice)) * cssH;
        };
    }

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // Scale context for DPR — draw in CSS coords
    ctx.clearRect(0, 0, cssW, cssH);

    const LATEST = DOM2D_STATE._snapshots[DOM2D_STATE._snapshots.length - 1];
    let bucketSize = LATEST._bucketSize || 0.25;

    const COL_W = 6;
    const RAW_SPACE = cssW - DOM2D_STATE.CURRENT_COL_WIDTH - 24; // room for VP
    const heatmapRight = cssW - DOM2D_STATE.CURRENT_COL_WIDTH;
    const maxCols = Math.max(1, Math.floor(RAW_SPACE / COL_W));

    let displaySnaps;
    if (DOM2D_STATE._snapshots.length <= maxCols) {
        displaySnaps = DOM2D_STATE._snapshots;
    } else {
        displaySnaps = DOM2D_STATE._snapshots.slice(-maxCols);
    }

    const numCols = displaySnaps.length;
    if (numCols === 0) { ctx.restore(); return; }

    const curWidths = numCols * COL_W;
    const heatmapLeft = heatmapRight - curWidths;

    const testY1 = _priceToY(midPrice);
    const testY2 = _priceToY(midPrice - bucketSize * 2);
    const rawRowH = testY1 !== null && testY2 !== null ? Math.abs(testY1 - testY2) / 2 : 8;
    const rowH = Math.max(rawRowH, 2); // Min 2px per level — prevents negative cellH
    const GAP = rowH < 4 ? 0 : Math.max(1, Math.floor(rowH * 0.12));

    const _buyRgb = _hexToRgb(HeatmapSettings.buyColor || '#00ff78');
    const _sellRgb = _hexToRgb(HeatmapSettings.sellColor || '#ff3246');
    const _bidRgb = _hexToRgb(HeatmapSettings.bidColor || '#00ff96');
    const _askRgb = _hexToRgb(HeatmapSettings.askColor || '#ff4030');

    const intensityScale = HeatmapSettings.densityBoost / 100;
    const blendFactor = HeatmapSettings.wallBlend / 100;

    for (let col = 0; col < numCols; col++) {
        const snap = displaySnaps[col];
        const x = heatmapLeft + col * COL_W;
        
        // Fast culling — sort _priceArr descending if not already
        if (snap._priceArr && snap._priceArr.length > 0) {
            if (!snap._priceArrSorted) {
                snap._priceArr.sort((a, b) => b - a);
                snap._priceArrSorted = true;
            }
            let maxP = snap._priceArr[0], minP = snap._priceArr[snap._priceArr.length - 1];
            if (minP > visMax || maxP < visMin) continue;
        }

        // Use cached price array if available (avoids Set + spread allocation per frame)
        const allPrices = snap._priceArr || Object.keys(snap.bids).concat(Object.keys(snap.asks));

        for (const priceStr of allPrices) {
            const price = parseFloat(priceStr);
            if (isNaN(price) || price < visMin || price > visMax) continue;

            const bidS = snap.bids[priceStr] || 0;
            const askS = snap.asks[priceStr] || 0;
            if (bidS < 1 && askS < 1) continue;

            const isBid = bidS > askS;
            const dominant = isBid ? bidS : askS;
            const weaker = isBid ? askS : bidS;

            const persistCount = DOM2D_STATE._depthPersistence.get(priceStr) || 1;
            if (HeatmapSettings.flickerFilter > 0 && persistCount < HeatmapSettings.flickerFilter) continue;

            const norm = HeatmapSettings.depthMax > 0
                ? Math.min(dominant / HeatmapSettings.depthMax, 1.0)
                : Math.min(dominant / (DOM2D_STATE._ewmaMean + 2 * DOM2D_STATE._ewmaStdDev), 1.0);

            const bucketPrice = Math.floor(price / bucketSize) * bucketSize;
            const y = _priceToY(bucketPrice + bucketSize / 2);
            if (y === null || y < -rowH || y > cssH + rowH) continue;

            let alpha = 0.05 + 0.95 * Math.pow(norm, 1.2);
            alpha *= intensityScale;
            if (alpha > 1) alpha = 1; else if (alpha < 0.1) alpha = 0.1;

            let r, g, b;
            const baseRgb = isBid ? _bidRgb : _askRgb;

            if (weaker > dominant * 0.3) {
                const conflictRatio = weaker / dominant;
                r = Math.floor(baseRgb.r * (1 - conflictRatio) + 180 * conflictRatio);
                g = Math.floor(baseRgb.g * (1 - conflictRatio) + 120 * conflictRatio);
                b = Math.floor(baseRgb.b * (1 - conflictRatio) + 200 * conflictRatio);
            } else if (norm > 0.8) {
                const bf = blendFactor * ((norm - 0.8) / 0.2);
                r = baseRgb.r + (255 - baseRgb.r) * bf;
                g = baseRgb.g + (255 - baseRgb.g) * bf;
                b = baseRgb.b + (255 - baseRgb.b) * bf;
            } else {
                r = baseRgb.r; g = baseRgb.g; b = baseRgb.b;
            }

            ctx.fillStyle = _cachedRgba(r, g, b, alpha);
            const cellX = x + GAP * 0.5;
            const cellY = y - rowH / 2 + GAP;
            const cellW = Math.max(COL_W - GAP, 1);
            const cellH = Math.max(rowH - GAP * 2, 1);
            const cr = Math.max(0, Math.min(1.5, cellH / 4, cellW / 4));

            ctx.beginPath();
            if (cr > 0.1) {
                ctx.roundRect(cellX, cellY, cellW, cellH, cr);
            } else {
                ctx.rect(cellX, cellY, cellW, cellH);
            }
            ctx.fill();
        }
    }

    // ── Phase 1b: BBO Imbalance Bar
    if (HeatmapSettings.bboBar && LATEST) {
        const bidEntries = LATEST._bidEntries || [];
        const askEntries = LATEST._askEntries || [];
        const bidSize = bidEntries.slice(0, 3).reduce((sum, e) => sum + e[1], 0);
        const askSize = askEntries.slice(0, 3).reduce((sum, e) => sum + e[1], 0);
        const total = bidSize + askSize;

        if (total > 0) {
            const ratio = bidSize / total;
            const barY = 16, barW = Math.min(numCols * COL_W, 180), barH = 6;
            const barX = heatmapLeft + numCols * COL_W - barW;

            ctx.fillStyle = 'rgba(20, 25, 35, 0.7)';
            ctx.fillRect(barX - 1, barY - 1, barW + 2, barH + 2);

            const bidW = barW * ratio;
            ctx.fillStyle = ratio > 0.55 ? 'rgba(31, 209, 122, 0.85)' : 'rgba(31, 209, 122, 0.5)';
            ctx.fillRect(barX, barY, bidW, barH);
            ctx.fillStyle = ratio < 0.45 ? 'rgba(224, 48, 96, 0.85)' : 'rgba(224, 48, 96, 0.5)';
            ctx.fillRect(barX + bidW, barY, barW - bidW, barH);

            ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(barX + barW / 2, barY); ctx.lineTo(barX + barW / 2, barY + barH); ctx.stroke();

            ctx.font = '8px "JetBrains Mono", monospace';
            ctx.textBaseline = 'top';
            const pct = (ratio * 100).toFixed(0);
            if (ratio > 0.55) {
                ctx.fillStyle = 'rgba(31, 209, 122, 0.9)'; ctx.textAlign = 'left';
                ctx.fillText(`B${bidSize} (${pct}%)`, barX, barY + barH + 2);
            } else if (ratio < 0.45) {
                ctx.fillStyle = 'rgba(224, 48, 96, 0.9)'; ctx.textAlign = 'right';
                ctx.fillText(`A${askSize} (${(100 - pct)}%)`, barX + barW, barY + barH + 2);
            }
        }
    }

    // ── Phase 1c: Clustered Trade Tape
    if (HeatmapSettings.clusterTape && LATEST && LATEST.trades && LATEST.trades.length > 0) {
        const clusters = {};
        for (const t of LATEST.trades) {
            const price = t.p;
            if (!price || price < visMin || price > visMax) continue;
            const bucket = Math.floor(price / bucketSize) * bucketSize;
            const key = `${bucket}_${t.s || 'u'}`;
            if (!clusters[key]) clusters[key] = { bucket, side: t.s, totalVol: 0, count: 0 };
            clusters[key].totalVol += (t.v || 1);
            clusters[key].count++;
        }
        const lastColXtape = heatmapLeft + (numCols - 1) * COL_W;
        for (const [, cl] of Object.entries(clusters)) {
            if (cl.count < 2) continue;
            const y = _priceToY(cl.bucket + bucketSize / 2);
            if (y === null || y < 0 || y > cssH) continue;

            const volNorm = Math.min(Math.sqrt(cl.totalVol / 10), 1.0);
            const blockW = 6 + volNorm * 14;
            const blockH = Math.max(rowH - 2, 5);
            const isBuy = cl.side === 'b';
            const rgb = isBuy ? '31, 209, 122' : '224, 48, 96';
            const bx = lastColXtape - blockW - 2;

            ctx.fillStyle = `rgba(${rgb}, ${(0.3 + volNorm * 0.4).toFixed(2)})`;
            ctx.fillRect(bx, y - blockH / 2, blockW, blockH);
            ctx.strokeStyle = `rgba(${rgb}, 0.7)`;
            ctx.lineWidth = 1;
            ctx.strokeRect(bx, y - blockH / 2, blockW, blockH);

            ctx.font = '7px "JetBrains Mono", monospace';
            ctx.fillStyle = `rgba(255, 255, 255, 0.9)`;
            ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
            ctx.fillText(`${cl.count}×${cl.totalVol}`, bx + blockW / 2, y);
        }
    }

    // LAYER 3/3b: Mid-price / Micro-price Trails
    if (HeatmapSettings.midprice) {
        const _mpRgb = _hexToRgb(HeatmapSettings.midpriceColor);
        ctx.strokeStyle = `rgba(${_mpRgb.r}, ${_mpRgb.g}, ${_mpRgb.b}, 0.80)`;
        ctx.lineWidth = HeatmapSettings.midpriceWidth;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        const midPts = [];
        for (let col = 0; col < numCols; col++) {
            const my = _priceToY(displaySnaps[col]._midPrice || midPrice);
            if (my !== null) midPts.push({ x: heatmapLeft + col * COL_W + COL_W / 2, y: my });
        }
        if (midPts.length > 1) {
            ctx.moveTo(midPts[0].x, midPts[0].y);
            for (let i = 1; i < midPts.length; i++) {
                const cpx = (midPts[i - 1].x + midPts[i].x) / 2;
                ctx.quadraticCurveTo(midPts[i - 1].x, midPts[i - 1].y, cpx, (midPts[i - 1].y + midPts[i].y) / 2);
            }
            ctx.lineTo(midPts[midPts.length - 1].x, midPts[midPts.length - 1].y);
            ctx.stroke();
        }
        ctx.setLineDash([]);
    }

    if (HeatmapSettings.microprice) {
        const _mcRgb = _hexToRgb(HeatmapSettings.micropriceColor);
        ctx.strokeStyle = `rgba(${_mcRgb.r}, ${_mcRgb.g}, ${_mcRgb.b}, 1)`;
        ctx.lineWidth = HeatmapSettings.micropriceWidth;
        ctx.beginPath();
        const microPts = [];
        for (let col = 0; col < numCols; col++) {
            const my = _priceToY(displaySnaps[col]._micro || displaySnaps[col]._midPrice || midPrice);
            if (my !== null) microPts.push({ x: heatmapLeft + col * COL_W + COL_W / 2, y: my });
        }
        if (microPts.length > 1) {
            ctx.moveTo(microPts[0].x, microPts[0].y);
            for (let i = 1; i < microPts.length; i++) {
                const cpx = (microPts[i - 1].x + microPts[i].x) / 2;
                ctx.quadraticCurveTo(microPts[i - 1].x, microPts[i - 1].y, cpx, (microPts[i - 1].y + microPts[i].y) / 2);
            }
            ctx.lineTo(microPts[microPts.length - 1].x, microPts[microPts.length - 1].y);
            ctx.stroke();
        }
    }

    // ── LAYER 6: Cumulative Delta (O(1) rendering) ──
    if (HeatmapSettings.delta) {
        const deltaPoints = [];
        let deltaMin = 0, deltaMax = 0;

        for (let col = 0; col < numCols; col++) {
            const cumDelta = displaySnaps[col]._cumDelta || 0;
            deltaPoints.push({ col, delta: cumDelta });
            if (cumDelta < deltaMin) deltaMin = cumDelta;
            if (cumDelta > deltaMax) deltaMax = cumDelta;
        }

        const deltaRange = Math.max(Math.abs(deltaMin), Math.abs(deltaMax), 1);
        const DELTA_STRIP_H = HeatmapSettings.deltaHeight;
        const deltaStripTop = cssH - 28 - DELTA_STRIP_H;
        const deltaStripMid = deltaStripTop + DELTA_STRIP_H / 2;

        ctx.fillStyle = 'rgba(4, 6, 14, 0.6)';
        ctx.fillRect(heatmapLeft, deltaStripTop, numCols * COL_W, DELTA_STRIP_H);
        ctx.strokeStyle = 'rgba(100, 110, 130, 0.3)';
        ctx.lineWidth = 0.5;
        ctx.beginPath(); ctx.moveTo(heatmapLeft, deltaStripMid); ctx.lineTo(heatmapLeft + numCols * COL_W, deltaStripMid); ctx.stroke();

        ctx.beginPath();
        let pathSt = false;
        for (const pt of deltaPoints) {
            const px = heatmapLeft + pt.col * COL_W + COL_W / 2;
            const norm = pt.delta / deltaRange;
            const py = deltaStripMid - norm * (DELTA_STRIP_H / 2 - 2);
            if (!pathSt) { ctx.moveTo(px, deltaStripMid); ctx.lineTo(px, py); pathSt = true; }
            else ctx.lineTo(px, py);
        }

        if (pathSt && deltaPoints.length) {
            const lastX = heatmapLeft + deltaPoints[deltaPoints.length - 1].col * COL_W + COL_W / 2;
            ctx.lineTo(lastX, deltaStripMid);
            ctx.closePath();
            const finalDelta = deltaPoints[deltaPoints.length - 1].delta;
            if (finalDelta >= 0) {
                ctx.fillStyle = 'rgba(0, 200, 100, 0.25)'; ctx.strokeStyle = 'rgba(0, 255, 120, 0.6)';
            } else {
                ctx.fillStyle = 'rgba(200, 40, 40, 0.25)'; ctx.strokeStyle = 'rgba(255, 50, 70, 0.6)';
            }
            ctx.fill(); ctx.lineWidth = 1; ctx.stroke();
            
            ctx.font = '7px "JetBrains Mono", monospace';
            ctx.fillStyle = 'rgba(160, 170, 190, 0.6)';
            ctx.textAlign = 'left'; ctx.textBaseline = 'top';
            ctx.fillText(`Δ ${finalDelta >= 0 ? '+' : ''}${finalDelta}`, heatmapLeft + 3, deltaStripTop + 2);
        }
    }

    // Time labels
    ctx.font = '8px "JetBrains Mono", "SF Mono", monospace';
    ctx.fillStyle = 'rgba(120, 130, 155, 0.5)';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const labelInterval = Math.max(1, Math.floor(40 / COL_W));
    for (let col = 0; col < numCols; col += labelInterval) {
        const snap = displaySnaps[col];
        const t = new Date(snap.ts * 1000);
        const mm = t.getMinutes().toString().padStart(2, '0');
        const ss = t.getSeconds().toString().padStart(2, '0');
        ctx.fillText(`${t.getHours()}:${mm}:${ss}`, heatmapLeft + col * COL_W + COL_W / 2, cssH - 12);
    }

    const sepTop = Math.max(0, (topY || 0) - 10);
    const sepBot = Math.min(cssH, (botY || cssH) + 10);

    // ── LAYER 9: CURRENT STATE COLUMN ──
    if (LATEST) {
        const CSC_W = DOM2D_STATE.CURRENT_COL_WIDTH;
        const cscX = heatmapRight - CSC_W;

        ctx.fillStyle = 'rgba(8, 10, 20, 0.7)';
        ctx.fillRect(cscX, Math.min(topY || 0, botY || 0) - 2, CSC_W, Math.abs((botY || cssH) - (topY || 0)) + 4);

        let cscMaxDepth = 1;
        const allCscSizes = [];
        for (const s of Object.values(LATEST.bids)) { cscMaxDepth = Math.max(cscMaxDepth, s); if(s>0)allCscSizes.push(s); }
        for (const s of Object.values(LATEST.asks)) { cscMaxDepth = Math.max(cscMaxDepth, s); if(s>0)allCscSizes.push(s); }
        const cscMean = allCscSizes.length > 0 ? allCscSizes.reduce((a, b) => a + b, 0) / allCscSizes.length : 1;

        const cscCenter = cscX + CSC_W / 2, halfW = CSC_W / 2 - 1;

        for (const [priceStr, size] of Object.entries(LATEST.bids)) {
            const price = parseFloat(priceStr);
            if (isNaN(price) || price < visMin || price > visMax) continue;
            const bp = Math.floor(price / bucketSize) * bucketSize;
            const y = _priceToY(bp + bucketSize / 2);
            if (y === null || y < -rowH || y > cssH + rowH) continue;
            const norm = Math.min(size / cscMaxDepth, 1.0);
            const barW = norm * halfW;
            ctx.fillStyle = `rgba(0, 220, 140, ${(0.3 + norm * 0.6).toFixed(2)})`;
            ctx.fillRect(cscCenter - barW, y - rowH / 2 + 0.5, barW, rowH - 1);
            if (size >= cscMean * 1.5 && rowH >= 6) {
                ctx.font = '7px "JetBrains Mono", monospace'; ctx.fillStyle = `rgba(200, 255, 220, ${Math.min(0.5 + norm, 0.95).toFixed(2)})`;
                ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(size.toString(), cscCenter - barW - 2, y);
            }
        }

        for (const [priceStr, size] of Object.entries(LATEST.asks)) {
            const price = parseFloat(priceStr);
            if (isNaN(price) || price < visMin || price > visMax) continue;
            const bp = Math.floor(price / bucketSize) * bucketSize;
            const y = _priceToY(bp + bucketSize / 2);
            if (y === null || y < -rowH || y > cssH + rowH) continue;
            const norm = Math.min(size / cscMaxDepth, 1.0);
            const barW = norm * halfW;
            ctx.fillStyle = `rgba(240, 80, 60, ${(0.3 + norm * 0.6).toFixed(2)})`;
            ctx.fillRect(cscCenter, y - rowH / 2 + 0.5, barW, rowH - 1);
            if (size >= cscMean * 1.5 && rowH >= 6) {
                ctx.font = '7px "JetBrains Mono", monospace'; ctx.fillStyle = `rgba(255, 200, 190, ${Math.min(0.5 + norm, 0.95).toFixed(2)})`;
                ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(size.toString(), cscCenter + barW + 2, y);
            }
        }
    }

    // ── LAYER 10: Volume Profile Sidebar (O(Levels) reading from Cache) ──
    const VP_WIDTH = 22;
    const vpLeft = heatmapLeft - VP_WIDTH - 2;
    if (vpLeft > 0 && DOM2D_STATE._volumeProfile.size > 0) {
        let vpMax = 1;
        for (const v of DOM2D_STATE._volumeProfile.values()) vpMax = Math.max(vpMax, Math.max(0, v.bid) + Math.max(0, v.ask));

        for (const [priceStr, vol] of DOM2D_STATE._volumeProfile.entries()) {
            const price = parseFloat(priceStr);
            if (price < visMin || price > visMax) continue;
            const y = _priceToY(price + bucketSize / 2);
            if (y === null || y < 0 || y > cssH) continue;

            const bV = Math.max(0, vol.bid);
            const aV = Math.max(0, vol.ask);
            const total = bV + aV;
            if (total <= 0) continue;

            const totalNorm = total / vpMax;
            const barW = totalNorm * VP_WIDTH;
            const bidFrac = bV / total;

            const bidW = barW * bidFrac;
            ctx.fillStyle = 'rgba(0, 180, 160, 0.45)';
            ctx.fillRect(vpLeft + VP_WIDTH - barW, y - rowH / 2, bidW, rowH - 0.5);

            const askW = barW * (1 - bidFrac);
            ctx.fillStyle = 'rgba(220, 120, 30, 0.45)';
            ctx.fillRect(vpLeft + VP_WIDTH - askW, y - rowH / 2, askW, rowH - 0.5);
        }
    }

    // ── Tooltip Setup & Rendering ──
    if (!canvas._dom2dTooltipAttached) {
        canvas._dom2dTooltipAttached = true;
        canvas._dom2dHoverData = null;
        let tooltip = document.getElementById('dom2d-tooltip');
        if (!tooltip) {
            tooltip = document.createElement('div');
            tooltip.id = 'dom2d-tooltip';
            tooltip.style.cssText = `position:fixed;display:none;pointer-events:none;background:rgba(10,14,26,0.92);border:1px solid rgba(80,120,200,0.4);border-radius:4px;padding:5px 8px;font:10px "JetBrains Mono",monospace;color:rgba(200,210,230,0.9);z-index:9999;max-width:200px;backdrop-filter:blur(6px);box-shadow:0 2px 8px rgba(0,0,0,0.5);`;
            document.body.appendChild(tooltip);
        }
        canvas._dom2dTooltipRef = tooltip;
        if (!canvas._dom2dMouseMove) {
            canvas._dom2dMouseMove = (e) => {
                const rect = canvas.getBoundingClientRect();
                canvas._dom2dHoverData = { mx: e.clientX - rect.left, my: e.clientY - rect.top, clientX: e.clientX, clientY: e.clientY };
            };
            canvas._dom2dMouseLeave = () => {
                canvas._dom2dHoverData = null;
                if (canvas._dom2dTooltipRef) canvas._dom2dTooltipRef.style.display = 'none';
            };
            canvas.addEventListener('mousemove', canvas._dom2dMouseMove);
            canvas.addEventListener('mouseleave', canvas._dom2dMouseLeave);
        }
    }

    const hoverData = canvas._dom2dHoverData;
    const tooltip = canvas._dom2dTooltipRef;
    if (hoverData && tooltip && hoverData.mx >= heatmapLeft && hoverData.mx <= heatmapRight) {
        const col = Math.floor((hoverData.mx - heatmapLeft) / COL_W);
        if (col >= 0 && col < numCols) {
            const snap = displaySnaps[col];
            const hoverPrice = visMin + (1 - hoverData.my / cssH) * (visMax - visMin);
            const closestPrice = (Math.round(hoverPrice / bucketSize) * bucketSize).toFixed(2);
            const hasBid = snap.bids[closestPrice] !== undefined;
            const hasAsk = snap.asks[closestPrice] !== undefined;
            const closestDist = hasBid || hasAsk ? 0 : rowH * 3;

            if (closestDist < rowH * 2) {
                const bidSize = snap.bids[closestPrice] || 0, askSize = snap.asks[closestPrice] || 0;
                const tradeCount = (snap.trades || []).length;
                const absEntry = snap.absorption ? snap.absorption[closestPrice] : null;
                const t = new Date(snap.ts * 1000);
                const timeStr = `${t.getHours()}:${t.getMinutes().toString().padStart(2,'0')}:${t.getSeconds().toString().padStart(2,'0')}`;
                
                let html = `<div style="color:#8af">${parseFloat(closestPrice).toFixed(2)}</div><div>⏱ ${timeStr}</div>`;
                if (bidSize) html += `<div style="color:#0fb">BID: ${bidSize}</div>`;
                if (askSize) html += `<div style="color:#f84">ASK: ${askSize}</div>`;
                if (tradeCount) html += `<div style="color:#aaa">Fills: ${tradeCount}</div>`;
                if (absEntry) {
                    const score = absEntry.s || 0, waves = absEntry.w || 0;
                    if (score >= 2 && waves >= 2) html += `<div style="color:#88f">ABS ${score.toFixed(1)}x W${waves}</div>`;
                    else if (score >= 1) html += `<div style="color:#da0">HOLD ${score.toFixed(1)}x</div>`;
                    else if (score < 0.3 && (absEntry.sh || 0) >= 3) html += `<div style="color:#f44">CRACK -${absEntry.c || 0}</div>`;
                }
                tooltip.innerHTML = html; tooltip.style.display = 'block';

                const ttW = tooltip.getBoundingClientRect().width || 140, ttH = tooltip.getBoundingClientRect().height || 80;
                let ttLeft = hoverData.clientX + 12, ttTop = Math.max(4, hoverData.clientY - 10);
                if (ttLeft + ttW > window.innerWidth - 4) ttLeft = hoverData.clientX - ttW - 12;
                if (ttTop + ttH > window.innerHeight - 4) ttTop = window.innerHeight - ttH - 4;
                tooltip.style.left = ttLeft + 'px'; tooltip.style.top = ttTop + 'px';
            } else { tooltip.style.display = 'none'; }
        } else { tooltip.style.display = 'none'; }
    } else if (tooltip) { tooltip.style.display = 'none'; }

    ctx.restore();
}

let _dom2dRafPending = false;
let _dom2dRafArgs = null;
function renderDomHeatmap2DThrottled(canvas, priceToY, midPrice, chartContext) {
    if (window._chartScrolling) return; // skip during scroll
    _dom2dRafArgs = [canvas, priceToY, midPrice, chartContext];
    if (_dom2dRafPending) return;
    _dom2dRafPending = true;
    requestAnimationFrame(() => {
        _dom2dRafPending = false;
        if (window._chartScrolling) return; // double-check
        if (_dom2dRafArgs) { renderDomHeatmap2D(..._dom2dRafArgs); _dom2dRafArgs = null; }
    });
}

// ═══════════════════════════════════════════════════════════════════════════
// EXPORTS
// ═══════════════════════════════════════════════════════════════════════════
window.startDomHistory = startDomHistory;
window.stopDomHistory = stopDomHistory;
window.renderDomHeatmap2D = renderDomHeatmap2DThrottled;
window.HeatmapSettings = HeatmapSettings;
window.DOM2D_STATE = DOM2D_STATE;

})(); // end IIFE
