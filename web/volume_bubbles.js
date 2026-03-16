/**
 * Volume Bubble Renderer — LWC Custom Series Plugin (v2)
 *
 * Renders buy/sell volume bubbles from the `bp` (bubble profile) dict
 * produced by l2_worker.py's tick classification engine.
 *
 * Features:
 *   - Institutional highlight ring (100+ lot prints glow)
 *   - Opacity by dominance (stronger imbalance = more opaque)
 *   - Absorption detection (large buy+sell at same price = special marker)
 *   - Noise filter (hides bubbles below MIN_BUBBLE_VOL)
 *
 * LOD (Level of Detail) tiers:
 *   - barSpacing > 20px  → full bubbles with text + effects
 *   - barSpacing 6-20px  → small colored dots only
 *   - barSpacing <= 5px  → nothing rendered (bird's eye)
 */

// ═══════════════════════════════════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════════════════════════════════
const BUBBLE_CONFIG = {
    // ── Thresholds ──
    MIN_BUBBLE_VOL: 10,           // min total vol to draw (filters noise during NY open)
    INSTITUTIONAL_THRESHOLD: 100, // 100+ contracts = institutional print
    ABSORPTION_MIN: 50,           // both buy AND sell must exceed this for absorption
    ABSORPTION_RATIO: 0.35,       // minor side must be at least 35% of total (no 95/5 splits)

    // ── Sizing ──
    MAX_RADIUS: 24,               // max bubble radius in px
    MIN_RADIUS: 3,                // min bubble radius
    DOT_RADIUS: 2.5,              // radius for macro-zoom dots

    // ── Colors ──
    BUY_COLOR:        [31, 209, 122],   // green RGB
    SELL_COLOR:       [224, 48, 96],     // red RGB
    ABSORPTION_COLOR: [168, 85, 247],    // purple RGB (absorption)
    TEXT_COLOR: 'rgba(255, 255, 255, 0.92)',
    NEUTRAL_COLOR: 'rgba(140, 160, 200, 0.3)',

    // ── Institutional glow ──
    GLOW_COLOR_BUY:  'rgba(31, 209, 122, 0.35)',
    GLOW_COLOR_SELL: 'rgba(224, 48, 96, 0.35)',
    GLOW_COLOR_ABSORB: 'rgba(168, 85, 247, 0.35)',
    GLOW_EXTRA_RADIUS: 6,  // px added to radius for the glow ring

    // ── Typography ──
    FONT: '10px "JetBrains Mono", "SF Mono", monospace',
    FONT_SMALL: '8px "JetBrains Mono", "SF Mono", monospace',
};

// ═══════════════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Build an rgba() string from RGB array + alpha.
 */
