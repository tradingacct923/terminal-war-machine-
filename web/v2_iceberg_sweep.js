/**
 * V2 ICEBERG + SWEEP VISUALIZERS + HAWKES FIX
 *
 * Module 9:  IcebergVisualizer — Surfaces backend iceberg detections on the tape
 * Module 10: SweepRenderer — Lightning bolt visualization for multi-level sweeps
 * Module 11: HawkesFix — Incremental state management (fixes the per-frame reset bug)
 *
 * Backend data paths:
 *   bar.originalData.icebergs = {priceStr: {clips, est_total, avg_clip, cv,
 *     confidence, side, size_rank, decay, absorbing, urgency, pressure,
 *     psi, dom_confirmed, timing, ...}}
 *   bar.originalData.sweeps = [{prices: [f,f,...], vol, levels, side, ts, notional}]
 *   bar.originalData.drifting_iceberg = {type:"drifting", fills, prices_hit,
 *     band_low, band_high, total_vol, avg_clip, cv, side, drift_confidence, ...}
 *   bar.originalData.wall_gone = [{price_str, side, duration, ...}]
 */
(function() {
'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// 9. ICEBERG VISUALIZER
// ═══════════════════════════════════════════════════════════════════════════
//
// Renders backend iceberg detections as pulsing diamond markers on the tape.
//
// Data source: bar.originalData.icebergs (attached to each candle by l2_worker.py)
// Each iceberg dict contains ~30 fields. We use:
//   - side: "b" (ask iceberg / seller wall) or "s" (bid iceberg / buyer wall)
//   - confidence: "high", "medium", "low"
//   - size_rank: "whale", "institutional", "professional", "retail"
//   - urgency: 0-1 composite score
//   - pressure: "wall_fresh", "wall_active", "wall_exhausted", "wall_breaking", etc.
//   - decay: "holding", "exhausting", "strengthening"
//   - est_total: estimated total visible volume
//   - psi: Dark Pool Absorption Coefficient (Ψ)
//
// Visual design:
//   - Diamond shape (rotated square) — distinct from circular bubbles
//   - Blue diamond: bid iceberg (buyer wall absorbing sellers) → LONG signal
//   - Pink diamond: ask iceberg (seller wall absorbing buyers) → SHORT signal
//   - Size scales with est_total
//   - Pulsing animation via ICE_PULSE_SPEED (2000ms sine cycle)
//   - Confidence → opacity: high=0.9, medium=0.6, low=0.3
//   - Urgency → glow intensity
//   - Decay state → border style: solid=holding, dashed=exhausting

const IcebergVisualizer = {

    /**
     * Render all iceberg detections across visible bars.
     * Call from inside draw() after bubble layers.
     *
     * @param {CanvasRenderingContext2D} ctx
     * @param {Object} d - data object {bars, visibleRange}
     * @param {Function} priceConverter - price → Y
     * @param {Object} config - BUBBLE_CONFIG
     */
    render(ctx, d, priceConverter, config) {
        const { from, to } = d.visibleRange;
        const now = performance.now();

        ctx.save();

        for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData) continue;
            const x = bar.x;

            // ── Static icebergs (single-level) ──
            const icebergs = bar.originalData.icebergs;
            if (icebergs) {
                for (const priceStr in icebergs) {
                    const ice = icebergs[priceStr];
                    // Quality gate: skip low-confidence and low-clip detections
                    if (ice.confidence === 'low') continue;
                    if ((ice.clips || 0) < 3) continue;
                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    this._drawIceberg(ctx, x, y, ice, config, now);
                }
            }

            // ── Drifting icebergs (multi-level band) ──
            const drift = bar.originalData.drifting_iceberg;
            if (drift && drift.drift_confidence === 'confirmed') {
                const midPrice = (drift.band_low + drift.band_high) / 2;
                const y = priceConverter(midPrice);
                if (y !== null && y !== undefined && !isNaN(y)) {
                    this._drawDriftingIceberg(ctx, x, y, drift, priceConverter, config, now);
                }
            }

            // ── Wall Gone markers ──
            const wallGone = bar.originalData.wall_gone;
            if (wallGone && Array.isArray(wallGone)) {
                for (const wg of wallGone) {
                    const price = parseFloat(wg.price_str || wg.price || 0);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;
                    this._drawWallGone(ctx, x, y, wg, config);
                }
            }
        }

        ctx.restore();
    },

    /**
     * Draw a single iceberg detection.
     */
    _drawIceberg(ctx, x, y, ice, config, now) {
        // Direction: side="s" → bid iceberg (buyers) → blue
        //            side="b" → ask iceberg (sellers) → pink
        const isBuyWall = ice.side === 's';
        const rgb = isBuyWall ? config.ICE_BUY_COLOR : config.ICE_SELL_COLOR;
        const [r, g, b] = rgb;

        // Size from est_total (clip count × avg_clip)
        const estVol = ice.est_total || (ice.clips * ice.avg_clip) || 10;
        const volScale = Math.min(Math.log(estVol + 1) / Math.log(500), 1.0);
        const halfSize = config.ICE_DOT_SIZE + volScale * (config.ICE_DIAMOND_SIZE - config.ICE_DOT_SIZE);

        // Confidence → opacity
        const confAlpha = ice.confidence === 'high' ? 0.92
            : ice.confidence === 'medium' ? 0.65 : 0.35;

        // Pulse animation (sine wave → 0.7 to 1.0)
        const pulsePhase = (now % config.ICE_PULSE_SPEED) / config.ICE_PULSE_SPEED;
        const pulse = 0.7 + 0.3 * Math.sin(pulsePhase * Math.PI * 2);
        const alpha = confAlpha * pulse;

        // ── Urgency glow ──
        if (ice.urgency > 0.5) {
            ctx.shadowColor = `rgba(${r}, ${g}, ${b}, ${ice.urgency * 0.6})`;
            ctx.shadowBlur = 4 + ice.urgency * 8;
        }

        // ── Diamond shape (rotated square) ──
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
        ctx.beginPath();
        ctx.moveTo(x, y - halfSize);         // top
        ctx.lineTo(x + halfSize, y);         // right
        ctx.lineTo(x, y + halfSize);         // bottom
        ctx.lineTo(x - halfSize, y);         // left
        ctx.closePath();
        ctx.fill();

        // ── Border: solid=holding, dashed=exhausting ──
        ctx.lineWidth = ice.size_rank === 'whale' ? 2.5 : 1.5;
        ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${Math.min(alpha + 0.2, 1.0)})`;

        if (ice.decay === 'exhausting') {
            ctx.setLineDash([3, 2]);
        } else if (ice.decay === 'strengthening') {
            ctx.lineWidth = 2.5;  // thicker = getting stronger
        }
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.shadowBlur = 0;

        // ── Ψ coefficient indicator (tiny bar inside diamond) ──
        // Ψ > 1.0 = buy absorption dominant, < 1.0 = sell dominant
        if (ice.psi !== undefined && halfSize >= 6) {
            const psiNorm = Math.min(Math.max(ice.psi, 0), 3) / 3; // normalize to 0-1
            const psiBarW = halfSize * 0.8;
            const psiBarH = 2;
            const psiBarX = x - psiBarW / 2;
            const psiBarY = y + halfSize * 0.3;

            // Background
            ctx.fillStyle = `rgba(0, 0, 0, 0.3)`;
            ctx.fillRect(psiBarX, psiBarY, psiBarW, psiBarH);

            // Ψ fill
            const psiFillColor = ice.psi > 1.0
                ? `rgba(100, 180, 255, 0.8)`   // buy-absorb dominant
                : `rgba(255, 140, 180, 0.8)`;  // sell-absorb dominant
            ctx.fillStyle = psiFillColor;
            ctx.fillRect(psiBarX, psiBarY, psiBarW * psiNorm, psiBarH);
        }

        // ── Label ──
        if (halfSize >= 8) {
            ctx.font = '7px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';

            // Pressure label
            const pressureLabel = this._pressureLabel(ice.pressure);
            if (pressureLabel) {
                ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.9)`;
                ctx.fillText(pressureLabel, x, y + halfSize + 3);
            }

            // Clip count + confidence badge
            const badge = `${ice.clips}× ${ice.confidence[0].toUpperCase()}`;
            ctx.fillStyle = 'rgba(255, 255, 255, 0.7)';
            ctx.textBaseline = 'bottom';
            ctx.fillText(badge, x, y - halfSize - 2);
        }
    },

    /**
     * Draw drifting iceberg (multi-level band).
     */
    _drawDriftingIceberg(ctx, x, y, drift, priceConverter, config, now) {
        const isBuyWall = drift.side === 's';
        const [r, g, b] = isBuyWall ? config.ICE_BUY_COLOR : config.ICE_SELL_COLOR;

        // Band visualization: vertical bracket from band_low to band_high
        const yLow = priceConverter(drift.band_low);
        const yHigh = priceConverter(drift.band_high);
        if (yLow === null || yHigh === null) return;

        const pulsePhase = (now % config.ICE_PULSE_SPEED) / config.ICE_PULSE_SPEED;
        const pulse = 0.6 + 0.4 * Math.sin(pulsePhase * Math.PI * 2);

        const confAlpha = drift.drift_confidence === 'confirmed' ? 0.85
            : drift.drift_confidence === 'likely' ? 0.55 : 0.30;
        const alpha = confAlpha * pulse;

        // Vertical band
        const bandW = 6;
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha * 0.3})`;
        ctx.fillRect(x - bandW / 2, Math.min(yLow, yHigh), bandW, Math.abs(yHigh - yLow));

        // Border
        ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 3]);  // always dashed for drift
        ctx.strokeRect(x - bandW / 2, Math.min(yLow, yHigh), bandW, Math.abs(yHigh - yLow));
        ctx.setLineDash([]);

        // Diamond at midpoint
        const midY = (yLow + yHigh) / 2;
        const halfSize = 5;
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
        ctx.beginPath();
        ctx.moveTo(x, midY - halfSize);
        ctx.lineTo(x + halfSize, midY);
        ctx.lineTo(x, midY + halfSize);
        ctx.lineTo(x - halfSize, midY);
        ctx.closePath();
        ctx.fill();

        // Label
        ctx.font = '7px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.9)`;
        ctx.fillText(
            `DRIFT ${drift.prices_hit}lvl ${drift.total_vol}Δ`,
            x, Math.max(yLow, yHigh) + 3
        );
    },

    /**
     * Draw wall-gone marker (X through the old iceberg position).
     */
    _drawWallGone(ctx, x, y, wg, config) {
        const size = 6;
        ctx.strokeStyle = 'rgba(255, 80, 80, 0.7)';
        ctx.lineWidth = 2;

        // X mark
        ctx.beginPath();
        ctx.moveTo(x - size, y - size);
        ctx.lineTo(x + size, y + size);
        ctx.moveTo(x + size, y - size);
        ctx.lineTo(x - size, y + size);
        ctx.stroke();

        // Label
        ctx.font = '6px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillStyle = 'rgba(255, 80, 80, 0.8)';
        ctx.fillText('GONE', x, y + size + 6);
    },

    /**
     * Convert pressure string to compact label.
     */
    _pressureLabel(pressure) {
        const map = {
            'wall_fresh': 'FRESH',
            'wall_active': 'ACTIVE',
            'wall_exhausted': 'SPENT',
            'wall_breaking': 'BREAK',
            'bullish_wall': 'BID↑',
            'bearish_wall': 'ASK↓',
        };
        // Handle low_edge_ prefix
        if (pressure && pressure.startsWith('low_edge_')) {
            return '⚠' + (map[pressure.slice(9)] || '');
        }
        return map[pressure] || null;
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 10. SWEEP RENDERER
// ═══════════════════════════════════════════════════════════════════════════
//
// Renders multi-level aggressive fills as lightning bolt markers.
//
// Data source: bar.originalData.sweeps = [{prices, vol, levels, side, ts, notional}]
//
// Backend detection criteria (from l2_worker.py):
//   - 3+ consecutive price levels hit within 200ms
//   - All same side
//   - Total volume >= 100 contracts (~$2M notional on NQ)
//
// Visual design:
//   - Lightning bolt connecting swept price levels
//   - Green bolt: buy sweep (lifting asks)
//   - Red bolt: sell sweep (hitting bids)
//   - Width scales with total volume
//   - Burst effect at highest/lowest swept price
//   - Badge: "⚡ 5lvl 150Δ $6M"

const SweepRenderer = {

    /**
     * Render all sweep events across visible bars.
     * Call from inside draw() after iceberg layer.
     *
     * @param {CanvasRenderingContext2D} ctx
     * @param {Object} d - data object
     * @param {Function} priceConverter
     * @param {Object} config - BUBBLE_CONFIG
     */
    render(ctx, d, priceConverter, config) {
        const { from, to } = d.visibleRange;

        ctx.save();

        for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData || !bar.originalData.sweeps) continue;
            const sweeps = bar.originalData.sweeps;
            const x = bar.x;

            for (const sweep of sweeps) {
                if (!sweep.prices || sweep.prices.length < 2) continue;
                this._drawSweep(ctx, x, sweep, priceConverter, config);
            }
        }

        ctx.restore();
    },

    /**
     * Draw a single sweep event as a lightning bolt.
     */
    _drawSweep(ctx, x, sweep, priceConverter, config) {
        const isBuy = sweep.side === 'b';
        const [r, g, b] = isBuy ? config.SWEEP_BUY_COLOR : config.SWEEP_SELL_COLOR;

        // Sort prices (ascending for sells sweeping down, descending for buys sweeping up)
        const sortedPrices = [...sweep.prices].sort((a, b) => isBuy ? a - b : b - a);

        // Map prices to Y coordinates
        const points = [];
        for (const price of sortedPrices) {
            const y = priceConverter(price);
            if (y === null || y === undefined || isNaN(y)) continue;
            points.push({ price, y });
        }
        if (points.length < 2) return;

        // Line width scales with volume (log scale)
        const volScale = Math.min(Math.log(sweep.vol + 1) / Math.log(500), 1.0);
        const lineWidth = config.SWEEP_LINE_WIDTH + volScale * 2;

        // ── Glow ──
        ctx.shadowColor = `rgba(${r}, ${g}, ${b}, 0.6)`;
        ctx.shadowBlur = config.SWEEP_GLOW_BLUR;

        // ── Lightning bolt path ──
        // Zigzag between price levels for the bolt effect
        ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, 0.85)`;
        ctx.lineWidth = lineWidth;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';

        ctx.beginPath();
        ctx.moveTo(x, points[0].y);

        for (let p = 1; p < points.length; p++) {
            const prevY = points[p - 1].y;
            const currY = points[p].y;
            const midY = (prevY + currY) / 2;

            // Deterministic zigzag offset — seeded from sweep.ts + segment index.
            // FIX (Issue 2): Was Math.random(), produced different path every frame
            // at 60fps = visible vibration. Now: same sweep always renders same bolt.
            //
            // Hash: mix timestamp bits with segment index using integer operations.
            // Not cryptographic — just needs stable, visually varied offsets per segment.
            const seed = Math.floor((sweep.ts * 1000 + p * 2654435761) % 65536);
            const deterministicRand = (seed % 100) / 100;  // 0.0 to 0.99, stable
            const zigzag = (p % 2 === 0 ? 1 : -1) * (3 + deterministicRand * 4);

            // Quadratic bezier for the bolt segments
            ctx.quadraticCurveTo(x + zigzag, midY, x, currY);
        }
        ctx.stroke();

        // ── Burst effect at the impact point (end of sweep) ──
        const impactPoint = points[points.length - 1];
        const burstR = config.SWEEP_BURST_SIZE + volScale * 4;

        // Radial burst gradient
        const burstGrad = ctx.createRadialGradient(
            x, impactPoint.y, 0, x, impactPoint.y, burstR
        );
        burstGrad.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.8)`);
        burstGrad.addColorStop(0.5, `rgba(${r}, ${g}, ${b}, 0.3)`);
        burstGrad.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);

        ctx.fillStyle = burstGrad;
        ctx.beginPath();
        ctx.arc(x, impactPoint.y, burstR, 0, Math.PI * 2);
        ctx.fill();

        // ── Entry point marker ──
        const entryPoint = points[0];
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.9)`;
        ctx.beginPath();
        ctx.arc(x, entryPoint.y, 3, 0, Math.PI * 2);
        ctx.fill();

        ctx.shadowBlur = 0;

        // ── Badge ──
        const badgeY = Math.min(points[0].y, points[points.length - 1].y) - 10;
        const notionalStr = sweep.notional >= 1000000
            ? `$${(sweep.notional / 1000000).toFixed(1)}M`
            : `$${(sweep.notional / 1000).toFixed(0)}K`;

        const badge = `⚡${sweep.levels}lvl ${sweep.vol}Δ ${notionalStr}`;

        ctx.font = '7px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'bottom';

        // Background pill
        const bm = ctx.measureText(badge);
        ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
        ctx.beginPath();
        ctx.roundRect(x - bm.width / 2 - 3, badgeY - 11, bm.width + 6, 13, 3);
        ctx.fill();

        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.95)`;
        ctx.fillText(badge, x, badgeY);
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 11. HAWKES INCREMENTAL STATE MANAGER
// ═══════════════════════════════════════════════════════════════════════════
//
// Fixes the per-frame reset bug in v2_integration.js.
//
// PROBLEM: v2_integration.js calls HawkesClusterDetector.reset() every frame.
// This destroys ALL temporal state — the self-exciting intensity λ, the
// calibrated parameters, the event histories. The whole point of the
// Hawkes process is that past events influence current intensity. Resetting
// per frame makes it equivalent to V1's naive hit-count.
//
// SOLUTION: Incremental state management.
// - Track which bars have been ingested (by bar index)
// - On each frame, only ingest NEW bars (bars not yet seen)
// - When visible range changes (scroll/zoom), handle:
//   a) New bars entering from the right → ingest them
//   b) Old bars leaving from the left → handled naturally by decay
//   c) Complete range change (big scroll) → full re-ingest
//
// This preserves the O(1) incremental property of the Hawkes process
// while handling the real-time scroll/zoom use case.

