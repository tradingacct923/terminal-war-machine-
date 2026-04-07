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
// HEATMAP SETTINGS (user-configurable, persisted to localStorage)
// ═══════════════════════════════════════════════════════════════════════════
const HM_DEFAULTS = {
    imbalance: true, bidColor: '#00ff96', askColor: '#ff4030', imbalanceOpacity: 75,
    wallglow: false, wallglowBlur: 8,
    wallColor: '#ffffff',  // color heavy orders blend toward (default white)
    wallBlend: 90,         // 0-100: how much heavy orders blend toward wallColor
    densityBoost: 100,     // 50-300: intensity multiplier (100 = default, 200 = 2× brighter)
    midprice: true, midpriceColor: '#ffdc00', midpriceWidth: 2,
    microprice: true, micropriceColor: '#00dcff', micropriceWidth: 3,
    trades: false, buyColor: '#00ff78', sellColor: '#ff3246', tradesSize: 6,
    delta: true, deltaHeight: 40,
    spread: false, spreadHeight: 14,
    persistence: true,   // depth persistence borders
    velocity: true,      // depth velocity pulses
    depthMax: 0,         // 0 = auto (EWMA), >0 = manual max contracts for full brightness
    otrLow: 3,           // OTR below this = real (green diamond)
    otrHigh: 10,         // OTR above this = fake (red diamond)
    persistMid: 5,       // snapshots for established tier
    persistHigh: 20,     // snapshots for battle-tested tier
    velSigma: 10,        // velocity σ multiplier (/10 → 1.0σ default). Range 5-30
    wallglowPct: 90,     // percentile for wall glow threshold. Range 70-99
    ewmaAlpha: 5,        // EWMA decay rate (/100 → 0.05 default). Range 1-20
    // Phase 1: Institutional Upgrades
    flickerFilter: 0,    // min persistence (snapshots) to display a cell (0=off, 3-10 typical)
    bboBar: true,        // show best bid/ask size imbalance bar at top of heatmap
    clusterTape: true,   // show clustered trade tape on heatmap
};

const HeatmapSettings = { ...HM_DEFAULTS };

// Load saved settings from localStorage
(function _loadHMS() {
    try {
        const saved = localStorage.getItem('heatmapSettings');
        if (saved) Object.assign(HeatmapSettings, JSON.parse(saved));
    } catch (e) { /* ignore */ }
    // Force-disable removed features (override stale localStorage)
    HeatmapSettings.trades = false;
    HeatmapSettings.wallglow = false;
    HeatmapSettings.spread = false;
})();

function _saveHMS() {
    try { localStorage.setItem('heatmapSettings', JSON.stringify(HeatmapSettings)); } catch (e) { /* ignore */ }
}

// Helper: hex color → {r,g,b}
function _hexToRgb(hex) {
    const n = parseInt(hex.replace('#', ''), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

// Initialize settings panel UI once DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const panel = document.getElementById('hm-settings-panel');
    const openBtn = document.getElementById('t-heatmap-settings-btn');
    const closeBtn = document.getElementById('hm-settings-close');
    const resetBtn = document.getElementById('hms-reset');

    if (!panel || !openBtn) return;

    // Toggle panel — use event delegation so it survives layout re-renders
    document.addEventListener('click', (e) => {
        // Open/close when clicking the gear button (or its SVG child)
        if (e.target.closest('#t-heatmap-settings-btn')) {
            panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
            return;
        }
        // Close button (✕) inside the panel
        if (e.target.closest('#hm-settings-close')) {
            panel.style.display = 'none';
            return;
        }
        // Close when clicking outside the panel (but not the button)
        if (panel.style.display !== 'none' && !panel.contains(e.target)) {
            panel.style.display = 'none';
        }
    });

    // Map each control to its setting
    const bindings = [
        { id: 'hms-imbalance', key: 'imbalance', type: 'check' },
        { id: 'hms-bid-color', key: 'bidColor', type: 'color' },
        { id: 'hms-ask-color', key: 'askColor', type: 'color' },
        { id: 'hms-imbalance-opacity', key: 'imbalanceOpacity', type: 'range' },
        { id: 'hms-midprice', key: 'midprice', type: 'check' },
        { id: 'hms-midprice-color', key: 'midpriceColor', type: 'color' },
        { id: 'hms-midprice-width', key: 'midpriceWidth', type: 'range' },
        { id: 'hms-microprice', key: 'microprice', type: 'check' },
        { id: 'hms-microprice-color', key: 'micropriceColor', type: 'color' },
        { id: 'hms-microprice-width', key: 'micropriceWidth', type: 'range' },
        { id: 'hms-delta', key: 'delta', type: 'check' },
        { id: 'hms-delta-height', key: 'deltaHeight', type: 'range' },
        { id: 'hms-persistence', key: 'persistence', type: 'check' },
        { id: 'hms-velocity', key: 'velocity', type: 'check' },
        { id: 'hms-depthmax', key: 'depthMax', type: 'range' },
        { id: 'hms-otr-low', key: 'otrLow', type: 'range' },
        { id: 'hms-otr-high', key: 'otrHigh', type: 'range' },
        { id: 'hms-persist-mid', key: 'persistMid', type: 'range' },
        { id: 'hms-persist-high', key: 'persistHigh', type: 'range' },
        { id: 'hms-vel-sigma', key: 'velSigma', type: 'range' },
        { id: 'hms-wallglow-pct', key: 'wallglowPct', type: 'range' },
        { id: 'hms-ewma-alpha', key: 'ewmaAlpha', type: 'range' },
        { id: 'hms-flicker-filter', key: 'flickerFilter', type: 'range' },
        { id: 'hms-bbo-bar', key: 'bboBar', type: 'check' },
        { id: 'hms-cluster-tape', key: 'clusterTape', type: 'check' },
        { id: 'hms-wall-color', key: 'wallColor', type: 'color' },
        { id: 'hms-wall-blend', key: 'wallBlend', type: 'range' },
        { id: 'hms-density-boost', key: 'densityBoost', type: 'range' },
    ];

    // Set initial values from HeatmapSettings and bind listeners
    for (const b of bindings) {
        const el = document.getElementById(b.id);
        if (!el) continue;
        // Set initial value
        if (b.type === 'check') el.checked = HeatmapSettings[b.key];
        else if (b.type === 'color') el.value = HeatmapSettings[b.key];
        else el.value = HeatmapSettings[b.key];

        // Bind change
        const evt = b.type === 'range' ? 'input' : 'change';
        el.addEventListener(evt, () => {
            if (b.type === 'check') HeatmapSettings[b.key] = el.checked;
            else if (b.type === 'range') HeatmapSettings[b.key] = parseInt(el.value);
            else HeatmapSettings[b.key] = el.value;
            _saveHMS();
            // Update depthMax live label
            if (b.key === 'depthMax') {
                const lbl = document.getElementById('hms-depthmax-val');
                if (lbl) lbl.textContent = parseInt(el.value) === 0 ? 'auto' : el.value;
            }
            // Update OTR/persistence live labels
            const labelMap = {
                'otrLow': 'hms-otr-low-val',
                'otrHigh': 'hms-otr-high-val',
                'persistMid': 'hms-persist-mid-val',
                'persistHigh': 'hms-persist-high-val',
                'velSigma': 'hms-vel-sigma-val',
                'wallglowPct': 'hms-wallglow-pct-val',
                'ewmaAlpha': 'hms-ewma-alpha-val',
                'flickerFilter': 'hms-flicker-filter-val',
                'wallBlend': 'hms-wall-blend-val',
                'densityBoost': 'hms-density-boost-val',
            };
            if (labelMap[b.key]) {
                const lbl2 = document.getElementById(labelMap[b.key]);
                if (lbl2) {
                    // Special formatting for sigma and alpha values
                    if (b.key === 'velSigma') lbl2.textContent = (parseInt(el.value) / 10).toFixed(1);
                    else if (b.key === 'ewmaAlpha') lbl2.textContent = (parseInt(el.value) / 100).toFixed(2);
                    else if (b.key === 'flickerFilter') lbl2.textContent = parseInt(el.value) === 0 ? 'off' : el.value;
                    else if (b.key === 'densityBoost') lbl2.textContent = (parseInt(el.value) / 100).toFixed(1) + '×';
                    else lbl2.textContent = el.value;
                }
            }
        });
    }

    // Reset button
    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            Object.assign(HeatmapSettings, HM_DEFAULTS);
            _saveHMS();
            // Re-sync UI
            for (const b of bindings) {
                const el = document.getElementById(b.id);
                if (!el) continue;
                if (b.type === 'check') el.checked = HM_DEFAULTS[b.key];
                else el.value = HM_DEFAULTS[b.key];
            }
            // Re-sync all live labels
            const lbl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
            lbl('hms-depthmax-val', 'auto');
            lbl('hms-otr-low-val', String(HM_DEFAULTS.otrLow));
            lbl('hms-otr-high-val', String(HM_DEFAULTS.otrHigh));
            lbl('hms-persist-mid-val', String(HM_DEFAULTS.persistMid));
            lbl('hms-persist-high-val', String(HM_DEFAULTS.persistHigh));
            lbl('hms-vel-sigma-val', (HM_DEFAULTS.velSigma / 10).toFixed(1));
            lbl('hms-wallglow-pct-val', String(HM_DEFAULTS.wallglowPct));
            lbl('hms-ewma-alpha-val', (HM_DEFAULTS.ewmaAlpha / 100).toFixed(2));
            lbl('hms-flicker-filter-val', 'off');
            lbl('hms-wall-blend-val', String(HM_DEFAULTS.wallBlend));
            lbl('hms-density-boost-val', (HM_DEFAULTS.densityBoost / 100).toFixed(1) + '×');
        });
    }
});

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

    // ── DOM Depth Heatmap ──
    DOM_HEATMAP_ENABLED: true,
    DOM_HEATMAP_WIDTH: 120,           // px max width of the heatmap strip
    DOM_HEATMAP_GLOW_BLUR: 12,       // glow blur radius for heavy levels
    DOM_HEATMAP_PRICE_LABEL_W: 58,   // px width of chart's price label column
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

        // ── Always render full bubbles regardless of zoom level ──
        const useDots = false;  // dots-only mode removed; full bubbles at all zoom levels
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

                    // Convert price to Y coordinate (needed for sigma calc)
                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    // ── σ distance in log-space ──
                    const logVol = Math.log(totalVol + 1);
                    const sigmaDistance = logStddev > 0
                        ? (logVol - logAvg) / logStddev
                        : (totalVol > 0 ? 1 : 0);

                    // ═══ FILTERING: dim context + conviction highlights ═══
                    // Below 0.5σ = true noise (1-2 contracts), remove completely
                    // 0.5σ to 1.5σ = dim context (σ² gradient = 4-15% opacity, market texture)
                    // 1.5σ+ = pops — but needs 70% dominance (conviction, not balanced)
                    // Absorption always shows at 0.5σ+ (battle matters, not size)
                    if (sigmaDistance < 0.5) continue;  // true noise floor
                    if (!isAbsorb && sigmaDistance >= 1.5 && dominance < 0.70) continue;  // big but no conviction

                    // ── Step 3: THE EYES — Exponential gradient from log-σ ──

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

            });  // close useMediaCoordinateSpace
    }  // close draw()
}  // close VolumeBubbleRenderer class

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

// ═══════════════════════════════════════════════════════════════════════════
// [REMOVED] DOM Heatmap v1 (renderDomHeatmap, _drawHeatmapSide,
//           _drawGhostLevels, _updateLiquidityMemory)
// Superseded by renderDomHeatmap2D below. ~500 lines removed.
// ═══════════════════════════════════════════════════════════════════════════













