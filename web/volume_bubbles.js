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
    // Visual tuning only — these affect HOW things look, not WHAT’s significant
    GRADIENT_BASE_OPACITY: 0.04,  // opacity for noise (barely visible)
    GRADIENT_EXPONENT_SCALE: 0.05,// σ² multiplier for exponential curve
    GRADIENT_MAX_OPACITY: 0.92,   // cap
    CLUSTER_OPACITY_BOOST: 0.15,  // extra opacity for clustered bubbles
    DOMINANCE_OPACITY_SCALE: 0.10,// max opacity boost from dominance (90%+ → +0.04)

    // ── Cluster Detection ──
    CLUSTER_MIN_HITS: 3,          // minimum significant hits at same price
    CLUSTER_LINE_WIDTH_MIN: 1.0,  // thinnest segment (low vol hit)
    CLUSTER_LINE_WIDTH_MAX: 4.0,  // thickest segment (high vol hit)
    CLUSTER_GLOW_BLUR: 6,        // glow effect blur radius
    CLUSTER_DOT_RADIUS: 3.5,     // hit point dot size
    CLUSTER_BADGE_FONT: '10px "JetBrains Mono", "SF Mono", monospace',

    // ── Cumulative Level Delta (sidebar bars) ──
    CUML_DELTA_ENABLED: true,          // toggle on/off
    CUML_DELTA_BAR_MAX_WIDTH: 70,     // max horizontal bar width in px
    CUML_DELTA_BAR_HEIGHT: 3,         // bar thickness in px
    CUML_DELTA_BAR_ALPHA: 0.45,       // bar fill opacity
    CUML_DELTA_LABEL_ALPHA: 0.85,     // text label opacity
    CUML_DELTA_RIGHT_MARGIN: 8,       // px from right edge of chart
    CUML_DELTA_FONT: '9px "JetBrains Mono", "SF Mono", monospace',
    CUML_DELTA_MIN_SIGMA: 0.5,        // only show levels above 0.5σ cumulative vol
    CUML_DELTA_GLOW_THRESHOLD: 0.6,   // bars wider than 60% of max get a glow

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
            // ── Step 1: THE BRAIN — Log-Transform StdDev ──
            // Log-transform compresses scale so outliers don’t break sigma.
            // Without: one 200-lot makes threshold=91, hiding 50-lot prints.
            // With: threshold adapts properly, 50-lot prints show correctly.
            const n = allLevelVols.length;
            if (n === 0) return;

            const logVols = allLevelVols.map(v => Math.log(v + 1));
            const logAvg = logVols.reduce((a, b) => a + b, 0) / n;
            const logVariance = logVols.reduce((sum, v) => {
                const diff = v - logAvg;
                return sum + diff * diff;
            }, 0) / n;
            const logStddev = Math.sqrt(logVariance);

            // Significance thresholds: computed in log-space, converted back
            const sigThreshold  = Math.exp(logAvg + BUBBLE_CONFIG.SIGMA_SIGNIFICANT * logStddev) - 1;
            const instThreshold = Math.exp(logAvg + BUBBLE_CONFIG.SIGMA_INSTITUTIONAL * logStddev) - 1;
            const absorbMinVol  = Math.exp(logAvg + BUBBLE_CONFIG.SIGMA_ABSORPTION * logStddev) - 1;
            const highDomMinVol = Math.exp(logAvg + BUBBLE_CONFIG.SIGMA_HIGH_DOM * logStddev) - 1;

            // ── Cumulative Level Delta: aggregate buy/sell per price level ──
            const cumlDelta = {};  // {priceStr → {buy, sell, total}}
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const bp = bar.originalData.bp;
                for (const priceStr in bp) {
                    const bv = bp[priceStr][0], sv = bp[priceStr][1];
                    if (!cumlDelta[priceStr]) cumlDelta[priceStr] = { buy: 0, sell: 0, total: 0 };
                    cumlDelta[priceStr].buy += bv;
                    cumlDelta[priceStr].sell += sv;
                    cumlDelta[priceStr].total += (bv + sv);
                }
            }

            // Sigma-filter cumulative levels (same log-stddev approach)
            let cumlMinVol = 0;
            const cumlTotals = Object.values(cumlDelta).map(d => d.total);
            if (cumlTotals.length > 0) {
                const cumlLogTotals = cumlTotals.map(v => Math.log(v + 1));
                const cumlLogAvg = cumlLogTotals.reduce((a, b) => a + b, 0) / cumlLogTotals.length;
                const cumlLogVar = cumlLogTotals.reduce((s, v) => {
                    const diff = v - cumlLogAvg;
                    return s + diff * diff;
                }, 0) / cumlLogTotals.length;
                const cumlLogStd = Math.sqrt(cumlLogVar);
                cumlMinVol = Math.exp(cumlLogAvg + BUBBLE_CONFIG.CUML_DELTA_MIN_SIGMA * cumlLogStd) - 1;
            }

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

                    // ── Step 3: THE EYES — Exponential gradient from log-σ ──
                    // Sigma distance in log-space (immune to outlier distortion)
                    const logVol = Math.log(totalVol + 1);
                    const sigmaDistance = logStddev > 0
                        ? (logVol - logAvg) / logStddev
                        : (totalVol > 0 ? 1 : 0);

                    // Exponential opacity: σ² curve keeps noise dim, extremes POP
                    // 1σ=0.09, 2σ=0.24, 3σ=0.49, 4σ=0.84
                    let opacity = BUBBLE_CONFIG.GRADIENT_BASE_OPACITY
                        + Math.pow(Math.max(sigmaDistance, 0), 2) * BUBBLE_CONFIG.GRADIENT_EXPONENT_SCALE;

                    // Secondary signal: dominance nudge (one-sided prints slightly brighter)
                    // 50% dominance → +0.00, 90% dominance → +0.04
                    const domNudge = (dominance - 0.5) / 0.5 * BUBBLE_CONFIG.DOMINANCE_OPACITY_SCALE;
                    opacity += domNudge;

                    opacity = Math.min(opacity, BUBBLE_CONFIG.GRADIENT_MAX_OPACITY);

                    // Cluster boost: repeated significant levels
                    if (isInCluster) opacity = Math.min(opacity + BUBBLE_CONFIG.CLUSTER_OPACITY_BOOST, BUBBLE_CONFIG.GRADIENT_MAX_OPACITY);

                    // Radius: exponential σ-based scaling
                    let radius;
                    if (useDots) {
                        radius = Math.min(1.0 + Math.pow(Math.max(sigmaDistance, 0), 1.5) * 0.5, 4.0);
                    } else {
                        const sigmaRatio = Math.min(Math.pow(Math.max(sigmaDistance, 0) / 4, 1.5), 1);
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
            // LAYER 6.5: CLUSTER + VARIABLE-WIDTH ACCELERATION LINES
            // StdDev decided WHAT clusters matter. Gradient shows HOW MUCH.
            // Line thickness varies per segment based on volume at each hit.
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
                    const lineAlpha = Math.min(0.10 + accelRatio * 0.25, 0.85);

                    // ── Color: dominant side ──
                    const totalBuy = hits.reduce((a, h) => a + h.buy, 0);
                    const totalSell = hits.reduce((a, h) => a + h.sell, 0);
                    const lineColor = totalBuy >= totalSell
                        ? BUBBLE_CONFIG.BUY_COLOR : BUBBLE_CONFIG.SELL_COLOR;

                    // ── Variable-width line: thickness follows volume at each hit ──
                    const maxHitVol = Math.max(...vols);
                    const minHitVol = Math.min(...vols);
                    const volRange = maxHitVol - minHitVol || 1;

                    // Glow effect on cluster lines
                    ctx.save();
                    ctx.shadowColor = _rgba(lineColor, lineAlpha * 0.5);
                    ctx.shadowBlur = BUBBLE_CONFIG.CLUSTER_GLOW_BLUR;
                    ctx.setLineDash([]);

                    // Draw segments between consecutive hits with varying width
                    for (let h = 0; h < hits.length - 1; h++) {
                        const h1 = hits[h], h2 = hits[h + 1];
                        // Width = average volume of the two endpoints, mapped to min/max width
                        const segAvgVol = (h1.total + h2.total) / 2;
                        const widthRatio = (segAvgVol - minHitVol) / volRange;
                        const segWidth = BUBBLE_CONFIG.CLUSTER_LINE_WIDTH_MIN
                            + widthRatio * (BUBBLE_CONFIG.CLUSTER_LINE_WIDTH_MAX - BUBBLE_CONFIG.CLUSTER_LINE_WIDTH_MIN);

                        ctx.strokeStyle = _rgba(lineColor, lineAlpha);
                        ctx.lineWidth = segWidth;
                        ctx.beginPath();
                        ctx.moveTo(h1.x, y);
                        ctx.lineTo(h2.x, y);
                        ctx.stroke();
                    }
                    ctx.restore();

                    // Draw dots at each hit point (size follows volume)
                    for (let h = 0; h < hits.length; h++) {
                        const hit = hits[h];
                        const dotRatio = (hit.total - minHitVol) / volRange;
                        const dotRadius = BUBBLE_CONFIG.CLUSTER_DOT_RADIUS
                            + dotRatio * 1.5;  // 3.5 → 5.0 based on vol
                        ctx.fillStyle = _rgba(lineColor, Math.min(lineAlpha + 0.20, 0.92));
                        ctx.beginPath();
                        ctx.arc(hit.x, y, dotRadius, 0, Math.PI * 2);
                        ctx.fill();
                    }

                    // ── Badge: hit count + net delta (instantly readable) ──
                    const xEnd = hits[hits.length - 1].x;
                    const badgeX = xEnd + 8;
                    ctx.font = BUBBLE_CONFIG.CLUSTER_BADGE_FONT;
                    ctx.textAlign = 'left';
                    ctx.textBaseline = 'middle';

                    const netDelta = totalBuy - totalSell;
                    const deltaSign = netDelta >= 0 ? '+' : '';
                    const badge = `${hits.length}× ${deltaSign}${netDelta}Δ`;
                    const badgeAlpha = Math.min(lineAlpha + 0.20, 0.92);

                    // Badge background pill for readability
                    const bm = ctx.measureText(badge);
                    ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
                    ctx.beginPath();
                    ctx.roundRect(badgeX - 3, y - 7, bm.width + 6, 14, 3);
                    ctx.fill();

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

                        // Pulse speed varies by confidence: high=fast, low=slow
                        const conf = ice.confidence || 'high';
                        const pulseSpeed = conf === 'high' ? BUBBLE_CONFIG.ICE_PULSE_SPEED
                            : conf === 'medium' ? BUBBLE_CONFIG.ICE_PULSE_SPEED * 1.5
                            : BUBBLE_CONFIG.ICE_PULSE_SPEED * 2.5;
                        const t = (performance.now() % pulseSpeed) / pulseSpeed;
                        const pulseAlpha = 0.6 + 0.4 * Math.sin(t * Math.PI * 2);

                        // Glow — wider for high confidence
                        const glowMult = conf === 'high' ? 2.5 : conf === 'medium' ? 2.0 : 1.5;
                        const glowGrad = ctx.createRadialGradient(x, y, ds * 0.5, x, y, ds * glowMult);
                        glowGrad.addColorStop(0, _rgba(color, 0.3 * pulseAlpha));
                        glowGrad.addColorStop(1, 'rgba(0,0,0,0)');
                        ctx.fillStyle = glowGrad;
                        ctx.beginPath();
                        ctx.arc(x, y, ds * glowMult, 0, Math.PI * 2);
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

                        // Volume label: visible / est hidden
                        const visVol = ice.est_total >= 1000
                            ? (ice.est_total / 1000).toFixed(1) + 'k'
                            : String(ice.est_total);
                        const estHidden = ice.est_hidden && ice.est_hidden > ice.est_total
                            ? (ice.est_hidden >= 1000
                                ? '~' + (ice.est_hidden / 1000).toFixed(1) + 'k'
                                : '~' + ice.est_hidden)
                            : null;
                        const volLabel = estHidden ? `${visVol}/${estHidden}` : `~${visVol}`;

                        ctx.font = BUBBLE_CONFIG.FONT_SMALL;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        ctx.fillStyle = 'rgba(0,0,0,0.5)';
                        ctx.fillText(volLabel, x + 1, y + 1);
                        ctx.fillStyle = BUBBLE_CONFIG.TEXT_COLOR;
                        ctx.fillText(volLabel, x, y);

                        // ── PRESSURE + DECAY BADGE ──
                        const pressure = ice.pressure || 'wall_active';
                        const decay = ice.decay || 'holding';
                        const isZone = ice.zone || false;

                        // Decay arrow: ⬇ exhausting, ⬆ strengthening, ─ holding
                        const decayArrow = decay === 'exhausting' ? '⬇'
                            : decay === 'strengthening' ? '⬆' : '─';

                        // Pressure label
                        let pressureLabel = '';
                        let pressureColor = color;
                        if (pressure === 'bullish_wall') {
                            pressureLabel = 'BUY WALL';
                            pressureColor = '#00e676';
                        } else if (pressure === 'bearish_wall') {
                            pressureLabel = 'SELL WALL';
                            pressureColor = '#ff1744';
                        } else if (pressure === 'wall_breaking') {
                            pressureLabel = 'BREAKING';
                            pressureColor = '#ffab00';
                        } else if (pressure === 'wall_exhausted') {
                            pressureLabel = 'EMPTY';
                            pressureColor = '#ff6d00';
                        } else if (pressure === 'wall_fresh') {
                            pressureLabel = 'FRESH';
                            pressureColor = '#00b0ff';
                        } else {
                            pressureLabel = conf === 'high' ? 'ICE·H'
                                : conf === 'medium' ? 'ICE·M' : 'ICE·L';
                        }

                        // Zone prefix
                        const zonePrefix = isZone ? 'Z:' : '';
                        const badgeText = `${zonePrefix}${pressureLabel}${decayArrow}`;

                        ctx.font = BUBBLE_CONFIG.FONT_BADGE;
                        ctx.fillStyle = pressureColor;
                        ctx.fillText(badgeText, x, y + ds + 8);

                        // Fill % micro-bar below badge (tiny progress indicator)
                        const fillPct = ice.fill_pct || 0;
                        if (fillPct > 0 && fillPct < 1) {
                            const barW = ds * 1.4;
                            const barH = 2;
                            const barY = y + ds + 14;
                            ctx.fillStyle = 'rgba(255,255,255,0.15)';
                            ctx.fillRect(x - barW / 2, barY, barW, barH);
                            ctx.fillStyle = pressureColor;
                            ctx.fillRect(x - barW / 2, barY, barW * fillPct, barH);
                        }
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
            // LAYER 8.5: CUMULATIVE LEVEL DELTA (sidebar bars)
            // Aggregates all buy/sell per price level across visible candles.
            // Shows supply/demand zones at a glance on the right edge.
            // ════════════════════════════════════════════════════════════════
            if (BUBBLE_CONFIG.CUML_DELTA_ENABLED && !useDots) {
                // Find max absolute delta for proportional scaling
                let maxAbsDelta = 0;
                const filteredLevels = [];
                for (const priceStr in cumlDelta) {
                    const cd = cumlDelta[priceStr];
                    if (cd.total < cumlMinVol) continue;  // σ-filtered
                    const net = cd.buy - cd.sell;
                    const absDelta = Math.abs(net);
                    if (absDelta > maxAbsDelta) maxAbsDelta = absDelta;
                    filteredLevels.push({ priceStr, net, total: cd.total });
                }
                if (maxAbsDelta === 0) maxAbsDelta = 1;

                const rightEdge = mediaSize.width - BUBBLE_CONFIG.CUML_DELTA_RIGHT_MARGIN;

                ctx.font = BUBBLE_CONFIG.CUML_DELTA_FONT;
                ctx.textBaseline = 'middle';

                for (const level of filteredLevels) {
                    const price = parseFloat(level.priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    const net = level.net;
                    const barRatio = Math.abs(net) / maxAbsDelta;
                    const barWidth = barRatio * BUBBLE_CONFIG.CUML_DELTA_BAR_MAX_WIDTH;
                    const color = net >= 0 ? BUBBLE_CONFIG.BUY_COLOR : BUBBLE_CONFIG.SELL_COLOR;

                    // Glow on major bars (>60% of max)
                    if (barRatio >= BUBBLE_CONFIG.CUML_DELTA_GLOW_THRESHOLD) {
                        ctx.save();
                        ctx.shadowColor = _rgba(color, 0.4);
                        ctx.shadowBlur = 6;
                    }

                    // Horizontal bar: extends LEFT from right edge
                    ctx.fillStyle = _rgba(color, BUBBLE_CONFIG.CUML_DELTA_BAR_ALPHA);
                    ctx.fillRect(
                        rightEdge - barWidth,
                        y - BUBBLE_CONFIG.CUML_DELTA_BAR_HEIGHT / 2,
                        barWidth,
                        BUBBLE_CONFIG.CUML_DELTA_BAR_HEIGHT
                    );

                    if (barRatio >= BUBBLE_CONFIG.CUML_DELTA_GLOW_THRESHOLD) {
                        ctx.restore();
                    }

                    // Delta label: right-aligned next to bar
                    const sign = net >= 0 ? '+' : '';
                    const label = `${sign}${net}Δ`;
                    ctx.textAlign = 'right';
                    ctx.fillStyle = _rgba(color, BUBBLE_CONFIG.CUML_DELTA_LABEL_ALPHA);
                    ctx.fillText(label, rightEdge - barWidth - 4, y);
                }
                ctx.textAlign = 'center';  // reset
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
