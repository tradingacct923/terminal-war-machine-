/**
 * V2 INTEGRATION PATCH — Drop-in Replacement for VolumeBubbleRenderer.draw()
 *
 * ALL 11 MODULES:
 *   1. AdaptiveKalmanThreshold  (replaces per-frame log-σ)
 *   2. HawkesClusterDetector    (replaces hit-count ≥ 3 logic)
 *   3. AbsorptionAggregator     (replaces single-bar _isAbsorption)
 *   4. AdaptiveDominance        (replaces fixed 0.70 threshold)
 *   5. TemporalDecay            (adds recency weighting)
 *   6. RegressionAcceleration   (replaces 2-sample accel)
 *   7. CumlDeltaRenderer        (the missing sidebar)
 *   8. ExhaustionDetector       (flow-price divergence)
 *   9. IcebergVisualizer        (surfaces backend iceberg detections)
 *  10. SweepRenderer            (lightning bolt multi-level sweeps)
 *  11. HawkesStateManager       (fixes per-frame reset bug)
 *
 * Load order: volume_bubbles.js → v2_sigma_engine.js → v2_iceberg_sweep.js → v2_integration.js
 */
(function() {
'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// PATCHED draw() — Replace VolumeBubbleRenderer.prototype.draw
// ═══════════════════════════════════════════════════════════════════════════

const _originalDraw = VolumeBubbleRenderer.prototype.draw;

// ── Symbol change detection ──
// Tracks the active instrument so all modules can reset when it changes.
// d.symbol is passed by the chart host; falls back to first bar's 's' field
// or stays as the last known value.
let _v2ActiveSymbol = null;

// ── Backend absorption data bridge ──
// The dom_snapshot WebSocket emits data.abs = {priceStr: {s, w, sh, c, ...}}
// every ~500ms. We store the latest snapshot here so each draw() call can
// feed real backend absorption data to AbsorptionAggregator.ingest().
//
// HOW TO WIRE: In the dom_snapshot handler (research/volume_bubbles.js
// line ~1157), add:
//   window._v2AbsBuffer = data.abs || {};
//
// This file reads it each frame and calls ingest() before ingestFromBP().
// If _v2AbsBuffer is empty (WS not yet connected), falls back to BP-only.
if (!window._v2AbsBuffer) window._v2AbsBuffer = {};

// ── V2 Backend Signals Cache ──
// Populated by the separate 'v2_signals' Socket.IO channel (~2Hz).
// Contains pre-classified { kalman, hawkes } from AdaptiveKalmanOFI
// and HawkesBranchingRatio running in l2_worker.py.
//
// Graceful degradation: if _v2Signals is empty (backend not yet upgraded
// or WebSocket not connected), all gates fall back to local JS detection.
if (!window._v2Signals) window._v2Signals = {};

// ── TapeEWMA: Adaptive Print-Size Floor (The Market Maker Way) ──
// A market maker never uses a hardcoded contract floor like "NQ ≥ 30".
// Why: NQ at 9:31 AM open averages 80-lot prints — a 30-lot is noise.
//      NQ at 2:45 PM (pre-FOMC silence) averages 4-lot prints — a 30-lot
//      is a whale. Same number, opposite meaning.
//
// Correct approach: significance is RELATIVE to current tape participation.
// We track an EWMA of recent trade sizes from dom_snapshot.trades.
// The adaptive floor = EWMA_mean + 0.5σ ≈ the P70 of current tape.
// A print must exceed this to enter the classify pipeline — meaning it must
// be materially larger than what's currently printing.
//
// This self-calibrates in real time:
//   Slow tape (off-hours, pre-market): floor drops automatically → 8-lot signals
//   Fast open (RTH, FOMC): floor rises automatically → 80-lot signals only
//   Volatility spike: EWMA reacts within ~50 trades (~10-15 seconds)
const TapeEWMA = {
    _mu:  0,       // EWMA of log-trade-size (log for stability under outliers)
    _var: 1,       // EWMA variance of log-trade-size
    _n:   0,       // print count (warmup guard)
    _ALPHA: 0.03,  // EWMA decay ≈ half-life of ~23 prints (~5-8 seconds at RTH)

    ingest(tradeList) {
        if (!tradeList || !tradeList.length) return;
        for (const t of tradeList) {
            const v = t.v || t.volume || 0;
            if (v <= 0) continue;
            const logV = Math.log(v + 1);
            this._n++;
            if (this._n === 1) {
                this._mu = logV; this._var = 0.25; return;
            }
            const diff = logV - this._mu;
            this._mu  += this._ALPHA * diff;
            this._var  = (1 - this._ALPHA) * (this._var + this._ALPHA * diff * diff);
        }
    },

    // Adaptive floor: prints must be larger than EWMA_mean + 0.5σ in log-space.
    // In linear space this ≈ the P70 of recent tape sizes.
    // Falls back to 1 (no floor) during warmup (<30 prints seen).
    floor() {
        if (this._n < 30) return 1;
        const logFloor = this._mu + 0.5 * Math.sqrt(Math.max(this._var, 0));
        return Math.max(Math.round(Math.exp(logFloor) - 1), 1);
    },

    // Expose stats for _v2Debug diagnostics
    stats() {
        return {
            n: this._n,
            ewmaMean: Math.round(Math.exp(this._mu) - 1),
            floor: this.floor(),
            sigma: +Math.sqrt(Math.max(this._var, 0)).toFixed(3),
        };
    },

    reset() { this._mu = 0; this._var = 1; this._n = 0; },
};
window._tapeEWMA = TapeEWMA; // expose for console inspection

// Wire TapeEWMA to dom_snapshot: feed every incoming trade into the EWMA.
// Also cache spread/BBO from the new backend fields (best_bid, best_ask, spread).
// Uses a deferred approach so this works regardless of load order.
(function _wireTapeEWMAListener() {
    function attach() {
        const sio = window._sio;
        if (!sio) { setTimeout(attach, 400); return; }
        sio.on('dom_snapshot', (data) => {
            if (data && data.trades && data.trades.length) {
                TapeEWMA.ingest(data.trades);
            }

            // ── FIX 1: Wire _v2AbsBuffer ──
            // dom_snapshot.abs = {priceStr: {s, w, sh, c}} — per-price absorption scores.
            // The comment in the original code said "HOW TO WIRE: add window._v2AbsBuffer = data.abs"
            // but it was never done. Without this, AbsorptionAggregator.ingest() never
            // gets backend scores and falls back to local BP-only scoring.
            if (data && data.abs) {
                window._v2AbsBuffer = data.abs;
            }
            // ── BUG 5 FIX: Wire depth_vel ──
            if (data && data.depth_vel) {
                window._domDepthVel = data.depth_vel;
            }

            // ── FIX 2: Wire domSnapshotAbsTiers ──
            // dom_snapshot.abs_tiers = {priceStr: {tier, label, score}} — WALL/ABS/CRACK tiers.
            // Previously only written by v2_dom_heatmap.js (line 405), meaning it was only
            // populated when a heatmap pane was mounted. Bubble classify path checked this
            // global but got {} when no heatmap was active — fell back to local tier scoring.
            if (data && data.abs_tiers) {
                window.domSnapshotAbsTiers = data.abs_tiers;
            }

            // ── Model engine outputs: Queue Dynamics, Trade Toxicity, Level Survival ──
            if (data && data.queue_dynamics)  window._queueDynamics  = data.queue_dynamics;
            if (data && data.trade_toxicity)  window._tradeToxicity  = data.trade_toxicity;
            if (data && data.level_survival)  window._levelSurvival  = data.level_survival;

            // Cache BBO + spread for market-maker risk display.
            // Backend now emits best_bid, best_ask, spread on every snapshot.
            if (data && data.sym) {
                if (!window._v2BBO) window._v2BBO = {};
                window._v2BBO[data.sym] = {
                    bid:    data.best_bid  || 0,
                    ask:    data.best_ask  || 0,
                    spread: data.spread    || 0,
                    ts:     data.ts        || 0,
                };
            }
        });
    }
    attach();
})();;

// Wire the socket listener once (after Socket.IO connects).
// Uses a deferred approach so this works regardless of load order.
(function _wireV2SignalsListener() {
    function attach() {
        if (!window._sio) return;
        window._sio.off('v2_signals'); // remove stale listener on re-attach
        window._sio.on('v2_signals', (data) => {
            if (!data || !data.sym) return;
            // Store per-symbol so multi-symbol setups don't cross-contaminate
            window._v2Signals = data;

            // Update ρ(Γ) display in top toolbar if element exists
            const rhoEl = document.getElementById('t-hawkes-rho');
            if (rhoEl && data.hawkes) {
                const rho = data.hawkes.rho;
                if (rho === null || rho === undefined || !isFinite(rho)) {
                    rhoEl.textContent = '—';
                    rhoEl.style.color = '';
                } else {
                    rhoEl.textContent = rho.toFixed(3);
                    rhoEl.style.color = rho >= 1.0 ? '#ff3060'
                        : rho >= 0.8 ? '#ffb428' : '#2ee88a';
                    // Pulse animation on supercritical
                    if (rho >= 1.0) {
                        rhoEl.style.animation = 'none';
                        void rhoEl.offsetWidth; // force reflow
                        rhoEl.style.animation = 'rho-pulse 0.6s ease-out';
                    } else {
                        rhoEl.style.animation = '';
                    }
                }

                // ── FIX 4: Directional Hawkes badge (↑B / ↓S) ──
                // side_dominance = (rho_buy - rho_sell) / (rho_buy + rho_sell)
                //   +1.0 = pure buy self-excitation (ask sweeps incoming)
                //   -1.0 = pure sell self-excitation (bid dump incoming)
                //   near 0 = balanced / unclear
                const dirEl = document.getElementById('t-hawkes-dir');
                if (dirEl) {
                    const dom = data.hawkes.side_dominance;
                    if (dom === null || dom === undefined || !isFinite(dom) || rho === null || !isFinite(rho)) {
                        dirEl.textContent = '';
                    } else if (Math.abs(dom) < 0.15) {
                        // Balanced — no directional signal
                        dirEl.textContent = '≈';
                        dirEl.style.color = 'rgba(140,160,200,0.5)';
                    } else if (dom > 0) {
                        // Buy-dominant
                        dirEl.textContent = '↑B';
                        // Scale intensity by dominance strength × ρ (only meaningful when active)
                        const intensity = Math.min(dom * rho, 1.0);
                        dirEl.style.color = `rgba(46,232,138,${0.5 + intensity * 0.5})`;
                    } else {
                        // Sell-dominant
                        dirEl.textContent = '↓S';
                        const intensity = Math.min(Math.abs(dom) * rho, 1.0);
                        dirEl.style.color = `rgba(255,48,96,${0.5 + intensity * 0.5})`;
                    }
                }
            }

            // ── FIX 3: tape_floor override — always use backend value ──
            // PROBLEM: The old guard `TapeEWMA._n < 30` was supposed to only override
            // during warmup. But after 766 trades with ewmaMean=1 (tiny tick prints),
            // the EWMA is calibrated to the wrong value — the client sees fractional/
            // small prints while the backend VolumeClock sees the full unthrottled stream.
            //
            // The backend VolumeClock.bucket_size is ALWAYS more accurate than TapeEWMA:
            //   - VolumeClock: full 100% of prints at full resolution, continuously
            //   - TapeEWMA: ~500ms batches of compact_trades (sampled, batched)
            //
            // So: always apply when backend sends tape_floor > 0.
            // Only gate: if our local EWMA has already surpassed the backend value,
            // trust the local one (means we've seen a regime shift the backend hasn't sent yet).
            if (data.tape_floor && data.tape_floor > 0) {
                const backendFloor = data.tape_floor;
                const localFloor   = TapeEWMA.floor();
                // Apply if: backend > 1 (actually calibrated) AND backend > local
                // (so we only override upward, never suppress a real regime shift)
                if (backendFloor > 1 && backendFloor > localFloor) {
                    const logFloor = Math.log(backendFloor + 1);
                    TapeEWMA._mu  = logFloor;
                    TapeEWMA._var = 0.25;
                    TapeEWMA._n   = Math.max(TapeEWMA._n, 30); // ensure past warmup
                }
            }

            // ── spread HUD (market-maker risk signal) ──
            const spreadEl = document.getElementById('t-spread');
            if (spreadEl && data.kalman) {
                // theta > 0 = buy-side OFI dominant, < 0 = sell-side
                // SNR indicates regime confidence
                const snr = data.kalman.snr || 0;
                spreadEl.textContent = isFinite(snr) ? snr.toFixed(2) + 'σ' : '—';
                spreadEl.style.color = snr >= 2.0 ? '#ff3060' : snr >= 1.0 ? '#ffb428' : '#2ee88a';
            }
        });  // end sio.on('v2_signals')
        console.log('[V2.2] v2_signals listener attached');
    }  // end attach()
    // Try immediately, then retry until Socket.IO connects
    if (window._sio) { attach(); }
    else {
        let attempts = 0;
        const interval = setInterval(() => {
            if (window._sio) { attach(); clearInterval(interval); }
            else if (++attempts > 20) clearInterval(interval); // stop after 10s
        }, 500);
    }
})();

// ── Inline _rgba safety ──
// _rgba() is defined in volume_bubbles.js. If load order breaks, we need
// a fallback so render calls don't throw. The original version has a
// cache (_rgbaCache), ours doesn't — but correctness > speed for the fallback.
if (typeof _rgba !== 'function') {
    window._rgba = (rgb, alpha) => `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

VolumeBubbleRenderer.prototype.draw = function(target, priceConverter) {
    const d = this._data;
    if (!d || !d.bars || d.bars.length === 0) return;

    const { from, to } = d.visibleRange;
    const barSpacing = d.barSpacing || 6;
    const useDots = false;

    // ── Symbol change detection ──
    // Detect instrument switches and reset all stateful modules.
    // d.symbol is set by the chart host. Falls back to bar.originalData.s
    // (the 'sym' field l2_worker includes in candle data).
    const currentSymbol = d.symbol
        || (d.bars[from] && d.bars[from].originalData && d.bars[from].originalData.s)
        || 'NQ';  // final fallback

    if (currentSymbol !== _v2ActiveSymbol) {
        // Symbol changed: tear down all per-price state.
        // Hawkes λ, absorption scores, exhaustion histories and Kalman state
        // all accumulate per-price. Stale NQ prices mixed into GC data
        // would produce false clusters and phantom walls.
        HawkesStateManager.reset();        // also resets HawkesClusterDetector
        AbsorptionStateManager.reset();    // also resets AbsorptionAggregator
        ExhaustionDetector.reset();
        AdaptiveKalmanThreshold.reset();
        TapeEWMA.reset();                  // reset tape EWMA — NQ tape stats ≠ GC tape stats
        if (typeof AbsorptionZoneDetector !== 'undefined') AbsorptionZoneDetector.reset();

        // Flush absorption globals immediately on switch.
        // These globals are populated by dom_snapshot which filters by symbol
        // on WRITE (new data) but never flushes on SWITCH. During the 0-500ms
        // gap before the first dom_snapshot for the new instrument, the old
        // symbol's price levels would appear as walls on the new chart.
        window._v2AbsBuffer = {};
        window.domSnapshotAbsTiers = {};

        // Update tick size for the new instrument
        AbsorptionAggregator.setSymbol(currentSymbol);

        // Invalidate the render cache so the next frame recomputes from scratch
        this._v3sig = null;
        this._v3cache = null;

        _v2ActiveSymbol = currentSymbol;
        console.log(`[V3] Symbol switch → ${currentSymbol}, all state + abs globals flushed.`);
    }

    // ── Per-pane overlay config lookup ──
    // Container ref is set on the renderer by ChartCore during init
    const _paneOverlay = this._containerRef ? (this._containerRef._overlayConfig || null) : null;

    try { target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {

        // ══════════════════════════════════════════════════════════════
        // PHASE 1: KALMAN THRESHOLD (replaces lines 395-412)
        // Instead of recomputing log-σ over all visible bars per frame,
        // feed the Kalman filter incrementally and read thresholds.
        // ══════════════════════════════════════════════════════════════

        // Check if Kalman needs initialization from visible data
        if (!AdaptiveKalmanThreshold._initialized) {
            const initVols = [];
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const bp = bar.originalData.bp;
                for (const key in bp) {
                    const tv = bp[key][0] + bp[key][1];
                    if (tv > 0) initVols.push(tv);
                }
            }
            if (initVols.length > 0) AdaptiveKalmanThreshold.initialize(initVols);
            AdaptiveKalmanThreshold._fedFrom = from;
            AdaptiveKalmanThreshold._fedTo = to;
        }

        // FIX (Issue 6): Feed newly visible historical bars to Kalman.
        // When the user scrolls left, new bars enter with `from` < _fedFrom.
        // These bars have volume data the Kalman has never seen. Without
        // feeding them, the threshold is calibrated only on the initial
        // window, potentially missing a different volume regime to the left.
        //
        // When the user scrolls far (> 50% range shift), re-initialize
        // from scratch since the filter may be in a completely different
        // volume regime.
        const fedFrom = AdaptiveKalmanThreshold._fedFrom || from;
        const fedTo = AdaptiveKalmanThreshold._fedTo || to;
        const rangeShift = Math.abs(from - fedFrom) + Math.abs(to - fedTo);
        const rangeSize = to - from;

        if (rangeShift > rangeSize * 0.5) {
            // Big scroll — re-initialize from entire visible range
            const reInitVols = [];
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                const bp = bar.originalData.bp;
                for (const key in bp) {
                    const tv = bp[key][0] + bp[key][1];
                    if (tv > 0) reInitVols.push(tv);
                }
            }
            if (reInitVols.length > 0) {
                AdaptiveKalmanThreshold.reset();
                AdaptiveKalmanThreshold.initialize(reInitVols);
            }
            AdaptiveKalmanThreshold._fedFrom = from;
            AdaptiveKalmanThreshold._fedTo = to;
        } else {
            // Incremental: feed any bars that extended beyond the tracked range
            if (from < fedFrom) {
                for (let i = from; i < fedFrom; i++) {
                    const bar = d.bars[i];
                    if (!bar || !bar.originalData || !bar.originalData.bp) continue;
                    const bp = bar.originalData.bp;
                    for (const key in bp) {
                        const tv = bp[key][0] + bp[key][1];
                        if (tv > 0) AdaptiveKalmanThreshold.update(tv);
                    }
                }
                AdaptiveKalmanThreshold._fedFrom = from;
            }
            if (to > fedTo) {
                AdaptiveKalmanThreshold._fedTo = to;
            }
        }

        // Feed new volumes to Kalman ONLY when data changes.
        // BUG FIX: Was feeding all price levels of latest bar every frame
        // (40 levels × 60fps = 2,400 updates/sec of SAME data).
        // This drove P_mu → 0 (overconfidence), making the filter deaf
        // to real regime shifts. Now: signature guard, one feed per change.
        const latestBar = d.bars[to - 1];
        if (latestBar && latestBar.originalData && latestBar.originalData.bp) {
            const bp = latestBar.originalData.bp;
            let sig = 0, keyCount = 0;
            for (const key in bp) {
                sig += bp[key][0] + bp[key][1];
                keyCount++;
            }
            const kalmanSig = `${to - 1}:${keyCount}:${sig}`;
            if (kalmanSig !== AdaptiveKalmanThreshold._lastFedSig) {
                AdaptiveKalmanThreshold._lastFedSig = kalmanSig;
                for (const key in bp) {
                    const tv = bp[key][0] + bp[key][1];
                    if (tv > 0) AdaptiveKalmanThreshold.update(tv);
                }
            }
        }

        // Read current thresholds (O(1) — no per-frame recomputation)
        const thresholds = AdaptiveKalmanThreshold._thresholds();
        const { sigThreshold, instThreshold, absorbMinVol, highDomMinVol, wallThreshold, directionalThreshold } = thresholds;

        // Compute maxVol for radius scaling (still needed)
        let maxVol = 0;
        for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData || !bar.originalData.bp) continue;
            const bp = bar.originalData.bp;
            for (const key in bp) {
                const total = bp[key][0] + bp[key][1];
                if (total > maxVol) maxVol = total;
            }
        }
        if (maxVol === 0) return;


        // ══════════════════════════════════════════════════════════════
        // PHASE 2: HAWKES CLUSTER DETECTION
        // V3 FIX: Hawkes noise floor raised to match V3 bubble gate.
        //
        // The old code fed Hawkes with sigThreshold (1.5σ Kalman floor).
        // Since V3 bubbles require 2.5σ, Hawkes was ingesting hundreds of
        // prints that would NEVER render a bubble, creating false clusters
        // at every tick level — the 20× stacked horizontal band in the chart.
        //
        // Invariant: A cluster should only aggregate events that are
        // independently bubble-worthy at the consolidated level.
        // Feed threshold = max(2.0σ Kalman, TapeEWMA.floor()) so the
        // population feeding Hawkes matches what Phase 5 actually renders.
        // ══════════════════════════════════════════════════════════════
        // Compute V3-aligned Hawkes noise floor at EXACTLY 2.0σ in log-space.
        // thresholds already exposes mu and sigma from the Kalman filter — use them
        // directly so the calculation is exact, not an approximation.
        //
        // instThreshold * 0.6 was WRONG:
        //   instThreshold = e^(μ + 3σ) - 1
        //   0.6 × instThreshold ≠ e^(μ + 2σ) - 1  (not a σ-level, just a scalar)
        //
        // Correct:
        //   2.0σ level = e^(μ + 2σ) - 1  (exact log-normal inversion)
        const _kalman2Sigma = Math.exp(thresholds.mu + 2.0 * thresholds.sigma) - 1;
        const _hawkesFloor = Math.max(_kalman2Sigma, TapeEWMA.floor());

        // ── Declare _latestBar here (before Phase 2) — used in both Phase 2 and Phase 5 ──
        // IMPORTANT: was previously declared with `const` at Phase 5 (Dirty flag section),
        // causing a TDZ ReferenceError that crashed draw() on every frame silently.
        const _latestBar = d.bars[to - 1];

        HawkesStateManager.update(d, _hawkesFloor, _hawkesFloor);
        // Pass latest bar's Unix timestamp so getActiveClusters() can prune by
        // real clock time (30 min) rather than bar count.
        const _latestBarTs = _latestBar?.originalData?.ts || 0;
        const hawkesClusters = HawkesClusterDetector.getActiveClusters(to - 1, _latestBarTs);


        // ══════════════════════════════════════════════════════════════
        // PHASE 3: ABSORPTION AGGREGATION (replaces _isAbsorption)
        // V2.1 FIX: Uses AbsorptionStateManager for INCREMENTAL ingest.
        // No more per-frame reset — multi-bar temporal decay preserved.
        // ══════════════════════════════════════════════════════════════

        AbsorptionStateManager.update(d, absorbMinVol);

        // ── Feed backend absorption data if available ──
        // window._v2AbsBuffer is populated by the dom_snapshot WebSocket handler.
        // It contains the latest {priceStr: {s, w, sh, c}} snapshot from l2_worker.
        //
        // FIX (Bug C): Was calling ingest() every frame (60fps) with the same
        // snapshot that only updates every ~500ms from WebSocket. This inflated
        // wall scores by ~30× per update cycle. Now: signature guard that
        // detects when the buffer has actually changed before feeding.
        const absKeys = window._v2AbsBuffer ? Object.keys(window._v2AbsBuffer) : [];
        if (absKeys.length > 0) {
            const firstEntry = window._v2AbsBuffer[absKeys[0]];
            const absSig = `${absKeys.length}:${firstEntry ? (firstEntry.s || 0) : 0}`;
            if (absSig !== (AbsorptionAggregator._lastAbsSig || '')) {
                AbsorptionAggregator._lastAbsSig = absSig;
                AbsorptionAggregator.ingest(window._v2AbsBuffer, to - 1);
            }
        }


        // ══════════════════════════════════════════════════════════════
        // PHASE 4: CUMULATIVE DELTA (data computation — unchanged)
        // ══════════════════════════════════════════════════════════════

        const cumlDelta = {};
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

        // Sigma-filter cumulative levels
        let cumlMinVol = 0;
        const cumlTotals = Object.values(cumlDelta).map(d => d.total);
        if (cumlTotals.length > 0) {
            const cumlLogTotals = cumlTotals.map(v => Math.log(v + 1));
            const cumlLogAvg = cumlLogTotals.reduce((a, b) => a + b, 0) / cumlLogTotals.length;
            const cumlLogVar = cumlLogTotals.reduce((s, v) => {
                const diff = v - cumlLogAvg; return s + diff * diff;
            }, 0) / cumlLogTotals.length;
            cumlMinVol = Math.exp(cumlLogAvg + 0.5 * Math.sqrt(cumlLogVar)) - 1;
        }


        // ══════════════════════════════════════════════════════════════
        // PHASE 5: CLASSIFY BUBBLES — V3 INSTITUTIONAL ENGINE
        //
        // V3 fixes two architectural bugs from the junior implementation:
        //
        // BUG A: Per-bar stacking. The old loop rendered one bubble per
        //   (bar, price) pair. With 60 visible bars, price 18340 produced
        //   60 overlapping circles at the same Y coordinate — the visual
        //   explosion in the screenshot. Fix: aggregate all visible bars
        //   into a single volume profile first, then render ONE bubble per
        //   price level, positioned at the highest-sigma bar's X.
        //
        // BUG B: σ floor too loose. 1.5σ from a Kalman trained on a slow-
        //   tape window passes 10-lot NQ fills. 10 lots is retail noise.
        //   Fix: adaptive tape EWMA floor + raised σ floors
        //   (2.5σ directional, 2.0σ walls).
        //
        // PERF: Dirty-flag caches computed bubble arrays. LWC calls draw()
        //   at 60fps; backend data arrives at ~2Hz. 58/60 frames are now
        //   pure cache hits — no classification work, no shadow math.
        // ══════════════════════════════════════════════════════════════

        // ── Adaptive floor: TapeEWMA (the market-maker-correct approach) ──
        // NOT a hardcoded contract count. The floor adapts to current tape
        // participation so that "significant" always means the same thing:
        // materially larger than what's printing RIGHT NOW on the tape.
        //   - RTH open (heavy tape, avg 80 lots): floor auto-rises to ~60
        //   - Pre-FOMC silence (avg 4 lots):      floor auto-drops to ~3
        //   - Off-hours:                           floor auto-drops to ~2
        // Warmup (<30 prints): floor = 1 (no gate) until EWMA is calibrated.
        const _dynFloor = TapeEWMA.floor();

        // ── Dirty flag: skip consolidation if data hasn't changed ──
        // Signature = visible range + latest bar timestamp + backend signal timestamp.
        // If unchanged, blit cached bubble arrays directly.
        // Signature includes TapeEWMA.floor() so cache invalidates when tape
        // regime shifts (e.g. open → slow tape). floor() is O(1) EWMA read.
        // NOTE: _latestBar declared above at Phase 2 — do NOT re-declare here.
        const _frameSig = `${from}:${to}:${_latestBar?.originalData?.ts||0}:${window._v2Signals?.ts||0}:${Object.keys(window._v2AbsBuffer||{}).length}:${_dynFloor}`;

        let glowBubbles, buyBubbles, sellBubbles, absorbBubbles, labelBubbles, _priceBarX;

        if (_frameSig === this._v3sig && this._v3cache) {
            // ── Cache hit: reuse last frame's bubble arrays ──
            ({ glowBubbles, buyBubbles, sellBubbles, absorbBubbles, labelBubbles, _priceBarX } = this._v3cache);
        } else {
            // ── Cache miss: full consolidation + classification ──
            this._v3sig = _frameSig;
            glowBubbles = []; buyBubbles = []; sellBubbles = []; absorbBubbles = []; labelBubbles = []; _priceBarX = {};

            // ── PRE-PASS: Aggregate volumes per price across ALL visible bars ──
            // A 50-lot print at 18340 across 3 bars = ONE institutional decision.
            // Aggregate first, classify once. Eliminates the 60× stacking bug.
            const _priceAgg = {};
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar?.originalData?.bp) continue;
                const bp = bar.originalData.bp;
                for (const priceStr in bp) {
                    const entry = bp[priceStr];
                    const bv = entry[0] || 0, sv = entry[1] || 0;
                    const tv = bv + sv;
                    if (tv < 1) continue;
                    // Adaptive floor gate: reject prints below P70 of current tape
                    if (tv < _dynFloor) continue;

                    if (!_priceAgg[priceStr]) {
                        _priceAgg[priceStr] = { buyVol: 0, sellVol: 0, maxSigma: 0, barX: bar.x, lastBarIdx: i, newestBarIdx: i, firstBarIdx: i, barCount: 0, barVols: [] };
                    }
                    const agg = _priceAgg[priceStr];
                    agg.buyVol += bv;
                    agg.sellVol += sv;
                    agg.barCount++;
                    // Per-bar volume for dV/dt computation
                    agg.barVols.push({ idx: i, vol: tv, buy: bv, sell: sv });
                    // Track most recent bar at this price (for temporal decay)
                    if (i > agg.newestBarIdx) agg.newestBarIdx = i;
                    if (i < agg.firstBarIdx) agg.firstBarIdx = i;
                    // Track bar with highest single-bar sigma for X positioning.
                    // Also write to _priceBarX for O(1) lookup by Layer 8.5.
                    const _bSig = AdaptiveKalmanThreshold.sigmaDistance(tv);
                    if (_bSig > agg.maxSigma) {
                        agg.maxSigma = _bSig;
                        agg.barX = bar.x;
                        agg.lastBarIdx = i;
                    }
                    // _priceBarX always holds the LATEST (rightmost) bar.x seen
                    // so Layer 8.5 gets the most recent position for the level.
                    _priceBarX[priceStr] = bar.x;
                    // Feed ExhaustionDetector per-bar (needs bar-level granularity)
                    const _bClose = bar.originalData.c || bar.originalData.close || 0;
                    ExhaustionDetector.update(priceStr, i, _bSig, bv, sv, _bClose);
                }
            }

            // ── CLASSIFY PASS: one bubble per price level ──
            const _bkAbsTiersGlobal = window._v2AbsBuffer && window.domSnapshotAbsTiers;
            for (const priceStr in _priceAgg) {
                const agg = _priceAgg[priceStr];
                const { buyVol, sellVol, barX } = agg;
                const totalVol = buyVol + sellVol;

                const price = parseFloat(priceStr);
                if (isNaN(price)) continue;
                const y = priceConverter(price);
                if (y === null || y === undefined || isNaN(y)) continue;

                // σ on consolidated volume (represents full institutional conviction at level)
                const sigmaDistance = AdaptiveKalmanThreshold.sigmaDistance(totalVol);

                // Dominance: Wilson CI on consolidated buy/sell ratio
                const domResult = AdaptiveDominance.test(buyVol, sellVol);
                const isBuy = buyVol >= sellVol;
                const isInstitutional = totalVol >= instThreshold;
                const isInHawkesCluster = hawkesClusters.has(priceStr);

                // Absorption: backend tier preferred, local fallback
                let absClass;
                if (_bkAbsTiersGlobal && _bkAbsTiersGlobal[priceStr]) {
                    const bkT = _bkAbsTiersGlobal[priceStr];
                    const glowMap = [0, 0.3, 0.6, 1.0];
                    absClass = { tier: bkT.tier, label: bkT.label, glowIntensity: glowMap[Math.min(bkT.tier, 3)] ?? 0, score: bkT.score };
                } else {
                    absClass = AbsorptionAggregator.classify(priceStr, to - 1);
                }
                const isAbsorb = absClass.tier >= 2;
                const isConfirmedWall = absClass.tier >= 2;

                // ── V3: Kalman-derived gates (no hardcoded σ numbers) ──
                // wallThreshold = e^(μ+2.0σ)-1 from AdaptiveKalmanThreshold._thresholds()
                // directionalThreshold = e^(μ+2.5σ)-1
                // Both adapt to the current tape regime automatically.
                if (isConfirmedWall) {
                    if (totalVol < wallThreshold) continue;
                } else {
                    if (totalVol < directionalThreshold) continue;
                    if (!domResult.isDirectional) continue;
                }

                // ── Opacity from σ + conviction + recency ──
                // Use newestBarIdx (most recent bar at this price) for decay,
                // not lastBarIdx (highest sigma bar) — recency is about TIME not SIZE
                const recencyWeight = TemporalDecay.weight(agg.newestBarIdx, from, to);
                let opacity = BUBBLE_CONFIG.GRADIENT_BASE_OPACITY
                    + Math.pow(Math.max(sigmaDistance, 0), 2) * BUBBLE_CONFIG.GRADIENT_EXPONENT_SCALE;
                const conviction = AdaptiveDominance.convictionStrength(buyVol, sellVol);
                opacity += conviction * BUBBLE_CONFIG.DOMINANCE_OPACITY_SCALE;
                if (isInHawkesCluster) {
                    const cl = hawkesClusters.get(priceStr);
                    opacity += cl.clusterStrength * BUBBLE_CONFIG.CLUSTER_OPACITY_BOOST * 2;
                }
                if (absClass.tier >= 2) opacity += 0.15;
                opacity = Math.min(opacity * recencyWeight, BUBBLE_CONFIG.GRADIENT_MAX_OPACITY);

                // Radius: σ-scaled
                const sigmaRatio = Math.min(Math.pow(Math.max(sigmaDistance, 0) / 4, 1.5), 1);
                const radius = BUBBLE_CONFIG.MIN_RADIUS + sigmaRatio * (BUBBLE_CONFIG.MAX_RADIUS - BUBBLE_CONFIG.MIN_RADIUS);

                // ── dV/dt: volume rate acceleration at this price level ──
                // Compare second-half bar volumes vs first-half.
                // ratio > 2.0 = volume doubling = active loading RIGHT NOW
                let dvdt = 0;
                const bv_ = agg.barVols;
                if (bv_.length >= 3) {
                    const half = Math.floor(bv_.length / 2);
                    let sumFirst = 0, sumSecond = 0;
                    for (let k = 0; k < half; k++) sumFirst += bv_[k].vol;
                    for (let k = half; k < bv_.length; k++) sumSecond += bv_[k].vol;
                    const avgFirst = sumFirst / half;
                    const avgSecond = sumSecond / (bv_.length - half);
                    dvdt = avgFirst > 0 ? avgSecond / avgFirst : (avgSecond > 0 ? 3 : 0);
                }

                const _bpBar = d.bars[agg.lastBarIdx];
                const _bpEntry = _bpBar?.originalData?.bp?.[priceStr];
                const bookSize = (_bpEntry && _bpEntry[4] != null) ? _bpEntry[4] : 0;
                const bubble = { x: barX, y, radius, totalVol, buyVol, sellVol, opacity, isAbsorb, isInstitutional, absClass, priceStr, dvdt, bookSize };

                if (isInstitutional) glowBubbles.push(bubble);
                if (isAbsorb) absorbBubbles.push(bubble);
                else if (isBuy) buyBubbles.push(bubble);
                else sellBubbles.push(bubble);
                if (radius >= 7) labelBubbles.push(bubble);
            }

            // ── Top-N cap: keep only highest-conviction bubbles ──
            // Hard ceiling prevents extreme-tape charts from overloading.
            // Sorted by totalVol descending (proxy for conviction).
            const _cap = (arr, n) => {
                if (arr.length <= n) return arr;
                arr.sort((a, b) => b.totalVol - a.totalVol);
                return arr.slice(0, n);
            };
            buyBubbles    = _cap(buyBubbles, 15);
            sellBubbles   = _cap(sellBubbles, 15);
            absorbBubbles = _cap(absorbBubbles, 12);
            // Keep glow/label only for bubbles that made the cut
            const _kept = new Set([...buyBubbles, ...sellBubbles, ...absorbBubbles]);
            glowBubbles  = glowBubbles.filter(b => _kept.has(b));
            labelBubbles = labelBubbles.filter(b => _kept.has(b));

            // Store in cache for next frame — include _priceBarX so Layer 8.5
            // can O(1) resolve the rightmost bar X for any price level.
            this._v3cache = { glowBubbles, buyBubbles, sellBubbles, absorbBubbles, labelBubbles, _priceBarX };
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 0: ABSORPTION ZONES (behind everything)
        // Contiguous price bands where AbsorptionAggregator detects
        // 2+ adjacent ticks with absorption scores. Rendered as
        // translucent horizontal bands spanning chart width.
        // ══════════════════════════════════════════════════════════════

        if (typeof AbsorptionZoneDetector !== 'undefined') {
            const absZones = AbsorptionZoneDetector.detect(to - 1, _priceAgg || {});

            for (const zone of absZones) {
                const yLo = priceConverter(zone.lo);
                const yHi = priceConverter(zone.hi);
                if (yLo === null || yHi === null || isNaN(yLo) || isNaN(yHi)) continue;

                // yLo > yHi because higher price = lower Y in canvas coords
                const yTop = Math.min(yLo, yHi);
                const yBot = Math.max(yLo, yHi);
                const bandH = Math.max(yBot - yTop, 4); // min 4px visibility

                // Alpha from zone score: logarithmic compression
                // totalScore 2-5 → dim, 10-20 → moderate, 40+ → strong
                const alphaRaw = Math.log(zone.totalScore + 1) / Math.log(50);
                const alpha = Math.min(Math.max(alphaRaw * 0.25, 0.03), 0.18);

                // Color: bid absorption (defending support) = cyan-green
                //        ask absorption (defending resistance) = red-magenta
                if (zone.side === 'bid') {
                    ctx.fillStyle = `rgba(0, 180, 140, ${alpha})`;
                } else {
                    ctx.fillStyle = `rgba(180, 40, 80, ${alpha})`;
                }

                ctx.fillRect(0, yTop - 2, mediaSize.width, bandH + 4);

                // Zone border lines (top and bottom of zone)
                const borderAlpha = Math.min(alpha * 2.5, 0.35);
                ctx.strokeStyle = zone.side === 'bid'
                    ? `rgba(0, 220, 160, ${borderAlpha})`
                    : `rgba(220, 50, 90, ${borderAlpha})`;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.beginPath();
                ctx.moveTo(0, yTop - 2);
                ctx.lineTo(mediaSize.width, yTop - 2);
                ctx.moveTo(0, yTop + bandH + 2);
                ctx.lineTo(mediaSize.width, yTop + bandH + 2);
                ctx.stroke();
                ctx.setLineDash([]);

                // Label at right edge
                if (zone.tier >= 1) {
                    const labelX = mediaSize.width - BUBBLE_CONFIG.CUML_DELTA_BAR_MAX_WIDTH
                        - BUBBLE_CONFIG.CUML_DELTA_RIGHT_MARGIN - 60;
                    const labelY = yTop + bandH / 2;
                    const labelText = `${zone.ticks}T ${zone.label}`;

                    ctx.font = zone.tier >= 2
                        ? 'bold 8px "JetBrains Mono", monospace'
                        : '7px "JetBrains Mono", monospace';
                    ctx.textAlign = 'right';
                    ctx.textBaseline = 'middle';

                    // Background pill
                    const tm = ctx.measureText(labelText);
                    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
                    ctx.beginPath();
                    ctx.roundRect(labelX - tm.width - 6, labelY - 7, tm.width + 10, 14, 3);
                    ctx.fill();

                    // Text
                    const textColor = zone.side === 'bid'
                        ? `rgba(0, 220, 160, ${Math.min(alpha * 6, 0.9)})`
                        : `rgba(220, 80, 120, ${Math.min(alpha * 6, 0.9)})`;
                    ctx.fillStyle = textColor;
                    ctx.fillText(labelText, labelX, labelY);
                    ctx.textAlign = 'center'; // reset
                }
            }
        }


        // ══════════════════════════════════════════════════════════════
        // RENDER LAYERS 1-6 (per-pane toggleable — bubbles)
        // ══════════════════════════════════════════════════════════════

        // Skip bubble rendering if toggled off for this pane
        const _drawBubbles = !_paneOverlay || _paneOverlay.bubbles;

        // Layer 0.25: Book imbalance gradient
        // Subtle vertical gradient showing gravity field — where bids outweigh asks.
        // >0.5 = bid-heavy (support below) = subtle green tint
        // <0.5 = ask-heavy (ceiling above) = subtle red tint
        {
            const halfBar = barSpacing * 0.45;
            const _tickSzImb = { NQ: 0.25, ES: 0.25, GC: 0.10 }[currentSymbol] || 0.25;
            const _refPriceImb = d.bars[from] && d.bars[from].originalData
                ? (d.bars[from].originalData.c || d.bars[from].originalData.close || 0) : 0;
            let tickPxImb = 8;
            if (_refPriceImb > 0) {
                const y1 = priceConverter(_refPriceImb);
                const y2 = priceConverter(_refPriceImb + _tickSzImb);
                if (y1 != null && y2 != null && !isNaN(y1) && !isNaN(y2)) {
                    tickPxImb = Math.max(Math.abs(y2 - y1), 2);
                }
            }

            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.book_imbalance) continue;
                const imbData = bar.originalData.book_imbalance;

                for (const priceStr in imbData) {
                    const ratio = imbData[priceStr];
                    const deviation = ratio - 0.5;
                    if (Math.abs(deviation) < 0.08) continue;

                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y == null || isNaN(y)) continue;

                    const alpha = Math.min(Math.abs(deviation) * 0.3, 0.12);

                    if (deviation > 0) {
                        ctx.fillStyle = `rgba(40, 180, 100, ${alpha})`;
                    } else {
                        ctx.fillStyle = `rgba(180, 40, 60, ${alpha})`;
                    }

                    ctx.fillRect(bar.x - halfBar, y - tickPxImb / 2, halfBar * 2, tickPxImb);
                }
            }
        }

        // Layer 0.5: Micro-OFI heatmap
        // Background gradient behind bubbles showing dealer positioning.
        // Green = passive bids loading (bullish underpinning)
        // Red   = passive asks stacking (bearish ceiling)
        {
            const halfBar = barSpacing * 0.45;
            const _tickSz = { NQ: 0.25, ES: 0.25, GC: 0.10 }[currentSymbol] || 0.25;
            const _refPrice = d.bars[from] && d.bars[from].originalData
                ? (d.bars[from].originalData.c || d.bars[from].originalData.close || 0) : 0;
            let tickPx = 8;
            if (_refPrice > 0) {
                const y1 = priceConverter(_refPrice);
                const y2 = priceConverter(_refPrice + _tickSz);
                if (y1 != null && y2 != null && !isNaN(y1) && !isNaN(y2)) {
                    tickPx = Math.max(Math.abs(y2 - y1), 2);
                }
            }

            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar || !bar.originalData || !bar.originalData.micro_ofi) continue;
                const ofiData = bar.originalData.micro_ofi;

                for (const priceStr in ofiData) {
                    const ofiVal = ofiData[priceStr];
                    const norm = Math.abs(ofiVal);
                    if (norm < 0.1) continue;

                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y == null || isNaN(y)) continue;

                    const alpha = Math.min(norm * 0.2, 0.3);

                    if (ofiVal > 0) {
                        ctx.fillStyle = `rgba(0, 200, 80, ${alpha})`;
                    } else {
                        ctx.fillStyle = `rgba(200, 40, 40, ${alpha})`;
                    }

                    ctx.fillRect(bar.x - halfBar, y - tickPx / 2, halfBar * 2, tickPx);
                }
            }
        }

        // Layer 1: Institutional glow rings
        if (_drawBubbles) for (const b of glowBubbles) {
            const glowR = b.radius + BUBBLE_CONFIG.GLOW_EXTRA_RADIUS;
            let glowColor = b.isAbsorb ? BUBBLE_CONFIG.GLOW_COLOR_ABSORB
                : b.buyVol >= b.sellVol ? BUBBLE_CONFIG.GLOW_COLOR_BUY
                : BUBBLE_CONFIG.GLOW_COLOR_SELL;
            const grad = ctx.createRadialGradient(b.x, b.y, b.radius, b.x, b.y, glowR);
            grad.addColorStop(0, glowColor);
            grad.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = grad;
            ctx.beginPath();
            ctx.arc(b.x, b.y, glowR, 0, Math.PI * 2);
            ctx.fill();
        }

        // Layer 2: Buy bubbles
        if (_drawBubbles) for (const b of buyBubbles) {
            ctx.fillStyle = _rgba(BUBBLE_CONFIG.BUY_COLOR, b.opacity);
            ctx.beginPath();
            ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
            ctx.fill();
        }

        // Layer 3: Sell bubbles
        if (_drawBubbles) for (const b of sellBubbles) {
            ctx.fillStyle = _rgba(BUBBLE_CONFIG.SELL_COLOR, b.opacity);
            ctx.beginPath();
            ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
            ctx.fill();
        }

        // Layer 4: Absorption bubbles with tier-based glow
        if (_drawBubbles) for (const b of absorbBubbles) {
            // V2: Absorption glow scales with aggregator tier
            if (b.absClass.tier >= 2) {
                const glowR = b.radius + 4 + b.absClass.glowIntensity * 6;
                // shadowBlur removed — forces GPU layer composite per stroke (300+ ops/frame)
                // Glow effect preserved via higher opacity on the fill itself.
                ctx.fillStyle = _rgba(BUBBLE_CONFIG.ABSORPTION_COLOR, Math.min(b.opacity * (1.2 + b.absClass.glowIntensity * 0.4), 0.95));
                ctx.beginPath();
                ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
                ctx.fill();
            } else {
                ctx.fillStyle = _rgba(BUBBLE_CONFIG.ABSORPTION_COLOR, b.opacity);
                ctx.beginPath();
                ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
                ctx.fill();
            }

            // Dual-color split ring
            if (b.radius >= 5) {
                ctx.lineWidth = 2;
                ctx.strokeStyle = _rgba(BUBBLE_CONFIG.BUY_COLOR, 0.8);
                ctx.beginPath();
                ctx.arc(b.x, b.y, b.radius + 1, Math.PI, 0);
                ctx.stroke();
                ctx.strokeStyle = _rgba(BUBBLE_CONFIG.SELL_COLOR, 0.8);
                ctx.beginPath();
                ctx.arc(b.x, b.y, b.radius + 1, 0, Math.PI);
                ctx.stroke();
            }
        }

        // Layer 5: Institutional border rings
        if (_drawBubbles) for (const b of glowBubbles) {
            let ringColor = b.isAbsorb ? _rgba(BUBBLE_CONFIG.ABSORPTION_COLOR, 0.9)
                : b.buyVol >= b.sellVol ? _rgba(BUBBLE_CONFIG.BUY_COLOR, 0.9)
                : _rgba(BUBBLE_CONFIG.SELL_COLOR, 0.9);
            ctx.strokeStyle = ringColor;
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.arc(b.x, b.y, b.radius + 2, 0, Math.PI * 2);
            ctx.stroke();
        }

        // Layer 5.5: Book depth rings (Upgrade A)
        // Ring thickness shows how much resting size was at the trade price.
        // Thick ring = traded into a wall (absorption). Thin/no ring = thin air (sweep).
        const MAX_BOOK_SIZE = { NQ: 500, ES: 2000, GC: 50 }[currentSymbol] || 500;
        const allBubbles = [...buyBubbles, ...sellBubbles, ...absorbBubbles];
        if (_drawBubbles) for (const b of allBubbles) {
            if (!b.bookSize || b.bookSize < 5) continue;
            const bookNorm = Math.min(b.bookSize / MAX_BOOK_SIZE, 1.0);
            const ringWidth = 1 + bookNorm * 7;
            const ringAlpha = 0.15 + bookNorm * 0.45;
            ctx.lineWidth = ringWidth;
            ctx.strokeStyle = `rgba(200, 210, 230, ${ringAlpha})`;
            ctx.beginPath();
            ctx.arc(b.x, b.y, b.radius + 3, 0, Math.PI * 2);
            ctx.stroke();
        }

        // Layer 5.75: Depth delta arrows (Upgrade B)
        // Up arrow (green) = passive bids loaded during candle (accumulation)
        // Down arrow (red) = passive orders pulled during candle (trap/exhaustion)
        if (_drawBubbles) for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData) continue;
            const depthDeltas = bar.originalData.depth_deltas;
            const bpData = bar.originalData.bp;
            if (!depthDeltas || !bpData) continue;

            for (const priceStr in depthDeltas) {
                if (!bpData[priceStr]) continue;
                const delta = depthDeltas[priceStr];
                const absDelta = Math.abs(delta);
                const arrowSize = Math.min(absDelta / 100, 1) * 12;
                if (arrowSize < 3) continue;

                const price = parseFloat(priceStr);
                if (isNaN(price)) continue;
                const y = priceConverter(price);
                if (y == null || isNaN(y)) continue;

                const bv = bpData[priceStr][0] || 0, sv = bpData[priceStr][1] || 0;
                const tv = bv + sv;
                if (tv < BUBBLE_CONFIG.MIN_BUBBLE_VOL) continue;
                const sd = AdaptiveKalmanThreshold.sigmaDistance(tv);
                const sr = Math.min(Math.pow(Math.max(sd, 0) / 4, 1.5), 1);
                const bRadius = BUBBLE_CONFIG.MIN_RADIUS
                    + sr * (BUBBLE_CONFIG.MAX_RADIUS - BUBBLE_CONFIG.MIN_RADIUS);

                const arrowX = bar.x + bRadius + 6;
                const arrowY = y;

                ctx.save();
                ctx.translate(arrowX, arrowY);

                if (delta > 0) {
                    ctx.fillStyle = 'rgba(0, 220, 100, 0.85)';
                    ctx.beginPath();
                    ctx.moveTo(0, -arrowSize);
                    ctx.lineTo(arrowSize * 0.6, arrowSize * 0.3);
                    ctx.lineTo(-arrowSize * 0.6, arrowSize * 0.3);
                    ctx.closePath();
                } else {
                    ctx.fillStyle = 'rgba(255, 60, 60, 0.85)';
                    ctx.beginPath();
                    ctx.moveTo(0, arrowSize);
                    ctx.lineTo(arrowSize * 0.6, -arrowSize * 0.3);
                    ctx.lineTo(-arrowSize * 0.6, -arrowSize * 0.3);
                    ctx.closePath();
                }

                ctx.fill();
                ctx.restore();
            }
        }

        // Layer 5.85: Depth velocity indicators
        // Fast loading (negative rate) = green dashed ring
        // Fast draining (positive rate) = red dashed ring
        if (_drawBubbles) for (let i = from; i < to; i++) {
            const bar = d.bars[i];
            if (!bar || !bar.originalData) continue;
            const velData = bar.originalData.depth_vel;
            const bpData = bar.originalData.bp;
            if (!velData || !bpData) continue;

            for (const priceStr in velData) {
                if (!bpData[priceStr]) continue;
                const rate = velData[priceStr];
                const absRate = Math.abs(rate);
                if (absRate < 10) continue;

                const price = parseFloat(priceStr);
                if (isNaN(price)) continue;
                const y = priceConverter(price);
                if (y == null || isNaN(y)) continue;

                const velNorm = Math.min(absRate / 100, 1.0);
                const velAlpha = 0.15 + velNorm * 0.5;
                const velRadius = 6 + velNorm * 8;

                ctx.save();
                ctx.setLineDash([2, 3]);
                ctx.lineWidth = 1.5;

                if (rate > 0) {
                    ctx.strokeStyle = `rgba(255, 80, 80, ${velAlpha})`;
                } else {
                    ctx.strokeStyle = `rgba(80, 255, 120, ${velAlpha})`;
                }

                ctx.beginPath();
                ctx.arc(bar.x, y, velRadius, 0, Math.PI * 2);
                ctx.stroke();
                ctx.setLineDash([]);
                ctx.restore();
            }
        }

        // Layer 6: Text labels
        if (_drawBubbles && labelBubbles.length > 0) {
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            for (const b of labelBubbles) {
                const label = b.totalVol >= 1000
                    ? (b.totalVol / 1000).toFixed(1) + 'k' : String(b.totalVol);
                ctx.font = b.isInstitutional ? BUBBLE_CONFIG.FONT : BUBBLE_CONFIG.FONT_SMALL;
                ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
                ctx.fillText(label, b.x + 1, b.y + 1);
                ctx.fillStyle = BUBBLE_CONFIG.TEXT_COLOR;
                ctx.fillText(label, b.x, b.y);

                // V2: Absorption tier label (replaces simple "ABS")
                if (b.isAbsorb && b.radius >= 10 && b.absClass.label) {
                    ctx.font = '7px "JetBrains Mono", monospace';
                    ctx.fillStyle = _rgba(BUBBLE_CONFIG.ABSORPTION_COLOR, 0.9);
                    ctx.fillText(b.absClass.label, b.x, b.y + b.radius + 8);
                }
            }
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 6.3: VOLUME RATE ACCELERATION (dV/dt)
        // Double-chevron marker when volume at a price level is
        // accelerating (ratio > 1.8). Signals active loading.
        // ══════════════════════════════════════════════════════════════

        if (_drawBubbles) {
            const allBubbles = [...buyBubbles, ...sellBubbles, ...absorbBubbles];
            for (const b of allBubbles) {
                if (b.dvdt < 1.8 || b.radius < 5) continue;

                const isBuy = b.buyVol >= b.sellVol;
                const chevronX = b.x + b.radius + 5;
                const chevronSize = Math.min(3 + (b.dvdt - 1.8) * 2, 8);

                // Intensity from acceleration ratio: 1.8→dim, 3.0+→bright
                const accelAlpha = Math.min(0.3 + (b.dvdt - 1.8) * 0.25, 0.9);
                const color = isBuy ? BUBBLE_CONFIG.BUY_COLOR : BUBBLE_CONFIG.SELL_COLOR;
                ctx.strokeStyle = _rgba(color, accelAlpha);
                ctx.lineWidth = 1.5;

                // Double chevron: ▸▸ (buy=up, sell=down)
                const dir = isBuy ? -1 : 1; // up for buy, down for sell
                for (let c = 0; c < 2; c++) {
                    const cy = b.y + dir * (c * chevronSize * 0.8);
                    ctx.beginPath();
                    ctx.moveTo(chevronX - chevronSize * 0.5, cy - dir * chevronSize * 0.5);
                    ctx.lineTo(chevronX, cy);
                    ctx.lineTo(chevronX - chevronSize * 0.5, cy + dir * chevronSize * 0.5);
                    ctx.stroke();
                }

                // Rate label for strong acceleration (3x+)
                if (b.dvdt >= 3.0 && b.radius >= 8) {
                    ctx.font = '7px "JetBrains Mono", monospace';
                    ctx.textAlign = 'left';
                    ctx.textBaseline = 'middle';
                    ctx.fillStyle = _rgba(color, accelAlpha);
                    ctx.fillText(`${b.dvdt.toFixed(1)}×`, chevronX + chevronSize, b.y);
                    ctx.textAlign = 'center'; // reset
                }
            }
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 6.5: HAWKES CLUSTER LINES (per-pane toggleable, follows bubbles)
        // Uses self-exciting intensity for detection + WLS for acceleration.
        // ══════════════════════════════════════════════════════════════

        if (_drawBubbles) for (const [priceStr, cluster] of hawkesClusters) {
            const price = parseFloat(priceStr);
            if (isNaN(price)) continue;
            const y = priceConverter(price);
            if (y === null || y === undefined || isNaN(y)) continue;

            const events = cluster.events;
            // FIX 8: Adaptive minimum event count — bar-spacing aware.
            // At scalp timeframes (30s, tight bars ≤8px): 3 rapid events = real institutional cluster.
            // At swing timeframes (1h, wide bars ≥20px): need ≥4 events (coincidence filter).
            // BUG 3 FIX: Was using target.mediaSize which is undefined outside the
            // useMediaCoordinateSpace callback. mediaSize is the destructured closure
            // parameter — already in scope here (this code IS inside that callback).
            const _visibleBars = to - from;
            const _chartWidth = mediaSize ? mediaSize.width : 1000;
            const _barPx = _visibleBars > 0 ? _chartWidth / _visibleBars : 10;
            const _minEvents = _barPx <= 8 ? 3 : 4; // scalp=3, swing=4
            if (events.length < _minEvents) continue;

            // Map bar indices to x coordinates
            const hitPoints = [];
            for (const ev of events) {
                const bar = d.bars[ev.bar];
                if (!bar) continue;
                hitPoints.push({ x: bar.x, vol: ev.vol, buy: ev.buy, sell: ev.sell });
            }
            if (hitPoints.length < 2) continue;

            // ── V2: Opacity from Hawkes intensity (not acceleration ratio) ──
            const lineAlpha = Math.min(0.15 + cluster.clusterStrength * 0.70, 0.90);

            // ── Color from dominant side ──
            const lineColor = cluster.totalBuy >= cluster.totalSell
                ? BUBBLE_CONFIG.BUY_COLOR : BUBBLE_CONFIG.SELL_COLOR;

            // ── V2: Variable-width segments (unchanged logic, better data) ──
            const vols = hitPoints.map(h => h.vol);
            const maxHitVol = Math.max(...vols);
            const minHitVol = Math.min(...vols);
            const volRange = maxHitVol - minHitVol || 1;

            // shadowBlur removed from inner segment loop — was causing 300+ GPU composites/frame.
            // Cluster lines are bold enough at full alpha without glow.
            ctx.save();

            for (let h = 0; h < hitPoints.length - 1; h++) {
                const h1 = hitPoints[h], h2 = hitPoints[h + 1];
                const segAvgVol = (h1.vol + h2.vol) / 2;
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

            // Dots at each hit
            for (const hit of hitPoints) {
                const dotRatio = (hit.vol - minHitVol) / volRange;
                const dotRadius = BUBBLE_CONFIG.CLUSTER_DOT_RADIUS + dotRatio * 1.5;
                ctx.fillStyle = _rgba(lineColor, Math.min(lineAlpha + 0.20, 0.92));
                ctx.beginPath();
                ctx.arc(hit.x, y, dotRadius, 0, Math.PI * 2);
                ctx.fill();
            }

            // ── Badge X clamped to left edge of CumlDelta strip ──
            // CumlDelta renders at rightX = mediaSize.width - 10.
            // Bars extend leftward by CUML_DELTA_BAR_MAX_WIDTH + CUML_DELTA_RIGHT_MARGIN.
            // Badge must not enter this reserved strip.
            const _cumlLeft = (mediaSize.width - 10)
                - BUBBLE_CONFIG.CUML_DELTA_BAR_MAX_WIDTH
                - BUBBLE_CONFIG.CUML_DELTA_RIGHT_MARGIN
                - 4;  // 4px breathing room between badge tail and bar edge
            const xEnd = hitPoints[hitPoints.length - 1].x;
            const bm0 = ctx.measureText(`${events.length}\u00d7`);
            const badgeX = Math.min(xEnd + 8, _cumlLeft - bm0.width - 6);
            ctx.font = BUBBLE_CONFIG.CLUSTER_BADGE_FONT;
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';

            const netDelta = cluster.totalBuy - cluster.totalSell;
            const deltaSign = netDelta >= 0 ? '+' : '';

            // ── V2.1: R²-gated acceleration arrow ──
            // accelerationRSquared is now exposed from HawkesClusterDetector.getCluster().
            // Only show direction arrow if R² >= 0.4.
            // Below 0.4, the WLS slope is fitting noise — arrow would be misleading.
            // Example: 3 events with random volumes → R² = 0.05 → no arrow
            //          6 events with clear ramp-up → R² = 0.72 → show ↑
            const rSquared = cluster.accelerationRSquared || 0;
            const accelArrow = rSquared >= 0.4
                ? (cluster.acceleration.direction === 'accelerating' ? ' ↑'
                   : cluster.acceleration.direction === 'decelerating' ? ' ↓' : '')
                : '';  // R² too low — slope is noise, suppress arrow

            const badge = `${events.length}× ${deltaSign}${netDelta}Δ${accelArrow}`;
            const badgeAlpha = Math.min(lineAlpha + 0.20, 0.92);

            // Background pill
            const bm = ctx.measureText(badge);
            ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
            ctx.beginPath();
            ctx.roundRect(badgeX - 3, y - 7, bm.width + 6, 14, 3);
            ctx.fill();

            ctx.fillStyle = _rgba(lineColor, badgeAlpha);
            ctx.fillText(badge, badgeX, y);
            ctx.textAlign = 'center';
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 7: CUMULATIVE DELTA SIDEBAR (per-pane toggleable)
        // ══════════════════════════════════════════════════════════════

        if (!_paneOverlay || _paneOverlay.cumlDelta) {
            CumlDeltaRenderer.render(
                ctx, cumlDelta, priceConverter, BUBBLE_CONFIG,
                mediaSize.width - 10, cumlMinVol
            );
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 8: ICEBERG DETECTIONS (per-pane toggleable)
        // Surfaces the backend's 30-field enriched iceberg intelligence.
        // ══════════════════════════════════════════════════════════════

        if (!_paneOverlay || _paneOverlay.iceberg) {
            IcebergVisualizer.render(ctx, d, priceConverter, BUBBLE_CONFIG);
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 9: SWEEP DETECTIONS (per-pane toggleable, follows iceberg)
        // Multi-level aggressive fills within 200ms.
        // ══════════════════════════════════════════════════════════════

        if (!_paneOverlay || _paneOverlay.iceberg) {
            SweepRenderer.render(ctx, d, priceConverter, BUBBLE_CONFIG);
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 10: SPOOF DETECTIONS (pulsing threat rings)
        // DOM orders appearing and vanishing before fills.
        // Backend: _detect_spoof() → bar.originalData.spoofs
        // ⚠ red ring = bid spoof (fake support), blue = ask spoof
        // ══════════════════════════════════════════════════════════════

        if (window.SpoofRenderer) {
            SpoofRenderer.render(ctx, d, priceConverter, BUBBLE_CONFIG);
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 11: MOMENTUM IGNITION (chevron cascade + ↩ TRAP)
        // Small-clip monotonic price stepping (algos marking the market).
        // Backend: _detect_ignition() → bar.originalData.ignition
        // ↑↑↑ green = up ignition, ↓↓↓ red = down ignition
        // ↩TRAP = confirmed reversal (failed ignition)
        // ══════════════════════════════════════════════════════════════

        if (window.IgnitionRenderer) {
            IgnitionRenderer.render(ctx, d, priceConverter, BUBBLE_CONFIG);
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 12: DELTA DIVERGENCE (hidden institutional pressure)
        // Price makes new high/low but delta diverges — smart money fading.
        // Backend: _detect_delta_divergence() → bar.originalData.delta_div
        // ↘DIV red = bearish (price up, delta down = hidden selling)
        // ↗DIV green = bullish (price down, delta up = hidden buying)
        // ══════════════════════════════════════════════════════════════

        if (window.DeltaDivergenceLayer) {
            DeltaDivergenceLayer.render(ctx, d, priceConverter, BUBBLE_CONFIG);
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 8.5: EXHAUSTION INDICATORS
        // Flow-price divergence + Hawkes decay + volume climax.
        // V2.1 FIX: Was dead code — now wired into pipeline.
        // ══════════════════════════════════════════════════════════════

        const exhaustedLevels = ExhaustionDetector.getExhaustedLevels(
            to - 1, hawkesClusters
        );

        for (const [priceStr, exh] of exhaustedLevels) {
            const price = parseFloat(priceStr);
            if (isNaN(price)) continue;
            const y = priceConverter(price);
            if (y === null || y === undefined || isNaN(y)) continue;

            // O(1) lookup from _priceBarX map (built in Phase 5 pre-pass).
            // Eliminates the original O(bars) reverse scan per exhaustion level.
            const exhX = (_priceBarX && _priceBarX[priceStr]) || null;
            if (exhX === null) continue;

            // ── Visual: desaturation ring + label ──
            const isBuyExhaustion = exh.side === 'buy_exhaustion';

            if (exh.tier >= 2) {
                // Dashed gray ring: radius + alpha clamped — score can exceed 1.0
                // (ExhaustionDetector intentionally allows > 1.0 on climax tier)
                // Unclamped: 10 + 1.5 * 4 = 16px radius, alpha 0.40 + 0.60 = 1.0 → visible artifact
                const _exhScore = Math.min(exh.score, 1.0);
                ctx.strokeStyle = `rgba(160, 160, 180, ${0.4 + _exhScore * 0.35})`;
                ctx.lineWidth = 2;
                ctx.setLineDash([3, 2]);
                ctx.beginPath();
                ctx.arc(exhX, y, 10 + _exhScore * 4, 0, Math.PI * 2);
                ctx.stroke();
                ctx.setLineDash([]);
            }

            // Label
            if (exh.label) {
                const labelColor = exh.tier >= 3
                    ? 'rgba(255, 200, 60, 0.95)'   // gold for CLIMAX
                    : exh.tier >= 2
                    ? 'rgba(200, 200, 220, 0.85)'   // silver for EXH
                    : 'rgba(160, 160, 180, 0.6)';   // dim for exh

                ctx.font = exh.tier >= 3
                    ? 'bold 8px "JetBrains Mono", monospace'
                    : '7px "JetBrains Mono", monospace';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'bottom';

                // Background pill
                const lbl = exh.label + (isBuyExhaustion ? '↓' : '↑');
                const bm = ctx.measureText(lbl);
                ctx.fillStyle = 'rgba(0, 0, 0, 0.65)';
                ctx.beginPath();
                ctx.roundRect(exhX - bm.width / 2 - 3, y - 22, bm.width + 6, 13, 3);
                ctx.fill();

                ctx.fillStyle = labelColor;
                ctx.fillText(lbl, exhX, y - 10);
            }
        }


        // ══════════════════════════════════════════════════════════════
        // Layer 9.5: Hawkes regime indicator badge
        // Uses backend Kirchner 2017 bivariate Hawkes (spectral radius ρ)
        // Renders regime badge at chart top-right showing cascade state
        {
            const lastBar = d.bars[to - 1];
            const hawkesState = lastBar && lastBar.originalData ? lastBar.originalData.hawkes : null;
            if (hawkesState && hawkesState.rho != null) {
                const rho = hawkesState.rho;
                const phase = hawkesState.phase || (rho >= 1 ? 'supercritical' : rho >= 0.8 ? 'near_critical' : 'subcritical');
                const sideDom = hawkesState.side_dominance || '';

                let phaseColor, phaseLabel;
                if (phase === 'supercritical' || rho >= 1.0) {
                    phaseColor = 'rgba(255, 60, 60, 0.9)';
                    phaseLabel = 'CASCADE';
                } else if (phase === 'near_critical' || rho >= 0.8) {
                    phaseColor = 'rgba(255, 180, 40, 0.85)';
                    phaseLabel = 'TRANSITION';
                } else {
                    phaseColor = 'rgba(60, 200, 120, 0.7)';
                    phaseLabel = 'SUBCRIT';
                }

                const sideArrow = sideDom === 'buy' ? '▲BUY' : sideDom === 'sell' ? '▼SELL' : '';

                // Render badge at top-right of chart area
                const badgeX = d.bars[to - 1] ? d.bars[to - 1].x - 10 : 100;
                const badgeY = 20;

                ctx.save();
                ctx.font = 'bold 10px monospace';
                const text = `ρ=${rho.toFixed(2)} ${phaseLabel} ${sideArrow}`;
                const textW = ctx.measureText(text).width;

                // Background pill
                ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
                const pad = 4;
                ctx.beginPath();
                ctx.roundRect(badgeX - textW - pad * 2, badgeY - 8, textW + pad * 4, 18, 4);
                ctx.fill();

                // Text
                ctx.fillStyle = phaseColor;
                ctx.fillText(text, badgeX - textW / 2 - pad, badgeY + 5);
                ctx.restore();
            }
        }

        // DIAGNOSTICS — window._v2Debug
        // Inspect from browser console: _v2Debug
        // Updates every frame so you can watch state evolve live.
        // ══════════════════════════════════════════════════════════════

        const _thresholds = AdaptiveKalmanThreshold._thresholds();
        // Reuse hawkesClusters from PHASE 2 — do NOT call getActiveClusters() again.
        // That function has prune side effects (deletes dead levels from _levels Map).
        // Calling it twice per frame causes cluster lines to disappear mid-render.
        const _topClusters = [];
        for (const [p, c] of hawkesClusters) {
            _topClusters.push({ price: p, lambda: +c.lambda.toFixed(3), strength: +c.clusterStrength.toFixed(2), events: c.events.length });
        }
        _topClusters.sort((a, b) => b.lambda - a.lambda);

        // ── V2.2: Determine signal sources for diagnostics ──
        const _bkSigDbg = window._v2Signals || {};
        const _bkKalmanDbg = _bkSigDbg.kalman;
        const _bkHawkesDbg = _bkSigDbg.hawkes;

        window._v2Debug = {
            symbol: _v2ActiveSymbol,
            frame: (window._v2Debug ? window._v2Debug.frame + 1 : 1),
            signalSource: _bkKalmanDbg && _bkKalmanDbg.ready ? 'backend' : 'local_fallback',
            kalman: {
                // Local stats
                mu: +AdaptiveKalmanThreshold._mu.toFixed(4),
                sigma: +Math.sqrt(Math.max(AdaptiveKalmanThreshold._var, 0)).toFixed(4),
                P_mu: +AdaptiveKalmanThreshold._P_mu.toFixed(6),
                sigThreshold: Math.round(_thresholds.sigThreshold),
                instThreshold: Math.round(_thresholds.instThreshold),
                // Backend stats (null = not yet warmed up)
                backend_snr: _bkKalmanDbg ? _bkKalmanDbg.snr : null,
                backend_K: _bkKalmanDbg ? _bkKalmanDbg.K : null,
                backend_ready: _bkKalmanDbg ? _bkKalmanDbg.ready : false,
            },
            hawkes: {
                // Local clusters
                activeClusters: hawkesClusters.size,
                totalLevels: HawkesClusterDetector._levels.size,
                top3: _topClusters.slice(0, 3),
                // Backend branching ratio
                backend_rho: _bkHawkesDbg ? _bkHawkesDbg.rho : null,
                backend_rho_std: _bkHawkesDbg ? _bkHawkesDbg.rho_std : null,
                backend_phase: _bkHawkesDbg ? _bkHawkesDbg.phase : 'no_data',
            },
            absorption: {
                trackedLevels: AbsorptionAggregator._scores.size,
                absBufferKeys: Object.keys(window._v2AbsBuffer || {}).length,
                absTiersActive: Object.keys(window.domSnapshotAbsTiers || {}).length,
                source: (window.domSnapshotAbsTiers && Object.keys(window.domSnapshotAbsTiers).length > 0)
                    ? 'backend_abs_tiers' : 'local_aggregator',
            },
            exhaustion: {
                trackedLevels: ExhaustionDetector._state.size,
            },
            // V3 Noise elimination counters — inspect live: _v2Debug.noiseFilter
            // target: total < 20 on normal tape, 30-40 max during FOMC/open
            noiseFilter: {
                buy:   buyBubbles.length,
                sell:  sellBubbles.length,
                walls: absorbBubbles.length,
                glow:  glowBubbles.length,
                total: buyBubbles.length + sellBubbles.length + absorbBubbles.length,
            },
            // TapeEWMA: live adaptive floor stats
            // _v2Debug.tapeFloor.floor = current adaptive contract minimum
            // _v2Debug.tapeFloor.ewmaMean = EWMA of recent print sizes
            tapeFloor: TapeEWMA.stats(),
        };


    }); } catch(e) { /* LWC not ready */ } // close useMediaCoordinateSpace

};  // close draw()

console.log('[V2.1+Fix1] Integration patch loaded. Patched: VolumeBubbleRenderer.draw()');
console.log('[V2.1+Fix1] Fix 1: 1.5σ hard floor + Wilson CI (95%) for all directional prints');
console.log('[V2.1+Fix1] Fix 1: Absorption walls tier≥2 bypass at 1.0σ (direction-agnostic)');
console.log('[V2.1+Fix1] Modules: Kalman, Hawkes, Absorption, Dominance, Decay, Regression, CumlDelta, Exhaustion, Iceberg, Sweep');
console.log('[V2.1+Fix1] Diagnostics: window._v2Debug | noise: window._v2Debug.noiseFilter');

})(); // end IIFE
