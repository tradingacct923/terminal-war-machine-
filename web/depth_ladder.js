/**
 * Depth Ladder Renderer — Canvas 2D live order book visualization
 *
 * Extracted from volume_bubbles.js for maintainability.
 * Renders the current book as a vertical ladder with bid/ask bars + price labels.
 *
 * Phase 1 Enhancements:
 *   - Size heatmap cells: rows shaded by relative depth (larger = brighter)
 *   - Pull/Stack detection: tracks size deltas frame-to-frame, flashes on ±25%+
 *   - Absorption highlighting: pulsing glow when price hits a level but doesn't move
 *   - Last trade marker: shows where the last fill happened on the ladder
 *   - Cumulative delta strip: thin bar in footer showing buy vs sell aggression
 *
 * Dependencies: KineticText (optional, for WebGL overlay)
 * Exports: window.renderDepthLadder
 */

// ═══════════════════════════════════════════════════════════════════════════
// DEPTH LADDER RENDERER — Live DOM with bid/ask bars + price labels
// ═══════════════════════════════════════════════════════════════════════════

// ── Pull/Stack memory — persists across frames ──
const _ladderMemory = {};    // { priceKey: { size, ts } }
const _ladderFlash = {};     // { priceKey: { type:'pull'|'stack', ts, delta } }
const FLASH_DURATION = 800;  // ms to show flash

// ── Cumulative delta — running aggression tracker ──
let _cumulativeDelta = 0;
let _deltaMax = 1;  // for normalization

// ── Canvas context + gradient cache (avoid per-frame re-creation) ──
let _ladderCtxCache = null;
let _ladderCtxCanvas = null;
let _ladderGradCache = null;
let _ladderGradW = 0;
let _ladderGradH = 0;

