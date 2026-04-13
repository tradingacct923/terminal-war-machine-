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
    BAR_WIDTH_PCT: 0.22,
    BAR_ALPHA: 0.55,
    BAR_ALPHA_POC: 0.80,
    MIN_BAR_PX: 3,
    POC_LINE_WIDTH: 1.5,
    POC_LINE_DASH: [6, 3],
    VAH_VAL_WIDTH: 1,
    VAH_VAL_DASH: [4, 4],
    LABEL_FONT: '9px "JetBrains Mono", "SF Mono", monospace',
    REFRESH_INTERVAL: 10000,
    SIDE: 'left',
    MARGIN: 6,
    ROW_COUNT: 0,
    VA_PCT: 0.70,
    EXTEND_LINES: true,
    BUY_COLOR: [0, 230, 230],
    SELL_COLOR: [255, 100, 150],
    POC_COLOR: '#ffd700',
    VA_COLOR: 'rgba(74,144,217,0.7)',
};

// ── Per-Profile Visual Styles ──
// Each mode gets distinct side, opacity, and color treatment
const PROFILE_STYLES = {
    prior_day: {
        side: 'left',
        barAlpha: 0.30,
        barAlphaPOC: 0.50,
        buyColor: [80, 180, 200],     // desaturated cyan
        sellColor: [200, 120, 160],   // desaturated pink
        pocColor: 'rgba(255,215,0,0.4)',
        vaColor: 'rgba(74,144,217,0.25)',
        label: 'PD',
        barWidthPct: 0.18,
    },
    rolling_4h: {
        side: 'right',
        barAlpha: 0.60,
        barAlphaPOC: 0.85,
        buyColor: [0, 230, 230],      // vivid cyan
        sellColor: [255, 100, 150],   // vivid pink
        pocColor: '#ffd700',
        vaColor: 'rgba(74,144,217,0.7)',
        label: '4H',
        barWidthPct: 0.22,
    },
    session: {
        side: 'right',
        barAlpha: 0.65,
        barAlphaPOC: 0.90,
        buyColor: [0, 255, 255],      // bright cyan
        sellColor: [255, 80, 130],    // bright pink
        pocColor: '#ffd700',
        vaColor: 'rgba(74,144,217,0.7)',
        label: 'DEV',
        barWidthPct: 0.22,
    },
    rolling_1h: {
        side: 'right',
        barAlpha: 0.60,
        barAlphaPOC: 0.85,
        buyColor: [0, 230, 230],
        sellColor: [255, 100, 150],
        pocColor: '#ffd700',
        vaColor: 'rgba(74,144,217,0.7)',
        label: '1H',
        barWidthPct: 0.20,
    },
    rolling_2h: {
        side: 'right',
        barAlpha: 0.60,
        barAlphaPOC: 0.85,
        buyColor: [0, 230, 230],
        sellColor: [255, 100, 150],
        pocColor: '#ffd700',
        vaColor: 'rgba(74,144,217,0.7)',
        label: '2H',
        barWidthPct: 0.20,
    },
    '2day': {
        side: 'left',
        barAlpha: 0.35,
        barAlphaPOC: 0.55,
        buyColor: [100, 190, 210],
        sellColor: [210, 130, 165],
        pocColor: 'rgba(255,215,0,0.5)',
        vaColor: 'rgba(74,144,217,0.3)',
        label: '2D',
        barWidthPct: 0.18,
    },
    weekly: {
        side: 'left',
        barAlpha: 0.25,
        barAlphaPOC: 0.45,
        buyColor: [120, 170, 190],
        sellColor: [190, 140, 165],
        pocColor: 'rgba(255,215,0,0.35)',
        vaColor: 'rgba(74,144,217,0.2)',
        label: 'WK',
        barWidthPct: 0.16,
    },
    custom: {
        side: 'right',
        barAlpha: 0.55,
        barAlphaPOC: 0.80,
        buyColor: [0, 230, 230],
        sellColor: [255, 100, 150],
        pocColor: '#ffd700',
        vaColor: 'rgba(74,144,217,0.7)',
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
let _showIBLines = true;
let _showHVNLVN = true;
let _deltaMode = false;

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
    if (_pollInFlight) return; // prevent overlapping fetches
    _pollInFlight = true;
    try {
        const symbol = window._activeSymbol || 'NQ';
        for (const mode of _activeProfiles) {
            await _fetchProfile(mode, symbol);
        }
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
    }
    return Object.values(buckets).sort((a, b) => a.price - b.price).map(b => ({ ...b, barH: bucketPxH }));
}

// ── Render ──
function _renderProfile(ctx, mediaSize, priceConverter, mode, data) {
    if (!data || !data.levels || data.levels.length === 0) return;

    // Per-profile visual style (falls back to global config)
    const style = PROFILE_STYLES[mode] || PROFILE_STYLES.rolling_4h;
    const buyColor = style.buyColor;
    const sellColor = style.sellColor;
    const pocColor = style.pocColor;
    const vaColor = style.vaColor;
    const barAlpha = style.barAlpha;
    const barAlphaPOC = style.barAlphaPOC;
    const isLeft = style.side === 'left';
    const maxBarW = mediaSize.width * style.barWidthPct;
    const baseX = isLeft ? VP_CONFIG.MARGIN : mediaSize.width - VP_CONFIG.MARGIN;

    const buckets = _bucketLevels(data.levels, priceConverter, VP_CONFIG.MIN_BAR_PX);
    if (buckets.length === 0) return;
    const maxVol = Math.max(...buckets.map(b => b.total));
    if (maxVol === 0) return;

    // ── HVN/LVN Detection ──
    // Scan for local volume extrema using a 5-bucket window
    const hvnSet = new Set();
    const lvnSet = new Set();
    if (_showHVNLVN && buckets.length >= 5) {
        const avgVol = buckets.reduce((s, b) => s + b.total, 0) / buckets.length;
        const hvnThreshold = avgVol * 1.5;
        const lvnThreshold = avgVol * 0.4;
        for (let i = 2; i < buckets.length - 2; i++) {
            const v = buckets[i].total;
            const neighbors = [buckets[i-2].total, buckets[i-1].total, buckets[i+1].total, buckets[i+2].total];
            const isLocalMax = neighbors.every(n => v >= n);
            const isLocalMin = neighbors.every(n => v <= n);
            if (isLocalMax && v >= hvnThreshold) hvnSet.add(i);
            if (isLocalMin && v <= lvnThreshold) lvnSet.add(i);
        }
    }

    // ── Bars ──
    let bIdx = 0;
    for (const bk of buckets) {
        const y = priceConverter(bk.price);
        if (y == null || isNaN(y)) { bIdx++; continue; }
        if (y < -bk.barH || y > mediaSize.height + bk.barH) { bIdx++; continue; }

        const isPOC = data.poc >= bk.price - bk.barH && data.poc <= bk.price + bk.barH;
        const isHVN = hvnSet.has(bIdx);
        const isLVN = lvnSet.has(bIdx);
        // Intensity gradient: scale alpha by volume ratio (0.4–1.0 range)
        const volRatio = bk.total / maxVol;
        const intensityScale = 0.4 + volRatio * 0.6;
        const alpha = (isPOC ? barAlphaPOC : barAlpha) * intensityScale;
        const barH = bk.barH;
        const totalW = (bk.total / maxVol) * maxBarW;
        const buyW = bk.total > 0 ? (bk.buy / bk.total) * totalW : 0;
        const sellW = totalW - buyW;
        const barY = y - barH / 2;

        if (_deltaMode) {
            // Delta mode: single bar, green = net buy, red = net sell
            const delta = bk.buy - bk.sell;
            const deltaW = (Math.abs(delta) / maxVol) * maxBarW;
            if (deltaW > 0.5) {
                const dColor = delta >= 0 ? [0, 220, 120] : [255, 60, 80];
                ctx.fillStyle = _rgba(dColor, alpha);
                if (isLeft) {
                    ctx.fillRect(baseX, barY, deltaW, barH);
                } else {
                    ctx.fillRect(baseX - deltaW, barY, deltaW, barH);
                }
            }
        } else if (isLeft) {
            if (buyW > 0.5) { ctx.fillStyle = _rgba(buyColor, alpha); ctx.fillRect(baseX, barY, buyW, barH); }
            if (sellW > 0.5) { ctx.fillStyle = _rgba(sellColor, alpha); ctx.fillRect(baseX + buyW, barY, sellW, barH); }
        } else {
            if (sellW > 0.5) { ctx.fillStyle = _rgba(sellColor, alpha); ctx.fillRect(baseX - totalW, barY, sellW, barH); }
            if (buyW > 0.5) { ctx.fillStyle = _rgba(buyColor, alpha); ctx.fillRect(baseX - totalW + sellW, barY, buyW, barH); }
        }
        if (isPOC) {
            ctx.strokeStyle = pocColor;
            ctx.lineWidth = 2;
            ctx.strokeRect(isLeft ? baseX : baseX - totalW, barY, totalW, barH);
        }

        // ── HVN marker: solid diamond + subtle zone highlight ──
        if (isHVN) {
            const markerX = isLeft ? baseX + totalW + 6 : baseX - totalW - 6;
            ctx.save();
            ctx.fillStyle = 'rgba(0,200,255,0.7)';
            ctx.beginPath();
            ctx.moveTo(markerX, y - 4); ctx.lineTo(markerX + 4, y);
            ctx.lineTo(markerX, y + 4); ctx.lineTo(markerX - 4, y);
            ctx.closePath(); ctx.fill();
            // Zone highlight across chart
            ctx.fillStyle = 'rgba(0,200,255,0.02)';
            ctx.fillRect(0, barY, mediaSize.width, barH);
            ctx.restore();
        }

        // ── LVN marker: hollow triangle + thin dotted line ──
        if (isLVN) {
            const markerX = isLeft ? baseX + totalW + 6 : baseX - totalW - 6;
            ctx.save();
            ctx.strokeStyle = 'rgba(255,100,50,0.6)';
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(markerX - 3, y + 3); ctx.lineTo(markerX, y - 3);
            ctx.lineTo(markerX + 3, y + 3); ctx.closePath();
            ctx.stroke();
            // Thin dotted line at LVN price
            ctx.setLineDash([2, 4]);
            ctx.strokeStyle = 'rgba(255,100,50,0.15)';
            ctx.lineWidth = 0.5;
            ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(mediaSize.width, y); ctx.stroke();
            ctx.setLineDash([]);
            ctx.restore();
        }

        bIdx++;
    }

    // ── Lines ──
    const lineStart = VP_CONFIG.EXTEND_LINES ? 0 : baseX;
    const lineEnd = VP_CONFIG.EXTEND_LINES ? mediaSize.width : (isLeft ? baseX + maxBarW : baseX);

    // POC
    if (_showPOCLine) {
        const pocY = priceConverter(data.poc);
        if (pocY != null && !isNaN(pocY)) {
            ctx.save();
            ctx.strokeStyle = pocColor; ctx.lineWidth = VP_CONFIG.POC_LINE_WIDTH;
            ctx.setLineDash(VP_CONFIG.POC_LINE_DASH);
            ctx.beginPath(); ctx.moveTo(lineStart, pocY); ctx.lineTo(lineEnd, pocY); ctx.stroke();
            ctx.setLineDash([]);
            // Label
            ctx.font = VP_CONFIG.LABEL_FONT;
            const label = `${style.label} POC ${data.poc.toFixed(2)}`;
            const tm = ctx.measureText(label);
            const pW = tm.width + 8, pH = 14;
            const pX = isLeft ? VP_CONFIG.MARGIN : mediaSize.width - VP_CONFIG.MARGIN - pW;
            ctx.fillStyle = 'rgba(0,0,0,0.80)';
            _roundRect(ctx, pX, pocY - pH - 2, pW, pH, 3); ctx.fill();
            ctx.strokeStyle = pocColor; ctx.lineWidth = 0.5;
            _roundRect(ctx, pX, pocY - pH - 2, pW, pH, 3); ctx.stroke();
            ctx.fillStyle = pocColor; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(label, pX + 4, pocY - pH / 2 - 2 + pH / 2);
            ctx.restore();
        }
    }

    // VAH / VAL
    if (_showVALines) {
        for (const [level, lbl] of [[data.vah, 'VAH'], [data.val, 'VAL']]) {
            if (level == null) continue;
            const ly = priceConverter(level);
            if (ly == null || isNaN(ly)) continue;
            ctx.save();
            ctx.strokeStyle = vaColor; ctx.lineWidth = VP_CONFIG.VAH_VAL_WIDTH;
            ctx.setLineDash(VP_CONFIG.VAH_VAL_DASH);
            ctx.beginPath(); ctx.moveTo(lineStart, ly); ctx.lineTo(lineEnd, ly); ctx.stroke();
            ctx.setLineDash([]);
            ctx.font = VP_CONFIG.LABEL_FONT;
            const txt = `${style.label} ${lbl} ${level.toFixed(2)}`;
            const tm2 = ctx.measureText(txt);
            const w2 = tm2.width + 8, h2 = 13;
            const x2 = isLeft ? VP_CONFIG.MARGIN : mediaSize.width - VP_CONFIG.MARGIN - w2;
            const y2 = lbl === 'VAH' ? ly - h2 - 2 : ly + 3;
            ctx.fillStyle = 'rgba(0,0,0,0.75)';
            _roundRect(ctx, x2, y2, w2, h2, 3); ctx.fill();
            ctx.fillStyle = vaColor; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(txt, x2 + 4, y2 + h2 / 2);
            ctx.restore();
        }
    }

    // VA shade
    if (_showVAShade) {
        const vahY = priceConverter(data.vah);
        const valY = priceConverter(data.val);
        if (vahY != null && valY != null && !isNaN(vahY) && !isNaN(valY)) {
            ctx.save();
            ctx.fillStyle = _rgba(buyColor, 0.04);
            ctx.fillRect(lineStart, Math.min(vahY, valY), lineEnd - lineStart, Math.abs(valY - vahY));
            ctx.restore();
        }
    }

    // ── Initial Balance (IB) Lines ──
    if (_showIBLines && data.ib_high != null && data.ib_low != null) {
        const ibHY = priceConverter(data.ib_high);
        const ibLY = priceConverter(data.ib_low);
        if (ibHY != null && ibLY != null && !isNaN(ibHY) && !isNaN(ibLY)) {
            ctx.save();
            const ibColor = 'rgba(255,176,0,0.6)';  // amber
            const ibDimColor = 'rgba(255,176,0,0.25)';
            ctx.font = VP_CONFIG.LABEL_FONT;

            // IB High line
            ctx.strokeStyle = ibColor; ctx.lineWidth = 1.5;
            ctx.setLineDash([8, 4]);
            ctx.beginPath(); ctx.moveTo(0, ibHY); ctx.lineTo(mediaSize.width, ibHY); ctx.stroke();

            // IB Low line
            ctx.beginPath(); ctx.moveTo(0, ibLY); ctx.lineTo(mediaSize.width, ibLY); ctx.stroke();
            ctx.setLineDash([]);

            // IB shade between high and low
            ctx.fillStyle = 'rgba(255,176,0,0.03)';
            ctx.fillRect(0, Math.min(ibHY, ibLY), mediaSize.width, Math.abs(ibLY - ibHY));

            // IB labels
            const ibLabelStyle = (text, x, y) => {
                const tm = ctx.measureText(text);
                const pw = tm.width + 6, ph = 12;
                ctx.fillStyle = 'rgba(0,0,0,0.80)';
                _roundRect(ctx, x, y, pw, ph, 2); ctx.fill();
                ctx.strokeStyle = ibColor; ctx.lineWidth = 0.5;
                _roundRect(ctx, x, y, pw, ph, 2); ctx.stroke();
                ctx.fillStyle = ibColor; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
                ctx.fillText(text, x + 3, y + ph / 2);
            };
            ibLabelStyle(`IB-H ${data.ib_high.toFixed(2)}`, mediaSize.width - 120, ibHY - 14);
            ibLabelStyle(`IB-L ${data.ib_low.toFixed(2)}`, mediaSize.width - 120, ibLY + 2);

            // IB extension lines (if available)
            if (data.ib_ext) {
                ctx.setLineDash([3, 6]);
                ctx.lineWidth = 0.8;
                const extLabels = [
                    ['upper_0_5', '0.5x'], ['upper_1_0', '1.0x'], ['upper_1_5', '1.5x'], ['upper_2_0', '2.0x'],
                    ['lower_0_5', '0.5x'], ['lower_1_0', '1.0x'], ['lower_1_5', '1.5x'], ['lower_2_0', '2.0x'],
                ];
                for (const [key, label] of extLabels) {
                    const price = data.ib_ext[key];
                    if (price == null) continue;
                    const ey = priceConverter(price);
                    if (ey == null || isNaN(ey)) continue;
                    ctx.strokeStyle = ibDimColor;
                    ctx.beginPath(); ctx.moveTo(0, ey); ctx.lineTo(mediaSize.width, ey); ctx.stroke();
                    // Tiny label
                    ctx.fillStyle = ibDimColor; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
                    ctx.fillText(`IB ${label}`, mediaSize.width - 8, ey - 2);
                }
                ctx.setLineDash([]);
            }
            ctx.restore();
        }
    }
}

function _roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y, x + w, y + r, r); ctx.lineTo(x + w, y + h - r);
    ctx.arcTo(x + w, y + h, x + w - r, y + h, r); ctx.lineTo(x + r, y + h);
    ctx.arcTo(x, y + h, x, y + h - r, r); ctx.lineTo(x, y + r);
    ctx.arcTo(x, y, x + r, y, r); ctx.closePath();
}

