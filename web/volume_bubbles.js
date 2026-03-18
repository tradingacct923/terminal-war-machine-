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
    // ── StdDev Significance Levels (The Brain) ──
    // These define how many σ above average = "significant"
    // No fixed contract counts. Everything adapts to market regime.
    SIGMA_SIGNIFICANT: 1.5,       // 1.5σ = unusual (top ~7% of prints)
    SIGMA_INSTITUTIONAL: 3.0,     // 3.0σ = extreme outlier (top ~0.1%)
    SIGMA_ABSORPTION: 1.0,        // 1.0σ = absorption context level
    SIGMA_HIGH_DOM: 0.5,          // 0.5σ = min vol for high-dominance bypass
    HIGH_DOMINANCE: 0.90,         // 90%+ one-sided = directional aggression
    ABSORPTION_RATIO: 0.35,       // minor side must be ≥35% of total for absorption
    MIN_BUBBLE_VOL: 1,            // absolute floor (skip truly empty levels)

    // ── Gradient Display (The Eyes) ──
    // Maps sigma distance → opacity (smooth, no binary jumps)
    GRADIENT_BASE_OPACITY: 0.04,  // opacity for noise (barely visible)
    GRADIENT_SCALE: 0.20,         // opacity gain per 1σ above average
    GRADIENT_MAX_OPACITY: 0.92,   // cap

    // ── Cluster Detection ──
    CLUSTER_MIN_HITS: 3,          // minimum significant hits at same price
    CLUSTER_LINE_WIDTH: 1.5,      // connecting line width
    CLUSTER_BADGE_FONT: '9px "JetBrains Mono", "SF Mono", monospace',

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

    // ── Iceberg Detection ──
    ICE_BUY_COLOR:    [100, 180, 255],   // ice blue for buy icebergs
    ICE_SELL_COLOR:   [255, 140, 180],   // ice pink for sell icebergs
    ICE_DIAMOND_SIZE: 12,                // diamond half-size in px (zoomed in)
    ICE_DOT_SIZE:     4,                 // dot size when zoomed out
    ICE_PULSE_SPEED:  2000,              // pulse cycle duration in ms

    // ── Sweep Detection ──
    SWEEP_BUY_COLOR:  [31, 209, 122],    // green for buy sweeps
    SWEEP_SELL_COLOR: [224, 48, 96],     // red for sell sweeps
    SWEEP_LINE_WIDTH: 3,                 // lightning bolt line width
    SWEEP_GLOW_BLUR:  8,                 // shadowBlur for sweep glow
    SWEEP_BURST_SIZE: 6,                 // burst effect radius at endpoints

    // ── Delta Divergence ──
    DIV_BEAR_COLOR:   [224, 48, 96],     // red for bearish divergence
    DIV_BULL_COLOR:   [31, 209, 122],    // green for bullish divergence
    DIV_LINE_WIDTH:   2,                 // dashed line width
    DIV_DASH:         [6, 4],            // dash pattern

    // ── Momentum Ignition ──
    IGN_COLOR:        [255, 165, 0],     // orange for ignition zone
    IGN_TRAP_COLOR:   [255, 50, 50],     // red for confirmed trap
    IGN_ZONE_ALPHA:   0.15,              // zone fill opacity

    // ── Spoof Detection ──
    SPOOF_COLOR:      [180, 180, 200],   // grey-white for phantom orders
    SPOOF_DASH:       [4, 3],            // dashed border pattern
    SPOOF_RADIUS:     10,                // ghost circle radius

    // ── Typography ──
    FONT: '10px "JetBrains Mono", "SF Mono", monospace',
    FONT_SMALL: '8px "JetBrains Mono", "SF Mono", monospace',
    FONT_BADGE: '7px "JetBrains Mono", "SF Mono", monospace',
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
function _isAbsorption(buyVol, sellVol, minVol) {
    const total = buyVol + sellVol;
    if (total < minVol * 2) return false;
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

            // ── Adaptive threshold: compute rolling average vol per level ──
            const allLevelVols = [];
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const bp = bar.originalData.bp;
                for (const key in bp) {
                    const entry = bp[key];
                    const tv = entry[0] + entry[1];
                    if (tv > 0) allLevelVols.push(tv);
                }
            }
            // ── Step 1: THE BRAIN — Compute StdDev ──
            const n = allLevelVols.length;
            const avg = n > 0 ? allLevelVols.reduce((a, b) => a + b, 0) / n : 0;
            const variance = n > 0
                ? allLevelVols.reduce((sum, v) => {
                    const diff = v - avg;
                    return sum + diff * diff;
                }, 0) / n
                : 0;
            const stddev = Math.sqrt(variance);

            // Adaptive significance levels (all σ-based, zero fixed numbers)
            const sigThreshold  = avg + BUBBLE_CONFIG.SIGMA_SIGNIFICANT * stddev;
            const instThreshold = avg + BUBBLE_CONFIG.SIGMA_INSTITUTIONAL * stddev;
            const absorbMinVol  = avg + BUBBLE_CONFIG.SIGMA_ABSORPTION * stddev;
            const highDomMinVol = avg + BUBBLE_CONFIG.SIGMA_HIGH_DOM * stddev;

            // ── Cluster map: find significant-volume price levels hit 3+ times ──
            const clusterMap = {};  // {priceStr → [{idx, x, buy, sell, total}, ...]}
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const bp = bar.originalData.bp;
                for (const priceStr in bp) {
                    const bv = bp[priceStr][0], sv = bp[priceStr][1];
                    const tv = bv + sv;
                    if (tv >= sigThreshold) {  // only σ-significant prints
                        if (!clusterMap[priceStr]) clusterMap[priceStr] = [];
                        clusterMap[priceStr].push({ idx: i, x: bar.x, buy: bv, sell: sv, total: tv });
                    }
                }
            }
            // Build set of clustered prices for quick lookup
            const clusteredPrices = new Set();
            for (const priceStr in clusterMap) {
                if (clusterMap[priceStr].length >= BUBBLE_CONFIG.CLUSTER_MIN_HITS) {
                    clusteredPrices.add(priceStr);
                }
            }

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

                    // ── Step 2: THE BRAIN — Classify via σ (no fixed thresholds) ──
                    const isBuy = buyVol >= sellVol;
                    const dominance = _dominance(buyVol, sellVol);
                    const isAbsorb = _isAbsorption(buyVol, sellVol, absorbMinVol);
                    const isInstitutional = totalVol >= instThreshold;
                    const highDominance = dominance >= BUBBLE_CONFIG.HIGH_DOMINANCE
                        && totalVol >= highDomMinVol;
                    const isInCluster = clusteredPrices.has(priceStr);

                    // Absolute floor: skip truly empty levels
                    if (totalVol < BUBBLE_CONFIG.MIN_BUBBLE_VOL) continue;

                    // Convert price to Y coordinate
                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    // ── Step 3: THE EYES — Gradient from σ distance ──
                    // How many σ above average is this bubble?
                    const sigmaDistance = stddev > 0
                        ? (totalVol - avg) / stddev
                        : (totalVol > avg ? 1 : 0);

                    // Opacity: smooth gradient based on sigma distance
                    // 0σ → base (0.04), each σ adds 0.20, capped at 0.92
                    let opacity = BUBBLE_CONFIG.GRADIENT_BASE_OPACITY
                        + Math.max(sigmaDistance, 0) * BUBBLE_CONFIG.GRADIENT_SCALE;
                    opacity = Math.min(opacity, BUBBLE_CONFIG.GRADIENT_MAX_OPACITY);

                    // Cluster boost: smooth +0.15 for repeated significant levels
                    if (isInCluster) opacity = Math.min(opacity + 0.15, BUBBLE_CONFIG.GRADIENT_MAX_OPACITY);

                    // Radius: smooth gradient based on sigma distance
                    let radius;
                    if (useDots) {
                        // Dots: scale 1.0 → 4.0 based on sigma
                        radius = Math.min(1.0 + Math.max(sigmaDistance, 0) * 0.8, 4.0);
                    } else {
                        // Full bubbles: scale MIN_RADIUS → MAX_RADIUS based on sigma
                        const sigmaRatio = Math.min(Math.max(sigmaDistance, 0) / 4, 1);
                        radius = BUBBLE_CONFIG.MIN_RADIUS
                            + sigmaRatio * (BUBBLE_CONFIG.MAX_RADIUS - BUBBLE_CONFIG.MIN_RADIUS);
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

            // ════════════════════════════════════════════════════════════════
            // LAYER 6.5: CLUSTER + ACCELERATION GRADIENT
            // StdDev decided WHAT clusters matter. Gradient shows HOW MUCH.
            // ════════════════════════════════════════════════════════════════
            if (!useDots) {
                for (const priceStr of clusteredPrices) {
                    const hits = clusterMap[priceStr];
                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    // ── Acceleration: smooth ratio, no thresholds ──
                    const vols = hits.map(h => h.total);
                    const halfLen = Math.floor(vols.length / 2);
                    const firstHalf = vols.slice(0, halfLen);
                    const secondHalf = vols.slice(halfLen);
                    const avgFirst = firstHalf.reduce((a, b) => a + b, 0) / (firstHalf.length || 1);
                    const avgSecond = secondHalf.reduce((a, b) => a + b, 0) / (secondHalf.length || 1);
                    const accelRatio = avgFirst > 0 ? avgSecond / avgFirst : 1;

                    // ── Gradient: ratio → smooth opacity (no cutoffs) ──
                    // ratio 0.3 → 0.08, ratio 1.0 → 0.30, ratio 2.0 → 0.55, ratio 3.0+ → 0.80
                    const lineAlpha = Math.min(0.05 + accelRatio * 0.25, 0.80);

                    // ── Color: dominant side, saturation from ratio ──
                    const totalBuy = hits.reduce((a, h) => a + h.buy, 0);
                    const totalSell = hits.reduce((a, h) => a + h.sell, 0);
                    const lineColor = totalBuy >= totalSell
                        ? BUBBLE_CONFIG.BUY_COLOR : BUBBLE_CONFIG.SELL_COLOR;

                    // ── Draw connecting line ──
                    const xStart = hits[0].x;
                    const xEnd = hits[hits.length - 1].x;
                    ctx.strokeStyle = _rgba(lineColor, lineAlpha);
                    ctx.lineWidth = BUBBLE_CONFIG.CLUSTER_LINE_WIDTH;
                    ctx.setLineDash([]);
                    ctx.beginPath();
                    ctx.moveTo(xStart, y);
                    ctx.lineTo(xEnd, y);
                    ctx.stroke();

                    // Draw dots at each hit point (opacity follows gradient)
                    for (const hit of hits) {
                        ctx.fillStyle = _rgba(lineColor, Math.min(lineAlpha + 0.15, 0.90));
                        ctx.beginPath();
                        ctx.arc(hit.x, y, 2.5, 0, Math.PI * 2);
                        ctx.fill();
                    }

                    // ── Badge: hit count + raw ratio (no labels, just data) ──
                    const badgeX = xEnd + 6;
                    ctx.font = BUBBLE_CONFIG.CLUSTER_BADGE_FONT;
                    ctx.textAlign = 'left';
                    ctx.textBaseline = 'middle';

                    const badge = `${hits.length}× ${accelRatio.toFixed(1)}r`;
                    const badgeAlpha = Math.min(lineAlpha + 0.20, 0.90);

                    // Badge shadow
                    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
                    ctx.fillText(badge, badgeX + 1, y + 1);
                    // Badge text
                    ctx.fillStyle = _rgba(lineColor, badgeAlpha);
                    ctx.fillText(badge, badgeX, y);

                    ctx.textAlign = 'center';  // reset
                }
            }

            // ════════════════════════════════════════════════════════════════
            // LAYER 7: ICEBERG DETECTION (◆ diamond markers)
            // ════════════════════════════════════════════════════════════════
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.icebergs) continue;

                const icebergs = bar.originalData.icebergs;
                const x = bar.x;

                for (const priceStr in icebergs) {
                    const ice = icebergs[priceStr];
                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    const isBuy = ice.side === 'b';
                    const color = isBuy ? BUBBLE_CONFIG.ICE_BUY_COLOR : BUBBLE_CONFIG.ICE_SELL_COLOR;

                    if (useDots) {
                        // Macro zoom: small diamond dot
                        ctx.fillStyle = _rgba(color, 0.8);
                        ctx.beginPath();
                        const ds = BUBBLE_CONFIG.ICE_DOT_SIZE;
                        ctx.moveTo(x, y - ds);
                        ctx.lineTo(x + ds, y);
                        ctx.lineTo(x, y + ds);
                        ctx.lineTo(x - ds, y);
                        ctx.closePath();
                        ctx.fill();
                    } else {
                        // Full zoom: diamond with glow + pulse + label
                        const ds = BUBBLE_CONFIG.ICE_DIAMOND_SIZE;

                        // Pulse animation via timestamp
                        const t = (performance.now() % BUBBLE_CONFIG.ICE_PULSE_SPEED) / BUBBLE_CONFIG.ICE_PULSE_SPEED;
                        const pulseAlpha = 0.6 + 0.4 * Math.sin(t * Math.PI * 2);

                        // Glow
                        const glowGrad = ctx.createRadialGradient(x, y, ds * 0.5, x, y, ds * 2);
                        glowGrad.addColorStop(0, _rgba(color, 0.3 * pulseAlpha));
                        glowGrad.addColorStop(1, 'rgba(0,0,0,0)');
                        ctx.fillStyle = glowGrad;
                        ctx.beginPath();
                        ctx.arc(x, y, ds * 2, 0, Math.PI * 2);
                        ctx.fill();

                        // Diamond shape
                        ctx.fillStyle = _rgba(color, 0.85 * pulseAlpha);
                        ctx.beginPath();
                        ctx.moveTo(x, y - ds);
                        ctx.lineTo(x + ds * 0.7, y);
                        ctx.lineTo(x, y + ds);
                        ctx.lineTo(x - ds * 0.7, y);
                        ctx.closePath();
                        ctx.fill();

                        // Diamond border
                        ctx.strokeStyle = _rgba(color, 0.9);
                        ctx.lineWidth = 1.5;
                        ctx.stroke();

                        // Volume text inside diamond
                        const volLabel = ice.est_total >= 1000
                            ? '~' + (ice.est_total / 1000).toFixed(1) + 'k'
                            : '~' + ice.est_total;
                        ctx.font = BUBBLE_CONFIG.FONT_SMALL;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        ctx.fillStyle = 'rgba(0,0,0,0.5)';
                        ctx.fillText(volLabel, x + 1, y + 1);
                        ctx.fillStyle = BUBBLE_CONFIG.TEXT_COLOR;
                        ctx.fillText(volLabel, x, y);

                        // "ICE" badge below
                        ctx.font = BUBBLE_CONFIG.FONT_BADGE;
                        ctx.fillStyle = _rgba(color, 0.9);
                        ctx.fillText('ICE', x, y + ds + 8);
                    }
                }
            }

            // ════════════════════════════════════════════════════════════════
            // LAYER 8: SWEEP DETECTION (⚡ lightning bolt lines)
            // ════════════════════════════════════════════════════════════════
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.sweeps) continue;

                const sweeps = bar.originalData.sweeps;
                const x = bar.x;

                for (const sweep of sweeps) {
                    if (!sweep.prices || sweep.prices.length < 2) continue;

                    const isBuy = sweep.side === 'b';
                    const color = isBuy ? BUBBLE_CONFIG.SWEEP_BUY_COLOR : BUBBLE_CONFIG.SWEEP_SELL_COLOR;

                    // Convert prices to Y coordinates
                    const yCoords = [];
                    for (const p of sweep.prices) {
                        const yp = priceConverter(p);
                        if (yp !== null && yp !== undefined && !isNaN(yp)) {
                            yCoords.push({ price: p, y: yp });
                        }
                    }
                    if (yCoords.length < 2) continue;

                    // Sort by Y coordinate (top to bottom)
                    yCoords.sort((a, b) => a.y - b.y);

                    if (useDots) {
                        // Macro zoom: simple vertical line
                        ctx.strokeStyle = _rgba(color, 0.6);
                        ctx.lineWidth = 2;
                        ctx.beginPath();
                        ctx.moveTo(x, yCoords[0].y);
                        ctx.lineTo(x, yCoords[yCoords.length - 1].y);
                        ctx.stroke();
                    } else {
                        // Full zoom: lightning bolt with glow
                        ctx.save();
                        ctx.shadowColor = _rgba(color, 0.6);
                        ctx.shadowBlur = BUBBLE_CONFIG.SWEEP_GLOW_BLUR;
                        ctx.strokeStyle = _rgba(color, 0.9);
                        ctx.lineWidth = BUBBLE_CONFIG.SWEEP_LINE_WIDTH;
                        ctx.setLineDash([]);

                        // Draw zigzag lightning bolt
                        ctx.beginPath();
                        ctx.moveTo(x, yCoords[0].y);
                        for (let j = 1; j < yCoords.length; j++) {
                            const zigX = x + (j % 2 === 0 ? -6 : 6);  // zigzag offset
                            ctx.lineTo(zigX, yCoords[j].y);
                        }
                        ctx.stroke();
                        ctx.restore();

                        // Burst effect at the endpoint
                        const endY = yCoords[yCoords.length - 1].y;
                        const bs = BUBBLE_CONFIG.SWEEP_BURST_SIZE;
                        ctx.strokeStyle = _rgba(color, 0.8);
                        ctx.lineWidth = 2;
                        for (let angle = 0; angle < Math.PI * 2; angle += Math.PI / 4) {
                            ctx.beginPath();
                            ctx.moveTo(x + Math.cos(angle) * 3, endY + Math.sin(angle) * 3);
                            ctx.lineTo(x + Math.cos(angle) * bs, endY + Math.sin(angle) * bs);
                            ctx.stroke();
                        }

                        // Volume label at midpoint
                        const midY = (yCoords[0].y + endY) / 2;
                        const swpLabel = sweep.vol >= 1000
                            ? (sweep.vol / 1000).toFixed(1) + 'k'
                            : String(sweep.vol);
                        ctx.font = BUBBLE_CONFIG.FONT_SMALL;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';

                        // Background pill for sweep label
                        const tm = ctx.measureText(swpLabel);
                        ctx.fillStyle = 'rgba(0,0,0,0.7)';
                        ctx.beginPath();
                        ctx.roundRect(x - tm.width / 2 - 4, midY - 6, tm.width + 8, 12, 3);
                        ctx.fill();

                        ctx.fillStyle = _rgba(color, 0.95);
                        ctx.fillText(swpLabel, x, midY);

                        // "SWP" badge below burst
                        ctx.font = BUBBLE_CONFIG.FONT_BADGE;
                        ctx.fillStyle = _rgba(color, 0.85);
                        ctx.fillText('SWP', x, endY + bs + 10);
                    }
                }
            }

            // ════════════════════════════════════════════════════════════════
            // LAYER 9: DELTA DIVERGENCE (╱╱ dashed divergence lines)
            // ════════════════════════════════════════════════════════════════
            if (!useDots) {
                for (let i = from; i < to; i++) {
                    const bar = d.bars[i];
                    if (!bar || !bar.originalData || !bar.originalData.delta_div) continue;

                    const div = bar.originalData.delta_div;
                    const x = bar.x;
                    const isBear = div.type === 'bearish';
                    const color = isBear ? BUBBLE_CONFIG.DIV_BEAR_COLOR : BUBBLE_CONFIG.DIV_BULL_COLOR;

                    // Current price point
                    const priceKey = isBear ? 'price_high' : 'price_low';
                    const prevKey = isBear ? 'price_prev' : 'price_prev';
                    const yNow = priceConverter(div[priceKey]);
                    const yPrev = priceConverter(div[prevKey]);
                    if (!yNow || !yPrev || isNaN(yNow) || isNaN(yPrev)) continue;

                    // Find previous bar X (approximate from t_prev)
                    let xPrev = x - 80; // fallback
                    for (let j = from; j < i; j++) {
                        const pb = d.bars[j];
                        if (pb && pb.originalData && pb.originalData.time === div.t_prev) {
                            xPrev = pb.x;
                            break;
                        }
                    }

                    // Dashed divergence line
                    ctx.save();
                    ctx.strokeStyle = _rgba(color, 0.8);
                    ctx.lineWidth = BUBBLE_CONFIG.DIV_LINE_WIDTH;
                    ctx.setLineDash(BUBBLE_CONFIG.DIV_DASH);
                    ctx.beginPath();
                    ctx.moveTo(xPrev, yPrev);
                    ctx.lineTo(x, yNow);
                    ctx.stroke();
                    ctx.setLineDash([]);
                    ctx.restore();

                    // Divergence label
                    const divLabel = isBear ? 'DIV ▼' : 'DIV ▲';
                    const labelX = (xPrev + x) / 2;
                    const labelY = (yPrev + yNow) / 2 - 12;
                    ctx.font = BUBBLE_CONFIG.FONT_SMALL;
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';

                    // Background pill
                    const dtm = ctx.measureText(divLabel);
                    ctx.fillStyle = 'rgba(0,0,0,0.75)';
                    ctx.beginPath();
                    ctx.roundRect(labelX - dtm.width / 2 - 5, labelY - 7, dtm.width + 10, 14, 4);
                    ctx.fill();

                    ctx.fillStyle = _rgba(color, 0.95);
                    ctx.fillText(divLabel, labelX, labelY);

                    // Circle markers at both endpoints
                    ctx.fillStyle = _rgba(color, 0.7);
                    ctx.beginPath();
                    ctx.arc(xPrev, yPrev, 4, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.beginPath();
                    ctx.arc(x, yNow, 4, 0, Math.PI * 2);
                    ctx.fill();
                }
            }

            // ════════════════════════════════════════════════════════════════
            // LAYER 10: MOMENTUM IGNITION (⚠️ warning zones)
            // ════════════════════════════════════════════════════════════════
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.ignition) continue;

                const ignitions = bar.originalData.ignition;
                const x = bar.x;

                for (const ign of ignitions) {
                    const yMin = priceConverter(ign.price_min);
                    const yMax = priceConverter(ign.price_max);
                    if (!yMin || !yMax || isNaN(yMin) || isNaN(yMax)) continue;

                    const isReversed = ign.reversed === true;
                    const color = isReversed ? BUBBLE_CONFIG.IGN_TRAP_COLOR : BUBBLE_CONFIG.IGN_COLOR;
                    const yTop = Math.min(yMin, yMax);
                    const yBot = Math.max(yMin, yMax);
                    const zoneH = Math.max(yBot - yTop, 4);

                    if (useDots) {
                        // Macro zoom: thin vertical line
                        ctx.strokeStyle = _rgba(color, 0.5);
                        ctx.lineWidth = 2;
                        ctx.beginPath();
                        ctx.moveTo(x, yTop);
                        ctx.lineTo(x, yBot);
                        ctx.stroke();
                    } else {
                        // Full zoom: warning zone rectangle
                        const zoneW = 24;

                        // Semi-transparent fill
                        ctx.fillStyle = _rgba(color, BUBBLE_CONFIG.IGN_ZONE_ALPHA);
                        ctx.fillRect(x - zoneW / 2, yTop, zoneW, zoneH);

                        // Border (pulsing if not reversed)
                        if (!isReversed) {
                            const t = (performance.now() % 1500) / 1500;
                            const borderAlpha = 0.4 + 0.4 * Math.sin(t * Math.PI * 2);
                            ctx.strokeStyle = _rgba(color, borderAlpha);
                            ctx.lineWidth = 1.5;
                            ctx.setLineDash([4, 3]);
                        } else {
                            ctx.strokeStyle = _rgba(color, 0.9);
                            ctx.lineWidth = 2;
                            ctx.setLineDash([]);
                        }
                        ctx.strokeRect(x - zoneW / 2, yTop, zoneW, zoneH);
                        ctx.setLineDash([]);

                        // Direction arrow
                        const arrowY = ign.direction === 'up' ? yTop - 10 : yBot + 10;
                        const arrowChar = ign.direction === 'up' ? '↑' : '↓';
                        ctx.font = BUBBLE_CONFIG.FONT;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        ctx.fillStyle = _rgba(color, 0.9);
                        ctx.fillText(arrowChar, x, arrowY);

                        // Label
                        const ignLabel = isReversed ? '⚠ TRAP' : 'IGN';
                        ctx.font = BUBBLE_CONFIG.FONT_BADGE;
                        ctx.fillStyle = _rgba(color, 0.9);

                        const itm = ctx.measureText(ignLabel);
                        ctx.fillStyle = 'rgba(0,0,0,0.7)';
                        ctx.beginPath();
                        ctx.roundRect(x - itm.width / 2 - 3, yBot + 14, itm.width + 6, 11, 3);
                        ctx.fill();

                        ctx.fillStyle = _rgba(color, 0.95);
                        ctx.fillText(ignLabel, x, yBot + 19);
                    }
                }
            }

            // ════════════════════════════════════════════════════════════════
            // LAYER 11: SPOOF DETECTION (👻 ghost markers)
            // ════════════════════════════════════════════════════════════════
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.spoofs) continue;

                const spoofs = bar.originalData.spoofs;
                const x = bar.x;

                for (const spoof of spoofs) {
                    const price = parseFloat(spoof.price);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    const color = BUBBLE_CONFIG.SPOOF_COLOR;
                    const r = BUBBLE_CONFIG.SPOOF_RADIUS;

                    if (useDots) {
                        // Macro: small X mark
                        ctx.strokeStyle = _rgba(color, 0.5);
                        ctx.lineWidth = 1.5;
                        ctx.beginPath();
                        ctx.moveTo(x - 3, y - 3);
                        ctx.lineTo(x + 3, y + 3);
                        ctx.moveTo(x + 3, y - 3);
                        ctx.lineTo(x - 3, y + 3);
                        ctx.stroke();
                    } else {
                        // Full zoom: ghost circle with dashed border
                        ctx.save();

                        // Translucent fill
                        ctx.fillStyle = _rgba(color, 0.1);
                        ctx.beginPath();
                        ctx.arc(x, y, r, 0, Math.PI * 2);
                        ctx.fill();

                        // Dashed border
                        ctx.strokeStyle = _rgba(color, 0.5);
                        ctx.lineWidth = 1.5;
                        ctx.setLineDash(BUBBLE_CONFIG.SPOOF_DASH);
                        ctx.stroke();
                        ctx.setLineDash([]);

                        // Red X through it
                        ctx.strokeStyle = 'rgba(255, 60, 60, 0.7)';
                        ctx.lineWidth = 2;
                        ctx.beginPath();
                        ctx.moveTo(x - r * 0.5, y - r * 0.5);
                        ctx.lineTo(x + r * 0.5, y + r * 0.5);
                        ctx.moveTo(x + r * 0.5, y - r * 0.5);
                        ctx.lineTo(x - r * 0.5, y + r * 0.5);
                        ctx.stroke();
                        ctx.restore();

                        // Fake size label
                        const fakeLabel = spoof.fake_size >= 1000
                            ? (spoof.fake_size / 1000).toFixed(1) + 'k'
                            : String(spoof.fake_size);
                        ctx.font = BUBBLE_CONFIG.FONT_SMALL;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        ctx.fillStyle = 'rgba(0,0,0,0.6)';
                        ctx.fillText(fakeLabel, x + 1, y + 1);
                        ctx.fillStyle = _rgba(color, 0.9);
                        ctx.fillText(fakeLabel, x, y);

                        // "SPOOF" badge below
                        ctx.font = BUBBLE_CONFIG.FONT_BADGE;
                        ctx.fillStyle = 'rgba(255, 60, 60, 0.85)';
                        ctx.fillText('SPOOF', x, y + r + 10);

                        // Occurrence count badge
                        if (spoof.count > 2) {
                            ctx.fillStyle = _rgba(color, 0.7);
                            ctx.fillText('x' + spoof.count, x, y + r + 20);
                        }
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
