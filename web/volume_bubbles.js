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
            const opening = panel.style.display === 'none';
            if (opening && window.closeAllSettingsPanels) window.closeAllSettingsPanels('hm-settings-panel');
            panel.style.display = opening ? 'block' : 'none';
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
 * Detect absorption via Shannon entropy.
 * H = -p·log2(p) - (1-p)·log2(1-p)
 * H >= 0.65 with sufficient volume = absorption (balanced two-sided flow).
 */
function _isAbsorption(buyVol, sellVol, minVol) {
    const total = buyVol + sellVol;
    if (total < minVol) return false;
    const p = total > 0 ? buyVol / total : 0.5;
    const q = 1 - p;
    if (p < 0.001 || q < 0.001) return false;
    const entropy = -(p * Math.log2(p) + q * Math.log2(q));
    return entropy >= 0.65;
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

        try { target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
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
            const glowBubbles = [];       // institutional prints (drawn first, behind)
            const buyBubbles = [];
            const sellBubbles = [];
            const absorbBubbles = [];     // heuristic absorption pattern
            const trueAbsorbBubbles = []; // footprint-confirmed true absorption (gold ring)
            const labelBubbles = [];      // text labels (drawn last, on top)

            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;

                const bp = bar.originalData.bp;
                const x = bar.x;

                for (const priceStr in bp) {
                    const entry = bp[priceStr];
                    const buyVol  = entry[0];
                    const sellVol = entry[1];
                    // entry[2] = fp_absorption_score (0.0–1.0), entry[3] = true_absorption (0|1)
                    // These are stamped by the backend when _detect_iceberg fires at this price.
                    const fpScore    = (entry.length >= 3 && entry[2] != null) ? entry[2] : 0;
                    const trueAbsorb = (entry.length >= 4 && entry[3] === 1);
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

                    const bubble = { x, y, radius, totalVol, buyVol, sellVol, opacity,
                                     isAbsorb, isInstitutional, trueAbsorb, fpScore };

                    // ── Sort into render layers ──
                    if ((isInstitutional || trueAbsorb) && !useDots) {
                        glowBubbles.push(bubble);
                    }

                    // True absorption gets its own gold-ring layer (highest conviction)
                    if (trueAbsorb && !useDots) {
                        trueAbsorbBubbles.push(bubble);
                    }

                    if (isAbsorb || trueAbsorb) {
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

            // ── Layer 5.5: TRUE ABSORPTION gold ring (footprint-confirmed) ──
            // This is the highest-conviction visual: the backend proved that
            // the bid held under massive sell pressure (negative delta, anchored bid).
            // Render a double gold ring with higher brightness to stand out.
            if (!useDots && trueAbsorbBubbles.length > 0) {
                for (const b of trueAbsorbBubbles) {
                    // Outer diffuse glow
                    const grad = ctx.createRadialGradient(b.x, b.y, b.radius, b.x, b.y, b.radius + 10);
                    grad.addColorStop(0, 'rgba(255, 215, 0, 0.5)');   // gold core
                    grad.addColorStop(1, 'rgba(255, 215, 0, 0.0)');   // fade out
                    ctx.strokeStyle = grad;
                    ctx.lineWidth = 3;
                    ctx.beginPath();
                    ctx.arc(b.x, b.y, b.radius + 4, 0, Math.PI * 2);
                    ctx.stroke();
                    // Inner sharp gold ring
                    ctx.strokeStyle = 'rgba(255, 215, 0, 0.95)';
                    ctx.lineWidth = 1.5;
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

                    // True absorption badge: gold "ABS★" (footprint confirmed)
                    if (b.trueAbsorb && b.radius >= 10) {
                        ctx.font = '7px "JetBrains Mono", monospace';
                        ctx.fillStyle = 'rgba(255, 215, 0, 0.95)';  // gold
                        ctx.fillText('ABS\u2605', b.x, b.y + b.radius + 8);
                    // Standard heuristic absorption badge
                    } else if (b.isAbsorb && b.radius >= 10) {
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

                    // NOTE: draw() is overridden by v2_integration.js (V3 engine).
                    // This cluster renderer code is DEAD and never executes at runtime.
                    // Kept here for reference only. shadowBlur removed to prevent GPU
                    // regression if this code path is ever reached unexpectedly.
                    ctx.setLineDash([]);

                    // Draw segments between consecutive hits with varying width
                    for (let h = 0; h < hits.length - 1; h++) {
                        const h1 = hits[h], h2 = hits[h + 1];
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

            }); } catch(e) { /* LWC not ready */ }  // close useMediaCoordinateSpace
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
window.VolumeBubbleSeries    = VolumeBubbleSeries;
window.VolumeBubbleRenderer  = VolumeBubbleRenderer;  // needed by v2_integration.js prototype patch
window.BUBBLE_CONFIG         = BUBBLE_CONFIG;