// ── MotiveWave Settings Panel ──
function _buildSettingsPanel() {
    const panel = document.getElementById('vp-settings-panel');
    if (!panel) return;
    panel.innerHTML = `
        <div class="hm-settings-header">
            <span><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="rgba(255,215,0,.6)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:3px"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg> VOLUME PROFILE</span>
            <button class="hm-settings-close" id="vp-settings-close">\u2715</button>
        </div>
        <div class="hm-settings-body">

            <!-- PROFILES -->
            <div class="hm-group" data-open="true">
                <div class="hm-group-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                    <span class="hm-group-arrow">\u25B8</span> Profiles
                </div>
                <div class="hm-group-body">
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-prior-day" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">Prior Day <span style="color:#ff8c00;font-size:7px">\u25CF</span></span>
                        <span class="hm-controls" style="font-size:7px;color:rgba(160,170,200,.5)">prev session</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-session" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">Developing <span style="color:#00ffff;font-size:7px">\u25CF</span></span>
                        <span class="hm-controls" style="font-size:7px;color:rgba(160,170,200,.5)">current session</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-rolling-1h"><span class="hm-slider"></span></label>
                        <span class="hm-label">Rolling 1H</span>
                        <span class="hm-controls" style="font-size:7px;color:rgba(160,170,200,.5)">last 1 hour</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-rolling-2h"><span class="hm-slider"></span></label>
                        <span class="hm-label">Rolling 2H</span>
                        <span class="hm-controls" style="font-size:7px;color:rgba(160,170,200,.5)">last 2 hours</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-rolling-4h"><span class="hm-slider"></span></label>
                        <span class="hm-label">Rolling 4H <span style="color:#00ff96;font-size:7px">\u25CF</span></span>
                        <span class="hm-controls" style="font-size:7px;color:rgba(160,170,200,.5)">last 4 hours</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-2day"><span class="hm-slider"></span></label>
                        <span class="hm-label">2-Day Composite</span>
                        <span class="hm-controls" style="font-size:7px;color:rgba(160,170,200,.5)">2 sessions</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-weekly"><span class="hm-slider"></span></label>
                        <span class="hm-label">Weekly</span>
                        <span class="hm-controls" style="font-size:7px;color:rgba(160,170,200,.5)">7 days</span>
                    </div>
                </div>
            </div>

            <!-- RESOLUTION -->
            <div class="hm-group" data-open="true">
                <div class="hm-group-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                    <span class="hm-group-arrow">\u25B8</span> Resolution
                </div>
                <div class="hm-group-body">
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:4px;font-size:9px">Row Count</span>
                        <div class="hm-controls" style="flex:1">
                            <select id="vps-row-count" style="background:rgba(255,255,255,.05);border:1px solid rgba(100,120,180,.2);color:rgba(200,210,230,.85);font-size:9px;padding:2px 4px;border-radius:4px;font-family:inherit">
                                <option value="0" selected>Auto</option>
                                <option value="50">50</option>
                                <option value="100">100</option>
                                <option value="200">200</option>
                                <option value="500">500</option>
                            </select>
                        </div>
                    </div>
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:4px;font-size:9px">Value Area</span>
                        <div class="hm-controls" style="flex:1">
                            <input type="range" min="50" max="90" value="70" id="vps-va-pct">
                            <span id="vps-va-pct-val" style="font-size:8px;color:rgba(160,170,200,.5);min-width:24px;text-align:right">70%</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- DISPLAY -->
            <div class="hm-group" data-open="true">
                <div class="hm-group-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                    <span class="hm-group-arrow">\u25B8</span> Display
                </div>
                <div class="hm-group-body">
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-poc-line" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">POC Line</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-va-lines" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">VAH / VAL Lines</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-va-shade" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">Value Area Shade</span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-delta-mode"><span class="hm-slider"></span></label>
                        <span class="hm-label">Delta Mode <span style="color:#00dc78;font-size:7px">\u25CF</span><span style="color:#ff3c50;font-size:7px;margin-left:1px">\u25CF</span></span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-hvn-lvn" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">HVN / LVN <span style="color:#00c8ff;font-size:7px">\u25C6</span><span style="color:#ff6432;font-size:7px;margin-left:2px">\u25B3</span></span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-ib-lines" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">Initial Balance <span style="color:#ffb000;font-size:7px">\u25CF</span></span>
                    </div>
                    <div class="hm-row">
                        <label class="hm-toggle"><input type="checkbox" id="vps-extend" checked><span class="hm-slider"></span></label>
                        <span class="hm-label">Extend Lines</span>
                    </div>
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:28px;font-size:9px;color:rgba(160,170,200,.6)">Bar Width</span>
                        <div class="hm-controls" style="flex:1">
                            <input type="range" min="10" max="50" value="25" id="vps-bar-width">
                            <span id="vps-bar-width-val" style="font-size:8px;color:rgba(160,170,200,.5);min-width:24px;text-align:right">25%</span>
                        </div>
                    </div>
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:28px;font-size:9px;color:rgba(160,170,200,.6)">Opacity</span>
                        <div class="hm-controls" style="flex:1">
                            <input type="range" min="20" max="90" value="55" id="vps-opacity">
                            <span id="vps-opacity-val" style="font-size:8px;color:rgba(160,170,200,.5);min-width:24px;text-align:right">55%</span>
                        </div>
                    </div>
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:28px;font-size:9px;color:rgba(160,170,200,.6)">Side</span>
                        <div class="hm-controls" style="flex:1;gap:4px">
                            <button class="t-btn t-btn-xs active" id="vps-side-left" style="font-size:8px">Left</button>
                            <button class="t-btn t-btn-xs" id="vps-side-right" style="font-size:8px">Right</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- COLORS -->
            <div class="hm-group" data-open="true">
                <div class="hm-group-header" onclick="this.parentElement.dataset.open = this.parentElement.dataset.open === 'true' ? 'false' : 'true'">
                    <span class="hm-group-arrow">\u25B8</span> Colors
                </div>
                <div class="hm-group-body">
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:4px">Buy</span>
                        <div class="hm-controls"><input type="color" id="vps-buy-color" value="#00e6e6"></div>
                    </div>
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:4px">Sell</span>
                        <div class="hm-controls"><input type="color" id="vps-sell-color" value="#ff6496"></div>
                    </div>
                    <div class="hm-row">
                        <span class="hm-label" style="padding-left:4px">POC</span>
                        <div class="hm-controls"><input type="color" id="vps-poc-color" value="#ffd700"></div>
                    </div>
                </div>
            </div>

            <div style="padding:6px 4px;font-size:8px;color:rgba(140,160,200,.4);text-align:center;border-top:1px solid rgba(100,120,180,.1);margin-top:4px">
                TopStepX NQ 1m \u00b7 Poll 10s
            </div>
        </div>
    `;

    // Wire close
    document.getElementById('vp-settings-close').addEventListener('click', () => { panel.style.display = 'none'; });

    // Profiles
    const profileMap = {
        'vps-prior-day': 'prior_day', 'vps-session': 'session',
        'vps-rolling-1h': 'rolling_1h', 'vps-rolling-2h': 'rolling_2h',
        'vps-rolling-4h': 'rolling_4h', 'vps-2day': '2day', 'vps-weekly': 'weekly',
    };
    for (const [id, mode] of Object.entries(profileMap)) {
        document.getElementById(id).addEventListener('change', (e) => {
            if (e.target.checked) {
                if (!_activeProfiles.includes(mode)) _activeProfiles.push(mode);
                _fetchProfile(mode);
            } else {
                _activeProfiles = _activeProfiles.filter(m => m !== mode);
            }
            _syncToolbarButtons();
        });
    }

    // Resolution
    document.getElementById('vps-row-count').addEventListener('change', (e) => {
        VP_CONFIG.ROW_COUNT = parseInt(e.target.value);
        _pollProfiles();
    });
    const vaPctSlider = document.getElementById('vps-va-pct');
    const vaPctVal = document.getElementById('vps-va-pct-val');
    vaPctSlider.addEventListener('input', () => {
        VP_CONFIG.VA_PCT = parseInt(vaPctSlider.value) / 100;
        vaPctVal.textContent = vaPctSlider.value + '%';
        _pollProfiles();
    });

    // Display toggles
    document.getElementById('vps-poc-line').addEventListener('change', (e) => { _showPOCLine = e.target.checked; });
    document.getElementById('vps-va-lines').addEventListener('change', (e) => { _showVALines = e.target.checked; });
    document.getElementById('vps-va-shade').addEventListener('change', (e) => { _showVAShade = e.target.checked; });
    document.getElementById('vps-delta-mode').addEventListener('change', (e) => { _deltaMode = e.target.checked; });
    document.getElementById('vps-hvn-lvn').addEventListener('change', (e) => { _showHVNLVN = e.target.checked; });
    document.getElementById('vps-ib-lines').addEventListener('change', (e) => { _showIBLines = e.target.checked; });
    document.getElementById('vps-extend').addEventListener('change', (e) => { VP_CONFIG.EXTEND_LINES = e.target.checked; });

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
}