const HawkesStateManager = {
    _lastFrom: -1,
    _lastTo: -1,
    _ingestedBars: new Set(),  // bar indices already processed
    _latestBarSignature: '',   // fingerprint of latest bar's bp data

    /**
     * Incrementally update HawkesClusterDetector for the visible range.
     * Call this INSTEAD of reset() + full re-ingest.
     *
     * BUG FIX (v2.1): Removed forced re-ingest of latest bar every frame.
     * That was adding α×g(v) to λ 60 times/second = 60× inflation.
     * Now: latest bar is ingested once. If its data changes (forming bar
     * gets new trades), we evict it from _ingestedBars so the incremental
     * loop picks it up on the next frame — one ingest per data change.
     *
     * Tradeoff: Hawkes can't "undo" old excitation, so when the forming
     * bar grows, we accept slight over-counting of the early trades.
     * This is negligible vs the 60× inflation bug.
     *
     * @param {Object} d - data object with bars and visibleRange
     * @param {number} sigThreshold - current threshold from Kalman
     * @param {number} noiseFloor - minimum volume for Hawkes λ excitation
     */
    update(d, sigThreshold, noiseFloor) {
        const { from, to } = d.visibleRange;

        // ── Detect if visible range changed drastically (big scroll) ──
        const rangeShift = Math.abs(from - this._lastFrom) + Math.abs(to - this._lastTo);
        const rangeSize = to - from;

        if (rangeShift > rangeSize * 0.5 || this._ingestedBars.size === 0) {
            // Major range change or first run → full re-ingest
            HawkesClusterDetector.reset();
            this._ingestedBars.clear();
            this._latestBarSignature = '';

            // FIX (Issue 5 — Exhaustion sync): Clear ExhaustionDetector's
            // per-state _ingestedBars so bars get re-processed for exhaustion.
            // Without this, a big scroll resets Hawkes but ExhaustionDetector
            // still thinks it already processed these bar indices, causing
            // stale cumBuy/cumSell and missed divergence signals.
            if (typeof ExhaustionDetector !== 'undefined') {
                ExhaustionDetector.clearIngested();
            }

            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const barTs = bar.originalData.ts || 0;
                HawkesClusterDetector.ingestBar(i, bar.originalData.bp, sigThreshold, noiseFloor, barTs);
                this._ingestedBars.add(i);
            }
            // Capture latest bar signature
            this._latestBarSignature = this._bpSignature(d.bars[to - 1]);
        } else {
            // ── Check if latest (forming) bar's data changed ──
            const latestIdx = to - 1;
            const newSig = this._bpSignature(d.bars[latestIdx]);
            if (newSig !== this._latestBarSignature && this._ingestedBars.has(latestIdx)) {
                // Data changed → evict so incremental loop re-ingests it
                this._ingestedBars.delete(latestIdx);
                this._latestBarSignature = newSig;
            }

            // Incremental: only ingest bars not yet seen
            for (let i = from; i < to; i++) {
                if (this._ingestedBars.has(i)) continue;
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const barTs = bar.originalData.ts || 0;
                HawkesClusterDetector.ingestBar(i, bar.originalData.bp, sigThreshold, noiseFloor, barTs);
                this._ingestedBars.add(i);
            }

            // Prune ingested bars that are no longer in visible range
            for (const idx of this._ingestedBars) {
                if (idx < from - 10) {
                    this._ingestedBars.delete(idx);
                }
            }
        }

        this._lastFrom = from;
        this._lastTo = to;
    },

    /**
     * Compute a fast fingerprint of a bar's bubble profile.
     * Used to detect when the forming bar's data has changed.
     * We use total volume + key count — cheap and catches all real changes.
     */
    _bpSignature(bar) {
        if (!bar || !bar.originalData || !bar.originalData.bp) return '';
        const bp = bar.originalData.bp;
        let totalVol = 0, keyCount = 0;
        for (const key in bp) {
            totalVol += bp[key][0] + bp[key][1];
            keyCount++;
        }
        return `${keyCount}:${totalVol}`;
    },

    reset() {
        this._lastFrom = -1;
        this._lastTo = -1;
        this._ingestedBars.clear();
        this._latestBarSignature = '';
        HawkesClusterDetector.reset();
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 12. ABSORPTION INCREMENTAL STATE MANAGER
// ═══════════════════════════════════════════════════════════════════════════
//
// BUG FIX: AbsorptionAggregator.reset() was called every frame in
// v2_integration.js — same disease as the Hawkes per-frame reset.
// This destroyed the multi-bar temporal decay that the module was
// designed to accumulate. A wall absorbing across 5 bars was being
// reduced to single-bar detection — identical to V1.
//
// Same incremental pattern as HawkesStateManager:
// Track ingested bars, only ingest new ones, evict forming bar
// when its data changes.

const AbsorptionStateManager = {
    _lastFrom: -1,
    _lastTo: -1,
    _ingestedBars: new Set(),
    _latestBarSignature: '',

    /**
     * Incrementally update AbsorptionAggregator.
     * @param {Object} d - data object
     * @param {number} absorbMinVol - from Kalman thresholds
     */
    update(d, absorbMinVol) {
        const { from, to } = d.visibleRange;

        const rangeShift = Math.abs(from - this._lastFrom) + Math.abs(to - this._lastTo);
        const rangeSize = to - from;

        if (rangeShift > rangeSize * 0.5 || this._ingestedBars.size === 0) {
            AbsorptionAggregator.reset();
            this._ingestedBars.clear();
            this._latestBarSignature = '';

            // Sync ExhaustionDetector: same pattern as HawkesStateManager.
            // Both managers detect big-scroll independently — either one
            // must clear Exhaustion's ingested bars to prevent stale state.
            if (typeof ExhaustionDetector !== 'undefined') {
                ExhaustionDetector.clearIngested();
            }

            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                AbsorptionAggregator.ingestFromBP(bar.originalData.bp, i, absorbMinVol);
                this._ingestedBars.add(i);
            }
            this._latestBarSignature = this._bpSignature(d.bars[to - 1]);
        } else {
            // Check forming bar
            const latestIdx = to - 1;
            const newSig = this._bpSignature(d.bars[latestIdx]);
            if (newSig !== this._latestBarSignature && this._ingestedBars.has(latestIdx)) {
                this._ingestedBars.delete(latestIdx);
                this._latestBarSignature = newSig;
            }

            for (let i = from; i < to; i++) {
                if (this._ingestedBars.has(i)) continue;
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                AbsorptionAggregator.ingestFromBP(bar.originalData.bp, i, absorbMinVol);
                this._ingestedBars.add(i);
            }

            for (const idx of this._ingestedBars) {
                if (idx < from - 10) this._ingestedBars.delete(idx);
            }
        }

        this._lastFrom = from;
        this._lastTo = to;
    },

    _bpSignature(bar) {
        if (!bar || !bar.originalData || !bar.originalData.bp) return '';
        const bp = bar.originalData.bp;
        let totalVol = 0, keyCount = 0;
        for (const key in bp) {
            totalVol += bp[key][0] + bp[key][1];
            keyCount++;
        }
        return `${keyCount}:${totalVol}`;
    },

    reset() {
        this._lastFrom = -1;
        this._lastTo = -1;
        this._ingestedBars.clear();
        this._latestBarSignature = '';
        AbsorptionAggregator.reset();
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 13. SPOOF RENDERER
// ═══════════════════════════════════════════════════════════════════════════
//
// Renders backend spoof detections as pulsing threat markers.
//
// Data source: bar.originalData.spoofs = [{price, fake_size, side, count}, ...]
//
// Backend detection: large order appears in DOM, then vanishes within
// _SPOOF_MAX_LIFETIME (1.0s) without being filled. Pattern must repeat
// >= _SPOOF_MIN_OCCUR (3x) to confirm spoofing intent.
//
// Visual design:
//   - ⚠ badge at the spoof price level with threat count
//   - Oscillating ring = DOM manipulation is active
//   - Red ring: bid spoof (fake support below), Blue line-through: ask spoof
//   - Width scales with fake_size

const SpoofRenderer = {
    render(ctx, d, priceConverter, config) {
        const { from, to } = d.visibleRange;
        const now = performance.now();

        ctx.save();

        for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData || !bar.originalData.spoofs) continue;
            const spoofs = bar.originalData.spoofs;
            if (!Array.isArray(spoofs) || !spoofs.length) continue;
            const x = bar.x;

            for (const spoof of spoofs) {
                const price = parseFloat(spoof.price);
                if (isNaN(price)) continue;
                const y = priceConverter(price);
                if (y === null || y === undefined || isNaN(y)) continue;

                const isBidSpoof = spoof.side === 'bid'; // fake bid = wash support
                const r = isBidSpoof ? 220 : 100;
                const g = isBidSpoof ? 60  : 140;
                const b = isBidSpoof ? 60  : 255;

                // Threat intensity: count → glow + ring size
                const threat = Math.min(spoof.count / 10, 1.0); // normalize to 0-1
                const pulse  = 0.5 + 0.5 * Math.sin((now / 600) * Math.PI * 2); // slow oscillation

                // ── Oscillating threat ring ──
                ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${0.4 + threat * 0.4 * pulse})`;
                ctx.lineWidth = 1.5 + threat;
                ctx.setLineDash([3, 3]);
                ctx.beginPath();
                ctx.arc(x, y, 8 + threat * 6 * pulse, 0, Math.PI * 2);
                ctx.stroke();
                ctx.setLineDash([]);

                // ── Strike-through line (crossed-out fake size) ──
                const lineW = Math.min(Math.log(spoof.fake_size + 1) / Math.log(300), 1.0) * 20;
                ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${0.7 + threat * 0.2})`;
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(x - lineW, y);
                ctx.lineTo(x + lineW, y);
                ctx.stroke();

                // ── ⚠ Badge ──
                const label = `⚠ ×${spoof.count}`;
                ctx.font = '7px "JetBrains Mono", monospace';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'bottom';
                const bm = ctx.measureText(label);
                ctx.fillStyle = 'rgba(0, 0, 0, 0.75)';
                ctx.beginPath();
                ctx.roundRect(x - bm.width / 2 - 3, y - 18, bm.width + 6, 12, 2);
                ctx.fill();
                ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.95)`;
                ctx.fillText(label, x, y - 7);
            }
        }

        ctx.restore();
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 14. IGNITION RENDERER
// ═══════════════════════════════════════════════════════════════════════════
//
// Renders momentum ignition patterns and confirmed reversals.
//
// Data source: bar.originalData.ignition = [{direction, levels_swept,
//   reversed, ts, price_min, price_max}, ...]
//
// Backend detection:
//   - 8+ small-clip (≤5 lots) trades in 2s, monotonically stepping price
//   - Total volume < 30 (probing, not real conviction)
//   - Price spans >= _IGN_MIN_PRICE_SPREAD levels
//
// Visual design:
//   - Up ignition: green upward arrow cascade from price_min
//   - Down ignition: red downward arrow cascade from price_max
//   - reversed=true: ↩ bracket showing the failed ignition

const IgnitionRenderer = {
    render(ctx, d, priceConverter, config) {
        const { from, to } = d.visibleRange;

        ctx.save();

        for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData || !bar.originalData.ignition) continue;
            const ignitions = bar.originalData.ignition;
            if (!Array.isArray(ignitions) || !ignitions.length) continue;
            const x = bar.x;

            for (const ign of ignitions) {
                const isUp   = ign.direction === 'up';
                const isDown = ign.direction === 'down';
                if (!isUp && !isDown) continue;

                // Visual anchor: top of range for down, bottom for up
                const anchorPrice = isUp
                    ? (ign.price_min || (ign.price_max - 1))
                    : (ign.price_max || (ign.price_min + 1));
                const y = priceConverter(anchorPrice);
                if (y === null || y === undefined || isNaN(y)) continue;

                if (ign.reversed) {
                    // Confirmed reversal: ↩ bracket (failed ignition = trap)
                    const [r, g, b] = isUp ? [255, 80, 80] : [80, 255, 160];
                    ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, 0.8)`;
                    ctx.lineWidth = 1.5;
                    ctx.setLineDash([2, 2]);

                    // Bracket
                    ctx.beginPath();
                    const dir = isUp ? -1 : 1; // up ignition reversal = down failure
                    ctx.moveTo(x - 6, y);
                    ctx.lineTo(x - 6, y + dir * 10);
                    ctx.lineTo(x + 6, y + dir * 10);
                    ctx.lineTo(x + 6, y);
                    ctx.stroke();
                    ctx.setLineDash([]);

                    // ↩ label
                    ctx.font = '8px "JetBrains Mono", monospace';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = isUp ? 'top' : 'bottom';
                    ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.9)`;
                    ctx.fillText('↩TRAP', x, y + dir * 12);

                } else {
                    // Active ignition: arrow cascade
                    const [r, g, b] = isUp ? [80, 255, 120] : [255, 80, 100];
                    const dir = isUp ? -1 : 1;   // -1 = draw upward

                    ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, 0.75)`;
                    ctx.fillStyle   = `rgba(${r}, ${g}, ${b}, 0.85)`;
                    ctx.lineWidth = 1.5;

                    // Cascade of 3 chevrons (↑↑↑ or ↓↓↓)
                    for (let step = 0; step < 3; step++) {
                        const cy = y + dir * step * 7;
                        ctx.beginPath();
                        ctx.moveTo(x - 5, cy - dir * 5);
                        ctx.lineTo(x,     cy);
                        ctx.lineTo(x + 5, cy - dir * 5);
                        ctx.stroke();
                    }

                    // Level count badge
                    const levels = ign.levels_swept || '?';
                    const label  = `IGN ${levels}L`;
                    ctx.font = '7px "JetBrains Mono", monospace';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = isUp ? 'bottom' : 'top';
                    const bm = ctx.measureText(label);
                    ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
                    ctx.beginPath();
                    ctx.roundRect(x - bm.width / 2 - 3, y + dir * 22 - 6, bm.width + 6, 12, 2);
                    ctx.fill();
                    ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.95)`;
                    ctx.fillText(label, x, y + dir * 22);
                }
            }
        }

        ctx.restore();
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 15. DELTA DIVERGENCE LAYER
// ═══════════════════════════════════════════════════════════════════════════
//
// Renders cumulative delta divergence signals.
//
// Data source: bar.originalData.delta_div = {type, price_high/price_low,
//   price_prev, delta_current, delta_prev, t_prev}
//
// Backend detection:
//   - Bearish: price makes new high but delta LOWER than at previous high
//   - Bullish: price makes new low but delta HIGHER than at previous low
//
// Visual design:
//   - Divergence wedge drawn from the current bar to the reference bar
//   - Bearish = red wedge at price high (hidden selling pressure)
//   - Bullish = green wedge at price low (hidden buying pressure)
//   - Delta gap text: shows exact delta difference at the two pivots

const DeltaDivergenceLayer = {
    render(ctx, d, priceConverter, config) {
        const { from, to } = d.visibleRange;

        ctx.save();

        for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData || !bar.originalData.delta_div) continue;
            const div = bar.originalData.delta_div;
            if (!div || !div.type) continue;
            const x = bar.x;

            const isBearish = div.type === 'bearish';
            const [r, g, b] = isBearish ? [255, 80, 80] : [80, 220, 140];

            // Price for this bar's pivot
            const pivotPrice = isBearish ? div.price_high : div.price_low;
            if (pivotPrice === undefined || pivotPrice === null) continue;
            const y = priceConverter(pivotPrice);
            if (y === null || y === undefined || isNaN(y)) continue;

            // ── Divergence marker (wedge shape at the pivot) ──
            const halfW = 10;
            const tipH  = isBearish ? 8 : -8;  // down for bearish, up for bullish
            ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.25)`;
            ctx.beginPath();
            ctx.moveTo(x - halfW, y);
            ctx.lineTo(x + halfW, y);
            ctx.lineTo(x, y + tipH);
            ctx.closePath();
            ctx.fill();

            ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, 0.85)`;
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(x - halfW, y);
            ctx.lineTo(x + halfW, y);
            ctx.lineTo(x, y + tipH);
            ctx.closePath();
            ctx.stroke();

            // ── Delta gap label ──
            const deltaGap = Math.round((div.delta_current || 0) - (div.delta_prev || 0));
            const label = `${isBearish ? '↘DIV' : '↗DIV'} Δ${deltaGap > 0 ? '+' : ''}${deltaGap}`;
            ctx.font = '7px "JetBrains Mono", monospace';
            ctx.textAlign = 'center';
            ctx.textBaseline = isBearish ? 'bottom' : 'top';
            const bm = ctx.measureText(label);
            ctx.fillStyle = 'rgba(0, 0, 0, 0.75)';
            ctx.beginPath();
            const labelY = y + (isBearish ? -5 : 5);
            ctx.roundRect(x - bm.width / 2 - 3, labelY - (isBearish ? 12 : 0), bm.width + 6, 12, 2);
            ctx.fill();
            ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.95)`;
            ctx.fillText(label, x, labelY);
        }

        ctx.restore();
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// EXPORTS
// ═══════════════════════════════════════════════════════════════════════════

window.IcebergVisualizer     = IcebergVisualizer;
window.SweepRenderer         = SweepRenderer;
window.HawkesStateManager    = HawkesStateManager;
window.AbsorptionStateManager = AbsorptionStateManager;
window.SpoofRenderer         = SpoofRenderer;
window.IgnitionRenderer      = IgnitionRenderer;
window.DeltaDivergenceLayer  = DeltaDivergenceLayer;

})(); // end IIFE
