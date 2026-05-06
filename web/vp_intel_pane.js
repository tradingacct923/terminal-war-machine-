/**
 * VP Intel Pane v2 — Unified Bar Architecture
 *
 * Each price level = ONE bar with 4 visual layers:
 *   Layer 1: DOM depth (translucent, leftmost)
 *   Layer 2: Traded volume (solid buy/sell, after depth)
 *   Layer 3: Absorption border (edge glow brightness)
 *   Layer 4: Exhaustion gradient (opacity fade direction)
 *
 * Plus: aggression heatmap (saturation), delta arrows, POC/VAH/VAL, bid/ask bar
 *
 * Zero text labels. Zero thresholds. Pure visual encoding.
 */
(function() {
'use strict';

// ── Configurable colors + settings ──
const _cfg = {
    buyColor:    [0, 230, 118],       // vivid green — unmistakable
    sellColor:   [255, 23, 68],       // vivid red — unmistakable
    bidColor:    [0, 200, 255],       // electric cyan — DOM bids
    askColor:    [255, 64, 129],      // hot pink — DOM asks
    pocColor:    'rgba(255,193,7,0.9)', // bright amber POC
    vaColor:     'rgba(33,150,243,0.6)', // material blue VA
    barOpacity:  0.85,                // HIGH opacity — bars must be solid
    depthOpacity: 0.45,              // depth clearly visible behind bars
    borderMax:   1.0,                // max border brightness
    barWidthPct: 0.65,
    barHeight:   5,
    showDepth:   true,
    showDeltas:  true,
    showAbsBorder: true,
    showExhGradient: true,
    showAggression: true,
    showBidAskBar: true,
    showAbsLabel: true,    // FORTRESS/SOLID/HELD text label + refill dot
    showHVNLVN:   true,    // HVN/LVN badges
    showNumbers:  true,    // vol + delta numbers on key bars
    showWall:     true,    // WALL badge when DOM >> average
    showZoneBand: true,    // vertical absorption zone tint
    showNakedPoc: true,    // naked POC horizontal dashed lines
    useKDE:       true,    // KDE density for bar brightness
    showPD:       true,    // prior-day POC/VAH/VAL dotted lines (dedup vs session)
    showZoneLabel:true,    // price-range label on absorption zone band
    showDevTrail: true,    // developing POC migration trail (fading dots)
    showStateGlyph:true,   // DEF/CONSUMED/FRESH state glyph per level
    hvnLvnFallback:true,   // use KDE rank as implicit HVN/LVN when none detected
    showWallBands: true,   // call_wall / put_wall horizontal band tint + γ-flip dashed line
    showConfluence: true,  // MAGNET diamond when session/PD/weekly POCs align
    confluenceTicks: 3,    // alignment tolerance in ticks
    showZScore: false,     // cross-sectional z-score coloring (overrides aggression sat)
    showMiniLadder: false, // right-edge 5+5 DOM heatmap strip
    miniLadderWidth: 80,
    showLegend: false,     // ? popup cheat-sheet
    lockToMid: false,      // keep current NQ mid centered during zoom
};

let _canvas = null, _ctx = null, _slotEl = null;
let _raf = 0, _destroyed = false;
let _priceTop = 0, _priceBottom = 0;
let _settingsPanel = null;
let _toolbar = null;
// TF/step state drives which profile VP Intel reads from the overlay cache.
// Overlay is shared with chart-layer VP; ensureProfileActive adds to (not
// replaces) the active set so enabling "weekly" here doesn't evict session.
let _mode = 'session', _step = '';
// User zoom/pan — independent of chart. When non-null, the pane stops
// following ChartCore and locks to this window. Data (levels, POC, etc.)
// is unaffected; we only change the y-axis mapping via _priceToY.
let _userPriceTop = null, _userPriceBottom = null;
let _isPanning = false, _panStartY = 0, _panStartTop = 0, _panStartBot = 0;
// Dealer-positioning context pulled from /api/walls + zone_update events.
// Shapes the canvas (wall bands, γ-flip line) so VP Intel becomes decision-ready.
let _wallData = null;
let _wallUnsub = null;
// Auto-fit guard — fire once when data is sparse on screen.
let _didAutoFit = false;
// Hover tooltip state
let _tooltipEl = null, _lastHoverLevel = null, _lastHoverTs = 0;
let _lastLevels = null, _levelYs = null;
// Legend popup element
let _legendEl = null;
// Live cross-sectional z-score scale (Welford, recomputed per render)
let _zStd = 0;

function _rgba(rgb, a) { return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`; }

function _priceToY(price, h) {
    if (_priceTop <= _priceBottom) return null;
    return h - ((price - _priceBottom) / (_priceTop - _priceBottom)) * h;
}

function _syncPriceRange() {
    // User zoom/pan takes precedence — lock to custom window.
    if (_userPriceTop != null && _userPriceBottom != null && _userPriceTop > _userPriceBottom) {
        _priceTop = _userPriceTop;
        _priceBottom = _userPriceBottom;
        return true;
    }
    if (typeof ChartCore !== 'undefined') {
        const inst = ChartCore.getInstances();
        if (inst.length && inst[0].candleSeries && _canvas) {
            const h = _canvas.height / (window.devicePixelRatio || 1);
            try {
                const t = inst[0].candleSeries.coordinateToPrice(0);
                const b = inst[0].candleSeries.coordinateToPrice(h);
                if (t > b && !isNaN(t) && !isNaN(b)) { _priceTop = t; _priceBottom = b; return true; }
            } catch(e) {}
        }
    }
    if (typeof VolumeProfileOverlay !== 'undefined') {
        const p = VolumeProfileOverlay.getProfiles();
        const sd = p[_mode]?.data;
        if (sd?.levels?.length > 0) {
            const px = sd.levels.map(l => l.price);
            _priceTop = Math.max(...px) + 5; _priceBottom = Math.min(...px) - 5;
            return true;
        }
    }
    return false;
}

function _subscribeWallData() {
    // Live push from schwab_bridge GEX engine.
    // FIX 2026-05-04: AltarisEvents.on() returns undefined — store handler ref
    // and build an off()-bound unsub closure so destroy() actually detaches.
    // Prior bug: _wallUnsub was always undefined, every remount stacked another
    // live listener that mutated _wallData forever.
    if (typeof window.AltarisEvents !== 'undefined' && window.AltarisEvents.on) {
        const _wallHandler = (d) => {
            if (_destroyed || !d) return;
            _wallData = {
                call_wall:  d.call_wall ?? d.underlying_call_wall ?? null,
                put_wall:   d.put_wall  ?? d.underlying_put_wall  ?? null,
                gamma_flip: d.gamma_flip ?? d.underlying_gamma_flip ?? null,
            };
            _lastRenderSig = '';
        };
        window.AltarisEvents.on('data:zone:update', _wallHandler);
        _wallUnsub = () => {
            try { window.AltarisEvents.off('data:zone:update', _wallHandler); } catch (_) {}
        };
    }
    // REST seed so bands/line paint before first WS tick arrives.
    if (typeof window.authFetch === 'function') {
        window.authFetch('/api/walls?symbol=NQ')
            .then(r => r.json())
            .then(d => {
                if (_destroyed || !d || d.error) return;
                _wallData = {
                    call_wall:  d.call_wall ?? d.underlying_call_wall ?? null,
                    put_wall:   d.put_wall  ?? d.underlying_put_wall  ?? null,
                    gamma_flip: d.gamma_flip ?? d.underlying_gamma_flip ?? null,
                };
                _lastRenderSig = '';
            })
            .catch(() => {});
    }
}

function _fitToData() {
    if (typeof VolumeProfileOverlay === 'undefined') return false;
    const sd = VolumeProfileOverlay.getProfiles()?.[_mode]?.data;
    if (!sd?.levels?.length) return false;
    const px = sd.levels.map(l => l.price);
    const hi = Math.max(...px), lo = Math.min(...px);
    const pad = Math.max(0.10 * (hi - lo), 1);
    _userPriceTop = hi + pad;
    _userPriceBottom = lo - pad;
    _lastRenderSig = '';
    return true;
}

function _render() {
    if (!_canvas || !_ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = _canvas.getBoundingClientRect();
    const w = rect.width, h = rect.height;
    if (w <= 0 || h <= 0) return;

    if (_canvas.width !== Math.round(w * dpr) || _canvas.height !== Math.round(h * dpr)) {
        _canvas.width = Math.round(w * dpr);
        _canvas.height = Math.round(h * dpr);
    }

    _ctx.save();
    _ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    _ctx.clearRect(0, 0, w, h);
    _ctx.fillStyle = '#06090e';
    _ctx.fillRect(0, 0, w, h);

    if (!_syncPriceRange()) {
        _ctx.fillStyle = 'rgba(140,160,200,0.25)';
        _ctx.font = '10px "JetBrains Mono", monospace';
        _ctx.textAlign = 'center';
        _ctx.fillText('VP INTEL', w / 2, h / 2);
        _ctx.restore(); return;
    }

    if (typeof VolumeProfileOverlay === 'undefined') { _ctx.restore(); return; }
    const profiles = VolumeProfileOverlay.getProfiles();
    const domData = VolumeProfileOverlay.getDOM();
    const sd = profiles[_mode]?.data;
    if (!sd?.levels?.length) {
        _ctx.fillStyle = 'rgba(140,160,200,0.2)';
        _ctx.font = '10px "JetBrains Mono", monospace';
        _ctx.textAlign = 'center';
        _ctx.fillText('waiting for data...', w / 2, h / 2);
        _ctx.restore(); return;
    }

    const levels = sd.levels;
    const maxVol = Math.max(...levels.map(l => l.total));
    const poc = sd.poc, vah = sd.vah, val = sd.val;
    const M = 4;
    // Right edge reserves room for the mini-ladder strip when enabled.
    // All right-anchored labels/grid use rightEdge instead of w.
    const rightEdge = _cfg.showMiniLadder ? (w - _cfg.miniLadderWidth) : w;
    const BAR_W = rightEdge * _cfg.barWidthPct;

    // Compute actual pixels per tick from price range
    const tick = levels.length >= 2 ? Math.abs(levels[1].price - levels[0].price) : 0.25;
    const pxPerTick = Math.abs((_priceToY(0, h) || 0) - (_priceToY(tick, h) || 0));
    // Bar height: user slider caps it (1–8 px). Default 5. Falls back to pxPerTick-1 when
    // the tick space is tighter than the user's preference (keeps bars from overlapping).
    const _barHeightPref = (_cfg.barHeight != null) ? _cfg.barHeight : 5;
    const BH = Math.max(2, Math.min(_barHeightPref, Math.floor(pxPerTick) - 1 || _barHeightPref));

    // ── Precompute percentile ranks for absorption borders ──
    const absRatios = levels.filter(l => (l.abs_ratio || 0) > 0).map(l => l.abs_ratio).sort((a, b) => a - b);
    const absRank = (v) => {
        if (absRatios.length === 0 || v <= 0) return 0;
        let idx = 0;
        for (let i = 0; i < absRatios.length; i++) { if (absRatios[i] <= v) idx = i; }
        return idx / Math.max(absRatios.length - 1, 1);
    };

    // ── DOM depth max for scaling ──
    let maxDepth = 0;
    if (_cfg.showDepth && domData.dom) {
        for (const sz of Object.values(domData.dom.bids || {})) maxDepth = Math.max(maxDepth, sz);
        for (const sz of Object.values(domData.dom.asks || {})) maxDepth = Math.max(maxDepth, sz);
    }

    // ── Data-driven thresholds (no magic numbers) ──
    const _visRange = _priceTop - _priceBottom;          // visible price range
    const _nearDist = _visRange * 0.1;                   // "near" = 10% of visible
    const _midDist = _visRange * 0.3;                    // "mid" = 30% of visible
    const _farDist = _visRange * 0.8;                    // "far" = 80% of visible
    const _allExh = levels.filter(l => l.exh !== undefined).map(l => Math.abs(l.exh)).sort((a, b) => a - b);
    const _exhP75 = _allExh.length > 0 ? _allExh[Math.floor(_allExh.length * 0.75)] : 0.25;
    const _exhThresh = Math.max(_exhP75, 0.15);          // exhaustion trigger = P75 of abs(exh)
    const _deltaMin = maxDepth > 0 ? Math.max(maxDepth * 0.05, 1) : 2; // depth delta minimum = 5% of max depth
    const _volFloor = maxVol * 0.003;                    // skip bars below 0.3% of max vol

    // ── Average DOM depth for WALL badge threshold ──
    let _avgDepth = 0, _depthN = 0;
    if (_cfg.showWall && domData.dom) {
        for (const sz of Object.values(domData.dom.bids || {})) { _avgDepth += sz; _depthN++; }
        for (const sz of Object.values(domData.dom.asks || {})) { _avgDepth += sz; _depthN++; }
        _avgDepth = _depthN > 0 ? _avgDepth / _depthN : 5;
    }
    const _wallThr = Math.max(8, _avgDepth * 3);

    // ── KDE brightness availability ──
    const _hasKDE = levels.some(l => l.kde !== undefined);

    // ── Absorption zone detection (vertical tint across adjacent high-abs levels) ──
    const _absZones = [];
    if (_cfg.showZoneBand) {
        const _bkAbs = levels.filter(l => (l.abs_ratio || 0) > 0).map(l => l.abs_ratio).sort((a,b) => a-b);
        const _bkAbsP70 = _bkAbs.length > 0 ? _bkAbs[Math.floor(_bkAbs.length * 0.70)] : 999;
        let cur = null;
        const _ztick = tick || 0.25;
        const _sortedLv = [...levels].sort((a,b) => a.price - b.price);
        for (const lv of _sortedLv) {
            if ((lv.abs_ratio || 0) >= _bkAbsP70) {
                if (cur && lv.price - cur.hi <= _ztick * 3) {
                    cur.hi = lv.price;
                    cur.maxAbs = Math.max(cur.maxAbs, lv.abs_ratio);
                    cur.levels++;
                } else {
                    if (cur && cur.levels >= 2) _absZones.push(cur);
                    cur = { lo: lv.price, hi: lv.price, maxAbs: lv.abs_ratio, levels: 1 };
                }
            } else {
                if (cur && cur.levels >= 2) _absZones.push(cur);
                cur = null;
            }
        }
        if (cur && cur.levels >= 2) _absZones.push(cur);
    }
    // Render zone bands BEFORE bars so they sit behind
    for (const z of _absZones) {
        const yLo = _priceToY(z.lo, h);
        const yHi = _priceToY(z.hi, h);
        if (yLo == null || yHi == null) continue;
        const top = Math.min(yLo, yHi);
        const bandH = Math.max(Math.abs(yLo - yHi), 4);
        const zColor = z.maxAbs >= 50 ? [40,255,180] : z.maxAbs >= 30 ? [40,220,160] : [140,200,220];
        _ctx.fillStyle = _rgba(zColor, 0.06);
        _ctx.fillRect(0, top, rightEdge, bandH);
        _ctx.strokeStyle = _rgba(zColor, 0.25);
        _ctx.lineWidth = 0.5;
        _ctx.strokeRect(0, top, rightEdge, bandH);
        // Zone price-range label (e.g. "26461–26486")
        if (_cfg.showZoneLabel && bandH >= 10) {
            const rangeTxt = `${z.lo.toFixed(2)}–${z.hi.toFixed(2)}`;
            _ctx.save();
            _ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const rtm = _ctx.measureText(rangeTxt);
            const rx = rightEdge - rtm.width - 42;
            const ry = top + bandH / 2;
            _ctx.fillStyle = 'rgba(0,0,0,0.68)';
            _ctx.fillRect(rx - 2, ry - 5, rtm.width + 5, 10);
            _ctx.fillStyle = _rgba(zColor, 0.9);
            _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
            _ctx.fillText(rangeTxt, rx, ry);
            _ctx.restore();
        }
    }

    // ── Dealer walls + γ-flip (rendered after zones, before bars) ──
    if (_cfg.showWallBands && _wallData) {
        const _zt = tick || 0.25;
        const _drawBand = (px, rgb, label) => {
            if (px == null) return;
            const yMid = _priceToY(px, h);
            if (yMid == null) return;
            const bandHalf = _zt * 1.5;
            const yHi = _priceToY(px + bandHalf, h);
            const yLo = _priceToY(px - bandHalf, h);
            if (yHi == null || yLo == null) return;
            const top = Math.min(yHi, yLo);
            const band = Math.max(Math.abs(yLo - yHi), 3);
            _ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.08)`;
            _ctx.fillRect(0, top, rightEdge, band);
            _ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.45)`;
            _ctx.lineWidth = 0.6;
            _ctx.beginPath(); _ctx.moveTo(0, yMid); _ctx.lineTo(rightEdge, yMid); _ctx.stroke();
            _ctx.save();
            _ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const lbl = `${label} ${px.toFixed(2)}`;
            const ltm = _ctx.measureText(lbl);
            _ctx.fillStyle = 'rgba(0,0,0,0.7)';
            _ctx.fillRect(2, yMid - 5, ltm.width + 5, 10);
            _ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.95)`;
            _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
            _ctx.fillText(lbl, 5, yMid);
            _ctx.restore();
        };
        _drawBand(_wallData.call_wall, [255, 80, 90],  'CW');   // red call wall
        _drawBand(_wallData.put_wall,  [80, 220, 130], 'PW');   // green put wall
        // γ-flip — cyan dashed horizontal
        if (_wallData.gamma_flip != null) {
            const gy = _priceToY(_wallData.gamma_flip, h);
            if (gy != null && gy >= 0 && gy <= h) {
                _ctx.save();
                _ctx.strokeStyle = 'rgba(80,210,255,0.8)';
                _ctx.lineWidth = 1;
                _ctx.setLineDash([5, 4]);
                _ctx.beginPath(); _ctx.moveTo(0, gy); _ctx.lineTo(rightEdge, gy); _ctx.stroke();
                _ctx.setLineDash([]);
                _ctx.font = 'bold 7px "JetBrains Mono", monospace';
                const gTxt = `γ FLIP ${_wallData.gamma_flip.toFixed(2)}`;
                const gtm = _ctx.measureText(gTxt);
                _ctx.fillStyle = 'rgba(0,0,0,0.72)';
                _ctx.fillRect(2, gy - 5, gtm.width + 5, 10);
                _ctx.fillStyle = 'rgba(140,230,255,0.95)';
                _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
                _ctx.fillText(gTxt, 5, gy);
                _ctx.restore();
            }
        }
    }

    // ── Z-score scale (Welford single-pass over delta) ──
    if (_cfg.showZScore) {
        let mean = 0, m2 = 0, n = 0;
        for (const lv of levels) {
            const d = (lv.buy || 0) - (lv.sell || 0);
            n++;
            const delta = d - mean;
            mean += delta / n;
            m2 += delta * (d - mean);
        }
        _zStd = n > 1 ? Math.sqrt(m2 / (n - 1)) : 0;
    } else {
        _zStd = 0;
    }

    let _lastAbsLabelY = -Infinity;
    let _lastNumY = -Infinity;
    // y-index for O(log n) hover hit-test (filled inside bar loop).
    const _yBuf = new Float32Array(levels.length);
    let _yBufN = 0;

    // ── Render each price level as one unified bar ──
    for (const lv of levels) {
        const y = _priceToY(lv.price, h);
        if (y == null || y < -BH || y > h + BH) continue;
        const volRatio = lv.total / maxVol;
        if (lv.total < _volFloor) continue;

        const ps = lv.price.toFixed(2);
        const delta = (lv.buy || 0) - (lv.sell || 0);
        const buyRatio = lv.total > 0 ? lv.buy / lv.total : 0.5;
        const sellRatio = 1 - buyRatio;
        const barY = Math.round(y - BH / 2);

        // KDE density-driven brightness (fallback to volRatio)
        const density = _hasKDE ? (lv.kde || 0) : volRatio;
        const kdeAlpha = _cfg.useKDE && _hasKDE ? (0.20 + density * 0.70) : 1.0;
        const isPOC = poc && Math.abs(lv.price - poc) < 0.01;
        const isHVN = !!lv.hvn;
        const isLVN = !!lv.lvn;
        const inVA = (val != null && vah != null && lv.price >= val && lv.price <= vah);

        // Track y for hover hit-test.
        _yBuf[_yBufN++] = y;

        // Aggression: balanced = slightly muted, one-sided = full vivid
        const aggression = lv.total > 0 ? Math.abs(delta) / lv.total : 0;
        let satMult = _cfg.showAggression ? (0.6 + aggression * 0.4) : 0.8;
        if (_cfg.showZScore && _zStd > 0) {
            // Cross-sectional z — saturate by statistical significance instead of raw magnitude.
            const z = Math.min(Math.abs(delta) / _zStd, 3);
            satMult = 0.4 + 0.6 * (z / 3);
        }

        // Absorption border rank
        const absP = _cfg.showAbsBorder ? absRank(lv.abs_ratio || 0) : 0;

        // Exhaustion gradient
        let exhAlphaL = _cfg.barOpacity, exhAlphaR = _cfg.barOpacity;
        if (_cfg.showExhGradient && lv.exh !== undefined) {
            if (lv.exh < -_exhThresh) {
                // Exhausting: fade out on right side
                exhAlphaR = _cfg.barOpacity * Math.max(0.2, 1 + lv.exh);
            } else if (lv.exh > _exhThresh) {
                // Strengthening: fade out on left side (building up)
                exhAlphaL = _cfg.barOpacity * Math.max(0.2, 1 - lv.exh * 0.5);
            }
        }

        let xCursor = M;

        // ── Layer 1: DOM depth (translucent, leftmost) ──
        if (_cfg.showDepth && maxDepth > 0 && domData.dom) {
            // Try multiple key formats for DOM lookup
            const _p = lv.price;
            const bidD = domData.dom.bids?.[ps] || domData.dom.bids?.[String(_p)] || domData.dom.bids?.[_p.toFixed(1)] || 0;
            const askD = domData.dom.asks?.[ps] || domData.dom.asks?.[String(_p)] || domData.dom.asks?.[_p.toFixed(1)] || 0;
            const totalD = bidD + askD;
            if (totalD > 0) {
                const depthW = (totalD / maxDepth) * BAR_W * 0.4;
                const isBidSide = bidD >= askD;
                const depthColor = isBidSide ? _cfg.bidColor : _cfg.askColor;
                // Strong opacity so depth is clearly visible
                const dAlpha = _cfg.depthOpacity * (0.5 + (totalD / maxDepth) * 0.5);
                _ctx.fillStyle = _rgba(depthColor, dAlpha);
                _ctx.fillRect(xCursor, barY, depthW, BH);
                xCursor += depthW;
            }
        }

        // ── Layer 2: Traded volume (buy green + sell red) ──
        const tradedW = volRatio * BAR_W * 0.6;
        const buyW = tradedW * buyRatio;
        const sellW = tradedW * sellRatio;

        // Use gradient for exhaustion effect; KDE density modulates alpha
        if (buyW > 0.5) {
            const buyAlpha = Math.min(exhAlphaL, exhAlphaR) * satMult * kdeAlpha;
            _ctx.fillStyle = _rgba(_cfg.buyColor, buyAlpha);
            _ctx.fillRect(xCursor, barY, buyW, BH);
        }
        if (sellW > 0.5) {
            const sellAlpha = Math.min(exhAlphaL, exhAlphaR) * satMult * kdeAlpha;
            _ctx.fillStyle = _rgba(_cfg.sellColor, sellAlpha);
            _ctx.fillRect(xCursor + buyW, barY, sellW, BH);
        }

        const totalBarW = xCursor - M + buyW + sellW;

        // ── Layer 3: Absorption border ──
        // Top 20% of absorption levels get visible borders
        if (_cfg.showAbsBorder && absP > 0.8 && totalBarW > 3) {
            const borderAlpha = Math.min((absP - 0.8) * 4.0, 1.0) * _cfg.borderMax;
            const borderColor = delta >= 0 ? _cfg.bidColor : _cfg.askColor;
            _ctx.strokeStyle = _rgba(borderColor, borderAlpha);
            const bThick = absP > 0.95 ? 2.0 : 1.0;
            _ctx.lineWidth = bThick;
            _ctx.strokeRect(M - 1, barY - 1, totalBarW + 2, BH + 2);
            // Top 5% — glow behind bar
            if (absP > 0.95) {
                _ctx.fillStyle = _rgba(borderColor, 0.08);
                _ctx.fillRect(M - 3, barY - 3, totalBarW + 6, BH + 6);
            }
        }

        // ── Layer 4: Exhaustion indicator ──
        if (_cfg.showExhGradient && lv.exh !== undefined && Math.abs(lv.exh) > _exhThresh && totalBarW > 3) {
            const tickW = Math.max(BH, 3); // tick scales with bar height
            const exhX = M + totalBarW + 1;
            if (lv.exh < -_exhThresh) {
                // Exhausting — red mark + fade on bar
                const intensity = Math.min(0.9, 0.3 + Math.abs(lv.exh) * 0.5);
                _ctx.fillStyle = `rgba(255,60,60,${intensity})`;
                _ctx.fillRect(exhX, barY, tickW, BH);
                // Fade right 30% of bar darker
                const fadeW = totalBarW * 0.3;
                _ctx.fillStyle = `rgba(0,0,0,${Math.min(0.4, Math.abs(lv.exh) * 0.3)})`;
                _ctx.fillRect(M + totalBarW - fadeW, barY, fadeW, BH);
            } else if (lv.exh > _exhThresh) {
                // Strengthening — green mark
                const intensity = Math.min(0.9, 0.3 + lv.exh * 0.5);
                _ctx.fillStyle = `rgba(40,255,140,${intensity})`;
                _ctx.fillRect(exhX, barY, tickW, BH);
            }
        }

        // ── FORTRESS/SOLID/HELD text label + refill class dot ──
        const absR = lv.abs_ratio || 0;
        let absLabel = '', absRGB = null;
        if (absR >= 50)      { absLabel = 'FORTRESS'; absRGB = [40,255,180]; }
        else if (absR >= 30) { absLabel = 'SOLID';    absRGB = [40,220,160]; }
        else if (absR >= 15) { absLabel = 'HELD';     absRGB = [140,200,220]; }
        let _labelEndX = M + totalBarW + 16; // track rightmost edge so other badges stack
        if (_cfg.showAbsLabel && absLabel && totalBarW > 4 && Math.abs(y - _lastAbsLabelY) >= 11) {
            _ctx.save();
            _ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const tm = _ctx.measureText(absLabel);
            const badgeW = tm.width + 6;
            const bx = M + totalBarW + 4;
            // Pill bg + border
            _ctx.fillStyle = 'rgba(0,0,0,0.75)';
            _ctx.fillRect(bx - 1, y - 5, badgeW, 10);
            _ctx.strokeStyle = _rgba(absRGB, 0.85);
            _ctx.lineWidth = 0.75;
            _ctx.strokeRect(bx - 1, y - 5, badgeW, 10);
            _ctx.fillStyle = _rgba(absRGB, 0.95);
            _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
            _ctx.fillText(absLabel, bx + 2, y);
            // Refill class dot
            if (lv.refill_class) {
                const rc = lv.refill_class;
                const dotColor = rc === 'instant' ? 'rgba(40,255,140,0.95)' :
                                 rc === 'fast'    ? 'rgba(255,220,40,0.9)'  :
                                                    'rgba(255,80,80,0.85)';
                _ctx.fillStyle = dotColor;
                _ctx.beginPath();
                _ctx.arc(bx + badgeW + 3, y, 2.2, 0, Math.PI * 2);
                _ctx.fill();
            }
            _labelEndX = bx + badgeW + 10;
            _ctx.restore();
            _lastAbsLabelY = y;
        }

        // ── HVN / LVN dots (with KDE-rank fallback when formal peaks are sparse) ──
        const _isHVNEff = isHVN || (_cfg.hvnLvnFallback && density >= 0.90 && !isLVN);
        const _isLVNEff = isLVN || (_cfg.hvnLvnFallback && _hasKDE && density <= 0.05 && lv.total > 0 && !isPOC);
        if (_cfg.showHVNLVN && (_isHVNEff || _isLVNEff) && totalBarW > 3) {
            const rawConv = _isHVNEff ? (lv.hvn || (isHVN ? 0 : density))
                                      : (lv.lvn || (isLVN ? 0 : 1 - density));
            const conv = Math.min(rawConv, 1);
            // Fallback dots get slightly lower opacity to distinguish from formal peaks
            const isFallback = (_isHVNEff && !isHVN) || (_isLVNEff && !isLVN);
            const baseAlpha = isFallback ? 0.35 : 0.55;
            _ctx.save();
            _ctx.fillStyle = _isHVNEff ? `rgba(40,220,255,${baseAlpha + conv * 0.40})` : `rgba(255,200,60,${baseAlpha + conv * 0.40})`;
            _ctx.beginPath();
            _ctx.arc(_labelEndX, y, Math.max(2.2, BH * 0.5), 0, Math.PI * 2);
            _ctx.fill();
            _labelEndX += 8;
            _ctx.restore();
        }

        // ── State glyph: DEF (▲ defended), CONSUMED (▽ ammo spent), FRESH (◆ untested wall) ──
        if (_cfg.showStateGlyph && lv.state && lv.state !== 'AIR' && totalBarW > 4) {
            const glyphMap = {
                DEF:      { ch: '\u25B2', rgb: [40, 255, 180] },  // ▲ bright green — defended
                CONSUMED: { ch: '\u25BD', rgb: [200, 140, 255] }, // ▽ purple — ammo spent
                FRESH:    { ch: '\u25C6', rgb: [255, 200, 40] },  // ◆ amber — untested wall
            };
            const g = glyphMap[lv.state];
            if (g) {
                _ctx.save();
                _ctx.font = 'bold 9px "JetBrains Mono", monospace';
                _ctx.fillStyle = _rgba(g.rgb, 0.85);
                _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
                _ctx.fillText(g.ch, _labelEndX, y);
                _labelEndX += 11;
                _ctx.restore();
            }
        }

        // ── WALL badge: resting depth >> average ──
        if (_cfg.showWall && domData.dom) {
            const _pVal = lv.price;
            const _bidD = domData.dom.bids?.[ps] || domData.dom.bids?.[String(_pVal)] || domData.dom.bids?.[_pVal.toFixed(1)] || 0;
            const _askD = domData.dom.asks?.[ps] || domData.dom.asks?.[String(_pVal)] || domData.dom.asks?.[_pVal.toFixed(1)] || 0;
            if ((_bidD + _askD) >= _wallThr) {
                _ctx.save();
                _ctx.font = 'bold 7px "JetBrains Mono", monospace';
                const wTm = _ctx.measureText('WALL');
                const wx = _labelEndX;
                _ctx.fillStyle = 'rgba(0,0,0,0.75)';
                _ctx.fillRect(wx - 1, y - 5, wTm.width + 6, 10);
                _ctx.strokeStyle = 'rgba(255,200,40,0.9)';
                _ctx.lineWidth = 0.75;
                _ctx.strokeRect(wx - 1, y - 5, wTm.width + 6, 10);
                _ctx.fillStyle = 'rgba(255,200,40,0.95)';
                _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
                _ctx.fillText('WALL', wx + 2, y);
                _ctx.restore();
            }
        }

        // ── Contract numbers (vol + delta) on key levels ──
        if (_cfg.showNumbers && totalBarW > 20 && BH >= 4 && Math.abs(y - _lastNumY) >= 9) {
            const showIt = (_hasKDE ? (density >= 0.40 || isPOC || isHVN || isLVN) : volRatio >= 0.30);
            if (showIt) {
                _ctx.save();
                _ctx.font = `${isPOC || isHVN || isLVN ? 'bold ' : ''}7px "JetBrains Mono", monospace`;
                _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
                const vol = Math.round(lv.total);
                const volTxt = vol >= 1000 ? `${(vol/1000).toFixed(1)}k` : `${vol}`;
                const dTxt = delta >= 0 ? `+${Math.round(delta)}` : `${Math.round(delta)}`;
                const txtX = M + 3;
                // Small shadow for readability over bar
                _ctx.fillStyle = 'rgba(0,0,0,0.55)';
                const _m1 = _ctx.measureText(volTxt + ' ' + dTxt);
                _ctx.fillRect(txtX - 1, y - 4, _m1.width + 2, 8);
                // Vol (density-scaled)
                const vCol = isPOC ? 'rgba(232,184,48,0.95)' :
                             isHVN ? 'rgba(40,220,255,0.9)' :
                             isLVN ? 'rgba(255,200,60,0.85)' :
                             `rgba(220,225,240,${0.55 + density * 0.40})`;
                _ctx.fillStyle = vCol;
                _ctx.fillText(volTxt, txtX, y);
                const volW = _ctx.measureText(volTxt + ' ').width;
                // Delta (colored)
                _ctx.fillStyle = delta > 0 ? 'rgba(30,210,150,0.9)' :
                                 delta < 0 ? 'rgba(240,60,80,0.9)' :
                                             'rgba(160,170,190,0.6)';
                _ctx.fillText(dTxt, txtX + volW, y);
                _ctx.restore();
                _lastNumY = y;
            }
        }
    }

    // Stash for hit-test (binary search needs monotonic y — price-sorted ascending ⇒ y descending).
    _lastLevels = levels;
    _levelYs = _yBuf;  // already length == levels.length; _yBufN used below if needed

    // ── Naked POCs (horizontal dashed lines with age badge) ──
    if (_cfg.showNakedPoc && typeof VolumeProfileOverlay !== 'undefined' && typeof VolumeProfileOverlay.getNakedPocs === 'function') {
        const naked = VolumeProfileOverlay.getNakedPocs() || [];
        for (const np of naked) {
            if (!np || np.price == null) continue;
            const ny = _priceToY(np.price, h);
            if (ny == null || ny < 0 || ny > h) continue;
            _ctx.save();
            _ctx.strokeStyle = 'rgba(200,160,255,0.55)';
            _ctx.lineWidth = 0.75;
            _ctx.setLineDash([3, 3]);
            _ctx.beginPath(); _ctx.moveTo(0, ny); _ctx.lineTo(rightEdge, ny); _ctx.stroke();
            _ctx.setLineDash([]);
            // Age badge
            const ageTxt = `nPOC ${np.price.toFixed(2)}${np.age_days != null ? ` · ${np.age_days}d` : ''}`;
            _ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const ntm = _ctx.measureText(ageTxt);
            _ctx.fillStyle = 'rgba(0,0,0,0.7)';
            _ctx.fillRect(2, ny - 6, ntm.width + 6, 12);
            _ctx.fillStyle = 'rgba(210,180,255,0.9)';
            _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
            _ctx.fillText(ageTxt, 5, ny);
            _ctx.restore();
        }
    }

    // No text labels. Bars + depth + borders + fades speak for themselves.

    // ── Dev POC migration trail — fading dots showing POC drift across session ──
    if (_cfg.showDevTrail && typeof VolumeProfileOverlay.getDevPocPath === 'function') {
        const path = VolumeProfileOverlay.getDevPocPath() || [];
        if (path.length >= 2) {
            const n = path.length;
            // Anchor trail to right side of pane; trail width = 25% of pane
            const trailW = Math.min(rightEdge * 0.25, 120);
            const trailX0 = rightEdge - trailW - 6;
            const trailX1 = rightEdge - 6;
            _ctx.save();
            // Connecting line beneath dots
            _ctx.strokeStyle = 'rgba(232,184,48,0.25)';
            _ctx.lineWidth = 0.75;
            _ctx.setLineDash([]);
            _ctx.beginPath();
            let started = false;
            for (let i = 0; i < n; i++) {
                const pt = path[i];
                const py = _priceToY(pt.poc, h);
                if (py == null || py < 0 || py > h) continue;
                const px = trailX0 + (i / (n - 1)) * (trailX1 - trailX0);
                if (!started) { _ctx.moveTo(px, py); started = true; }
                else { _ctx.lineTo(px, py); }
            }
            _ctx.stroke();
            // Dots — fading from oldest (dim) to newest (bright)
            for (let i = 0; i < n; i++) {
                const pt = path[i];
                const py = _priceToY(pt.poc, h);
                if (py == null || py < 0 || py > h) continue;
                const px = trailX0 + (i / (n - 1)) * (trailX1 - trailX0);
                const age = (n - 1 - i) / Math.max(n - 1, 1); // 0=newest, 1=oldest
                const alpha = 0.25 + (1 - age) * 0.65;        // 0.25 oldest, 0.90 newest
                const radius = i === n - 1 ? 3 : 1.75;        // latest dot is bigger
                _ctx.fillStyle = `rgba(232,184,48,${alpha})`;
                _ctx.beginPath();
                _ctx.arc(px, py, radius, 0, Math.PI * 2);
                _ctx.fill();
            }
            _ctx.restore();
        }
    }

    // ── POC line — prominent, professional ──
    if (poc) {
        const pocY = _priceToY(poc, h);
        if (pocY != null && pocY >= 0 && pocY <= h) {
            _ctx.strokeStyle = _cfg.pocColor;
            _ctx.lineWidth = 2;
            _ctx.beginPath(); _ctx.moveTo(0, pocY); _ctx.lineTo(rightEdge, pocY); _ctx.stroke();
            // Label with background
            _ctx.font = 'bold 9px "JetBrains Mono", monospace';
            const _pocLabel = `POC ${poc.toFixed(2)}`;
            const _pocTm = _ctx.measureText(_pocLabel);
            _ctx.fillStyle = 'rgba(0,0,0,0.8)';
            _ctx.fillRect(rightEdge - _pocTm.width - 8, pocY - 7, _pocTm.width + 6, 14);
            _ctx.fillStyle = _cfg.pocColor;
            _ctx.textAlign = 'right';
            _ctx.textBaseline = 'middle';
            _ctx.fillText(_pocLabel, rightEdge - 4, pocY);
        }
    }

    // ── VAH / VAL lines ──
    for (const [price, label] of [[vah, 'VAH'], [val, 'VAL']]) {
        if (!price) continue;
        const ly = _priceToY(price, h);
        if (ly == null || ly < 0 || ly > h) continue;
        _ctx.strokeStyle = _cfg.vaColor;
        _ctx.lineWidth = 0.75;
        _ctx.setLineDash([4, 3]);
        _ctx.beginPath(); _ctx.moveTo(0, ly); _ctx.lineTo(rightEdge, ly); _ctx.stroke();
        _ctx.setLineDash([]);
        _ctx.font = '7px "JetBrains Mono", monospace';
        _ctx.fillStyle = _cfg.vaColor;
        _ctx.textAlign = 'right';
        _ctx.fillText(`${label} ${price.toFixed(2)}`, rightEdge - 2, label === 'VAH' ? ly - 4 : ly + 9);
    }

    // ── Multi-TF POC confluence — MAGNET when session/PD/weekly align ──
    // Guard: require distinct total_vol between profiles. When backend has
    // insufficient history (fresh server start), PD/weekly fall back to
    // session candles → identical total_vol → spurious confluence. Skip those.
    if (_cfg.showConfluence) {
        const pocs = [];
        const sessVol = sd.total_vol || 0;
        const _distinct = (pv) => sessVol <= 0 || (pv != null && Math.abs(pv - sessVol) / sessVol > 0.05);
        if (poc != null) pocs.push(poc);
        const pdData = profiles['prior_day']?.data;
        if (pdData?.poc != null && _distinct(pdData.total_vol)) pocs.push(pdData.poc);
        const wkData = profiles['weekly']?.data;
        if (wkData?.poc != null && _distinct(wkData.total_vol)) pocs.push(wkData.poc);
        if (pocs.length >= 2) {
            const hi = Math.max(...pocs), lo = Math.min(...pocs);
            if (hi - lo <= _cfg.confluenceTicks * (tick || 0.25)) {
                const mid = (hi + lo) / 2;
                const my = _priceToY(mid, h);
                if (my != null && my >= 0 && my <= h) {
                    _ctx.save();
                    const mx = w * 0.35;
                    _ctx.fillStyle = 'rgba(200,140,255,0.85)';
                    _ctx.beginPath();
                    _ctx.moveTo(mx, my - 5);
                    _ctx.lineTo(mx + 5, my);
                    _ctx.lineTo(mx, my + 5);
                    _ctx.lineTo(mx - 5, my);
                    _ctx.closePath();
                    _ctx.fill();
                    _ctx.font = 'bold 8px "JetBrains Mono", monospace';
                    const mTxt = `MAGNET ${pocs.length}×`;
                    const mtm = _ctx.measureText(mTxt);
                    _ctx.fillStyle = 'rgba(0,0,0,0.7)';
                    _ctx.fillRect(mx + 8, my - 6, mtm.width + 5, 12);
                    _ctx.fillStyle = 'rgba(220,180,255,0.95)';
                    _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
                    _ctx.fillText(mTxt, mx + 10, my);
                    _ctx.restore();
                }
            }
        }
    }

    // ── Prior-day POC / VAH / VAL — dimmed dotted lines, dedup vs session ──
    if (_cfg.showPD && profiles['prior_day']?.data) {
        const pd = profiles['prior_day'].data;
        const tickTol = (tick || 0.25) * 2;
        const _renderPD = (pdPrice, devPrice, lbl, color) => {
            if (pdPrice == null) return;
            if (devPrice != null && Math.abs(pdPrice - devPrice) < tickTol) return; // collapsed into DEV label
            const py = _priceToY(pdPrice, h);
            if (py == null || py < 0 || py > h) return;
            _ctx.save();
            _ctx.strokeStyle = color;
            _ctx.lineWidth = 0.75;
            _ctx.setLineDash([1, 3]);
            _ctx.beginPath(); _ctx.moveTo(0, py); _ctx.lineTo(rightEdge, py); _ctx.stroke();
            _ctx.setLineDash([]);
            _ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const txt = `PD ${lbl} ${pdPrice.toFixed(2)}`;
            const ptm = _ctx.measureText(txt);
            _ctx.fillStyle = 'rgba(0,0,0,0.72)';
            _ctx.fillRect(rightEdge - ptm.width - 60, py - 6, ptm.width + 6, 11);
            _ctx.fillStyle = color;
            _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
            _ctx.fillText(txt, rightEdge - ptm.width - 57, py);
            _ctx.restore();
        };
        _renderPD(pd.poc, poc, 'POC', 'rgba(232,184,48,0.55)');
        _renderPD(pd.vah, vah, 'VAH', 'rgba(33,150,243,0.45)');
        _renderPD(pd.val, val, 'VAL', 'rgba(33,150,243,0.45)');
    }

    // ── Depth delta indicators ──
    if (_cfg.showDeltas && domData.deltas) {
        for (const [ps, d] of Object.entries(domData.deltas)) {
            if (Math.abs(d) < _deltaMin) continue;
            const y = _priceToY(parseFloat(ps), h);
            if (y == null || y < 0 || y > h) continue;
            const intensity = Math.min(0.8, 0.2 + Math.abs(d) * 0.03);
            const dotSize = Math.max(BH - 1, 2);
            // Small colored square at the right edge of bar area
            const dx = BAR_W + M + 4;
            _ctx.fillStyle = d > 0 ? `rgba(40,255,140,${intensity})` : `rgba(255,80,80,${intensity})`;
            _ctx.fillRect(dx, y - dotSize/2, dotSize, dotSize);
            // Number label if bar is tall enough
            if (BH >= 5) {
                _ctx.font = `${Math.min(BH, 8)}px "JetBrains Mono", monospace`;
                _ctx.textAlign = 'left';
                _ctx.fillText(d > 0 ? `+${d}` : `${d}`, dx + dotSize + 2, y + BH * 0.2);
            }
        }
    }

    // ── Price axis — clean, professional ──
    _ctx.font = '10px "JetBrains Mono", monospace';
    _ctx.textAlign = 'right';
    _ctx.textBaseline = 'middle';
    const range = _priceTop - _priceBottom;
    const step = range > 100 ? 25 : range > 50 ? 10 : range > 20 ? 5 : range > 10 ? 2 : 1;
    for (let p = Math.ceil(_priceBottom / step) * step; p <= _priceTop; p += step) {
        const y = _priceToY(p, h);
        if (y == null || y < 10 || y > h - 25) continue;
        // Price label
        _ctx.fillStyle = 'rgba(130,145,175,0.5)';
        _ctx.fillText(p.toFixed(0), rightEdge - 3, y);
        // Subtle grid line
        _ctx.strokeStyle = 'rgba(255,255,255,0.035)';
        _ctx.lineWidth = 0.5;
        _ctx.beginPath(); _ctx.moveTo(0, y); _ctx.lineTo(rightEdge - 35, y); _ctx.stroke();
    }

    // ── Right-edge mini-ladder: 5 bids + 5 asks around mid ──
    if (_cfg.showMiniLadder && domData.dom) {
        const mid = window._latestHeatmapData?.mid_price;
        const bids = domData.dom.bids || {}, asks = domData.dom.asks || {};
        const pairs = [];
        for (const [p, sz] of Object.entries(bids)) {
            const px = parseFloat(p);
            if (!isFinite(px) || !sz) continue;
            if (mid == null || px <= mid) pairs.push({ price: px, size: sz, side: 'B' });
        }
        for (const [p, sz] of Object.entries(asks)) {
            const px = parseFloat(p);
            if (!isFinite(px) || !sz) continue;
            if (mid == null || px >= mid) pairs.push({ price: px, size: sz, side: 'A' });
        }
        // Nearest 5 either side of mid, else top-sized.
        if (mid != null) pairs.sort((a, b) => Math.abs(a.price - mid) - Math.abs(b.price - mid));
        else pairs.sort((a, b) => b.size - a.size);
        const take = pairs.slice(0, 10);
        const maxSz = Math.max(1, ...take.map(p => p.size));
        const lx = rightEdge, lw = _cfg.miniLadderWidth;
        // Strip background
        _ctx.fillStyle = 'rgba(14,18,28,0.75)';
        _ctx.fillRect(lx, 0, lw, h);
        _ctx.strokeStyle = 'rgba(80,90,120,0.2)';
        _ctx.lineWidth = 0.5;
        _ctx.beginPath(); _ctx.moveTo(lx + 0.5, 0); _ctx.lineTo(lx + 0.5, h); _ctx.stroke();
        _ctx.font = 'bold 8px "JetBrains Mono", monospace';
        _ctx.textBaseline = 'middle';
        for (const row of take) {
            const ry = _priceToY(row.price, h);
            if (ry == null || ry < 0 || ry > h) continue;
            const ratio = row.size / maxSz;
            const bw = Math.max(2, ratio * (lw - 30));
            const col = row.side === 'B' ? _cfg.bidColor : _cfg.askColor;
            _ctx.fillStyle = `rgba(${col[0]},${col[1]},${col[2]},${0.25 + ratio * 0.55})`;
            _ctx.fillRect(lx + 2, ry - 4, bw, 8);
            _ctx.fillStyle = `rgba(220,230,245,0.92)`;
            _ctx.textAlign = 'right';
            _ctx.fillText(String(row.size), lx + lw - 3, ry);
        }
        if (mid != null) {
            const my = _priceToY(mid, h);
            if (my != null && my >= 0 && my <= h) {
                _ctx.strokeStyle = 'rgba(255,220,60,0.6)';
                _ctx.lineWidth = 0.75;
                _ctx.setLineDash([2, 2]);
                _ctx.beginPath(); _ctx.moveTo(lx, my); _ctx.lineTo(lx + lw, my); _ctx.stroke();
                _ctx.setLineDash([]);
            }
        }
    }

    // ── Bid/Ask total bar ──
    if (_cfg.showBidAskBar && (domData.bidTotal > 0 || domData.askTotal > 0)) {
        const btY = h - 16;
        const btW = w * 0.45;
        const btMax = Math.max(domData.bidTotal, domData.askTotal, 1);
        _ctx.fillStyle = _rgba(_cfg.bidColor, 0.3);
        _ctx.fillRect(M, btY, (domData.bidTotal / btMax) * btW, 5);
        _ctx.fillStyle = _rgba(_cfg.askColor, 0.3);
        _ctx.fillRect(M, btY + 7, (domData.askTotal / btMax) * btW, 5);
        _ctx.font = '7px "JetBrains Mono", monospace';
        _ctx.textBaseline = 'middle';
        _ctx.textAlign = 'left';
        _ctx.fillStyle = _rgba(_cfg.bidColor, 0.7);
        _ctx.fillText(`B ${(domData.bidTotal||0).toLocaleString()}`, (domData.bidTotal/btMax)*btW + M + 3, btY + 2);
        _ctx.fillStyle = _rgba(_cfg.askColor, 0.7);
        _ctx.fillText(`A ${(domData.askTotal||0).toLocaleString()}`, (domData.askTotal/btMax)*btW + M + 3, btY + 9);
        const ratio = (domData.bidTotal||0) / Math.max(domData.askTotal||1, 1);
        _ctx.fillStyle = ratio > 1.2 ? 'rgba(40,255,140,0.6)' : ratio < 0.8 ? 'rgba(255,80,80,0.6)' : 'rgba(160,170,200,0.4)';
        _ctx.textAlign = 'right';
        _ctx.fillText(`${ratio.toFixed(2)}x`, w - M, btY + 5);
    }

    _ctx.restore();
}