// ═══════════════════════════════════════════════════════════════════════════════
// 2D PASSIVE DOM HEATMAP v2 — Market-Maker Grade
// ═══════════════════════════════════════════════════════════════════════════════
//
// Institutional upgrades over v1:
//  1. Percentile-based normalization (P75 ref) — only true walls glow hot
//  2. Liquidity delta layer — flash cyan/magenta when orders appear/vanish
//  3. Multi-stop perceptual color ramp (dark → dim → saturated → white-hot)
//  4. Wall glow effect for ≥90th percentile levels
//  5. Liquidity vacuum detection (empty bands where orders pulled)
//  6. Bid/ask overlap zone highlighting (contested price = spread battle)
//
// Data source: WebSocket push (dom_snapshot event) with REST fallback
// ═══════════════════════════════════════════════════════════════════════════════

const DOM2D = {
    // ── Config ──
    ENABLED: true,
    COL_WIDTH: 4,               // px per time column (wider for readability)
    MAX_COLS: 2500,             // max columns displayed (supports up to 10k pixels width)
    FETCH_INTERVAL_MS: 2000,    // poll interval (REST fallback only)
    HEATMAP_2D_WIDTH: 350,      // max px width of the 2D area (focused right-side strip)
    WALL_GLOW_BLUR: 8,         // glow blur for heavy walls
    CURRENT_COL_WIDTH: 12,     // px width of the rightmost "current state" column

    // ── State ──
    _snapshots: [],             // [{ts, bids:{price:size}, asks:{price:size}}, ...]
    _lastFetchTs: 0,
    _fetchTimer: null,
    _wsActive: false,           // true when WebSocket push is active
    _wsSnapCount: 0,            // counter for throttled percentile recalc
    _wsRetryTimer: null,        // retry timer for deferred WS init
    _globalMax: 1,
    _p75: 1,                    // 75th percentile of all sizes
    _p90: 1,                    // 90th percentile (wall threshold)
    _p50: 1,                    // median

    // ── Persistence & Velocity State ──
    _depthPersistence: new Map(),  // price → consecutive snapshot count above mean
    _prevSnapSizes: new Map(),    // price → previous snapshot size (for velocity diff)
    _velocityFlash: new Map(),    // price → {delta, age} for velocity pulse rendering

    // ── OTR (Order-to-Trade Ratio) ──
    _fillAccum: new Map(),        // price → total fills accumulated at that level
    _otrScores: new Map(),        // price → OTR score (resting/fills). High = decoration, Low = real

    // ── EWMA Normalization ──
    _ewmaMean: 0,                 // exponentially weighted moving average of book depth
    _ewmaVar: 0,                  // exponentially weighted moving variance
    _ewmaStdDev: 1,               // sqrt of ewmaVar
    _ewmaAlpha: 0.05,             // decay factor (0.05 = ~20 snapshots half-life)
    _ewmaInitialized: false,

    // ── Book Asymmetry ──
    _bookAsymmetry: 0.5,          // 0 = all asks, 0.5 = balanced, 1 = all bids
    _asymmetryHistory: [],        // rolling window for sparkline

    // ── Phase 1: Institutional Upgrades ──
    // BBO Imbalance: best bid/ask size ratio history
    _bboHistory: [],              // [{ts, bidSize, askSize, ratio}]  rolling 120 entries

    // Clustered Trade Tape: aggregated trades grouped by time+price
    _clusteredTape: [],           // [{ts, price, side, totalVol, count, maxSingle}]

    // ── Lifecycle ──
    destroy() {
        this._snapshots = [];
        this._depthPersistence.clear();
        this._prevSnapSizes.clear();
        this._velocityFlash.clear();
        this._fillAccum.clear();
        this._otrScores.clear();
        this._bboHistory = [];
        this._asymmetryHistory = [];
        this._clusteredTape = [];
        this._ewmaInitialized = false;
        _rgbaCache.clear();
        // Clean up KineticText WebGL if active
        if (typeof KineticText !== 'undefined' && KineticText.gl) {
            try { KineticText.destroy(); } catch (e) { /* ignore */ }
        }
    },
};

// ═══════════════════════════════════════════════════════════════════════════
// PHASE 1: ZERO-ALLOCATION INFRASTRUCTURE
// ═══════════════════════════════════════════════════════════════════════════

// ── Pre-built rgba() string cache (eliminates per-cell template literal + .toFixed) ──
// Key: (r << 24 | g << 16 | b << 8 | alphaIdx) → cached "rgba(r,g,b,0.xxx)" string
const _rgbaCache = new Map();
function _cachedRgba(r, g, b, alpha) {
    // Quantize alpha to 64 levels (0.015 precision — invisible difference)
    const ai = (alpha * 64 + 0.5) | 0;
    const key = (r << 17) | (g << 9) | (b << 1) | (ai > 63 ? 63 : ai);
    let s = _rgbaCache.get(key);
    if (s) return s;
    const qa = (ai / 64);
    s = `rgba(${r},${g},${b},${qa < 0.004 ? '0' : qa > 0.996 ? '1' : qa.toFixed(3)})`;
    _rgbaCache.set(key, s);
    return s;
}

// ── Snapshot pre-processing: compute allPrices array + sorted data at ingestion ──
// Eliminates per-frame new Set(Object.keys()) + parseFloat() for every column
function _prepSnapshot(snap) {
    // Build combined unique price set ONCE at ingestion time
    const priceSet = new Set();
    const bidKeys = Object.keys(snap.bids);
    const askKeys = Object.keys(snap.asks);

    // ── Pre-compute BBO, mid, micro, spread at ingestion (not per-frame) ──
    // Parse bid entries: [price, size] sorted descending
    const bidEntries = [];
    for (let i = 0; i < bidKeys.length; i++) {
        const p = parseFloat(bidKeys[i]);
        const s = snap.bids[bidKeys[i]];
        priceSet.add(bidKeys[i]);
        if (!isNaN(p) && s > 0) bidEntries.push([p, s]);
    }
    bidEntries.sort((a, b) => b[0] - a[0]);

    // Parse ask entries: [price, size] sorted ascending
    const askEntries = [];
    for (let i = 0; i < askKeys.length; i++) {
        const p = parseFloat(askKeys[i]);
        const s = snap.asks[askKeys[i]];
        priceSet.add(askKeys[i]);
        if (!isNaN(p) && s > 0) askEntries.push([p, s]);
    }
    askEntries.sort((a, b) => a[0] - b[0]);

    const bestBid = bidEntries.length ? bidEntries[0][0] : null;
    const bestAsk = askEntries.length ? askEntries[0][0] : null;
    snap._bestBid = bestBid;
    snap._bestAsk = bestAsk;
    snap._midPrice = (bestBid !== null && bestAsk !== null) ? (bestBid + bestAsk) / 2 : null;
    snap._spread = (bestBid !== null && bestAsk !== null) ? (bestAsk - bestBid) : 0;
    snap._bidEntries = bidEntries;
    snap._askEntries = askEntries;

    // ── Multi-level weighted micro-price (computed once at ingestion) ──
    let microNum = 0, microDen = 0;
    const LAMBDA = 0.5;
    const maxLevels = Math.max(bidEntries.length, askEntries.length);
    for (let i = 0; i < maxLevels; i++) {
        const w = Math.exp(-LAMBDA * i);
        if (i < bidEntries.length && i < askEntries.length) {
            const [bidP, bidS] = bidEntries[i];
            const [askP, askS] = askEntries[i];
            microNum += (askP * bidS * w) + (bidP * askS * w);
            microDen += (bidS + askS) * w;
        } else if (i < bidEntries.length) {
            const [bidP, bidS] = bidEntries[i];
            microNum += bidP * bidS * w;
            microDen += bidS * w;
        } else if (i < askEntries.length) {
            const [askP, askS] = askEntries[i];
            microNum += askP * askS * w;
            microDen += askS * w;
        }
    }
    snap._micro = microDen > 0 ? microNum / microDen : (snap._midPrice || 0);

    // Convert to array (reusable, no per-frame allocation)
    snap._allPrices = Array.from(priceSet);
    // Pre-parse float values for hot path
    snap._parsedPrices = new Float64Array(snap._allPrices.length);
    for (let i = 0; i < snap._allPrices.length; i++) {
        snap._parsedPrices[i] = parseFloat(snap._allPrices[i]);
    }
    return snap;
}


// ── Perceptual color ramps (HSL-inspired, 5 stops each) ──
// Each stop: [r, g, b, minNorm, maxNorm]
// Bid ramp: dark teal → dim teal → bright cyan → white-hot
const BID_RAMP = [
    { r: 8,   g: 30,  b: 35,  lo: 0.00, hi: 0.15 },  // barely visible
    { r: 15,  g: 70,  b: 75,  lo: 0.15, hi: 0.35 },  // dim teal
    { r: 0,   g: 150, b: 140, lo: 0.35, hi: 0.60 },  // mid teal
    { r: 0,   g: 220, b: 200, lo: 0.60, hi: 0.85 },  // bright cyan
    { r: 180, g: 255, b: 245, lo: 0.85, hi: 1.00 },  // white-hot
];
// Ask ramp: dark amber → dim orange → bright orange → white-hot
const ASK_RAMP = [
    { r: 35,  g: 20,  b: 5,   lo: 0.00, hi: 0.15 },
    { r: 80,  g: 45,  b: 10,  lo: 0.15, hi: 0.35 },
    { r: 180, g: 100, b: 20,  lo: 0.35, hi: 0.60 },
    { r: 240, g: 150, b: 30,  lo: 0.60, hi: 0.85 },
    { r: 255, g: 230, b: 180, lo: 0.85, hi: 1.00 },
];

function _rampColor(norm, ramp) {
    // Find the two stops to interpolate between
    const clamped = Math.max(0, Math.min(1, norm));
    for (let i = 0; i < ramp.length; i++) {
        const stop = ramp[i];
        if (clamped <= stop.hi) {
            // Interpolate within this stop range
            const t = (clamped - stop.lo) / (stop.hi - stop.lo);
            const next = ramp[Math.min(i + 1, ramp.length - 1)];
            const r = Math.round(stop.r + (next.r - stop.r) * t);
            const g = Math.round(stop.g + (next.g - stop.g) * t);
            const b = Math.round(stop.b + (next.b - stop.b) * t);
            return [r, g, b];
        }
    }
    const last = ramp[ramp.length - 1];
    return [last.r, last.g, last.b];
}

/**
 * Fetch DOM history from the backend and update the local snapshot cache.
 */
