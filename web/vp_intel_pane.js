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
};

let _canvas = null, _ctx = null, _slotEl = null;
let _raf = 0, _destroyed = false;
let _priceTop = 0, _priceBottom = 0;
let _settingsPanel = null;

function _rgba(rgb, a) { return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`; }

function _priceToY(price, h) {
    if (_priceTop <= _priceBottom) return null;
    return h - ((price - _priceBottom) / (_priceTop - _priceBottom)) * h;
}

function _syncPriceRange() {
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
        const sd = p['session']?.data;
        if (sd?.levels?.length > 0) {
            const px = sd.levels.map(l => l.price);
            _priceTop = Math.max(...px) + 5; _priceBottom = Math.min(...px) - 5;
            return true;
        }
    }
    return false;
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
    const sd = profiles['session']?.data;
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
    const BAR_W = w * _cfg.barWidthPct;

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
        _ctx.fillRect(0, top, w, bandH);
        _ctx.strokeStyle = _rgba(zColor, 0.25);
        _ctx.lineWidth = 0.5;
        _ctx.strokeRect(0, top, w, bandH);
        // Zone price-range label (e.g. "26461–26486")
        if (_cfg.showZoneLabel && bandH >= 10) {
            const rangeTxt = `${z.lo.toFixed(2)}–${z.hi.toFixed(2)}`;
            _ctx.save();
            _ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const rtm = _ctx.measureText(rangeTxt);
            const rx = w - rtm.width - 42;
            const ry = top + bandH / 2;
            _ctx.fillStyle = 'rgba(0,0,0,0.68)';
            _ctx.fillRect(rx - 2, ry - 5, rtm.width + 5, 10);
            _ctx.fillStyle = _rgba(zColor, 0.9);
            _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
            _ctx.fillText(rangeTxt, rx, ry);
            _ctx.restore();
        }
    }

    let _lastAbsLabelY = -Infinity;
    let _lastNumY = -Infinity;

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

        // Aggression: balanced = slightly muted, one-sided = full vivid
        const aggression = lv.total > 0 ? Math.abs(delta) / lv.total : 0;
        const satMult = _cfg.showAggression ? (0.6 + aggression * 0.4) : 0.8;

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
            _ctx.beginPath(); _ctx.moveTo(0, ny); _ctx.lineTo(w, ny); _ctx.stroke();
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
            const trailW = Math.min(w * 0.25, 120);
            const trailX0 = w - trailW - 6;
            const trailX1 = w - 6;
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
            _ctx.beginPath(); _ctx.moveTo(0, pocY); _ctx.lineTo(w, pocY); _ctx.stroke();
            // Label with background
            _ctx.font = 'bold 9px "JetBrains Mono", monospace';
            const _pocLabel = `POC ${poc.toFixed(2)}`;
            const _pocTm = _ctx.measureText(_pocLabel);
            _ctx.fillStyle = 'rgba(0,0,0,0.8)';
            _ctx.fillRect(w - _pocTm.width - 8, pocY - 7, _pocTm.width + 6, 14);
            _ctx.fillStyle = _cfg.pocColor;
            _ctx.textAlign = 'right';
            _ctx.textBaseline = 'middle';
            _ctx.fillText(_pocLabel, w - 4, pocY);
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
        _ctx.beginPath(); _ctx.moveTo(0, ly); _ctx.lineTo(w, ly); _ctx.stroke();
        _ctx.setLineDash([]);
        _ctx.font = '7px "JetBrains Mono", monospace';
        _ctx.fillStyle = _cfg.vaColor;
        _ctx.textAlign = 'right';
        _ctx.fillText(`${label} ${price.toFixed(2)}`, w - 2, label === 'VAH' ? ly - 4 : ly + 9);
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
            _ctx.beginPath(); _ctx.moveTo(0, py); _ctx.lineTo(w, py); _ctx.stroke();
            _ctx.setLineDash([]);
            _ctx.font = 'bold 7px "JetBrains Mono", monospace';
            const txt = `PD ${lbl} ${pdPrice.toFixed(2)}`;
            const ptm = _ctx.measureText(txt);
            _ctx.fillStyle = 'rgba(0,0,0,0.72)';
            _ctx.fillRect(w - ptm.width - 60, py - 6, ptm.width + 6, 11);
            _ctx.fillStyle = color;
            _ctx.textAlign = 'left'; _ctx.textBaseline = 'middle';
            _ctx.fillText(txt, w - ptm.width - 57, py);
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
        _ctx.fillText(p.toFixed(0), w - 3, y);
        // Subtle grid line
        _ctx.strokeStyle = 'rgba(255,255,255,0.035)';
        _ctx.lineWidth = 0.5;
        _ctx.beginPath(); _ctx.moveTo(0, y); _ctx.lineTo(w - 35, y); _ctx.stroke();
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

    // ── Header + Settings button ──
    _ctx.font = '9px "JetBrains Mono", monospace';
    _ctx.textAlign = 'left';
    _ctx.fillStyle = 'rgba(140,160,200,0.4)';
    _ctx.fillText('VP INTEL', M, 10);

    // Settings button — visible clickable area
    _ctx.fillStyle = 'rgba(80,100,140,0.3)';
    _ctx.fillRect(w - 30, 2, 26, 14);
    _ctx.strokeStyle = 'rgba(120,140,180,0.3)';
    _ctx.lineWidth = 0.5;
    _ctx.strokeRect(w - 30, 2, 26, 14);
    _ctx.fillStyle = 'rgba(180,190,210,0.6)';
    _ctx.font = '8px "JetBrains Mono", monospace';
    _ctx.textAlign = 'center';
    _ctx.fillText('\u2699 SET', w - 17, 11);

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
}

// ── Click handler for settings gear ──
function _onCanvasClick(e) {
    if (!_canvas) return;
    const rect = _canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (x > rect.width - 32 && y < 18) {
        if (!_slotEl) return;
        _buildSettings();
        if (_settingsPanel) _settingsPanel.style.display = _settingsPanel.style.display === 'none' ? '' : 'none';
    }
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
    let sig = `${_priceTop?.toFixed(2)}|${_priceBottom?.toFixed(2)}`;
    try {
        if (typeof VolumeProfileOverlay !== 'undefined') {
            const p = VolumeProfileOverlay.getProfiles()?.session?.data;
            if (p) sig += `|${p.poc}|${p.vah}|${p.val}|${p.levels?.length}|${p.total_vol}`;
        }
    } catch (_) {}
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
        _canvas.style.cssText = 'width:100%;height:100%;display:block;cursor:default;';
        slotEl.innerHTML = '';
        slotEl.appendChild(_canvas);
        _ctx = _canvas.getContext('2d');
        if (!_ctx) return;
        _canvas.addEventListener('click', _onCanvasClick);
        if (typeof VolumeProfileOverlay !== 'undefined') VolumeProfileOverlay.setIntelPaneActive(true);
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
        if (_canvas) {
            _canvas.removeEventListener('click', _onCanvasClick);
            _canvas = null;
        }
        _ctx = null;
        if (_settingsPanel) { _settingsPanel.remove(); _settingsPanel = null; }
        if (typeof VolumeProfileOverlay !== 'undefined') VolumeProfileOverlay.setIntelPaneActive(false);
    }
};

})();