// ── Settings Panel ──
function _buildSettings() {
    if (_settingsPanel) return;
    _settingsPanel = document.createElement('div');
    _settingsPanel.style.cssText = 'position:absolute;top:20px;left:4px;width:220px;background:rgba(12,16,28,0.96);border:1px solid rgba(80,90,120,0.3);border-radius:6px;padding:8px;font-family:"JetBrains Mono",monospace;font-size:9px;color:rgba(180,190,210,0.8);z-index:999;display:none;';
    _settingsPanel.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <b style="font-size:10px">VP INTEL SETTINGS</b>
            <span id="vpi-close" style="cursor:pointer;opacity:0.5">\u2715</span>
        </div>
        <div style="margin-bottom:4px"><label>Buy <input type="color" id="vpi-buy" value="#28c88c" style="width:24px;height:14px;border:0;padding:0;vertical-align:middle"></label>
        <label style="margin-left:8px">Sell <input type="color" id="vpi-sell" value="#dc3c50" style="width:24px;height:14px;border:0;padding:0;vertical-align:middle"></label></div>
        <div style="margin-bottom:4px"><label>Bid <input type="color" id="vpi-bid" value="#00b4dc" style="width:24px;height:14px;border:0;padding:0;vertical-align:middle"></label>
        <label style="margin-left:8px">Ask <input type="color" id="vpi-ask" value="#dc508c" style="width:24px;height:14px;border:0;padding:0;vertical-align:middle"></label></div>
        <div style="margin-bottom:3px"><label>Bar Opacity <input type="range" id="vpi-bar-op" min="20" max="100" value="65" style="width:80px;vertical-align:middle"> <span id="vpi-bar-op-v">65%</span></label></div>
        <div style="margin-bottom:3px"><label>Depth Opacity <input type="range" id="vpi-depth-op" min="5" max="60" value="25" style="width:80px;vertical-align:middle"> <span id="vpi-depth-op-v">25%</span></label></div>
        <div style="margin-bottom:3px"><label>Bar Width <input type="range" id="vpi-bar-w" min="30" max="90" value="70" style="width:80px;vertical-align:middle"> <span id="vpi-bar-w-v">70%</span></label></div>
        <div style="margin-bottom:3px" title="Max bar height in pixels. Auto-clamped at tight zoom to prevent overlap."><label>Bar Height (max) <input type="range" id="vpi-bar-h" min="1" max="8" value="3" style="width:80px;vertical-align:middle"> <span id="vpi-bar-h-v">3px</span></label></div>
        <hr style="border:0;border-top:1px solid rgba(80,90,120,0.2);margin:4px 0">
        <div><label><input type="checkbox" id="vpi-t-depth" checked> Depth Glow</label></div>
        <div><label><input type="checkbox" id="vpi-t-deltas" checked> Delta Arrows</label></div>
        <div><label><input type="checkbox" id="vpi-t-border" checked> Absorption Border</label></div>
        <div><label><input type="checkbox" id="vpi-t-exh" checked> Exhaustion Gradient</label></div>
        <div><label><input type="checkbox" id="vpi-t-agg" checked> Aggression Color</label></div>
        <div><label><input type="checkbox" id="vpi-t-ba" checked> Bid/Ask Bar</label></div>
        <hr style="border:0;border-top:1px solid rgba(80,90,120,0.2);margin:4px 0">
        <div><label><input type="checkbox" id="vpi-t-abslabel" checked> Absorption Labels (FORTRESS/SOLID/HELD)</label></div>
        <div><label><input type="checkbox" id="vpi-t-hvnlvn" checked> HVN / LVN Dots</label></div>
        <div><label><input type="checkbox" id="vpi-t-nums" checked> Contract Numbers</label></div>
        <div><label><input type="checkbox" id="vpi-t-wall" checked> WALL Badges</label></div>
        <div><label><input type="checkbox" id="vpi-t-zone" checked> Absorption Zone Band</label></div>
        <div><label><input type="checkbox" id="vpi-t-naked" checked> Naked POC Lines</label></div>
        <div><label><input type="checkbox" id="vpi-t-kde" checked> KDE Brightness</label></div>
        <div><label><input type="checkbox" id="vpi-t-devtrail" checked> Dev POC Trail</label></div>
        <div><label><input type="checkbox" id="vpi-t-state" checked> State Glyphs (▲DEF ▽CONSUMED ◆FRESH)</label></div>
        <div><label><input type="checkbox" id="vpi-t-fallback" checked> HVN/LVN KDE Fallback</label></div>
        <hr style="border:0;border-top:1px solid rgba(80,90,120,0.2);margin:4px 0">
        <div><label><input type="checkbox" id="vpi-t-walls" checked> Wall Bands + γ-Flip</label></div>
        <div><label><input type="checkbox" id="vpi-t-conf" checked> MAGNET Confluence</label></div>
        <div><label><input type="checkbox" id="vpi-t-zscore"> Z-Score Coloring</label></div>
        <div><label><input type="checkbox" id="vpi-t-ladder"> Mini-Ladder (right edge)</label></div>
        <div><label><input type="checkbox" id="vpi-t-lock"> Lock to Mid Price</label></div>
    `;
    _slotEl.style.position = 'relative';
    _slotEl.appendChild(_settingsPanel);

    // Safe element wiring helper
    const _wire = (id, event, fn) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener(event, fn);
    };
    const hexToRgb = (hex) => {
        if (!hex || hex.length < 7) return [128, 128, 128];
        return [parseInt(hex.slice(1,3),16), parseInt(hex.slice(3,5),16), parseInt(hex.slice(5,7),16)];
    };

    _wire('vpi-close', 'click', () => { if (_settingsPanel) _settingsPanel.style.display = 'none'; });
    _wire('vpi-buy', 'input', (e) => { _cfg.buyColor = hexToRgb(e.target.value); });
    _wire('vpi-sell', 'input', (e) => { _cfg.sellColor = hexToRgb(e.target.value); });
    _wire('vpi-bid', 'input', (e) => { _cfg.bidColor = hexToRgb(e.target.value); });
    _wire('vpi-ask', 'input', (e) => { _cfg.askColor = hexToRgb(e.target.value); });

    const _slider = (id, vId, fn) => {
        _wire(id, 'input', () => {
            const el = document.getElementById(id);
            if (!el) return;
            fn(parseInt(el.value));
            const vEl = document.getElementById(vId);
            if (vEl) vEl.textContent = el.value + (id.includes('bar-h') ? 'px' : '%');
        });
    };
    _slider('vpi-bar-op', 'vpi-bar-op-v', (v) => { _cfg.barOpacity = v / 100; });
    _slider('vpi-depth-op', 'vpi-depth-op-v', (v) => { _cfg.depthOpacity = v / 100; });
    _slider('vpi-bar-w', 'vpi-bar-w-v', (v) => { _cfg.barWidthPct = v / 100; });
    _slider('vpi-bar-h', 'vpi-bar-h-v', (v) => { _cfg.barHeight = v; });

    _wire('vpi-t-depth', 'change', (e) => { _cfg.showDepth = e.target.checked; });
    _wire('vpi-t-deltas', 'change', (e) => { _cfg.showDeltas = e.target.checked; });
    _wire('vpi-t-border', 'change', (e) => { _cfg.showAbsBorder = e.target.checked; });
    _wire('vpi-t-exh', 'change', (e) => { _cfg.showExhGradient = e.target.checked; });
    _wire('vpi-t-agg', 'change', (e) => { _cfg.showAggression = e.target.checked; });
    _wire('vpi-t-ba', 'change', (e) => { _cfg.showBidAskBar = e.target.checked; });
    _wire('vpi-t-abslabel', 'change', (e) => { _cfg.showAbsLabel = e.target.checked; });
    _wire('vpi-t-hvnlvn', 'change', (e) => { _cfg.showHVNLVN = e.target.checked; });
    _wire('vpi-t-nums', 'change', (e) => { _cfg.showNumbers = e.target.checked; });
    _wire('vpi-t-wall', 'change', (e) => { _cfg.showWall = e.target.checked; });
    _wire('vpi-t-zone', 'change', (e) => { _cfg.showZoneBand = e.target.checked; });
    _wire('vpi-t-naked', 'change', (e) => { _cfg.showNakedPoc = e.target.checked; });
    _wire('vpi-t-kde', 'change', (e) => { _cfg.useKDE = e.target.checked; });
    _wire('vpi-t-devtrail', 'change', (e) => { _cfg.showDevTrail = e.target.checked; });
    _wire('vpi-t-state', 'change', (e) => { _cfg.showStateGlyph = e.target.checked; });
    _wire('vpi-t-fallback', 'change', (e) => { _cfg.hvnLvnFallback = e.target.checked; });
    _wire('vpi-t-walls', 'change', (e) => { _cfg.showWallBands = e.target.checked; _lastRenderSig = ''; });
    _wire('vpi-t-conf', 'change', (e) => { _cfg.showConfluence = e.target.checked; _lastRenderSig = ''; });
    _wire('vpi-t-zscore', 'change', (e) => { _cfg.showZScore = e.target.checked; _lastRenderSig = ''; });
    _wire('vpi-t-ladder', 'change', (e) => { _cfg.showMiniLadder = e.target.checked; _lastRenderSig = ''; });
    _wire('vpi-t-lock', 'change', (e) => {
        _cfg.lockToMid = e.target.checked;
        const btn = _toolbar?.querySelector('[data-lockBtn], [data-lock-btn]');
        const lockBtn = Array.from(_toolbar?.querySelectorAll('button') || []).find(b => b.title === 'Lock window to mid price');
        if (lockBtn) {
            lockBtn.textContent = _cfg.lockToMid ? '\uD83D\uDD12' : '\uD83D\uDD13';
            lockBtn.style.background = _cfg.lockToMid ? 'rgba(80,140,220,0.45)' : 'rgba(40,50,70,0.45)';
        }
        _lastRenderSig = '';
    });
}

// ── DOM toolbar: VP INTEL label + TF switcher + step chooser + gear ──
const _TF_BTNS = [
    ['session',    'SESS'],
    ['prior_day',  'PD'],
    ['rolling_4h', '4H'],
    ['rolling_1h', '1H'],
    ['2day',       '2D'],
    ['weekly',     'WK'],
];
const _STEP_BTNS = [
    ['',    'FLAT'],
    ['15m', '15m'],
    ['30m', '30m'],
    ['1h',  '1H'],
];

function _buildToolbar() {
    if (_toolbar || !_slotEl) return;
    _slotEl.style.position = 'relative';
    _toolbar = document.createElement('div');
    _toolbar.className = 'vpi-toolbar';
    _toolbar.style.cssText = 'position:absolute;top:2px;left:2px;right:2px;height:16px;display:flex;align-items:center;gap:4px;padding:0 4px;font-family:"JetBrains Mono",monospace;font-size:8px;letter-spacing:.5px;color:rgba(180,190,210,0.7);background:rgba(6,9,14,0.55);border:1px solid rgba(80,90,120,0.18);border-radius:3px;z-index:5;user-select:none;pointer-events:auto;';

    const label = document.createElement('span');
    label.textContent = 'VP INTEL';
    label.style.cssText = 'color:rgba(140,160,200,0.55);font-weight:600;';
    _toolbar.appendChild(label);

    const tfGroup = document.createElement('div');
    tfGroup.style.cssText = 'display:flex;gap:1px;margin-left:6px;';
    for (const [mode, lbl] of _TF_BTNS) {
        const b = document.createElement('button');
        b.textContent = lbl;
        b.dataset.mode = mode;
        b.style.cssText = `background:${mode===_mode?'rgba(80,140,220,0.35)':'rgba(40,50,70,0.45)'};color:${mode===_mode?'#cfe4ff':'rgba(180,190,210,0.65)'};border:1px solid ${mode===_mode?'rgba(120,170,230,0.55)':'rgba(80,90,120,0.25)'};border-radius:2px;padding:1px 4px;font:inherit;cursor:pointer;line-height:1;`;
        b.addEventListener('click', () => _setMode(mode));
        tfGroup.appendChild(b);
    }
    _toolbar.appendChild(tfGroup);

    const sep = document.createElement('span');
    sep.textContent = '|';
    sep.style.cssText = 'color:rgba(80,90,120,0.5);margin:0 2px;';
    _toolbar.appendChild(sep);

    const stepGroup = document.createElement('div');
    stepGroup.style.cssText = 'display:flex;gap:1px;';
    for (const [s, lbl] of _STEP_BTNS) {
        const b = document.createElement('button');
        b.textContent = lbl;
        b.dataset.step = s;
        b.style.cssText = `background:${s===_step?'rgba(80,140,220,0.35)':'rgba(40,50,70,0.45)'};color:${s===_step?'#cfe4ff':'rgba(180,190,210,0.65)'};border:1px solid ${s===_step?'rgba(120,170,230,0.55)':'rgba(80,90,120,0.25)'};border-radius:2px;padding:1px 4px;font:inherit;cursor:pointer;line-height:1;`;
        b.addEventListener('click', () => _setStep(s));
        stepGroup.appendChild(b);
    }
    _toolbar.appendChild(stepGroup);

    const spacer = document.createElement('div');
    spacer.style.cssText = 'flex:1;';
    _toolbar.appendChild(spacer);

    const fitBtn = document.createElement('button');
    fitBtn.textContent = '\u2922'; // ⤢ FIT
    fitBtn.title = 'Fit view to data';
    fitBtn.style.cssText = 'background:rgba(40,50,70,0.45);color:rgba(180,190,210,0.75);border:1px solid rgba(80,90,120,0.25);border-radius:2px;padding:1px 6px;font:inherit;font-size:10px;cursor:pointer;line-height:1;';
    fitBtn.addEventListener('click', () => { _didAutoFit = true; _fitToData(); });
    _toolbar.appendChild(fitBtn);

    const lockBtn = document.createElement('button');
    lockBtn.textContent = '\uD83D\uDD13'; // 🔓 unlocked by default
    lockBtn.title = 'Lock window to mid price';
    lockBtn.dataset.lockBtn = '1';
    lockBtn.style.cssText = 'background:rgba(40,50,70,0.45);color:rgba(180,190,210,0.75);border:1px solid rgba(80,90,120,0.25);border-radius:2px;padding:1px 6px;font:inherit;font-size:10px;cursor:pointer;line-height:1;';
    lockBtn.addEventListener('click', () => {
        _cfg.lockToMid = !_cfg.lockToMid;
        lockBtn.textContent = _cfg.lockToMid ? '\uD83D\uDD12' : '\uD83D\uDD13';
        lockBtn.style.background = _cfg.lockToMid ? 'rgba(80,140,220,0.45)' : 'rgba(40,50,70,0.45)';
        _lastRenderSig = '';
    });
    _toolbar.appendChild(lockBtn);

    const helpBtn = document.createElement('button');
    helpBtn.textContent = '?';
    helpBtn.title = 'Legend';
    helpBtn.style.cssText = 'background:rgba(40,50,70,0.45);color:rgba(180,190,210,0.75);border:1px solid rgba(80,90,120,0.25);border-radius:2px;padding:1px 6px;font:inherit;font-size:10px;cursor:pointer;line-height:1;';
    helpBtn.addEventListener('click', () => {
        _buildLegend();
        if (_legendEl) _legendEl.style.display = _legendEl.style.display === 'none' || !_legendEl.style.display ? 'block' : 'none';
    });
    _toolbar.appendChild(helpBtn);

    const resetBtn = document.createElement('button');
    resetBtn.textContent = '\u21BA';
    resetBtn.title = 'Reset zoom/pan (or double-click pane)';
    resetBtn.style.cssText = 'background:rgba(40,50,70,0.45);color:rgba(180,190,210,0.75);border:1px solid rgba(80,90,120,0.25);border-radius:2px;padding:1px 6px;font:inherit;font-size:10px;cursor:pointer;line-height:1;';
    resetBtn.addEventListener('click', _resetZoom);
    _toolbar.appendChild(resetBtn);

    const gear = document.createElement('button');
    gear.textContent = '\u2699';
    gear.title = 'Settings';
    gear.style.cssText = 'background:rgba(40,50,70,0.45);color:rgba(180,190,210,0.75);border:1px solid rgba(80,90,120,0.25);border-radius:2px;padding:1px 6px;font:inherit;font-size:10px;cursor:pointer;line-height:1;';
    gear.addEventListener('click', () => {
        _buildSettings();
        if (_settingsPanel) _settingsPanel.style.display = _settingsPanel.style.display === 'none' ? '' : 'none';
    });
    _toolbar.appendChild(gear);

    _slotEl.appendChild(_toolbar);
}

function _refreshToolbarHighlight() {
    if (!_toolbar) return;
    for (const b of _toolbar.querySelectorAll('button[data-mode]')) {
        const on = b.dataset.mode === _mode;
        b.style.background = on ? 'rgba(80,140,220,0.35)' : 'rgba(40,50,70,0.45)';
        b.style.color = on ? '#cfe4ff' : 'rgba(180,190,210,0.65)';
        b.style.borderColor = on ? 'rgba(120,170,230,0.55)' : 'rgba(80,90,120,0.25)';
    }
    for (const b of _toolbar.querySelectorAll('button[data-step]')) {
        const on = b.dataset.step === _step;
        b.style.background = on ? 'rgba(80,140,220,0.35)' : 'rgba(40,50,70,0.45)';
        b.style.color = on ? '#cfe4ff' : 'rgba(180,190,210,0.65)';
        b.style.borderColor = on ? 'rgba(120,170,230,0.55)' : 'rgba(80,90,120,0.25)';
    }
}

function _setMode(mode) {
    if (mode === _mode) return;
    _mode = mode;
    _refreshToolbarHighlight();
    if (typeof VolumeProfileOverlay !== 'undefined' && VolumeProfileOverlay.ensureProfileActive) {
        VolumeProfileOverlay.ensureProfileActive(mode);
    }
    _lastRenderSig = '';  // force re-render on next tick
}

function _setStep(step) {
    if (step === _step) return;
    _step = step;
    _refreshToolbarHighlight();
    if (typeof VolumeProfileOverlay !== 'undefined' && VolumeProfileOverlay.setStepSize) {
        VolumeProfileOverlay.setStepSize(step);
    }
    _lastRenderSig = '';
}

function _resetZoom() {
    _userPriceTop = null;
    _userPriceBottom = null;
    _didAutoFit = false;
    _lastRenderSig = '';
}

function _buildLegend() {
    if (_legendEl || !_slotEl) return;
    _slotEl.style.position = 'relative';
    _legendEl = document.createElement('div');
    _legendEl.className = 'vpi-legend';
    _legendEl.style.cssText = 'position:absolute;top:22px;right:4px;width:210px;background:rgba(8,12,22,0.97);border:1px solid rgba(80,90,120,0.35);border-radius:5px;padding:7px 9px;font-family:"JetBrains Mono",monospace;font-size:9px;color:rgba(200,210,225,0.85);z-index:20;display:none;line-height:1.45;';
    _legendEl.innerHTML = `
        <div style="display:flex;justify-content:space-between;margin-bottom:5px">
            <b style="font-size:10px;color:rgba(200,210,230,0.95)">LEGEND</b>
            <span style="cursor:pointer;opacity:0.5" data-close="1">\u2715</span>
        </div>
        <div><span style="color:#00e676">■</span> buy &nbsp; <span style="color:#ff1744">■</span> sell</div>
        <div><span style="color:#00c8ff">■</span> bid depth &nbsp; <span style="color:#ff4081">■</span> ask depth</div>
        <div><b>saturation</b> = aggression (or z-score)</div>
        <div><b>border glow</b> = absorption (P80+)</div>
        <div><b>fade direction</b> = exhaustion</div>
        <div>━ <span style="color:#ffc107">POC</span> &nbsp; <span style="color:#2196f3">VAH/VAL</span></div>
        <div>▲ DEF &nbsp; ▽ CONSUMED &nbsp; ◆ FRESH</div>
        <div>◆ <span style="color:#c88cff">MAGNET</span> = multi-TF POC align</div>
        <div>━━ <span style="color:#ff505a">CW</span> / <span style="color:#50dc82">PW</span> / <span style="color:#50d2ff">γFLIP</span></div>
        <div>nPOC = naked POC (unvisited)</div>
        <div>WALL = resting depth &gt;&gt; avg</div>
    `;
    const closer = _legendEl.querySelector('[data-close]');
    if (closer) closer.addEventListener('click', () => { _legendEl.style.display = 'none'; });
    _slotEl.appendChild(_legendEl);
}

function _enforceLock() {
    if (!_cfg.lockToMid) return;
    const mid = window._latestHeatmapData?.mid_price;
    if (mid == null || !isFinite(mid)) return;
    if (_userPriceTop == null || _userPriceBottom == null) return;
    const range = _userPriceTop - _userPriceBottom;
    if (range <= 0) return;
    const edgePad = range * 0.10;
    if (mid > _userPriceBottom + edgePad && mid < _userPriceTop - edgePad) return; // still centered enough
    // Recenter while preserving range.
    _userPriceTop = mid + range / 2;
    _userPriceBottom = mid - range / 2;
    _lastRenderSig = '';
}

// Price-at-cursor stays pinned during zoom — feels natural.
function _onWheel(e) {
    if (!_canvas || !_priceTop || !_priceBottom || _priceTop <= _priceBottom) return;
    e.preventDefault();
    // First wheel seeds the user window from whatever we're currently showing.
    if (_userPriceTop == null) {
        _userPriceTop = _priceTop;
        _userPriceBottom = _priceBottom;
    }
    const rect = _canvas.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const h = rect.height;
    if (h <= 0) return;
    const anchor = _userPriceBottom + (1 - y / h) * (_userPriceTop - _userPriceBottom);
    const factor = (e.deltaY > 0) ? 1.15 : (1 / 1.15);  // wheel-down = zoom out
    const newTop = anchor + (_userPriceTop - anchor) * factor;
    const newBot = anchor - (anchor - _userPriceBottom) * factor;
    // Guard against tick-scale collapse / absurd zoom-out
    if (newTop - newBot < 0.5) return;
    if (newTop - newBot > 100000) return;
    _userPriceTop = newTop;
    _userPriceBottom = newBot;
    _lastRenderSig = '';
}

function _onMouseDown(e) {
    if (e.button !== 0) return;
    // Don't hijack clicks on toolbar (buttons, gear, etc.)
    if (e.target && e.target.closest && e.target.closest('.vpi-toolbar')) return;
    _isPanning = true;
    _panStartY = e.clientY;
    if (_userPriceTop == null) {
        _userPriceTop = _priceTop;
        _userPriceBottom = _priceBottom;
    }
    _panStartTop = _userPriceTop;
    _panStartBot = _userPriceBottom;
    if (_canvas) _canvas.style.cursor = 'grabbing';
    e.preventDefault();
}

function _onMouseMove(e) {
    if (!_isPanning || !_canvas) return;
    const rect = _canvas.getBoundingClientRect();
    if (rect.height <= 0) return;
    const dy = e.clientY - _panStartY;
    const priceRange = _panStartTop - _panStartBot;
    // Drag DOWN → show higher prices (shift window up). dy positive = price delta positive.
    const deltaPrice = (dy / rect.height) * priceRange;
    _userPriceTop = _panStartTop + deltaPrice;
    _userPriceBottom = _panStartBot + deltaPrice;
    _lastRenderSig = '';
}

function _onMouseUp() {
    if (!_isPanning) return;
    _isPanning = false;
    if (_canvas) _canvas.style.cursor = 'grab';
}

function _onDblClick(e) {
    if (e.target && e.target.closest && e.target.closest('.vpi-toolbar')) return;
    _resetZoom();
}

function _buildTooltip() {
    if (_tooltipEl || !_slotEl) return;
    _tooltipEl = document.createElement('div');
    _tooltipEl.className = 'vpi-tooltip';
    _tooltipEl.style.cssText = 'position:absolute;pointer-events:none;background:rgba(8,12,22,0.95);border:1px solid rgba(100,140,200,0.4);border-radius:4px;padding:4px 7px;font-family:"JetBrains Mono",monospace;font-size:9px;color:rgba(220,230,245,0.95);z-index:30;display:none;white-space:nowrap;line-height:1.5;box-shadow:0 2px 8px rgba(0,0,0,0.5);';
    _slotEl.appendChild(_tooltipEl);
}

function _hitTestY(yPx) {
    if (!_levelYs || !_lastLevels || _lastLevels.length === 0) return null;
    let bestIdx = -1, bestDist = Infinity;
    for (let i = 0; i < _lastLevels.length; i++) {
        const dy = Math.abs(yPx - _levelYs[i]);
        if (dy < bestDist) { bestDist = dy; bestIdx = i; }
    }
    if (bestIdx < 0 || bestDist > 10) return null;
    return _lastLevels[bestIdx];
}

function _onHover(e) {
    if (_isPanning || !_tooltipEl || !_canvas) return;
    const now = performance.now();
    if (now - _lastHoverTs < 16) return;  // ~60Hz
    _lastHoverTs = now;
    const rect = _canvas.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const x = e.clientX - rect.left;
    const lv = _hitTestY(y);
    if (!lv) { _tooltipEl.style.display = 'none'; _lastHoverLevel = null; return; }
    if (_lastHoverLevel && _lastHoverLevel.price === lv.price) {
        // Just reposition.
        const tx = Math.min(x + 12, rect.width - 170);
        _tooltipEl.style.left = tx + 'px';
        _tooltipEl.style.top = Math.max(0, y - 48) + 'px';
        return;
    }
    _lastHoverLevel = lv;
    const buy = Math.round(lv.buy || 0), sell = Math.round(lv.sell || 0);
    const delta = buy - sell;
    const total = Math.round(lv.total || 0);
    const absR = lv.abs_ratio != null ? lv.abs_ratio.toFixed(1) : '—';
    const exh = lv.exh != null ? lv.exh.toFixed(2) : '—';
    const rc = lv.refill_class || '—';
    const state = lv.state || '—';
    const zTxt = (_cfg.showZScore && _zStd > 0) ? `z=${(delta / _zStd).toFixed(2)}σ` : '';
    _tooltipEl.innerHTML = `
        <div style="color:#ffc107;font-weight:600;margin-bottom:3px">${lv.price.toFixed(2)}</div>
        <div>total <b>${total.toLocaleString()}</b> &nbsp; Δ <span style="color:${delta>=0?'#1fd17a':'#e03060'}">${delta>=0?'+':''}${delta}</span> ${zTxt}</div>
        <div>buy <span style="color:#1fd17a">${buy}</span> &nbsp; sell <span style="color:#e03060">${sell}</span></div>
        <div style="opacity:.8">abs ${absR} &nbsp; exh ${exh}</div>
        <div style="opacity:.8">state ${state} &nbsp; refill ${rc}</div>
    `;
    _tooltipEl.style.display = 'block';
    const tx = Math.min(x + 12, rect.width - 170);
    _tooltipEl.style.left = tx + 'px';
    _tooltipEl.style.top = Math.max(0, y - 48) + 'px';
}

function _onMouseLeave() {
    if (_tooltipEl) _tooltipEl.style.display = 'none';
    _lastHoverLevel = null;
}

// ── Render loop with dirty flag + visibility + throttle ──
let _lastRenderSig = '';
let _lastRenderTime = 0;
let _lastCanvasW = 0, _lastCanvasH = 0;
const _RENDER_MIN_MS = 50;  // cap to ~20Hz (data polls at 10s; scroll needs responsiveness)
function _loop() {
    if (_destroyed) return;
    _raf = requestAnimationFrame(_loop);
    if (!_canvas || !_ctx) return;
    if (_canvas.offsetParent === null) return;
    const now = performance.now();
    if (now - _lastRenderTime < _RENDER_MIN_MS) return;
    const rect = _canvas.getBoundingClientRect();
    const resized = rect.width !== _lastCanvasW || rect.height !== _lastCanvasH;
    _syncPriceRange();
    // Auto-fit: when data covers <70% of current window, snap once.
    if (!_didAutoFit && _userPriceTop == null) {
        try {
            const sd = (typeof VolumeProfileOverlay !== 'undefined')
                ? VolumeProfileOverlay.getProfiles()?.[_mode]?.data : null;
            if (sd?.levels?.length && _priceTop > _priceBottom) {
                const px = sd.levels.map(l => l.price);
                const cov = (Math.max(...px) - Math.min(...px)) / (_priceTop - _priceBottom);
                if (cov > 0 && cov < 0.70) { _fitToData(); _didAutoFit = true; }
            }
        } catch (_) {}
    }
    _enforceLock();
    let sig = `${_priceTop?.toFixed(2)}|${_priceBottom?.toFixed(2)}|${_mode}|${_step}|${_cfg.showWallBands?1:0}|${_cfg.showZScore?1:0}|${_cfg.showConfluence?1:0}|${_cfg.showMiniLadder?1:0}|${_cfg.lockToMid?1:0}`;
    if (_wallData) sig += `|${_wallData.call_wall}|${_wallData.put_wall}|${_wallData.gamma_flip}`;
    try {
        if (typeof VolumeProfileOverlay !== 'undefined') {
            const p = VolumeProfileOverlay.getProfiles()?.[_mode]?.data;
            if (p) sig += `|${p.poc}|${p.vah}|${p.val}|${p.levels?.length}|${p.total_vol}`;
            const wp = VolumeProfileOverlay.getProfiles()?.['weekly']?.data;
            if (wp && _cfg.showConfluence) sig += `|${wp.poc}`;
        }
    } catch (_) {}
    if (_cfg.lockToMid && window._latestHeatmapData?.mid_price != null) {
        sig += `|${window._latestHeatmapData.mid_price}`;
    }
    if (!resized && sig === _lastRenderSig) return;
    _lastRenderSig = sig;
    _lastRenderTime = now;
    _lastCanvasW = rect.width; _lastCanvasH = rect.height;
    _render();
}

window.VPIntelPane = {
    init(slotEl) {
        _destroyed = false;
        _slotEl = slotEl;
        _canvas = document.createElement('canvas');
        _canvas.setAttribute('data-vpintel', '1');
        _canvas.style.cssText = 'width:100%;height:100%;display:block;cursor:grab;';
        slotEl.innerHTML = '';
        slotEl.appendChild(_canvas);
        _ctx = _canvas.getContext('2d');
        if (!_ctx) return;
        _buildToolbar();
        _buildTooltip();
        // Zoom/pan wiring — wheel stays on canvas; mousemove/up go on window
        // so a drag that leaves the canvas still tracks cleanly.
        _canvas.addEventListener('wheel', _onWheel, { passive: false });
        _canvas.addEventListener('mousedown', _onMouseDown);
        _canvas.addEventListener('dblclick', _onDblClick);
        _canvas.addEventListener('mousemove', _onHover);
        _canvas.addEventListener('mouseleave', _onMouseLeave);
        window.addEventListener('mousemove', _onMouseMove);
        window.addEventListener('mouseup', _onMouseUp);
        _subscribeWallData();
        if (typeof VolumeProfileOverlay !== 'undefined') {
            VolumeProfileOverlay.setIntelPaneActive(true);
            // Make sure the initial mode is in the overlay's active set so it polls.
            if (VolumeProfileOverlay.ensureProfileActive) {
                VolumeProfileOverlay.ensureProfileActive(_mode);
                // Weekly is required for MAGNET confluence glyph.
                VolumeProfileOverlay.ensureProfileActive('weekly');
                VolumeProfileOverlay.ensureProfileActive('prior_day');
            }
        }
        _raf = requestAnimationFrame(_loop);
    },
    destroy(slotEl) {
        // Guard: singleton state may belong to a different slot after a layout swap.
        // Layout swap order: new slot init() runs BEFORE old slot destroy().
        // Without this guard, old slot's teardown clobbers new slot's live state.
        if (slotEl && _slotEl && slotEl !== _slotEl) return;
        _destroyed = true;
        if (_raf) cancelAnimationFrame(_raf);
        _raf = 0;
        window.removeEventListener('mousemove', _onMouseMove);
        window.removeEventListener('mouseup', _onMouseUp);
        if (_canvas) {
            _canvas.removeEventListener('wheel', _onWheel);
            _canvas.removeEventListener('mousedown', _onMouseDown);
            _canvas.removeEventListener('dblclick', _onDblClick);
            _canvas.removeEventListener('mousemove', _onHover);
            _canvas.removeEventListener('mouseleave', _onMouseLeave);
            _canvas = null;
        }
        _ctx = null;
        _userPriceTop = null; _userPriceBottom = null; _isPanning = false;
        _didAutoFit = false;
        _wallData = null;
        if (_wallUnsub) { try { _wallUnsub(); } catch (_) {} _wallUnsub = null; }
        _lastLevels = null; _levelYs = null; _lastHoverLevel = null;
        if (_toolbar) { _toolbar.remove(); _toolbar = null; }
        if (_settingsPanel) { _settingsPanel.remove(); _settingsPanel = null; }
        if (_tooltipEl) { _tooltipEl.remove(); _tooltipEl = null; }
        if (_legendEl) { _legendEl.remove(); _legendEl = null; }
        if (typeof VolumeProfileOverlay !== 'undefined') VolumeProfileOverlay.setIntelPaneActive(false);
    }
};

})();