function _fetchDomHistory(symbol) {
    if (!DOM2D.ENABLED) return;
    const since = DOM2D._lastFetchTs || 0;
    fetch(`/api/l2/dom-history?symbol=${symbol}&since=${since}&res=auto`)
        .then(r => r.json())
        .then(data => {
            if (!data || !data.snapshots || !data.snapshots.length) return;

            for (const snap of data.snapshots) {
                const ts = snap[0];
                const bids = snap[1] || {};
                const asks = snap[2] || {};
                const trades = snap[3] || [];  // [{p, v, s}, ...]
                const absorption = snap[4] || {};  // {price: {s, w, i, h, c, sh, rs, sd}}
                DOM2D._snapshots.push(_prepSnapshot({ ts, bids, asks, trades, absorption }));
                if (ts > DOM2D._lastFetchTs) DOM2D._lastFetchTs = ts;
            }

            const maxKeep = DOM2D.MAX_COLS * 3;
            if (DOM2D._snapshots.length > maxKeep) {
                DOM2D._snapshots = DOM2D._snapshots.slice(-maxKeep);
            }

            // ── Compute percentile-based normalization ──
            const allSizes = [];
            for (const snap of DOM2D._snapshots) {
                for (const s of Object.values(snap.bids)) if (s > 0) allSizes.push(s);
                for (const s of Object.values(snap.asks)) if (s > 0) allSizes.push(s);
            }
            if (allSizes.length > 0) {
                allSizes.sort((a, b) => a - b);
                const p = (pct) => allSizes[Math.min(Math.floor(pct * allSizes.length), allSizes.length - 1)];
                DOM2D._p50 = p(0.50);
                DOM2D._p75 = Math.max(p(0.75), 1);
                DOM2D._p90 = Math.max(p(0.90), 1);
                DOM2D._globalMax = allSizes[allSizes.length - 1];

                // Bootstrap EWMA from initial REST data so normalization isn't stuck at 0
                if (!DOM2D._ewmaInitialized) {
                    const avg = allSizes.reduce((a, b) => a + b, 0) / allSizes.length;
                    DOM2D._ewmaMean = avg;
                    DOM2D._ewmaVar = allSizes.reduce((acc, s) => acc + (s - avg) ** 2, 0) / allSizes.length;
                    DOM2D._ewmaStdDev = Math.max(Math.sqrt(DOM2D._ewmaVar), 1);
                    DOM2D._ewmaInitialized = true;
                }
            }
        })
        .catch(() => {});
}

/**
 * Recalculate percentile-based normalization from current snapshot cache.
 */
function _updatePercentiles() {
    const allSizes = [];
    for (const snap of DOM2D._snapshots) {
        for (const s of Object.values(snap.bids)) if (s > 0) allSizes.push(s);
        for (const s of Object.values(snap.asks)) if (s > 0) allSizes.push(s);
    }
    if (allSizes.length > 0) {
        allSizes.sort((a, b) => a - b);
        const p = (pct) => allSizes[Math.min(Math.floor(pct * allSizes.length), allSizes.length - 1)];
        DOM2D._p50 = p(0.50);
        DOM2D._p75 = Math.max(p(0.75), 1);
        DOM2D._p90 = Math.max(p(0.90), 1);
        DOM2D._globalMax = allSizes[allSizes.length - 1];
    }
}

/**
 * Initialize WebSocket listener for real-time DOM snapshot push.
 * Returns true if WebSocket is available, false otherwise.
 */
function _initDomWebSocket(symbol) {
    if (!window._sio) return false;

    window._sio.off('dom_snapshot'); // remove stale listener
    window._sio.on('dom_snapshot', (data) => {
        const _auditT0 = performance.now(); // pipeline latency start
        if (!data || data.sym !== symbol) return;

        const snap = _prepSnapshot({
            ts: data.ts,
            bids: data.bids || {},
            asks: data.asks || {},
            trades: data.trades || [],
            absorption: data.abs || {},
        });
        DOM2D._snapshots.push(snap);
        if (data.ts > DOM2D._lastFetchTs) DOM2D._lastFetchTs = data.ts;

        // ── Feed trade data to KineticText shock engine ──
        if (typeof KineticText !== 'undefined' && KineticText.programValid) {
            const trades = data.trades || [];
            if (trades.length > 0) {
                KineticText.processTrades(trades, data.bids || {}, data.asks || {});
            }
        }

        // ── Feed SigmaEngine with raw trade volumes (sigma-driven thresholds) ──
        if (typeof SigmaEngine !== 'undefined') {
            SigmaEngine.feedTrades(data.trades || []);
            SigmaEngine.feedAbsorption(data.abs || {});
        }

        // ── Feed absorption data to PressureField (continuous pressure sources) ──
        if (typeof PressureField !== 'undefined' && PressureField._ready && data.abs) {
            PressureField.feedAbsorption(data.abs);
        }

        // Trim to max capacity
        const maxKeep = DOM2D.MAX_COLS * 3;
        if (DOM2D._snapshots.length > maxKeep) {
            DOM2D._snapshots = DOM2D._snapshots.slice(-maxKeep);
        }

        // Throttled percentile recalc (every 10 snapshots ≈ 5s)
        DOM2D._wsSnapCount = (DOM2D._wsSnapCount || 0) + 1;
        if (DOM2D._wsSnapCount % 10 === 0) _updatePercentiles();

        // ── DataAudit: measure pipeline latency ──
        if (typeof DataAudit !== 'undefined' && DataAudit._active) {
            DataAudit.measure(data, _auditT0);
        }
    });

    DOM2D._wsActive = true;
    console.log('[DOM-WS] WebSocket push active for', symbol);
    return true;
}

function startDomHistory(symbol) {
    if (DOM2D._fetchTimer) clearInterval(DOM2D._fetchTimer);
    if (DOM2D._wsRetryTimer) clearTimeout(DOM2D._wsRetryTimer);
    DOM2D._snapshots = [];
    DOM2D._lastFetchTs = 0;
    DOM2D._globalMax = 1;
    DOM2D._p75 = 1;
    DOM2D._p90 = 1;
    DOM2D._p50 = 1;
    DOM2D._wsActive = false;
    DOM2D._wsSnapCount = 0;

    // ── Full state reset on symbol switch ──
    // Prevents stale data from previous symbol bleeding into new heatmap
    DOM2D._bboHistory = [];
    DOM2D._asymmetryHistory = [];
    DOM2D._bookAsymmetry = 0.5;
    DOM2D._depthPersistence = new Map();
    DOM2D._velocityFlash = new Map();
    DOM2D._fillAccum = new Map();
    DOM2D._otrScores = new Map();
    DOM2D._prevSnapSizes = new Map();
    DOM2D._ewmaInitialized = false;
    DOM2D._ewmaMean = 0;
    DOM2D._ewmaVar = 0;
    DOM2D._ewmaStdDev = 1;

    // Always do one REST fetch for history backfill
    _fetchDomHistory(symbol);

    // Try WebSocket push — retry if _sio not yet connected (page load timing)
    let retries = 0;
    const maxRetries = 10; // 10 × 500ms = 5 seconds
    function tryWs() {
        if (_initDomWebSocket(symbol)) {
            // WS connected — stop REST polling if running
            if (DOM2D._fetchTimer) { clearInterval(DOM2D._fetchTimer); DOM2D._fetchTimer = null; }
            return;
        }
        retries++;
        if (retries < maxRetries) {
            DOM2D._wsRetryTimer = setTimeout(tryWs, 500);
        } else {
            // Fallback: REST polling (Socket.IO never connected)
            console.log('[DOM-WS] WebSocket not available after 5s, falling back to REST polling');
            DOM2D._fetchTimer = setInterval(() => _fetchDomHistory(symbol), DOM2D.FETCH_INTERVAL_MS);
        }
    }
    tryWs();
}

function stopDomHistory() {
    if (DOM2D._fetchTimer) { clearInterval(DOM2D._fetchTimer); DOM2D._fetchTimer = null; }
    if (DOM2D._wsRetryTimer) { clearTimeout(DOM2D._wsRetryTimer); DOM2D._wsRetryTimer = null; }
    if (window._sio) window._sio.off('dom_snapshot');
    DOM2D._wsActive = false;
    // Clean up GC engine listeners and timers to prevent memory leaks
    if (typeof DOM2D.destroy === 'function') DOM2D.destroy();
}

/**
 * Render the 2D scrolling DOM heatmap — Market-Maker Grade.
 */