function _rgba(rgb, alpha) {
    return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

/**
 * Calculate dominance ratio: how one-sided the volume is.
 * Returns 0.5 (perfectly balanced) to 1.0 (completely one-sided).
 */
function _dominance(buyVol, sellVol) {
    const total = buyVol + sellVol;
    if (total === 0) return 0.5;
    return Math.max(buyVol, sellVol) / total;
}

/**
 * Map dominance (0.5 → 1.0) to opacity (0.25 → 0.85).
 * Balanced = faded, one-sided = strong.
 */
function _opacityFromDominance(dominance) {
    // dominance range: 0.5 → 1.0, output: 0.25 → 0.85
    const t = (dominance - 0.5) / 0.5;  // normalize to 0..1
    return 0.25 + t * 0.60;
}

/**
 * Detect absorption: both sides have significant volume at the same price.
 */
function _isAbsorption(buyVol, sellVol) {
    const total = buyVol + sellVol;
    if (total < BUBBLE_CONFIG.ABSORPTION_MIN * 2) return false;
    const minSide = Math.min(buyVol, sellVol);
    return (minSide / total) >= BUBBLE_CONFIG.ABSORPTION_RATIO;
}

// ═══════════════════════════════════════════════════════════════════════════
// RENDERER — draws bubbles on the LWC canvas
// ═══════════════════════════════════════════════════════════════════════════
class VolumeBubbleRenderer {
    constructor() {
        this._data = null;
    }

    update(data) {
        this._data = data;
    }

    draw(target, priceConverter) {
        const d = this._data;
        if (!d || !d.bars || d.bars.length === 0) return;

        const barSpacing = d.barSpacing || 6;

        // ── Bird's eye: skip rendering entirely ──
        if (barSpacing <= 5) return;

        const useDots = barSpacing <= 20;  // macro zoom: dots only
        const { from, to } = d.visibleRange;

        target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
            // ── Pre-compute volume scale ──
            let maxVol = 0;
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const bp = bar.originalData.bp;
                for (const key in bp) {
                    const entry = bp[key];
                    const total = entry[0] + entry[1];
                    if (total > maxVol) maxVol = total;
                }
            }
            if (maxVol === 0) return;

            // ── Classify all bubbles ──
            const glowBubbles = [];     // institutional prints (drawn first, behind)
            const buyBubbles = [];
            const sellBubbles = [];
            const absorbBubbles = [];   // absorption pattern (special)
            const labelBubbles = [];    // text labels (drawn last, on top)

            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;

                const bp = bar.originalData.bp;
                const x = bar.x;

                for (const priceStr in bp) {
                    const entry = bp[priceStr];
                    const buyVol = entry[0];
                    const sellVol = entry[1];
                    const totalVol = buyVol + sellVol;

                    // ── Noise filter ──
                    if (totalVol < BUBBLE_CONFIG.MIN_BUBBLE_VOL) continue;

                    // Convert price to Y coordinate
                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    // ── Classify ──
                    const isBuy = buyVol >= sellVol;
                    const dominance = _dominance(buyVol, sellVol);
                    const opacity = _opacityFromDominance(dominance);
                    const isAbsorb = _isAbsorption(buyVol, sellVol);
                    const isInstitutional = totalVol >= BUBBLE_CONFIG.INSTITUTIONAL_THRESHOLD;

                    // ── Radius ──
                    let radius;
                    if (useDots) {
                        radius = BUBBLE_CONFIG.DOT_RADIUS;
                        // Slightly bigger dots for institutional
                        if (isInstitutional) radius = 4;
                    } else {
                        const ratio = totalVol / maxVol;
                        radius = BUBBLE_CONFIG.MIN_RADIUS +
                            ratio * (BUBBLE_CONFIG.MAX_RADIUS - BUBBLE_CONFIG.MIN_RADIUS);
                        radius = Math.min(radius, BUBBLE_CONFIG.MAX_RADIUS);
                    }

                    const bubble = { x, y, radius, totalVol, buyVol, sellVol, opacity, isAbsorb, isInstitutional };

                    // ── Sort into render layers ──
                    if (isInstitutional && !useDots) {
                        glowBubbles.push(bubble);
                    }

                    if (isAbsorb) {
                        absorbBubbles.push(bubble);
                    } else if (isBuy) {
                        buyBubbles.push(bubble);
                    } else {
                        sellBubbles.push(bubble);
                    }

                    // Labels for zoomed-in view
                    if (!useDots && radius >= 7) {
                        labelBubbles.push(bubble);
                    }
                }
            }

            // ════════════════════════════════════════════════════════════════
            // RENDER LAYERS (back to front)
            // ════════════════════════════════════════════════════════════════

            // ── Layer 1: Institutional glow rings (behind everything) ──
            for (const b of glowBubbles) {
                const glowR = b.radius + BUBBLE_CONFIG.GLOW_EXTRA_RADIUS;
                let glowColor;
                if (b.isAbsorb) {
                    glowColor = BUBBLE_CONFIG.GLOW_COLOR_ABSORB;
                } else if (b.buyVol >= b.sellVol) {
                    glowColor = BUBBLE_CONFIG.GLOW_COLOR_BUY;
                } else {
                    glowColor = BUBBLE_CONFIG.GLOW_COLOR_SELL;
                }

                // Radial gradient glow
                const grad = ctx.createRadialGradient(b.x, b.y, b.radius, b.x, b.y, glowR);
                grad.addColorStop(0, glowColor);
                grad.addColorStop(1, 'rgba(0,0,0,0)');
                ctx.fillStyle = grad;
                ctx.beginPath();
                ctx.arc(b.x, b.y, glowR, 0, Math.PI * 2);
                ctx.fill();
            }

            // ── Layer 2: Buy bubbles (green) ──
            if (buyBubbles.length > 0) {
                for (const b of buyBubbles) {
                    ctx.fillStyle = _rgba(BUBBLE_CONFIG.BUY_COLOR, b.opacity);
                    ctx.beginPath();
                    ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
                    ctx.fill();
                }
            }

            // ── Layer 3: Sell bubbles (red) ──
            if (sellBubbles.length > 0) {
                for (const b of sellBubbles) {
                    ctx.fillStyle = _rgba(BUBBLE_CONFIG.SELL_COLOR, b.opacity);
                    ctx.beginPath();
                    ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
                    ctx.fill();
                }
            }

            // ── Layer 4: Absorption bubbles (purple with dual-color ring) ──
            if (absorbBubbles.length > 0) {
                for (const b of absorbBubbles) {
                    // Inner fill: purple (absorption detected)
                    ctx.fillStyle = _rgba(BUBBLE_CONFIG.ABSORPTION_COLOR, b.opacity);
                    ctx.beginPath();
                    ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
                    ctx.fill();

                    // Dual-color split ring: green top half, red bottom half
                    if (!useDots && b.radius >= 5) {
                        ctx.lineWidth = 2;
                        // Top half — buy (green)
                        ctx.strokeStyle = _rgba(BUBBLE_CONFIG.BUY_COLOR, 0.8);
                        ctx.beginPath();
                        ctx.arc(b.x, b.y, b.radius + 1, Math.PI, 0);  // top semicircle
                        ctx.stroke();
                        // Bottom half — sell (red)
                        ctx.strokeStyle = _rgba(BUBBLE_CONFIG.SELL_COLOR, 0.8);
                        ctx.beginPath();
                        ctx.arc(b.x, b.y, b.radius + 1, 0, Math.PI);  // bottom semicircle
                        ctx.stroke();
                    }
                }
            }

            // ── Layer 5: Institutional border ring ──
            if (!useDots) {
                for (const b of glowBubbles) {
                    let ringColor;
                    if (b.isAbsorb) {
                        ringColor = _rgba(BUBBLE_CONFIG.ABSORPTION_COLOR, 0.9);
                    } else if (b.buyVol >= b.sellVol) {
                        ringColor = _rgba(BUBBLE_CONFIG.BUY_COLOR, 0.9);
                    } else {
                        ringColor = _rgba(BUBBLE_CONFIG.SELL_COLOR, 0.9);
                    }
                    ctx.strokeStyle = ringColor;
                    ctx.lineWidth = 2;
                    ctx.beginPath();
                    ctx.arc(b.x, b.y, b.radius + 2, 0, Math.PI * 2);
                    ctx.stroke();
                }
            }

            // ── Layer 6: Text labels (zoomed in only) ──
            if (labelBubbles.length > 0) {
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';

                for (const b of labelBubbles) {
                    // Volume count
                    const label = b.totalVol >= 1000
                        ? (b.totalVol / 1000).toFixed(1) + 'k'
                        : String(b.totalVol);

                    // Larger font for institutional prints
                    ctx.font = b.isInstitutional
                        ? BUBBLE_CONFIG.FONT
                        : BUBBLE_CONFIG.FONT_SMALL;

                    // Text shadow for readability
                    ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
                    ctx.fillText(label, b.x + 1, b.y + 1);

                    // Actual text
                    ctx.fillStyle = BUBBLE_CONFIG.TEXT_COLOR;
                    ctx.fillText(label, b.x, b.y);

                    // Absorption badge: small "ABS" label below
                    if (b.isAbsorb && b.radius >= 10) {
                        ctx.font = '7px "JetBrains Mono", monospace';
                        ctx.fillStyle = _rgba(BUBBLE_CONFIG.ABSORPTION_COLOR, 0.9);
                        ctx.fillText('ABS', b.x, b.y + b.radius + 8);
                    }
                }
            }
        });
    }
}


// ═══════════════════════════════════════════════════════════════════════════
// CUSTOM SERIES VIEW — bridges LWC's plugin API to our renderer
// ═══════════════════════════════════════════════════════════════════════════
class VolumeBubbleSeries {
    constructor() {
        this._renderer = new VolumeBubbleRenderer();
    }

    /**
     * Return the renderer instance.
     */
    renderer() {
        return this._renderer;
    }

    /**
     * Called by LWC with the latest data + series options.
     * We forward bars/barSpacing/visibleRange to our renderer.
     */
    update(data, seriesOptions) {
        this._renderer.update(data);
    }

    /**
     * Interpret custom data for auto-scaling.
     * Return only close price — do NOT expand scale with bp price keys.
     * The bubbles share the candlestick 'right' price scale.
     */
    priceValueBuilder(plotRow) {
        return [plotRow.close || 0];
    }

    /**
     * Determine if a data point is whitespace (no data).
     */
    isWhitespace(data) {
        return !data || data.close === undefined;
    }

    /**
     * Default options for the series.
     */
    defaultOptions() {
        return {};
    }
}

// Export for use in app.js
window.VolumeBubbleSeries = VolumeBubbleSeries;
window.BUBBLE_CONFIG = BUBBLE_CONFIG;