function renderDepthLadder(canvas, priceToY, domData, midPrice) {
    if (!canvas || !domData || !midPrice) return;

    // ── Reuse canvas context (avoid per-frame getContext overhead) ──
    if (_ladderCtxCanvas !== canvas) {
        _ladderCtxCache = canvas.getContext('2d');
        _ladderCtxCanvas = canvas;
        _ladderGradCache = null; // invalidate gradient cache
    }
    const ctx = _ladderCtxCache;
    if (!ctx) return;

    const cssRect = canvas.getBoundingClientRect();
    const cssW = cssRect.width;
    const cssH = cssRect.height;
    if (cssW <= 0 || cssH <= 0) return;

    const dpr = window.devicePixelRatio || 1;
    const _kineticActive = (typeof KineticText !== 'undefined' && KineticText.programValid);
    const now = Date.now();

    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
        _ladderGradCache = null; // invalidate gradient cache on resize
    }

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';

    // ── Deep space background (cached gradient) ──
    if (!_ladderGradCache || _ladderGradW !== cssW || _ladderGradH !== cssH) {
        _ladderGradW = cssW;
        _ladderGradH = cssH;
        const bgGrad = ctx.createLinearGradient(0, 0, 0, cssH);
        bgGrad.addColorStop(0, 'rgba(6, 9, 20, 0.98)');
        bgGrad.addColorStop(0.5, 'rgba(8, 11, 24, 0.98)');
        bgGrad.addColorStop(1, 'rgba(6, 8, 18, 0.98)');
        _ladderGradCache = bgGrad;
    }
    ctx.fillStyle = _ladderGradCache;
    ctx.fillRect(0, 0, cssW, cssH);

    const bids = domData.bids || {};
    const asks = domData.asks || {};

    const bidEntries = Object.entries(bids)
        .map(([p, s]) => [parseFloat(p), s])
        .filter(e => !isNaN(e[0]) && e[1] > 0)
        .sort((a, b) => b[0] - a[0]);

    const askEntries = Object.entries(asks)
        .map(([p, s]) => [parseFloat(p), s])
        .filter(e => !isNaN(e[0]) && e[1] > 0)
        .sort((a, b) => a[0] - b[0]);

    const bestBid = bidEntries.length ? bidEntries[0][0] : midPrice - 0.25;
    const bestAsk = askEntries.length ? askEntries[0][0] : midPrice + 0.25;
    const mid = (bestBid + bestAsk) / 2;
    const TICK = 0.25;

    const ROW_H = 18;
    const HALF_ROW = ROW_H / 2;
    const HEADER_H = 26;
    const FOOTER_H = 32;  // slightly taller for delta strip
    const usableH = cssH - HEADER_H - FOOTER_H;
    const maxRows = Math.floor(usableH / ROW_H);
    const halfRows = Math.floor(maxRows / 2);

    const centerPrice = Math.round(mid / TICK) * TICK;
    const visiblePrices = [];
    for (let i = -halfRows; i <= halfRows; i++) {
        visiblePrices.push(Math.round((centerPrice + i * TICK) * 100) / 100);
    }
    visiblePrices.sort((a, b) => b - a);

    const ladderToY = (price) => {
        const idx = visiblePrices.indexOf(price);
        if (idx === -1) return null;
        return HEADER_H + idx * ROW_H + HALF_ROW;
    };

    // ── Layout columns ──
    const PRICE_COL_W = 72;
    const SIZE_COL_W = 30;
    const BAR_AREA_W = Math.max((cssW - PRICE_COL_W - SIZE_COL_W * 2) / 2, 20);
    const bidBarRight = SIZE_COL_W + BAR_AREA_W;
    const priceLeft = bidBarRight;
    const priceRight = priceLeft + PRICE_COL_W;
    const askBarLeft = priceRight;
    const MAX_BAR_W = BAR_AREA_W - 2;

    // ── Max depth for normalization ──
    let maxDepth = 1;
    for (const [, s] of bidEntries) maxDepth = Math.max(maxDepth, s);
    for (const [, s] of askEntries) maxDepth = Math.max(maxDepth, s);

    // ── Totals for summary ──
    let totalBid = 0, totalAsk = 0;
    for (const [, s] of bidEntries) totalBid += s;
    for (const [, s] of askEntries) totalAsk += s;

    // ── Absorption data (from L2 worker via app.js) ──
    const absorptionData = (window._latestHeatmapData && window._latestHeatmapData.domData)
        ? window._latestHeatmapData.domData._absorption || {}
        : {};

    // ── Last trade info (from tape) ──
    const lastTrade = window._lastTradeForLadder || null;

    const fmtPrice = (p) => {
        const parts = p.toFixed(2).split('.');
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        return parts.join('.');
    };

    // ── PHASE 1: Pull/Stack detection — compare current sizes to previous frame ──
    for (const price of visiblePrices) {
        const pKey = price.toFixed(2);
        const pKey1 = price.toFixed(1);
        const bidSize = bids[pKey] || bids[pKey1] || bids[price.toString()] || 0;
        const askSize = asks[pKey] || asks[pKey1] || asks[price.toString()] || 0;
        const currentSize = bidSize + askSize;
        const prev = _ladderMemory[pKey];

        if (prev && prev.size > 0 && currentSize > 0) {
            const delta = currentSize - prev.size;
            const pct = delta / prev.size;

            if (pct > 0.30) {
                // Stack — size added significantly at this level
                _ladderFlash[pKey] = { type: 'stack', ts: now, delta: delta };
            } else if (pct < -0.30) {
                // Pull — size removed significantly
                _ladderFlash[pKey] = { type: 'pull', ts: now, delta: delta };
            }
        } else if (prev && prev.size > 3 && currentSize === 0) {
            // Full pull — level emptied
            _ladderFlash[pKey] = { type: 'pull', ts: now, delta: -prev.size };
        } else if ((!prev || prev.size === 0) && currentSize > 3) {
            // New stack — level appeared from nothing
            _ladderFlash[pKey] = { type: 'stack', ts: now, delta: currentSize };
        }

        _ladderMemory[pKey] = { size: currentSize, ts: now };
    }

    // ── Draw each price level ──
    for (const price of visiblePrices) {
        const y = ladderToY(price);
        if (y === null || y < HEADER_H - HALF_ROW || y > cssH - FOOTER_H + HALF_ROW) continue;

        const pKey2 = price.toFixed(2);
        const pKey1 = price.toFixed(1);
        const pKey = price.toString();
        const bidSize = bids[pKey2] || bids[pKey1] || bids[pKey] || 0;
        const askSize = asks[pKey2] || asks[pKey1] || asks[pKey] || 0;

        const isCurrentPrice = Math.abs(price - mid) < TICK * 0.6;
        if (bidSize === 0 && askSize === 0 && !isCurrentPrice) continue;

        const isBestBid = Math.abs(price - bestBid) < TICK * 0.1;
        const isBestAsk = Math.abs(price - bestAsk) < TICK * 0.1;
        const rowTop = y - HALF_ROW;

        // ═══ ENGINE 3: Level Survival — row background tint ═══
        // Blue = high P(hold), Orange = low P(hold). Derived from Beta posterior.
        const survData = window._levelSurvival || {};
        const survVal = survData[pKey2] !== undefined ? survData[pKey2] : null;  // 0 is valid (certain break)
        if (survVal !== null && !isCurrentPrice) {
            const survAlpha = 0.02 + survVal * 0.13;  // 2-15% opacity
            ctx.fillStyle = survVal > 0.5
                ? `rgba(0, 180, 255, ${survAlpha.toFixed(3)})`    // blue = will hold
                : `rgba(255, 140, 60, ${survAlpha.toFixed(3)})`;  // orange = will break
            ctx.fillRect(0, rowTop, cssW, ROW_H);
        }

        // ═══ ENHANCEMENT 1: Size heatmap background ═══
        // Shade the entire row by relative size — larger walls glow brighter
        const totalSizeAtLevel = bidSize + askSize;
        if (totalSizeAtLevel > 0 && !isCurrentPrice) {
            const sizeNorm = Math.min(totalSizeAtLevel / maxDepth, 1.0);
            // Use log scaling so mid-range sizes are visible
            const logNorm = Math.log1p(sizeNorm * 10) / Math.log1p(10);
            const heatAlpha = logNorm * 0.12; // subtle heat

            if (bidSize > askSize) {
                ctx.fillStyle = `rgba(0, 232, 123, ${heatAlpha.toFixed(3)})`;
            } else {
                ctx.fillStyle = `rgba(255, 59, 92, ${heatAlpha.toFixed(3)})`;
            }
            ctx.fillRect(0, rowTop, cssW, ROW_H);
        }

        // ═══ ENHANCEMENT 2: Pull/Stack flash overlay ═══
        const flash = _ladderFlash[pKey2];
        if (flash && (now - flash.ts) < FLASH_DURATION) {
            const progress = (now - flash.ts) / FLASH_DURATION;
            const fadeAlpha = (1 - progress) * 0.25;

            if (flash.type === 'stack') {
                // Cyan flash — size added
                ctx.fillStyle = `rgba(0, 210, 255, ${fadeAlpha.toFixed(3)})`;
            } else {
                // Magenta flash — size pulled
                ctx.fillStyle = `rgba(255, 50, 180, ${fadeAlpha.toFixed(3)})`;
            }
            ctx.fillRect(0, rowTop, cssW, ROW_H);
        }

        // ═══ ENHANCEMENT 3: Absorption highlighting ═══
        // If L2 worker detected absorption at this price, pulse cyan
        const absLevel = absorptionData[pKey2] || absorptionData[pKey1] || absorptionData[pKey];
        if (absLevel && absLevel > 0) {
            const pulse = 0.5 + 0.5 * Math.sin(now / 300); // pulsing effect
            const absAlpha = Math.min(absLevel, 1.0) * 0.15 * pulse;
            ctx.fillStyle = `rgba(0, 255, 220, ${absAlpha.toFixed(3)})`;
            ctx.fillRect(0, rowTop, cssW, ROW_H);

            // Thin cyan left edge indicator
            ctx.fillStyle = `rgba(0, 255, 220, ${(0.4 * pulse).toFixed(2)})`;
            ctx.fillRect(0, rowTop, 2, ROW_H);
        }

        // ── Row background (original logic on top of heatmap) ──
        if (isCurrentPrice) {
            const cpGrad = ctx.createLinearGradient(0, rowTop, cssW, rowTop);
            cpGrad.addColorStop(0, 'rgba(255, 220, 50, 0.12)');
            cpGrad.addColorStop(0.5, 'rgba(255, 220, 50, 0.08)');
            cpGrad.addColorStop(1, 'rgba(255, 220, 50, 0.12)');
            ctx.fillStyle = cpGrad;
            ctx.fillRect(0, rowTop, cssW, ROW_H);

            ctx.shadowColor = 'rgba(255, 220, 50, 0.25)';
            ctx.shadowBlur = 8;
            ctx.strokeStyle = 'rgba(255, 220, 50, 0.4)';
            ctx.lineWidth = 1;
            ctx.strokeRect(1, rowTop + 0.5, cssW - 2, ROW_H - 1);
            ctx.shadowBlur = 0;
        } else if (isBestBid) {
            ctx.fillStyle = 'rgba(0, 230, 120, 0.06)';
            ctx.fillRect(0, rowTop, cssW, ROW_H);
        } else if (isBestAsk) {
            ctx.fillStyle = 'rgba(255, 60, 92, 0.06)';
            ctx.fillRect(0, rowTop, cssW, ROW_H);
        } else if (bidSize > 0 && askSize === 0) {
            ctx.fillStyle = 'rgba(0, 200, 120, 0.015)';
            ctx.fillRect(0, rowTop, cssW, ROW_H);
        } else if (askSize > 0 && bidSize === 0) {
            ctx.fillStyle = 'rgba(255, 60, 92, 0.015)';
            ctx.fillRect(0, rowTop, cssW, ROW_H);
        }

        // ── Grid line ──
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.025)';
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(0, rowTop + ROW_H - 0.5);
        ctx.lineTo(cssW, rowTop + ROW_H - 0.5);
        ctx.stroke();

        // ── Bid bar (green, grows LEFT) ──
        if (bidSize > 0) {
            const norm = Math.min(bidSize / maxDepth, 1.0);
            const barW = norm * MAX_BAR_W;
            const barX = bidBarRight - barW;
            const barTop = rowTop + 1.5;
            const barH = ROW_H - 3;

            const bidGrad = ctx.createLinearGradient(barX, 0, bidBarRight, 0);
            const intensity = 0.3 + norm * 0.5;
            bidGrad.addColorStop(0, `rgba(0, 232, 123, ${(intensity * 0.4).toFixed(2)})`);
            bidGrad.addColorStop(1, `rgba(0, 232, 123, ${intensity.toFixed(2)})`);
            ctx.fillStyle = bidGrad;

            const r = Math.min(3, barH / 2);
            ctx.beginPath();
            ctx.moveTo(barX + r, barTop);
            ctx.lineTo(bidBarRight, barTop);
            ctx.lineTo(bidBarRight, barTop + barH);
            ctx.lineTo(barX + r, barTop + barH);
            ctx.arcTo(barX, barTop + barH, barX, barTop + barH - r, r);
            ctx.lineTo(barX, barTop + r);
            ctx.arcTo(barX, barTop, barX + r, barTop, r);
            ctx.fill();

            if (norm > 0.6) {
                ctx.shadowColor = 'rgba(0, 232, 123, 0.3)';
                ctx.shadowBlur = 6;
                ctx.fill();
                ctx.shadowBlur = 0;
            }

            // ═══ ENGINE 1: Queue Dynamics — bid bar glow ═══
            const qdData = window._queueDynamics || {};
            const qdBid = qdData[pKey2];  // backend keys are always .toFixed(2) format
            if (qdBid && qdBid.ratio != null) {
                const qr = qdBid.ratio;
                if (qr > 1.2) {
                    ctx.shadowColor = `rgba(0,255,180,${Math.min((qr-1)*0.15, 0.35).toFixed(2)})`;
                    ctx.shadowBlur = 4 + Math.min((qr - 1) * 3, 8);
                    ctx.fill();
                    ctx.shadowBlur = 0;
                } else if (qr < 0.8) {
                    ctx.shadowColor = `rgba(255,80,60,${Math.min((1-qr)*0.15, 0.35).toFixed(2)})`;
                    ctx.shadowBlur = 4 + Math.min((1 - qr) * 3, 8);
                    ctx.fill();
                    ctx.shadowBlur = 0;
                }
            }

            if (!_kineticActive) {
                ctx.font = `${ROW_H >= 16 ? 9 : 7}px "JetBrains Mono", monospace`;
                ctx.textBaseline = 'middle';
                if (barW > 24) {
                    ctx.fillStyle = 'rgba(255, 255, 255, 0.92)';
                    ctx.textAlign = 'left';
                    ctx.fillText(bidSize.toString(), barX + 4, y);
                } else {
                    ctx.fillStyle = 'rgba(0, 232, 123, 0.75)';
                    ctx.textAlign = 'right';
                    ctx.fillText(bidSize.toString(), barX - 3, y);
                }
            }
        }

        // ── Ask bar (red, grows RIGHT) ──
        if (askSize > 0) {
            const norm = Math.min(askSize / maxDepth, 1.0);
            const barW = norm * MAX_BAR_W;
            const barX = askBarLeft;
            const barTop = rowTop + 1.5;
            const barH = ROW_H - 3;

            const askGrad = ctx.createLinearGradient(barX, 0, barX + barW, 0);
            const intensity = 0.3 + norm * 0.5;
            askGrad.addColorStop(0, `rgba(255, 59, 92, ${intensity.toFixed(2)})`);
            askGrad.addColorStop(1, `rgba(255, 59, 92, ${(intensity * 0.4).toFixed(2)})`);
            ctx.fillStyle = askGrad;

            const r = Math.min(3, barH / 2);
            ctx.beginPath();
            ctx.moveTo(barX, barTop);
            ctx.lineTo(barX + barW - r, barTop);
            ctx.arcTo(barX + barW, barTop, barX + barW, barTop + r, r);
            ctx.lineTo(barX + barW, barTop + barH - r);
            ctx.arcTo(barX + barW, barTop + barH, barX + barW - r, barTop + barH, r);
            ctx.lineTo(barX, barTop + barH);
            ctx.fill();

            if (norm > 0.6) {
                ctx.shadowColor = 'rgba(255, 59, 92, 0.3)';
                ctx.shadowBlur = 6;
                ctx.fill();
                ctx.shadowBlur = 0;
            }

            // ═══ ENGINE 1: Queue Dynamics — ask bar glow ═══
            const qdAsk = (window._queueDynamics || {})[pKey2];  // backend keys are always .toFixed(2) format
            if (qdAsk && qdAsk.ratio != null) {
                const qra = qdAsk.ratio;
                if (qra > 1.2) {
                    ctx.shadowColor = `rgba(0,255,180,${Math.min((qra-1)*0.15, 0.35).toFixed(2)})`;
                    ctx.shadowBlur = 4 + Math.min((qra - 1) * 3, 8);
                    ctx.fill();
                    ctx.shadowBlur = 0;
                } else if (qra < 0.8) {
                    ctx.shadowColor = `rgba(255,80,60,${Math.min((1-qra)*0.15, 0.35).toFixed(2)})`;
                    ctx.shadowBlur = 4 + Math.min((1 - qra) * 3, 8);
                    ctx.fill();
                    ctx.shadowBlur = 0;
                }
            }

            if (!_kineticActive) {
                ctx.font = `${ROW_H >= 16 ? 9 : 7}px "JetBrains Mono", monospace`;
                ctx.textBaseline = 'middle';
                if (barW > 24) {
                    ctx.fillStyle = 'rgba(255, 255, 255, 0.92)';
                    ctx.textAlign = 'right';
                    ctx.fillText(askSize.toString(), barX + barW - 4, y);
                } else {
                    ctx.fillStyle = 'rgba(255, 59, 92, 0.75)';
                    ctx.textAlign = 'left';
                    ctx.fillText(askSize.toString(), barX + barW + 3, y);
                }
            }
        }

        // ═══ ENHANCEMENT 4: Last trade marker ═══
        if (lastTrade && Math.abs(price - lastTrade.price) < TICK * 0.1) {
            const isBuy = lastTrade.side === 'buy' || lastTrade.side === 'B';
            const arrowColor = isBuy ? 'rgba(0, 232, 123, 0.9)' : 'rgba(255, 59, 92, 0.9)';
            ctx.fillStyle = arrowColor;
            ctx.font = 'bold 8px "JetBrains Mono", monospace';
            ctx.textBaseline = 'middle';
            if (isBuy) {
                ctx.textAlign = 'right';
                ctx.fillText('◄', priceLeft - 1, y); // arrow points left = buy (bid lifted)
            } else {
                ctx.textAlign = 'left';
                ctx.fillText('►', priceRight + 1, y); // arrow points right = sell (ask hit)
            }
        }

        // ── Pull/Stack delta indicator (tiny +/- on right edge) ──
        if (flash && (now - flash.ts) < FLASH_DURATION && !_kineticActive) {
            const progress = (now - flash.ts) / FLASH_DURATION;
            const fadeAlpha = 1 - progress;
            const sign = flash.type === 'stack' ? '+' : '';
            ctx.font = '7px "JetBrains Mono", monospace';
            ctx.textAlign = 'right';
            ctx.textBaseline = 'middle';
            ctx.fillStyle = flash.type === 'stack'
                ? `rgba(0, 210, 255, ${fadeAlpha.toFixed(2)})`
                : `rgba(255, 50, 180, ${fadeAlpha.toFixed(2)})`;
            ctx.fillText(`${sign}${flash.delta}`, cssW - 3, y);
        }

        // ═══ ENGINE 2: Trade Toxicity — right edge indicator ═══
        // Red = informed flow (avoid posting). Green = noise (safe to post).
        const toxData = window._tradeToxicity || {};
        const toxLevel = toxData[pKey2];  // backend keys are always .toFixed(2) format
        if (toxLevel && toxLevel.t10 !== undefined && !isCurrentPrice) {
            const tox = toxLevel.t10;
            if (Math.abs(tox - 0.5) > 0.05) {
                const toxAlpha = Math.min(Math.abs(tox - 0.5) * 2, 0.8);
                ctx.fillStyle = tox > 0.5
                    ? `rgba(255, 50, 50, ${toxAlpha.toFixed(2)})`
                    : `rgba(50, 255, 120, ${toxAlpha.toFixed(2)})`;
                ctx.fillRect(cssW - 4, rowTop + 2, 3, ROW_H - 4);
            }
        }

        // ── Price label (center column) ──
        if (!_kineticActive) {
            const fontSize = ROW_H >= 16 ? 10 : 8;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';

            if (isCurrentPrice) {
                ctx.font = `bold ${fontSize}px "JetBrains Mono", monospace`;
                ctx.fillStyle = 'rgba(255, 220, 50, 0.95)';
                ctx.textAlign = 'right';
                ctx.fillText('▸', priceLeft + 6, y);
                ctx.textAlign = 'center';
            } else if (price > mid) {
                ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
                ctx.fillStyle = askSize > 0 ? 'rgba(255, 140, 150, 0.85)' : 'rgba(120, 130, 150, 0.35)';
            } else {
                ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
                ctx.fillStyle = bidSize > 0 ? 'rgba(140, 255, 180, 0.85)' : 'rgba(120, 130, 150, 0.35)';
            }

            ctx.fillText(fmtPrice(price), priceLeft + PRICE_COL_W / 2, y);
        }
    }

    // ── Center column borders ──
    ctx.strokeStyle = 'rgba(100, 115, 150, 0.08)';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(priceLeft + 0.5, 28);
    ctx.lineTo(priceLeft + 0.5, cssH - FOOTER_H);
    ctx.moveTo(priceRight - 0.5, 28);
    ctx.lineTo(priceRight - 0.5, cssH - FOOTER_H);
    ctx.stroke();

    // ── Header ──
    const headerH = 24;
    const hdrGrad = ctx.createLinearGradient(0, 0, cssW, 0);
    hdrGrad.addColorStop(0, 'rgba(0, 232, 123, 0.06)');
    hdrGrad.addColorStop(0.5, 'rgba(10, 14, 28, 0.9)');
    hdrGrad.addColorStop(1, 'rgba(255, 59, 92, 0.06)');
    ctx.fillStyle = hdrGrad;
    ctx.fillRect(0, 0, cssW, headerH);

    ctx.strokeStyle = 'rgba(100, 115, 150, 0.1)';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(0, headerH);
    ctx.lineTo(cssW, headerH);
    ctx.stroke();

    ctx.font = 'bold 9px "JetBrains Mono", monospace';
    ctx.textBaseline = 'middle';
    const hdrY = headerH / 2;

    ctx.fillStyle = 'rgba(0, 232, 123, 0.85)';
    ctx.textAlign = 'center';
    ctx.fillText('BID', SIZE_COL_W + BAR_AREA_W / 2, hdrY);

    ctx.fillStyle = 'rgba(255, 59, 92, 0.85)';
    ctx.fillText('ASK', askBarLeft + BAR_AREA_W / 2, hdrY);

    ctx.fillStyle = 'rgba(160, 170, 200, 0.6)';
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.fillText('PRICE', priceLeft + PRICE_COL_W / 2, hdrY);

    // ═══ ENHANCED FOOTER — with cumulative delta strip ═══
    const footerTop = cssH - FOOTER_H;
    ctx.fillStyle = 'rgba(8, 10, 22, 0.95)';
    ctx.fillRect(0, footerTop, cssW, FOOTER_H);

    ctx.strokeStyle = 'rgba(100, 115, 150, 0.1)';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(0, footerTop);
    ctx.lineTo(cssW, footerTop);
    ctx.stroke();

    // Row 1: Bid total / Spread / Ask total
    const row1Y = footerTop + 8;
    const total = totalBid + totalAsk;
    const imbPct = total > 0 ? (totalBid / total * 100).toFixed(0) : '50';
    const spread = (bestAsk - bestBid).toFixed(2);

    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.textBaseline = 'middle';

    ctx.fillStyle = 'rgba(0, 232, 123, 0.7)';
    ctx.textAlign = 'left';
    ctx.fillText(`B:${totalBid.toLocaleString()}`, 4, row1Y);

    ctx.fillStyle = 'rgba(255, 59, 92, 0.7)';
    ctx.textAlign = 'right';
    ctx.fillText(`A:${totalAsk.toLocaleString()}`, cssW - 4, row1Y);

    const imbColor = parseInt(imbPct) > 55
        ? 'rgba(0, 232, 123, 0.7)'
        : parseInt(imbPct) < 45
            ? 'rgba(255, 59, 92, 0.7)'
            : 'rgba(160, 170, 200, 0.5)';
    ctx.fillStyle = imbColor;
    ctx.textAlign = 'center';
    ctx.fillText(`SPD ${spread} | ${imbPct}%`, cssW / 2, row1Y);

    // ═══ Row 2: Cumulative delta strip ═══
    // Visual bar showing buy vs sell aggression balance
    const stripY = footerTop + 20;
    const stripH = 6;
    const stripPad = 8;
    const stripW = cssW - stripPad * 2;

    // Background track
    ctx.fillStyle = 'rgba(255, 255, 255, 0.04)';
    ctx.fillRect(stripPad, stripY, stripW, stripH);

    // Compute imbalance ratio
    if (total > 0) {
        const bidRatio = totalBid / total;   // 0.0 to 1.0
        const askRatio = totalAsk / total;

        // Left half = bid (green), right half = ask (red)
        // Width proportional to ratio
        const bidW = stripW * bidRatio;
        const askW = stripW * askRatio;

        // Bid bar (green, from left)
        const bidStripGrad = ctx.createLinearGradient(stripPad, 0, stripPad + bidW, 0);
        bidStripGrad.addColorStop(0, 'rgba(0, 232, 123, 0.15)');
        bidStripGrad.addColorStop(1, 'rgba(0, 232, 123, 0.5)');
        ctx.fillStyle = bidStripGrad;
        ctx.fillRect(stripPad, stripY, bidW, stripH);

        // Ask bar (red, from right)
        const askStripGrad = ctx.createLinearGradient(stripPad + bidW, 0, cssW - stripPad, 0);
        askStripGrad.addColorStop(0, 'rgba(255, 59, 92, 0.5)');
        askStripGrad.addColorStop(1, 'rgba(255, 59, 92, 0.15)');
        ctx.fillStyle = askStripGrad;
        ctx.fillRect(stripPad + bidW, stripY, askW, stripH);

        // Center marker (50% line)
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(stripPad + stripW / 2, stripY);
        ctx.lineTo(stripPad + stripW / 2, stripY + stripH);
        ctx.stroke();

        // Divider between bid/ask with glow
        const divX = stripPad + bidW;
        ctx.strokeStyle = 'rgba(255, 220, 50, 0.6)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(divX, stripY - 1);
        ctx.lineTo(divX, stripY + stripH + 1);
        ctx.stroke();
    }

    ctx.restore();

    // ── KineticText integration ──
    if (typeof KineticText !== 'undefined' && KineticText.programValid) {
        KineticText.setLadderData({
            cssW, cssH, visiblePrices, bids, asks,
            ladderToY, priceLeft, PRICE_COL_W,
            bidBarRight, askBarLeft, ROW_H, HEADER_H, FOOTER_H,
            bestBid, bestAsk, mid, bidEntries, askEntries, maxDepth
        });
    } else if (typeof KineticText !== 'undefined' && !KineticText.programValid && !KineticText._initAttempted) {
        KineticText._initAttempted = true;
        const kCanvas = document.getElementById('dom-kinetic-canvas');
        if (kCanvas) {
            KineticText.init(kCanvas);
        }
    }
}

// Export
window.renderDepthLadder = renderDepthLadder;