function renderDomHeatmap2D(canvas, priceToY, midPrice) {
    if (!DOM2D.ENABLED || !canvas || !priceToY || !midPrice) return;
    const snaps = DOM2D._snapshots;
    if (snaps.length < 2) return; // need ≥2 for delta

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const cssRect = canvas.getBoundingClientRect();
    const cssW = cssRect.width;
    const cssH = cssRect.height;
    if (cssW <= 0 || cssH <= 0) return;

    const dpr = window.devicePixelRatio || 1;
    const STRIP_W = BUBBLE_CONFIG.DOM_HEATMAP_WIDTH;
    const PRICE_W = 0; // prices now in separate ladder zone
    const COL_W = DOM2D.COL_WIDTH;

    const heatmapRight = cssW - 2;  // right edge of canvas (small margin)
    // Standalone heatmap pane: fill the entire canvas width
    // Overlay mode (on chart): limit to 25% / 350px strip on the right edge
    // Force standalone rendering to guarantee full-width heatmap drawing
    const isStandalone = canvas.parentElement && canvas.parentElement.dataset.feature === 'heatmap';
    const heatmapW = isStandalone
        ? cssW - 4  // full width minus small margins
        : Math.min(DOM2D.HEATMAP_2D_WIDTH, cssW * 0.25);
    const maxCols = Math.min(DOM2D.MAX_COLS, Math.floor(heatmapW / COL_W));

    const displaySnaps = snaps.slice(-maxCols);
    if (displaySnaps.length < 2) return;

    // ── Pixel scale ──
    // Standalone mode: use custom linear price mapping that fills the full canvas height
    // Overlay mode: use chart's priceToCoordinate mapping
    let pxPerTick, visMin, visMax, _priceToY;
    
    if (isStandalone) {
        // Standalone: center on midPrice, show ±7 points (28 ticks each side)
        const DOM_RANGE = 7.0; // ±7 points around mid (covers full 25-level depth)
        visMin = midPrice - DOM_RANGE;
        visMax = midPrice + DOM_RANGE;
        const totalTicks = (DOM_RANGE * 2) / 0.25; // 56 ticks
        pxPerTick = cssH / totalTicks; // ~16.6px per tick at 933px height
        // Custom priceToY: linear mapping, higher price = lower Y (inverted)
        _priceToY = (price) => {
            if (price < visMin || price > visMax) return null;
            return cssH - ((price - visMin) / (visMax - visMin)) * cssH;
        };
    } else {
        // Overlay: use chart coordinate system with safe wrapper
        const refY1 = priceToY(midPrice);
        const refY2 = priceToY(midPrice + 0.25);
        // If the chart cannot map the price (e.g., out of view), fall back to a clamped mapping.
        const safePriceToY = (price) => {
            const y = priceToY(price);
            if (y !== null) return y;
            // Clamp to canvas edges when out of range.
            return price < midPrice ? cssH : 0;
        };
        const _priceToY = safePriceToY;
        // Compute tick spacing using the reference points (fallback if null).
        const baseY1 = refY1 !== null ? refY1 : cssH;
        const baseY2 = refY2 !== null ? refY2 : 0;
        pxPerTick = Math.abs(baseY2 - baseY1);
        if (pxPerTick <= 0) return;
        
        // ── Visible price range (clamped to actual data window) ──
        const visibleTicks = cssH / pxPerTick;
        const chartRangeHalf = visibleTicks * 0.25 * 0.85;
        const dataRangeHalf = 25 * 0.25;
        const rangeHalf = Math.min(chartRangeHalf, dataRangeHalf * 1.1);
        visMin = midPrice - rangeHalf;
        visMax = midPrice + rangeHalf;
    }
    if (pxPerTick <= 0) return;

    const MIN_ROW_H = 3;
    let ticksPerRow = 1;
    if (pxPerTick < MIN_ROW_H) {
        ticksPerRow = Math.ceil(MIN_ROW_H / pxPerTick);
    }
    const bucketSize = ticksPerRow * 0.25;
    const rowH = Math.max(pxPerTick * ticksPerRow, MIN_ROW_H);

    // ── Draw ──
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';

    // Only clear the heatmap strip zone — leave the rest of the canvas untouched
    const heatmapLeft = Math.max(0, heatmapRight - heatmapW);
    ctx.clearRect(heatmapLeft - 2, 0, heatmapW + 4, cssH);

    const numCols = displaySnaps.length;

    // ── Deep space backdrop ──
    const topY = _priceToY(visMax);
    const botY = _priceToY(visMin);
    if (topY !== null && botY !== null) {
        const bgTop = Math.max(Math.min(topY, botY) - 2, 0);
        const bgBot = Math.min(Math.max(topY, botY) + 2, cssH);
        if (bgBot > bgTop) {
            const bgGr = ctx.createLinearGradient(0, bgTop, 0, bgBot);
            bgGr.addColorStop(0, 'rgba(4, 7, 18, 0.6)');
            bgGr.addColorStop(0.5, 'rgba(5, 8, 20, 0.55)');
            bgGr.addColorStop(1, 'rgba(4, 6, 16, 0.6)');
            ctx.fillStyle = bgGr;
            ctx.fillRect(heatmapLeft - 1, bgTop, numCols * COL_W + 2, bgBot - bgTop);
        }
    }

    // ── Normalization reference ──
    const userMax = HeatmapSettings.depthMax || 0;
    const normRef = userMax > 0 ? userMax : (DOM2D._ewmaMean > 1 ? DOM2D._ewmaMean * 2 : DOM2D._p75 || 1);
    // Wall glow: compute percentile from last displayed snapshot
    let wallThreshold = normRef * 2;
    const _lastSnap = displaySnaps[numCols - 1];
    if (_lastSnap) {
        const allWallSizes = [];
        for (const s of Object.values(_lastSnap.bids)) if (s > 0) allWallSizes.push(s);
        for (const s of Object.values(_lastSnap.asks)) if (s > 0) allWallSizes.push(s);
        if (allWallSizes.length > 0) {
            allWallSizes.sort((a, b) => a - b);
            const pctIdx = Math.floor(allWallSizes.length * (HeatmapSettings.wallglowPct / 100));
            wallThreshold = allWallSizes[Math.min(pctIdx, allWallSizes.length - 1)];
        }
    }
    const GAP = 0.5; // sub-pixel gap between cells

    // ── LAYER 1: Imbalance-Weighted Density Cells ──
    // Colors each cell by book imbalance ratio: bid_size / (bid_size + ask_size)
    // Green gradient (OBI > 0.5) = bid pressure, Red gradient (OBI < 0.5) = ask pressure
    // Intensity still reflects absolute depth magnitude for wall visibility
    const _bidRgb = _hexToRgb(HeatmapSettings.bidColor);
    const _askRgb = _hexToRgb(HeatmapSettings.askColor);
    const _imbOpacity = HeatmapSettings.imbalanceOpacity / 100;
    // PERF: Hoist per-settings constants out of inner loop (computed once per frame)
    const _wallRgb_hoisted = _hexToRgb(HeatmapSettings.wallColor);
    const _wallBlendPct_hoisted = HeatmapSettings.wallBlend / 100;
    const _densityMul_hoisted = HeatmapSettings.densityBoost / 100;
    if (HeatmapSettings.imbalance)
    for (let col = 0; col < numCols; col++) {
        const snap = displaySnaps[col];
        const x = heatmapLeft + col * COL_W;

        // PERF: Use pre-computed price array from _prepSnapshot (zero alloc)
        const allPriceStrs = snap._allPrices || Object.keys(snap.bids).concat(Object.keys(snap.asks));
        const parsedPrices = snap._parsedPrices; // Float64Array if available

        for (let pi = 0; pi < allPriceStrs.length; pi++) {
            const priceStr = allPriceStrs[pi];
            const price = parsedPrices ? parsedPrices[pi] : parseFloat(priceStr);
            if (price !== price) continue; // NaN check (faster than isNaN)
            if (price < visMin || price > visMax) continue;

            const bidSize = snap.bids[priceStr] || 0;
            const askSize = snap.asks[priceStr] || 0;
            const totalSize = bidSize + askSize;
            if (totalSize <= 0) continue;

            // ── Flicker Filter: skip cells below min persistence on latest column ──
            // When enabled, only show levels that have persisted for N+ consecutive snapshots.
            // This strips HFT noise, leaving only genuine resting liquidity.
            if (HeatmapSettings.flickerFilter > 0 && col === numCols - 1) {
                const persist = DOM2D._depthPersistence.get(priceStr) || 0;
                if (persist < HeatmapSettings.flickerFilter) continue;
            }

            const bucketPrice = Math.floor(price / bucketSize) * bucketSize;
            const y = _priceToY(bucketPrice + bucketSize / 2);
            if (y === null || y < -rowH || y > cssH + rowH) continue;

            // ── Imbalance ratio: 0 = all ask, 0.5 = balanced, 1 = all bid ──
            const imbalance = bidSize / totalSize;

            // ── Depth magnitude: sqrt normalization for dramatic visual range ──
            // sqrt compresses the top end so walls pop but mid-range isn't washed out
            const dominant = Math.max(bidSize, askSize);
            const rawNorm = Math.min(dominant / normRef, 3.0) / 3.0; // cap at 3× P75
            const norm = Math.sqrt(rawNorm); // sqrt curve: makes mid-range visible

            // Pick base color from imbalance direction
            let baseR, baseG, baseB;
            if (imbalance > 0.5) {
                baseR = _bidRgb.r; baseG = _bidRgb.g; baseB = _bidRgb.b;
            } else {
                baseR = _askRgb.r; baseG = _askRgb.g; baseB = _askRgb.b;
            }

            // ── Smooth 2-phase color ramp with configurable wall color ──
            // PERF: _wallRgb, _wallBlendPct, _densityMul hoisted above loop
            const boostedNorm = Math.min(norm * _densityMul_hoisted, 1.0);
            let r, g, b;
            if (boostedNorm < 0.5) {
                const p = boostedNorm * 2;
                const brightness = 0.03 + p * p * 0.97;
                r = Math.round(baseR * brightness);
                g = Math.round(baseG * brightness);
                b = Math.round(baseB * brightness);
            } else {
                const p = (boostedNorm - 0.5) * 2;
                const blend = p * p * _wallBlendPct_hoisted;
                r = Math.round(baseR + (_wallRgb_hoisted.r - baseR) * blend);
                g = Math.round(baseG + (_wallRgb_hoisted.g - baseG) * blend);
                b = Math.round(baseB + (_wallRgb_hoisted.b - baseB) * blend);
            }

            // Alpha: smooth quadratic ramp with density boost (0.06 base for visibility)
            const alpha = (0.06 + boostedNorm * boostedNorm * 0.88) * _imbOpacity;

            // PERF: cached rgba string (eliminates per-cell template literal + .toFixed())
            ctx.fillStyle = _cachedRgba(r, g, b, alpha);
            // Rounded cell for smoother look
            const cellX = x + GAP * 0.5;
            const cellY = y - rowH / 2 + GAP;
            const cellW = COL_W - GAP;
            const cellH = rowH - GAP * 2;
            const cr = Math.min(1.5, cellH / 4);
            ctx.beginPath();
            ctx.moveTo(cellX + cr, cellY);
            ctx.lineTo(cellX + cellW - cr, cellY);
            ctx.arcTo(cellX + cellW, cellY, cellX + cellW, cellY + cr, cr);
            ctx.lineTo(cellX + cellW, cellY + cellH - cr);
            ctx.arcTo(cellX + cellW, cellY + cellH, cellX + cellW - cr, cellY + cellH, cr);
            ctx.lineTo(cellX + cr, cellY + cellH);
            ctx.arcTo(cellX, cellY + cellH, cellX, cellY + cellH - cr, cr);
            ctx.lineTo(cellX, cellY + cr);
            ctx.arcTo(cellX, cellY, cellX + cr, cellY, cr);
            ctx.fill();

            // ── Wall glow for P90+ levels ──
            if (HeatmapSettings.wallglow && dominant >= wallThreshold) {
                ctx.shadowColor = `rgba(${r},${g},${b},0.5)`;
                ctx.shadowBlur = HeatmapSettings.wallglowBlur;
                ctx.fillStyle = `rgba(${r},${g},${b},0.30)`;
                // Reuse rounded rect path
                ctx.beginPath();
                ctx.moveTo(cellX + cr, cellY);
                ctx.lineTo(cellX + cellW - cr, cellY);
                ctx.arcTo(cellX + cellW, cellY, cellX + cellW, cellY + cr, cr);
                ctx.lineTo(cellX + cellW, cellY + cellH - cr);
                ctx.arcTo(cellX + cellW, cellY + cellH, cellX + cellW - cr, cellY + cellH, cr);
                ctx.lineTo(cellX + cr, cellY + cellH);
                ctx.arcTo(cellX, cellY + cellH, cellX, cellY + cellH - cr, cr);
                ctx.lineTo(cellX, cellY + cr);
                ctx.arcTo(cellX, cellY, cellX + cr, cellY, cr);
                ctx.fill();
                ctx.shadowBlur = 0;
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // LAYER 1b: Depth Persistence + Velocity (per-snapshot z-score engine)
    // ═══════════════════════════════════════════════════════════════════════
    // Uses the LAST displayed snapshot to update rolling state.
    // Persistence: how many consecutive snapshots a level has been > mean
    // Velocity: depth change vs previous snapshot at each level

    const latestSnap = displaySnaps[numCols - 1];
    const prevSnap = numCols >= 2 ? displaySnaps[numCols - 2] : null;

    if (latestSnap) {
        // ── Compute per-snapshot z-score stats ──
        const allLevelSizes = [];
        for (const s of Object.values(latestSnap.bids)) if (s > 0) allLevelSizes.push(s);
        for (const s of Object.values(latestSnap.asks)) if (s > 0) allLevelSizes.push(s);

        let snapMean = 0, snapStdDev = 1;
        if (allLevelSizes.length > 0) {
            snapMean = allLevelSizes.reduce((a, b) => a + b, 0) / allLevelSizes.length;
            const variance = allLevelSizes.reduce((acc, s) => acc + (s - snapMean) ** 2, 0) / allLevelSizes.length;
            snapStdDev = Math.max(Math.sqrt(variance), 1);
        }

        // ── Update persistence map (on latest snapshot only) ──
        const allPricesLatest = new Set([
            ...Object.keys(latestSnap.bids),
            ...Object.keys(latestSnap.asks),
        ]);

        // Decay: any price NOT in latest snapshot resets to 0
        const newPersistence = new Map();
        for (const priceStr of allPricesLatest) {
            const bidSize = latestSnap.bids[priceStr] || 0;
            const askSize = latestSnap.asks[priceStr] || 0;
            const dominant = Math.max(bidSize, askSize);
            if (dominant > snapMean) {
                // Above mean → increment persistence
                newPersistence.set(priceStr, (DOM2D._depthPersistence.get(priceStr) || 0) + 1);
            }
            // Below mean → not in newPersistence = reset to 0
        }
        DOM2D._depthPersistence = newPersistence;

        // ── Compute velocity (latest vs previous snapshot) ──
        if (prevSnap) {
            const prevSizes = new Map();
            for (const [p, s] of Object.entries(prevSnap.bids)) prevSizes.set(p, (prevSizes.get(p) || 0) + s);
            for (const [p, s] of Object.entries(prevSnap.asks)) prevSizes.set(p, (prevSizes.get(p) || 0) + s);

            for (const priceStr of allPricesLatest) {
                const currSize = (latestSnap.bids[priceStr] || 0) + (latestSnap.asks[priceStr] || 0);
                const prevSize = prevSizes.get(priceStr) || 0;
                const delta = currSize - prevSize;

                // Only flash if change is > 1σ
                if (Math.abs(delta) > snapStdDev * (HeatmapSettings.velSigma / 10)) {
                    DOM2D._velocityFlash.set(priceStr, { delta, age: 0 });
                }
            }

            // Age and expire velocity flashes
            for (const [p, flash] of DOM2D._velocityFlash) {
                flash.age++;
                if (flash.age > 6) DOM2D._velocityFlash.delete(p);
            }
        }

        // ── Render persistence borders on the LAST column ──
        if (HeatmapSettings.persistence) {
            const lastColX = heatmapLeft + (numCols - 1) * COL_W;
            for (const [priceStr, count] of DOM2D._depthPersistence) {
                if (count < 2) continue; // skip very new levels
                const price = parseFloat(priceStr);
                if (isNaN(price) || price < visMin || price > visMax) continue;
                const bucketPrice = Math.floor(price / bucketSize) * bucketSize;
                const y = _priceToY(bucketPrice + bucketSize / 2);
                if (y === null) continue;

                // Persistence tiers — COLOR CODED for visibility:
                // 2-5 snapshots (1-2.5 sec): GOLD dashed = new/untested (possible spoof)
                // 5-20 snapshots (2.5-10 sec): BRIGHT GREEN solid = established
                // 20+ snapshots (10+ sec): WHITE thick + strong glow = battle-tested
                let lineWidth, dashPattern, glowAlpha, borderColor;
                if (count < HeatmapSettings.persistMid) {
                    lineWidth = 1.5;
                    dashPattern = [3, 3];
                    glowAlpha = 0;
                    borderColor = '255, 200, 50'; // gold = untested
                } else if (count < HeatmapSettings.persistHigh) {
                    lineWidth = 2;
                    dashPattern = [];
                    glowAlpha = 0.15;
                    borderColor = '0, 255, 120'; // green = established
                } else {
                    lineWidth = 2.5;
                    dashPattern = [];
                    glowAlpha = 0.5;
                    borderColor = '255, 255, 255'; // white = battle-tested
                }

                // Draw persistence border
                const borderAlpha = Math.min(0.5 + count * 0.03, 1.0);
                ctx.strokeStyle = `rgba(${borderColor}, ${borderAlpha.toFixed(2)})`;
                ctx.lineWidth = lineWidth;
                ctx.setLineDash(dashPattern);
                ctx.strokeRect(lastColX + 0.5, y - rowH / 2 + GAP + 0.5, COL_W - GAP - 1, rowH - GAP * 2 - 1);
                ctx.setLineDash([]);

                // Battle-tested glow (strong white halo)
                if (glowAlpha > 0) {
                    ctx.shadowColor = `rgba(${borderColor}, ${glowAlpha})`;
                    ctx.shadowBlur = 6;
                    ctx.strokeRect(lastColX + 0.5, y - rowH / 2 + GAP + 0.5, COL_W - GAP - 1, rowH - GAP * 2 - 1);
                    ctx.shadowBlur = 0;
                }
            }
        }

        // ── Render velocity pulses on the LAST column ──
        if (HeatmapSettings.velocity) {
            const lastColX = heatmapLeft + (numCols - 1) * COL_W;
            for (const [priceStr, flash] of DOM2D._velocityFlash) {
                const price = parseFloat(priceStr);
                if (isNaN(price) || price < visMin || price > visMax) continue;
                const bucketPrice = Math.floor(price / bucketSize) * bucketSize;
                const y = _priceToY(bucketPrice + bucketSize / 2);
                if (y === null) continue;

                // Pulse fades with age (0→6 frames)
                const fadeAlpha = Math.max(0, 0.6 - flash.age * 0.1);
                if (fadeAlpha <= 0) continue;

                if (flash.delta > 0) {
                    // Depth ADDED → cyan pulse (someone loading up)
                    ctx.fillStyle = `rgba(0, 220, 255, ${fadeAlpha.toFixed(2)})`;
                } else {
                    // Depth REMOVED → magenta pulse (someone pulling)
                    ctx.fillStyle = `rgba(255, 50, 180, ${fadeAlpha.toFixed(2)})`;
                }
                ctx.fillRect(lastColX, y - rowH / 2 + GAP, COL_W - GAP, rowH - GAP * 2);
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // LAYER 1c: OTR + EWMA + Book Asymmetry (institutional-grade signals)
    // ═══════════════════════════════════════════════════════════════════════

    if (latestSnap) {
        // ── OTR: Accumulate fills from ALL displayed snapshots ──
        // Trade fills tell us which levels are ACTUALLY getting hit
        // CRITICAL: clear before re-accumulating to prevent multiplicative inflation
        DOM2D._fillAccum.clear();
        for (let col = 0; col < numCols; col++) {
            const snap = displaySnaps[col];
            if (snap.trades && snap.trades.length) {
                for (const t of snap.trades) {
                    const pKey = String(Math.round(t.p * 4) / 4); // round to 0.25 tick
                    DOM2D._fillAccum.set(pKey, (DOM2D._fillAccum.get(pKey) || 0) + (t.v || 1));
                }
            }
        }

        // Compute OTR per price level on latest snapshot
        DOM2D._otrScores.clear();
        const allPricesForOTR = new Set([
            ...Object.keys(latestSnap.bids),
            ...Object.keys(latestSnap.asks),
        ]);
        for (const priceStr of allPricesForOTR) {
            const resting = Math.max(latestSnap.bids[priceStr] || 0, latestSnap.asks[priceStr] || 0);
            if (resting <= 0) continue;
            const fills = DOM2D._fillAccum.get(priceStr) || 0;
            // OTR = resting / (fills + 1). +1 avoids division by zero
            // High OTR = lots resting, no fills = decoration/spoof
            // Low OTR = resting matches fills = real orders getting hit
            const otr = resting / (fills + 1);
            DOM2D._otrScores.set(priceStr, otr);
        }

        // ── EWMA: Update adaptive normalization ──
        // Exponential moving average of book depth for smooth baseline
        const allSizesNow = [];
        for (const s of Object.values(latestSnap.bids)) if (s > 0) allSizesNow.push(s);
        for (const s of Object.values(latestSnap.asks)) if (s > 0) allSizesNow.push(s);

        if (allSizesNow.length > 0) {
            const snapAvg = allSizesNow.reduce((a, b) => a + b, 0) / allSizesNow.length;
            const snapVar = allSizesNow.reduce((acc, s) => acc + (s - snapAvg) ** 2, 0) / allSizesNow.length;

            if (!DOM2D._ewmaInitialized) {
                // First snapshot: seed EWMA with snapshot values
                DOM2D._ewmaMean = snapAvg;
                DOM2D._ewmaVar = snapVar;
                DOM2D._ewmaInitialized = true;
            } else {
                // Exponential decay update
                const a = HeatmapSettings.ewmaAlpha / 100;
                DOM2D._ewmaMean = a * snapAvg + (1 - a) * DOM2D._ewmaMean;
                DOM2D._ewmaVar = a * snapVar + (1 - a) * DOM2D._ewmaVar;
            }
            DOM2D._ewmaStdDev = Math.max(Math.sqrt(DOM2D._ewmaVar), 1);
        }

        // ── Book Asymmetry: total bid depth vs total ask depth ──
        let totalBidDepth = 0, totalAskDepth = 0;
        for (const s of Object.values(latestSnap.bids)) totalBidDepth += (s > 0 ? s : 0);
        for (const s of Object.values(latestSnap.asks)) totalAskDepth += (s > 0 ? s : 0);
        const totalDepth = totalBidDepth + totalAskDepth;
        if (totalDepth > 0) {
            DOM2D._bookAsymmetry = totalBidDepth / totalDepth;
        }
        DOM2D._asymmetryHistory.push(DOM2D._bookAsymmetry);
        if (DOM2D._asymmetryHistory.length > 60) DOM2D._asymmetryHistory.shift();

        // ── Render OTR indicators on rightmost column ──
        const lastColXotr = heatmapLeft + (numCols - 1) * COL_W;
        for (const [priceStr, otr] of DOM2D._otrScores) {
            const price = parseFloat(priceStr);
            if (isNaN(price) || price < visMin || price > visMax) continue;
            // Only show OTR indicator for above-mean levels (significant depth)
            const resting = Math.max(latestSnap.bids[priceStr] || 0, latestSnap.asks[priceStr] || 0);
            if (resting <= DOM2D._ewmaMean) continue;

            const bucketPrice = Math.floor(price / bucketSize) * bucketSize;
            const y = _priceToY(bucketPrice + bucketSize / 2);
            if (y === null) continue;

            // Small diamond marker at right edge of cell
            const dx = lastColXotr + COL_W - 4;
            const dy = y;
            const sz = 2.5;

            if (otr > HeatmapSettings.otrHigh) {
                // HIGH OTR = decoration/spoof (resting >> fills)
                ctx.fillStyle = 'rgba(255, 60, 60, 0.8)';  // red = fake
            } else if (otr > HeatmapSettings.otrLow) {
                // MEDIUM OTR = uncertain
                ctx.fillStyle = 'rgba(255, 200, 50, 0.6)';  // yellow = caution
            } else {
                // LOW OTR = real (fills match resting)
                ctx.fillStyle = 'rgba(0, 255, 120, 0.8)';  // green = real
            }
            ctx.beginPath();
            ctx.moveTo(dx, dy - sz);
            ctx.lineTo(dx + sz, dy);
            ctx.lineTo(dx, dy + sz);
            ctx.lineTo(dx - sz, dy);
            ctx.closePath();
            ctx.fill();
        }

        // ── Render Book Asymmetry indicator (top-right of heatmap) ──
        const asymPct = (DOM2D._bookAsymmetry * 100).toFixed(0);
        const asymLabel = DOM2D._bookAsymmetry > 0.5
            ? `BID ${asymPct}%`
            : `ASK ${(100 - parseInt(asymPct))}%`;
        const asymColor = DOM2D._bookAsymmetry > 0.55
            ? 'rgba(0, 255, 120, 0.7)'    // bid-heavy = green
            : DOM2D._bookAsymmetry < 0.45
                ? 'rgba(255, 60, 60, 0.7)' // ask-heavy = red
                : 'rgba(160, 170, 190, 0.5)'; // balanced = gray

        ctx.font = '9px "JetBrains Mono", monospace';
        ctx.fillStyle = asymColor;
        ctx.textAlign = 'right';
        ctx.textBaseline = 'top';
        ctx.fillText(asymLabel, heatmapLeft + numCols * COL_W - 4, 4);

        // ── Render EWMA σ bands as reference lines ──
        // Draw μ ± 1.5σ as very faint horizontal guide
        ctx.font = '7px "JetBrains Mono", monospace';
        ctx.fillStyle = 'rgba(100, 110, 130, 0.4)';
        ctx.textAlign = 'left';
        ctx.fillText(`μ:${DOM2D._ewmaMean.toFixed(0)} σ:${DOM2D._ewmaStdDev.toFixed(0)}`, heatmapLeft + 3, 4);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // PHASE 1b: BBO IMBALANCE BAR
    // ═══════════════════════════════════════════════════════════════════════
    // Real-time bar showing best bid vs best ask size ratio.
    // The single most predictive short-term signal for market makers.
    if (HeatmapSettings.bboBar && latestSnap) {
        // Use pre-computed sorted entries from _prepSnapshot (zero alloc)
        const bidEntries = latestSnap._bidEntries || [];
        const askEntries = latestSnap._askEntries || [];

        // Sum top 3 levels for a more stable signal
        const bidSize = bidEntries.slice(0, 3).reduce((sum, e) => sum + e[1], 0);
        const askSize = askEntries.slice(0, 3).reduce((sum, e) => sum + e[1], 0);
        const total = bidSize + askSize;

        if (total > 0) {
            const ratio = bidSize / total; // 0-1: 0=all ask, 0.5=balanced, 1=all bid

            // Track history
            DOM2D._bboHistory.push({ ts: Date.now(), bidSize, askSize, ratio });
            if (DOM2D._bboHistory.length > 120) DOM2D._bboHistory.shift();

            // Draw bar at top of heatmap area
            const barY = 16;
            const barW = Math.min(numCols * COL_W, 180);
            const barH = 6;
            const barX = heatmapLeft + numCols * COL_W - barW;

            // Background
            ctx.fillStyle = 'rgba(20, 25, 35, 0.7)';
            ctx.fillRect(barX - 1, barY - 1, barW + 2, barH + 2);

            // Bid side (green, left)
            const bidW = barW * ratio;
            ctx.fillStyle = ratio > 0.55 ? 'rgba(31, 209, 122, 0.85)' : 'rgba(31, 209, 122, 0.5)';
            ctx.fillRect(barX, barY, bidW, barH);

            // Ask side (red, right)
            ctx.fillStyle = ratio < 0.45 ? 'rgba(224, 48, 96, 0.85)' : 'rgba(224, 48, 96, 0.5)';
            ctx.fillRect(barX + bidW, barY, barW - bidW, barH);

            // Center line
            ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(barX + barW / 2, barY);
            ctx.lineTo(barX + barW / 2, barY + barH);
            ctx.stroke();

            // Label
            ctx.font = '8px "JetBrains Mono", monospace';
            ctx.textBaseline = 'top';
            const pct = (ratio * 100).toFixed(0);
            if (ratio > 0.55) {
                ctx.fillStyle = 'rgba(31, 209, 122, 0.9)';
                ctx.textAlign = 'left';
                ctx.fillText(`B${bidSize} (${pct}%)`, barX, barY + barH + 2);
            } else if (ratio < 0.45) {
                ctx.fillStyle = 'rgba(224, 48, 96, 0.9)';
                ctx.textAlign = 'right';
                ctx.fillText(`A${askSize} (${(100 - pct)}%)`, barX + barW, barY + barH + 2);
            } else {
                ctx.fillStyle = 'rgba(160, 170, 190, 0.6)';
                ctx.textAlign = 'center';
                ctx.fillText(`${bidSize}|${askSize}`, barX + barW / 2, barY + barH + 2);
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // PHASE 1c: CLUSTERED TRADE TAPE
    // ═══════════════════════════════════════════════════════════════════════
    // Groups rapid consecutive trades at the same price into aggregated blocks.
    // Shows intent: a cluster of 15 × 1-lot fills matters more than scattered noise.
    if (HeatmapSettings.clusterTape && latestSnap && latestSnap.trades && latestSnap.trades.length > 0) {
        // Cluster trades from latest snapshot by price bucket
        const clusters = {};
        for (const t of latestSnap.trades) {
            const price = t.p;
            if (!price || price < visMin || price > visMax) continue;
            const bucket = Math.floor(price / bucketSize) * bucketSize;
            const key = `${bucket}_${t.s || 'u'}`;
            if (!clusters[key]) {
                clusters[key] = { bucket, side: t.s, totalVol: 0, count: 0, maxSingle: 0 };
            }
            clusters[key].totalVol += (t.v || 1);
            clusters[key].count++;
            clusters[key].maxSingle = Math.max(clusters[key].maxSingle, t.v || 1);
        }

        // Only show clusters with ≥2 trades (filter noise)
        const lastColXtape = heatmapLeft + (numCols - 1) * COL_W;
        for (const [, cl] of Object.entries(clusters)) {
            if (cl.count < 2) continue;
            const y = _priceToY(cl.bucket + bucketSize / 2);
            if (y === null || y < 0 || y > cssH) continue;

            // Size proportional to volume (sqrt scaling)
            const volNorm = Math.min(Math.sqrt(cl.totalVol / 10), 1.0);
            const blockW = 6 + volNorm * 14;
            const blockH = Math.max(rowH - 2, 5);

            const isBuy = cl.side === 'b';
            const rgb = isBuy ? '31, 209, 122' : '224, 48, 96';
            const bgAlpha = 0.3 + volNorm * 0.4;

            // Draw block to the LEFT of the last heatmap column
            const bx = lastColXtape - blockW - 2;
            ctx.fillStyle = `rgba(${rgb}, ${bgAlpha.toFixed(2)})`;
            ctx.fillRect(bx, y - blockH / 2, blockW, blockH);

            // Border
            ctx.strokeStyle = `rgba(${rgb}, 0.7)`;
            ctx.lineWidth = 1;
            ctx.strokeRect(bx, y - blockH / 2, blockW, blockH);

            // Volume label inside block
            ctx.font = '7px "JetBrains Mono", monospace';
            ctx.fillStyle = `rgba(255, 255, 255, 0.9)`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(`${cl.count}×${cl.totalVol}`, bx + blockW / 2, y);
        }
    }

    // L2 REMOVED — heuristic thresholds, not proven math

    // ── LAYER 3: Mid-price trail (exact arithmetic mid) ──
    // Pre-compute per-snapshot bid/ask prices for reuse by micro-price and spread
    // ── Use pre-computed BBO/mid/micro/spread from _prepSnapshot ──
    // Zero per-frame allocation: no Object.entries, sort, or Math.exp calls here.
    const _snapMeta = new Array(numCols);
    for (let col = 0; col < numCols; col++) {
        const snap = displaySnaps[col];
        _snapMeta[col] = {
            mid: snap._midPrice || midPrice,
            micro: snap._micro || snap._midPrice || midPrice,
            spread: snap._spread || 0,
            bestBid: snap._bestBid || null,
            bestAsk: snap._bestAsk || null,
            bidEntries: snap._bidEntries || [],
            askEntries: snap._askEntries || [],
        };
    }

    // Draw mid-price trail (YELLOW dashed — smooth Bézier)
    if (HeatmapSettings.midprice) {
    const _mpRgb = _hexToRgb(HeatmapSettings.midpriceColor);
    ctx.strokeStyle = `rgba(${_mpRgb.r}, ${_mpRgb.g}, ${_mpRgb.b}, 0.80)`;
    ctx.lineWidth = HeatmapSettings.midpriceWidth;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    const midPts = [];
    for (let col = 0; col < numCols; col++) {
        const my = _priceToY(_snapMeta[col].mid);
        if (my === null) continue;
        midPts.push({ x: heatmapLeft + col * COL_W + COL_W / 2, y: my });
    }
    // Smooth Bézier path
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

    // ── LAYER 3b: Multi-Level Weighted Micro-Price Line (smooth Bézier + glow) ──
    if (HeatmapSettings.microprice) {
    const _mcRgb = _hexToRgb(HeatmapSettings.micropriceColor);
    // Glow layer
    ctx.shadowColor = `rgba(${_mcRgb.r}, ${_mcRgb.g}, ${_mcRgb.b}, 0.35)`;
    ctx.shadowBlur = 6;
    ctx.strokeStyle = `rgba(${_mcRgb.r}, ${_mcRgb.g}, ${_mcRgb.b}, 0.90)`;
    ctx.lineWidth = HeatmapSettings.micropriceWidth;
    ctx.beginPath();
    const microPts = [];
    for (let col = 0; col < numCols; col++) {
        const my = _priceToY(_snapMeta[col].micro);
        if (my === null) continue;
        microPts.push({ x: heatmapLeft + col * COL_W + COL_W / 2, y: my });
    }
    // Smooth Bézier path
    if (microPts.length > 1) {
        ctx.moveTo(microPts[0].x, microPts[0].y);
        for (let i = 1; i < microPts.length; i++) {
            const cpx = (microPts[i - 1].x + microPts[i].x) / 2;
            ctx.quadraticCurveTo(microPts[i - 1].x, microPts[i - 1].y, cpx, (microPts[i - 1].y + microPts[i].y) / 2);
        }
        ctx.lineTo(microPts[microPts.length - 1].x, microPts[microPts.length - 1].y);
        ctx.stroke();
    }
    ctx.shadowBlur = 0;
    }

    // ── LAYER 5: Aggressive Fills (Trades-on-Heatmap) ──
    // Renders aggressive trade fills as circles on the 2D heatmap.
    // Buy fills (lifted ask) = bright green circles
    // Sell fills (hit bid) = bright red circles
    // Size scales with trade volume (sqrt scaling for visual balance)
    const _buyRgb = _hexToRgb(HeatmapSettings.buyColor);
    const _sellRgb = _hexToRgb(HeatmapSettings.sellColor);
    const BUY_FILL_COLOR = `${_buyRgb.r}, ${_buyRgb.g}, ${_buyRgb.b}`;
    const SELL_FILL_COLOR = `${_sellRgb.r}, ${_sellRgb.g}, ${_sellRgb.b}`;
    const NEUTRAL_FILL_COLOR = '180, 180, 180'; // gray for unknown side
    const MIN_CIRCLE_R = 1.5;
    const MAX_CIRCLE_R = HeatmapSettings.tradesSize;
    if (HeatmapSettings.trades) {

    // Collect all trade volumes to compute normalization
    let allTradeVols = [];
    for (let col = 0; col < numCols; col++) {
        const snap = displaySnaps[col];
        if (snap.trades && snap.trades.length) {
            for (const t of snap.trades) {
                if (t.v > 0) allTradeVols.push(t.v);
            }
        }
    }
    // P90 of trade volumes = max radius reference
    let tradeVolRef = 5;
    if (allTradeVols.length > 0) {
        allTradeVols.sort((a, b) => a - b);
        tradeVolRef = Math.max(
            allTradeVols[Math.min(Math.floor(0.90 * allTradeVols.length), allTradeVols.length - 1)],
            1
        );
    }

    for (let col = 0; col < numCols; col++) {
        const snap = displaySnaps[col];
        if (!snap.trades || !snap.trades.length) continue;
        const x = heatmapLeft + col * COL_W + COL_W / 2;

        // Aggregate trades by price to avoid overlapping circles
        const byPrice = {};
        for (const t of snap.trades) {
            const price = t.p;
            if (!price || price < visMin || price > visMax) continue;
            const key = Math.floor(price / bucketSize) * bucketSize;
            if (!byPrice[key]) byPrice[key] = { buyVol: 0, sellVol: 0 };
            if (t.s === 'b') byPrice[key].buyVol += (t.v || 1);
            else if (t.s === 's') byPrice[key].sellVol += (t.v || 1);
            else { byPrice[key].buyVol += (t.v || 1) * 0.5; byPrice[key].sellVol += (t.v || 1) * 0.5; }
        }

        for (const [bucketStr, agg] of Object.entries(byPrice)) {
            const bucketPrice = parseFloat(bucketStr);
            const y = _priceToY(bucketPrice + bucketSize / 2);
            if (y === null || y < 0 || y > cssH) continue;

            // Draw buy circle (filled green)
            if (agg.buyVol > 0) {
                const normB = Math.min(Math.sqrt(agg.buyVol / tradeVolRef), 1.0);
                const rB = MIN_CIRCLE_R + normB * (MAX_CIRCLE_R - MIN_CIRCLE_R);
                const alphaB = 0.5 + normB * 0.4;
                ctx.beginPath();
                ctx.arc(x, y - rB * 0.3, rB, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(${BUY_FILL_COLOR},${alphaB.toFixed(3)})`;
                ctx.fill();
                // Bright edge
                if (rB > 3) {
                    ctx.strokeStyle = `rgba(${BUY_FILL_COLOR},0.8)`;
                    ctx.lineWidth = 0.5;
                    ctx.stroke();
                }
            }

            // Draw sell circle (filled red)
            if (agg.sellVol > 0) {
                const normS = Math.min(Math.sqrt(agg.sellVol / tradeVolRef), 1.0);
                const rS = MIN_CIRCLE_R + normS * (MAX_CIRCLE_R - MIN_CIRCLE_R);
                const alphaS = 0.5 + normS * 0.4;
                ctx.beginPath();
                ctx.arc(x, y + rS * 0.3, rS, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(${SELL_FILL_COLOR},${alphaS.toFixed(3)})`;
                ctx.fill();
                if (rS > 3) {
                    ctx.strokeStyle = `rgba(${SELL_FILL_COLOR},0.8)`;
                    ctx.lineWidth = 0.5;
                    ctx.stroke();
                }
            }
        }
    }
    } // end trades guard

    // ── LAYER 6: Cumulative Delta Line ──
    // Running sum of (buy_volume - sell_volume) from actual trade fills.
    // Plotted as a filled area. Divergence from price = key MM signal.
    if (HeatmapSettings.delta) {
    let cumDelta = 0;
    const deltaPoints = [];  // {col, delta, x, y}
    let deltaMin = 0, deltaMax = 0;

    for (let col = 0; col < numCols; col++) {
        const snap = displaySnaps[col];
        if (snap.trades && snap.trades.length) {
            for (const t of snap.trades) {
                if (t.s === 'b') cumDelta += (t.v || 1);
                else if (t.s === 's') cumDelta -= (t.v || 1);
            }
        }
        deltaPoints.push({ col, delta: cumDelta });
        if (cumDelta < deltaMin) deltaMin = cumDelta;
        if (cumDelta > deltaMax) deltaMax = cumDelta;
    }

    const deltaRange = Math.max(Math.abs(deltaMin), Math.abs(deltaMax), 1);
    // Map delta to Y: positive delta renders above center, negative below
    // Use a strip at the bottom-left of the heatmap area
    const DELTA_STRIP_H = HeatmapSettings.deltaHeight;  // user-configurable
    const deltaStripTop = cssH - 28 - DELTA_STRIP_H;  // above time labels
    const deltaStripMid = deltaStripTop + DELTA_STRIP_H / 2;

    // Background for delta strip
    ctx.fillStyle = 'rgba(4, 6, 14, 0.6)';
    ctx.fillRect(heatmapLeft, deltaStripTop, numCols * COL_W, DELTA_STRIP_H);

    // Zero line
    ctx.strokeStyle = 'rgba(100, 110, 130, 0.3)';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.moveTo(heatmapLeft, deltaStripMid);
    ctx.lineTo(heatmapLeft + numCols * COL_W, deltaStripMid);
    ctx.stroke();

    // Draw delta as filled area from zero line
    ctx.beginPath();
    let deltaPathStarted = false;
    for (const pt of deltaPoints) {
        const px = heatmapLeft + pt.col * COL_W + COL_W / 2;
        const norm = pt.delta / deltaRange;  // -1 to +1
        const py = deltaStripMid - norm * (DELTA_STRIP_H / 2 - 2);
        if (!deltaPathStarted) { ctx.moveTo(px, deltaStripMid); ctx.lineTo(px, py); deltaPathStarted = true; }
        else ctx.lineTo(px, py);
    }
    // Close back to zero line
    if (deltaPathStarted && deltaPoints.length) {
        const lastX = heatmapLeft + deltaPoints[deltaPoints.length - 1].col * COL_W + COL_W / 2;
        ctx.lineTo(lastX, deltaStripMid);
        ctx.closePath();

        // Fill green if net positive, red if net negative
        const finalDelta = deltaPoints[deltaPoints.length - 1].delta;
        if (finalDelta >= 0) {
            ctx.fillStyle = 'rgba(0, 200, 100, 0.25)';
            ctx.strokeStyle = 'rgba(0, 255, 120, 0.6)';
        } else {
            ctx.fillStyle = 'rgba(200, 40, 40, 0.25)';
            ctx.strokeStyle = 'rgba(255, 50, 70, 0.6)';
        }
        ctx.fill();
        ctx.lineWidth = 1;
        ctx.stroke();
    }

    // Delta label
    ctx.font = '7px "JetBrains Mono", monospace';
    ctx.fillStyle = 'rgba(160, 170, 190, 0.6)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText(`Δ ${cumDelta >= 0 ? '+' : ''}${cumDelta}`, heatmapLeft + 3, deltaStripTop + 2);
    } // end delta guard

    // ── LAYER 7: Spread Dynamics Line ──
    // Plots exact bid-ask spread per snapshot. Widening = volatility incoming.
    if (HeatmapSettings.spread) {
    const SPREAD_STRIP_H = HeatmapSettings.spreadHeight;  // user-configurable
    const spreadStripTop = cssH - 28 - (HeatmapSettings.delta ? HeatmapSettings.deltaHeight : 0) - SPREAD_STRIP_H - 2;

    // Find spread range for normalization
    let spreadMin = Infinity, spreadMax = 0;
    for (const meta of _snapMeta) {
        if (meta.spread > 0) {
            spreadMin = Math.min(spreadMin, meta.spread);
            spreadMax = Math.max(spreadMax, meta.spread);
        }
    }
    if (spreadMin === Infinity) spreadMin = 0;
    const spreadRange = Math.max(spreadMax - spreadMin, 0.25); // min range to avoid division by 0

    // Background
    ctx.fillStyle = 'rgba(4, 6, 14, 0.4)';
    ctx.fillRect(heatmapLeft, spreadStripTop, numCols * COL_W, SPREAD_STRIP_H);

    // Draw spread line
    ctx.beginPath();
    let spreadStarted = false;
    for (let col = 0; col < numCols; col++) {
        const meta = _snapMeta[col];
        if (meta.spread <= 0) continue;
        const px = heatmapLeft + col * COL_W + COL_W / 2;
        // Normalized: 0 = tightest, 1 = widest
        const norm = (meta.spread - spreadMin) / spreadRange;
        const py = spreadStripTop + SPREAD_STRIP_H - 2 - norm * (SPREAD_STRIP_H - 4);
        if (!spreadStarted) { ctx.moveTo(px, py); spreadStarted = true; }
        else ctx.lineTo(px, py);
    }
    if (spreadStarted) {
        // Color by current spread: tight=white, wide=yellow, very wide=red
        const lastSpread = _snapMeta[_snapMeta.length - 1].spread;
        const lastNorm = (lastSpread - spreadMin) / spreadRange;
        if (lastNorm > 0.7) {
            ctx.strokeStyle = 'rgba(255, 60, 60, 0.7)';   // wide = red (danger)
        } else if (lastNorm > 0.4) {
            ctx.strokeStyle = 'rgba(255, 200, 50, 0.7)';  // medium = yellow
        } else {
            ctx.strokeStyle = 'rgba(200, 210, 230, 0.5)'; // tight = white (safe)
        }
        ctx.lineWidth = 1;
        ctx.stroke();
    }

    // Spread label
    ctx.font = '7px "JetBrains Mono", monospace';
    ctx.fillStyle = 'rgba(160, 170, 190, 0.6)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    const currSpread = _snapMeta.length ? _snapMeta[_snapMeta.length - 1].spread : 0;
    ctx.fillText(`SPD ${currSpread.toFixed(2)}`, heatmapLeft + 3, spreadStripTop + 1);
    } // end spread guard
    ctx.font = '8px "JetBrains Mono", "SF Mono", monospace';
    ctx.fillStyle = 'rgba(120, 130, 155, 0.5)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const labelInterval = Math.max(1, Math.floor(40 / COL_W));
    for (let col = 0; col < numCols; col += labelInterval) {
        const snap = displaySnaps[col];
        const t = new Date(snap.ts * 1000);
        const mm = t.getMinutes().toString().padStart(2, '0');
        const ss = t.getSeconds().toString().padStart(2, '0');
        const label = `${t.getHours()}:${mm}:${ss}`;
        const lx = heatmapLeft + col * COL_W + COL_W / 2;
        ctx.fillText(label, lx, cssH - 12);
    }

    // ── Separator line ──
    ctx.strokeStyle = 'rgba(60, 80, 120, 0.25)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    const sepTop = Math.max(0, (topY || 0) - 10);
    const sepBot = Math.min(cssH, (botY || cssH) + 10);
    ctx.moveTo(heatmapRight, sepTop);
    ctx.lineTo(heatmapRight, sepBot);
    ctx.stroke();

    // ═══════════════════════════════════════════════════════════════════════
    // LAYER 9: CURRENT STATE COLUMN — MM-grade depth strip
    // ═══════════════════════════════════════════════════════════════════════
    // The rightmost column is drawn WIDER with clear bid/ask depth bars
    // and contract size labels for instant reads on current book state.
    if (latestSnap) {
        const CSC_W = DOM2D.CURRENT_COL_WIDTH;  // 12px wide
        const cscX = heatmapRight - CSC_W;

        // Dark backdrop for current-state column
        ctx.fillStyle = 'rgba(8, 10, 20, 0.7)';
        ctx.fillRect(cscX, Math.min(topY || 0, botY || 0) - 2, CSC_W, Math.abs((botY || cssH) - (topY || 0)) + 4);

        // Left border glow (subtle accent line)
        ctx.strokeStyle = 'rgba(80, 120, 200, 0.25)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(cscX, sepTop);
        ctx.lineTo(cscX, sepBot);
        ctx.stroke();

        // Find max depth for bar normalization
        let cscMaxDepth = 1;
        for (const s of Object.values(latestSnap.bids)) cscMaxDepth = Math.max(cscMaxDepth, s);
        for (const s of Object.values(latestSnap.asks)) cscMaxDepth = Math.max(cscMaxDepth, s);

        // Mean depth for label threshold
        const allCscSizes = [];
        for (const s of Object.values(latestSnap.bids)) if (s > 0) allCscSizes.push(s);
        for (const s of Object.values(latestSnap.asks)) if (s > 0) allCscSizes.push(s);
        const cscMean = allCscSizes.length > 0
            ? allCscSizes.reduce((a, b) => a + b, 0) / allCscSizes.length : 1;

        // Draw bid depth bars (green, growing LEFT from center)
        const cscCenter = cscX + CSC_W / 2;
        const halfW = CSC_W / 2 - 1;  // max bar half-width

        for (const [priceStr, size] of Object.entries(latestSnap.bids)) {
            const price = parseFloat(priceStr);
            if (isNaN(price) || price < visMin || price > visMax) continue;
            const bucketPrice = Math.floor(price / bucketSize) * bucketSize;
            const y = _priceToY(bucketPrice + bucketSize / 2);
            if (y === null || y < -rowH || y > cssH + rowH) continue;

            const norm = Math.min(size / cscMaxDepth, 1.0);
            const barW = norm * halfW;
            const intensity = 0.3 + norm * 0.6;

            // Bid bar (grows left from center)
            ctx.fillStyle = `rgba(0, 220, 140, ${intensity.toFixed(2)})`;
            ctx.fillRect(cscCenter - barW, y - rowH / 2 + 0.5, barW, rowH - 1);

            // Size label for levels above mean
            if (size >= cscMean * 1.5 && rowH >= 6) {
                ctx.font = '7px "JetBrains Mono", monospace';
                ctx.fillStyle = `rgba(200, 255, 220, ${Math.min(0.5 + norm, 0.95).toFixed(2)})`;
                ctx.textAlign = 'right';
                ctx.textBaseline = 'middle';
                ctx.fillText(size.toString(), cscCenter - barW - 2, y);
            }
        }

        // Draw ask depth bars (red/amber, growing RIGHT from center)
        for (const [priceStr, size] of Object.entries(latestSnap.asks)) {
            const price = parseFloat(priceStr);
            if (isNaN(price) || price < visMin || price > visMax) continue;
            const bucketPrice = Math.floor(price / bucketSize) * bucketSize;
            const y = _priceToY(bucketPrice + bucketSize / 2);
            if (y === null || y < -rowH || y > cssH + rowH) continue;

            const norm = Math.min(size / cscMaxDepth, 1.0);
            const barW = norm * halfW;
            const intensity = 0.3 + norm * 0.6;

            // Ask bar (grows right from center)
            ctx.fillStyle = `rgba(240, 80, 60, ${intensity.toFixed(2)})`;
            ctx.fillRect(cscCenter, y - rowH / 2 + 0.5, barW, rowH - 1);

            // Size label for levels above mean
            if (size >= cscMean * 1.5 && rowH >= 6) {
                ctx.font = '7px "JetBrains Mono", monospace';
                ctx.fillStyle = `rgba(255, 200, 190, ${Math.min(0.5 + norm, 0.95).toFixed(2)})`;
                ctx.textAlign = 'left';
                ctx.textBaseline = 'middle';
                ctx.fillText(size.toString(), cscCenter + barW + 2, y);
            }
        }

        // Center divider line in current-state column
        ctx.strokeStyle = 'rgba(120, 140, 180, 0.2)';
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(cscCenter, sepTop);
        ctx.lineTo(cscCenter, sepBot);
        ctx.stroke();
    }

    // ═══════════════════════════════════════════════════════════════════════
    // LAYER 9b: MID-PRICE GLOW LINE (horizontal accent across heatmap)
    // ═══════════════════════════════════════════════════════════════════════
    if (midPrice > 0) {
        const midY = _priceToY(midPrice);
        if (midY !== null && midY >= 0 && midY <= cssH) {
            // Subtle glow
            ctx.shadowColor = 'rgba(255, 220, 50, 0.35)';
            ctx.shadowBlur = 6;
            ctx.strokeStyle = 'rgba(255, 220, 50, 0.4)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(heatmapLeft, midY);
            ctx.lineTo(heatmapRight, midY);
            ctx.stroke();
            ctx.shadowBlur = 0;
        }
    }

    // ── LAYER 10: Volume Profile Sidebar ──
    // Aggregated bid+ask depth across all visible snapshots, shown as a slim histogram
    const VP_WIDTH = 22;
    const vpLeft = heatmapLeft - VP_WIDTH - 2;
    if (vpLeft > 0) {
        // Aggregate volumes by price bucket across all displayed snapshots
        const vpAgg = {};  // {priceStr: {bid: total, ask: total}}
        for (const snap of displaySnaps) {
            for (const [p, s] of Object.entries(snap.bids)) {
                const bp = (Math.floor(parseFloat(p) / bucketSize) * bucketSize).toFixed(2);
                if (!vpAgg[bp]) vpAgg[bp] = { bid: 0, ask: 0 };
                vpAgg[bp].bid += s;
            }
            for (const [p, s] of Object.entries(snap.asks)) {
                const bp = (Math.floor(parseFloat(p) / bucketSize) * bucketSize).toFixed(2);
                if (!vpAgg[bp]) vpAgg[bp] = { bid: 0, ask: 0 };
                vpAgg[bp].ask += s;
            }
        }

        // Find max for normalization
        let vpMax = 1;
        for (const v of Object.values(vpAgg)) {
            vpMax = Math.max(vpMax, v.bid + v.ask);
        }

        // Draw volume bars
        for (const [priceStr, vol] of Object.entries(vpAgg)) {
            const price = parseFloat(priceStr);
            if (price < visMin || price > visMax) continue;
            const y = _priceToY(price + bucketSize / 2);
            if (y === null || y < 0 || y > cssH) continue;

            const totalNorm = (vol.bid + vol.ask) / vpMax;
            const barW = totalNorm * VP_WIDTH;
            const bidFrac = vol.bid / (vol.bid + vol.ask);

            // Bid portion (teal, drawn from right)
            const bidW = barW * bidFrac;
            ctx.fillStyle = 'rgba(0, 180, 160, 0.45)';
            ctx.fillRect(vpLeft + VP_WIDTH - barW, y - rowH / 2, bidW, rowH - 0.5);

            // Ask portion (amber, stacked)
            const askW = barW * (1 - bidFrac);
            ctx.fillStyle = 'rgba(220, 120, 30, 0.45)';
            ctx.fillRect(vpLeft + VP_WIDTH - askW, y - rowH / 2, askW, rowH - 0.5);
        }

        // VP separator line
        ctx.strokeStyle = 'rgba(60, 80, 120, 0.3)';
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(vpLeft + VP_WIDTH, sepTop);
        ctx.lineTo(vpLeft + VP_WIDTH, sepBot);
        ctx.stroke();
    }

    // ── HOVER TOOLTIP: attach event listeners once ──
    if (!canvas._dom2dTooltipAttached) {
        canvas._dom2dTooltipAttached = true;
        canvas._dom2dHoverData = null;

        // Create tooltip div
        let tooltip = document.getElementById('dom2d-tooltip');
        if (!tooltip) {
            tooltip = document.createElement('div');
            tooltip.id = 'dom2d-tooltip';
            tooltip.style.cssText = `
                position: fixed; display: none; pointer-events: none;
                background: rgba(10, 14, 26, 0.92); border: 1px solid rgba(80, 120, 200, 0.4);
                border-radius: 4px; padding: 5px 8px; font: 10px "JetBrains Mono", monospace;
                color: rgba(200, 210, 230, 0.9); z-index: 9999; max-width: 200px;
                backdrop-filter: blur(6px); box-shadow: 0 2px 8px rgba(0,0,0,0.5);
            `;
            document.body.appendChild(tooltip);
        }

        canvas.addEventListener('mousemove', (e) => {
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;
            canvas._dom2dHoverData = { mx, my, clientX: e.clientX, clientY: e.clientY };
        });

        canvas.addEventListener('mouseleave', () => {
            canvas._dom2dHoverData = null;
            const tt = document.getElementById('dom2d-tooltip');
            if (tt) tt.style.display = 'none';
        });
    }

    // ── Render tooltip based on hover position ──
    const hoverData = canvas._dom2dHoverData;
    const tooltip = document.getElementById('dom2d-tooltip');
    if (hoverData && tooltip && hoverData.mx >= heatmapLeft && hoverData.mx <= heatmapRight) {
        const col = Math.floor((hoverData.mx - heatmapLeft) / COL_W);
        if (col >= 0 && col < numCols) {
            const snap = displaySnaps[col];
            // Find price at cursor Y
            // Reverse priceToY: iterate to find closest price
            let closestPrice = null, closestDist = Infinity;
            const allPrices = new Set([...Object.keys(snap.bids), ...Object.keys(snap.asks)]);
            for (const ps of allPrices) {
                const p = parseFloat(ps);
                const py = _priceToY(p + bucketSize / 2);
                if (py === null) continue;
                const d = Math.abs(py - hoverData.my);
                if (d < closestDist) { closestDist = d; closestPrice = ps; }
            }

            if (closestPrice && closestDist < rowH * 2) {
                const bidSize = snap.bids[closestPrice] || 0;
                const askSize = snap.asks[closestPrice] || 0;
                const tradeCount = (snap.trades || []).length;
                const absEntry = snap.absorption ? snap.absorption[closestPrice] : null;
                const t = new Date(snap.ts * 1000);
                const timeStr = `${t.getHours()}:${t.getMinutes().toString().padStart(2,'0')}:${t.getSeconds().toString().padStart(2,'0')}`;

                let html = `<div style="color:#8af">${parseFloat(closestPrice).toFixed(2)}</div>`;
                html += `<div>⏱ ${timeStr}</div>`;
                if (bidSize) html += `<div style="color:#0fb">BID: ${bidSize}</div>`;
                if (askSize) html += `<div style="color:#f84">ASK: ${askSize}</div>`;
                if (tradeCount) html += `<div style="color:#aaa">Fills: ${tradeCount}</div>`;
                if (absEntry) {
                    const score = absEntry.s || 0;
                    const waves = absEntry.w || 0;
                    if (score >= 2 && waves >= 2) {
                        html += `<div style="color:#88f">ABS ${score.toFixed(1)}x W${waves}</div>`;
                    } else if (score >= 1) {
                        html += `<div style="color:#da0">HOLD ${score.toFixed(1)}x</div>`;
                    } else if (score < 0.3 && (absEntry.sh || 0) >= 3) {
                        html += `<div style="color:#f44">CRACK -${absEntry.c || 0}</div>`;
                    }
                }

                tooltip.innerHTML = html;
                tooltip.style.display = 'block';

                // ── Viewport collision detection ──
                const ttRect = tooltip.getBoundingClientRect();
                const ttW = ttRect.width || 140;
                const ttH = ttRect.height || 80;
                const vpW = window.innerWidth;
                const vpH = window.innerHeight;
                const margin = 12;

                // Default: right of cursor
                let ttLeft = hoverData.clientX + margin;
                let ttTop = hoverData.clientY - 10;

                // Flip to left if clipping right edge
                if (ttLeft + ttW > vpW - 4) {
                    ttLeft = hoverData.clientX - ttW - margin;
                }
                // Push up if clipping bottom edge
                if (ttTop + ttH > vpH - 4) {
                    ttTop = vpH - ttH - 4;
                }
                // Clamp to top
                if (ttTop < 4) ttTop = 4;

                tooltip.style.left = ttLeft + 'px';
                tooltip.style.top = ttTop + 'px';
            } else {
                tooltip.style.display = 'none';
            }
        } else {
            tooltip.style.display = 'none';
        }
    } else if (tooltip) {
        tooltip.style.display = 'none';
    }

    ctx.restore();
}

// Preserve a reference to the real renderer before the throttle wrapper overwrites it
const _origRenderDomHeatmap2D = renderDomHeatmap2D;

// ═══════════════════════════════════════════════════════════════════════════
// EXPORTS & rAF THROTTLE
// ═══════════════════════════════════════════════════════════════════════════

// rAF gating: prevent render storms from overlapping poll + WebSocket triggers
let _dom2dRafPending = false;
let _dom2dRafArgs = null;

function _renderDomHeatmap2DThrottled(canvas, priceToY, midPrice) {
    _dom2dRafArgs = [canvas, priceToY, midPrice];
    if (_dom2dRafPending) return;
    _dom2dRafPending = true;
    requestAnimationFrame(() => {
        _dom2dRafPending = false;
        if (_dom2dRafArgs) {
            // Call the original drawing function, not the throttled wrapper
            _origRenderDomHeatmap2D(..._dom2dRafArgs);
            _dom2dRafArgs = null;
        }
    });
}

window.renderDomHeatmap2D = _renderDomHeatmap2DThrottled;
window.startDomHistory = startDomHistory;
window.stopDomHistory = stopDomHistory;
window.DOM2D = DOM2D;
window.HeatmapSettings = HeatmapSettings;