function _syncToolbarButtons() {
    document.querySelectorAll('#t-vp-modes .t-btn').forEach(btn => {
        btn.classList.toggle('active', _activeProfiles.includes(btn.dataset.vp));
    });
}

function _syncPanelFromState() {
    const el = (id) => document.getElementById(id);
    if (el('vps-prior-day')) el('vps-prior-day').checked = _activeProfiles.includes('prior_day');
    if (el('vps-session')) el('vps-session').checked = _activeProfiles.includes('session');
    if (el('vps-rolling-1h')) el('vps-rolling-1h').checked = _activeProfiles.includes('rolling_1h');
    if (el('vps-rolling-2h')) el('vps-rolling-2h').checked = _activeProfiles.includes('rolling_2h');
    if (el('vps-rolling-4h')) el('vps-rolling-4h').checked = _activeProfiles.includes('rolling_4h');
    if (el('vps-2day')) el('vps-2day').checked = _activeProfiles.includes('2day');
    if (el('vps-weekly')) el('vps-weekly').checked = _activeProfiles.includes('weekly');
    if (el('vps-poc-line')) el('vps-poc-line').checked = _showPOCLine;
    if (el('vps-va-lines')) el('vps-va-lines').checked = _showVALines;
    if (el('vps-va-shade')) el('vps-va-shade').checked = _showVAShade;
    if (el('vps-delta-mode')) el('vps-delta-mode').checked = _deltaMode;
    if (el('vps-hvn-lvn')) el('vps-hvn-lvn').checked = _showHVNLVN;
    if (el('vps-ib-lines')) el('vps-ib-lines').checked = _showIBLines;
    if (el('vps-extend')) el('vps-extend').checked = VP_CONFIG.EXTEND_LINES;
    if (el('vps-row-count')) el('vps-row-count').value = String(VP_CONFIG.ROW_COUNT);
    if (el('vps-va-pct')) {
        el('vps-va-pct').value = Math.round(VP_CONFIG.VA_PCT * 100);
        el('vps-va-pct-val').textContent = Math.round(VP_CONFIG.VA_PCT * 100) + '%';
    }
    if (el('vps-buy-color')) el('vps-buy-color').value = _rgbToHex(VP_CONFIG.BUY_COLOR);
    if (el('vps-sell-color')) el('vps-sell-color').value = _rgbToHex(VP_CONFIG.SELL_COLOR);
    if (el('vps-poc-color')) el('vps-poc-color').value = VP_CONFIG.POC_COLOR;
}

// ── Series Primitive (per-instance) ──
function _createPrimitive(seriesRef, containerRef) {
    const vpRenderer = {
        draw(target) {
            if (!_overlayVisible) return;
            // Per-pane toggle check
            if (containerRef && containerRef._overlayConfig && !containerRef._overlayConfig.vp) return;
            if (!seriesRef) return;
            try {
                target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
                    for (const mode of _activeProfiles) {
                        const cached = _profiles[mode];
                        if (cached && cached.data) {
                            const priceConverter = (price) => {
                                try { return seriesRef.priceToCoordinate(price); } catch(e) { return null; }
                            };
                            _renderProfile(ctx, mediaSize, priceConverter, mode, cached.data);
                        }
                    }
                });
            } catch(e) { /* LWC not ready yet */ }
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
        // Avoid duplicate attachment for same container
        if (container && _vpInstances.find(i => i.container === container)) return;
        const primitive = _createPrimitive(candleSeries, container);
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
    getActiveProfiles() { return [..._activeProfiles]; },
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
