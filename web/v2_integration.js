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
 *   9. SweepRenderer            (lightning bolt multi-level sweeps)
 *  10. HawkesStateManager       (fixes per-frame reset bug)
 *
 * Load order: volume_bubbles.js → v2_sigma_engine.js → v2_integration.js
 */
(function() {
'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// PATCHED draw() — Replace VolumeBubbleRenderer.prototype.draw
// ═══════════════════════════════════════════════════════════════════════════

// ── Symbol change detection ──
// Tracks the active instrument so all modules can reset when it changes.
// d.symbol is passed by the chart host; falls back to first bar's 's' field
// or stays as the last known value.
let _v2ActiveSymbol = null;
// Chart absorption-bubble feature toggles (exposed on window for live tuning)
let _showAbsTag  = true;   // FORTRESS/SOLID/HELD text tag next to bubble
let _showRefillPip = true; // refill-class colored dot on bubble edge
let _showAbsZoneBand = true; // horizontal band across chart for multi-level clusters
// Live-tunable from console: window.V2ChartAbs.tag = false; etc.
window.V2ChartAbs = {
    get tag() { return _showAbsTag; },
    set tag(v) { _showAbsTag = !!v; },
    get refillPip() { return _showRefillPip; },
    set refillPip(v) { _showRefillPip = !!v; },
    get zoneBand() { return _showAbsZoneBand; },
    set zoneBand(v) { _showAbsZoneBand = !!v; },
};

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

// ── Toolbar badges: t-hawkes-rho, t-hawkes-dir, t-spread ─────────────────
// Hawkes branching ratio and side-dominance come from candle_enriched.hawkes
// (emitted 5Hz by l2_worker.HawkesBranchingRatio). Spread is derived
// client-side from l2_update top-of-book (best_bid / best_ask) so it doesn't
// depend on a backend-side spread field that never shipped.
if (typeof AltarisEvents !== 'undefined') {
    const _els = {
        rho:    document.getElementById('t-hawkes-rho'),
        dir:    document.getElementById('t-hawkes-dir'),
        spread: document.getElementById('t-spread'),
    };

    AltarisEvents.on('data:candles:enriched', (d) => {
        const h = d && d.hawkes;
        if (!h) return;

        if (_els.rho) {
            if (h.rho == null) {
                _els.rho.textContent = '—';
                _els.rho.style.color = '#888';
            } else {
                _els.rho.textContent = h.rho.toFixed(2);
                const c = h.phase === 'supercritical' ? '#ff3060'
                        : h.phase === 'subcritical'   ? '#4cd964'
                        : '#ff9500';
                _els.rho.style.color = c;
            }
        }

        if (_els.dir) {
            const sd = h.side_dominance;
            if (sd == null) {
                _els.dir.textContent = '—';
                _els.dir.style.color = '#888';
            } else {
                // side_dominance ∈ [-1, +1]. Threshold at ±0.15 for visible tilt.
                const pct = (sd * 100).toFixed(0);
                if (sd > 0.15)       { _els.dir.textContent = `▲ ${pct}`; _els.dir.style.color = '#2ee88a'; }
                else if (sd < -0.15) { _els.dir.textContent = `▼ ${pct}`; _els.dir.style.color = '#ff3060'; }
                else                 { _els.dir.textContent = `◆ ${pct}`; _els.dir.style.color = '#888';    }
            }
        }
    });

    // Spread: l2_update.dom.NQ.spread is pre-computed by l2_worker.
    AltarisEvents.on('data:l2:update', (data) => {
        if (!_els.spread) return;
        const nq = data && data.dom && data.dom.NQ;
        if (!nq || typeof nq.spread !== 'number') return;
        const spread = nq.spread;
        _els.spread.textContent = spread.toFixed(2);
        // NQ tick = 0.25. Tight = 1 tick, wide = >2 ticks.
        _els.spread.style.color = spread <= 0.25 ? '#4cd964'
                                : spread <= 0.50 ? '#ff9500'
                                : '#ff3060';
    });
}

// ── Inline _rgba safety ──
// _rgba() is defined in volume_bubbles.js. If load order breaks, we need
// a fallback so render calls don't throw. The original version has a
// cache (_rgbaCache), ours doesn't — but correctness > speed for the fallback.
if (typeof _rgba !== 'function') {
    window._rgba = (rgb, alpha) => `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

// ── Scroll-aware rendering: skip heavy computation during active scroll ──
// Detect scroll by comparing visible range between frames.
// If range changed since last frame → user is scrolling → skip.
let _v2ScrollActive = false;
let _v2ScrollTimer = 0;
let _v2LastFrom = -1;
let _v2LastTo = -1;

// Also listen to event bus (backup detection)
if (typeof AltarisEvents !== 'undefined') {
    AltarisEvents.on('chart:scroll', () => {
        _v2ScrollActive = true;
        clearTimeout(_v2ScrollTimer);
        _v2ScrollTimer = setTimeout(() => { _v2ScrollActive = false; }, 150);
    });
}

// ── Draw timing profiler: logs to console every 60 frames ──
let _drawTimings = { total: 0, phaseA: 0, phaseB: 0, render: 0, count: 0, cacheHits: 0 };

VolumeBubbleRenderer.prototype.draw = function(target, priceConverter) {
    const _t0 = performance.now();
    const d = this._data;
    if (!d || !d.bars || d.bars.length === 0) return;

    const { from, to } = d.visibleRange;
    const barSpacing = d.barSpacing || 6;
    const useDots = false;

    // Bubbles must render during scroll to stay glued to candles.
    // LWC calls draw() with fresh bar.x coordinates on every scroll frame.

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
    // When VP Intel pane is mounted, skip all bars/badges/arrows/text on chart — Intel has them
    const _vpIntelActive = typeof VolumeProfileOverlay !== 'undefined' && VolumeProfileOverlay.isIntelActive && VolumeProfileOverlay.isIntelActive();

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
        // Two-level cache: data signature (classification) + viewport signature (x/y coords)
        const _dataSig = `${from}:${to}:${_latestBar?.originalData?.ts||0}:${window._v2Signals?.ts||0}:${Object.keys(window._v2AbsBuffer||{}).length}:${_dynFloor}`;
        const _firstBarX = d.bars[from]?.x || 0;

        const _tPhaseA0 = performance.now();
        let glowBubbles, buyBubbles, sellBubbles, absorbBubbles, labelBubbles, _priceBarX, _priceAgg;

        if (_dataSig === this._v3sig && this._v3cache) {
            // ── Data cache hit: same bars, same data — just remap x/y coords ──
            _drawTimings.cacheHits++;
            ({ glowBubbles, buyBubbles, sellBubbles, absorbBubbles, labelBubbles, _priceBarX, _priceAgg } = this._v3cache);
            // Remap pixel coordinates from fresh bar positions + priceConverter
            if (this._v3firstBarX !== _firstBarX) {
                this._v3firstBarX = _firstBarX;
                // Build barIdx → bar.x lookup from current frame
                const _barXMap = {};
                for (let i = from; i < to; i++) {
                    if (d.bars[i]) _barXMap[i] = d.bars[i].x;
                }
                const _allBubbles = [...glowBubbles, ...buyBubbles, ...sellBubbles, ...absorbBubbles];
                for (const b of _allBubbles) {
                    if (b._barIdx !== undefined && _barXMap[b._barIdx] !== undefined) {
                        b.x = _barXMap[b._barIdx];
                    }
                    b.y = priceConverter(b.price) || b.y;
                }
            }
        } else {
            // ── Cache miss: full consolidation + classification ──
            this._v3sig = _dataSig;
            this._v3firstBarX = _firstBarX;
            glowBubbles = []; buyBubbles = []; sellBubbles = []; absorbBubbles = []; labelBubbles = []; _priceBarX = {};

            // ── PER-BAR PASS: Each bar gets its own bubbles at its own x position ──
            // Bubbles are glued to the candle they belong to.
            // Also build _priceAgg for absorption zone detection (cross-bar).
            _priceAgg = {};
            const _bkAbsTiersGlobal = window._v2AbsBuffer && window.domSnapshotAbsTiers;
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar?.originalData?.bp) continue;
                const bp = bar.originalData.bp;
                const barX = bar.x;

                // Clamp bubbles to within bar's OHLC range + margin
                // Prevents DOM book levels from rendering bubbles far from actual price action
                const _barHigh = bar.originalData.h || bar.originalData.high || Infinity;
                const _barLow = bar.originalData.l || bar.originalData.low || -Infinity;
                const _barRange = _barHigh - _barLow;
                const _barMargin = Math.max(_barRange * 0.5, 2.0); // 50% of bar range or 2 pts min

                for (const priceStr in bp) {
                    const entry = bp[priceStr];
                    const bv = entry[0] || 0, sv = entry[1] || 0;
                    const tv = bv + sv;
                    if (tv < 1) continue;
                    if (tv < _dynFloor) continue;

                    // Skip price levels far from the candle's actual range
                    const _p = parseFloat(priceStr);
                    if (_p > _barHigh + _barMargin || _p < _barLow - _barMargin) continue;

                    // Cross-bar agg for absorption zones (Layer 0)
                    if (!_priceAgg[priceStr]) {
                        _priceAgg[priceStr] = { buyVol: 0, sellVol: 0, maxSigma: 0, barX, lastBarIdx: i, newestBarIdx: i, firstBarIdx: i, barCount: 0, barVols: [] };
                    }
                    const agg = _priceAgg[priceStr];
                    agg.buyVol += bv;
                    agg.sellVol += sv;
                    agg.barCount++;
                    agg.barVols.push({ idx: i, vol: tv, buy: bv, sell: sv });
                    if (i > agg.newestBarIdx) agg.newestBarIdx = i;
                    if (i < agg.firstBarIdx) agg.firstBarIdx = i;
                    _priceBarX[priceStr] = barX;

                    // Feed ExhaustionDetector
                    const _bSig = AdaptiveKalmanThreshold.sigmaDistance(tv);
                    if (_bSig > agg.maxSigma) { agg.maxSigma = _bSig; agg.barX = barX; agg.lastBarIdx = i; }
                    const _bClose = bar.originalData.c || bar.originalData.close || 0;
                    ExhaustionDetector.update(priceStr, i, _bSig, bv, sv, _bClose);

                    // ── Per-bar bubble classification ──
                    const sigmaDistance = AdaptiveKalmanThreshold.sigmaDistance(tv);
                    const domResult = AdaptiveDominance.test(bv, sv);
                    const isBuy = bv >= sv;
                    const isInstitutional = tv >= instThreshold;

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

                    // Per-bar gate: use TapeEWMA floor (adapts to current tape speed)
                    // absorbMinVol (1.0σ global) is too high for quiet bars
                    // _dynFloor auto-adapts: 2-3 lots off-hours, 60+ lots at RTH open
                    if (tv < _dynFloor * 2) continue;

                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;

                    // Opacity: per-bar needs higher base since individual bar σ is lower
                    const recencyWeight = TemporalDecay.weight(i, from, to);
                    let opacity = 0.25 + Math.min(sigmaDistance * 0.15, 0.5);
                    const conviction = AdaptiveDominance.convictionStrength(bv, sv);
                    opacity += conviction * 0.1;
                    if (absClass.tier >= 2) opacity += 0.15;
                    opacity = Math.min(opacity * Math.max(recencyWeight, 0.4), 0.92);

                    // Radius: per-bar σ-scaled with higher floor
                    const sigmaRatio = Math.min(Math.max(sigmaDistance, 0) / 3, 1);
                    const radius = 5 + sigmaRatio * (BUBBLE_CONFIG.MAX_RADIUS - 5);

                    const _bpEntry = entry;
                    const bookSize = (_bpEntry && _bpEntry[4] != null) ? _bpEntry[4] : 0;
                    const bubble = { x: barX, y, price, radius, totalVol: tv, buyVol: bv, sellVol: sv, opacity, isAbsorb, isInstitutional, absClass, priceStr, dvdt: 0, bookSize, _barIdx: i };

                    if (isInstitutional) glowBubbles.push(bubble);
                    // Purple absorption bubbles removed — entropy-based, not real L2 absorption.
                    // All bubbles route to buy/sell by dominant side.
                    if (isBuy) buyBubbles.push(bubble);
                    else sellBubbles.push(bubble);
                    if (radius >= 7) labelBubbles.push(bubble);
                }
            }

            // ── Top-N cap: keep only highest-conviction bubbles ──
            // Hard ceiling prevents extreme-tape charts from overloading.
            // Sorted by totalVol descending (proxy for conviction).
            const _cap = (arr, n) => {
                if (arr.length <= n) return arr;
                arr.sort((a, b) => b.totalVol - a.totalVol);
                return arr.slice(0, n);
            };
            buyBubbles    = _cap(buyBubbles, 40);
            sellBubbles   = _cap(sellBubbles, 40);
            absorbBubbles = _cap(absorbBubbles, 20);
            // Keep glow/label only for bubbles that made the cut
            const _kept = new Set([...buyBubbles, ...sellBubbles, ...absorbBubbles]);
            glowBubbles  = glowBubbles.filter(b => _kept.has(b));
            labelBubbles = labelBubbles.filter(b => _kept.has(b));

            // Store in cache for next frame — include _priceBarX so Layer 8.5
            // can O(1) resolve the rightmost bar X for any price level.
            this._v3cache = { glowBubbles, buyBubbles, sellBubbles, absorbBubbles, labelBubbles, _priceBarX, _priceAgg };
            this._v3sig_prev = _dataSig;
        }


        // LAYER 0: ABSORPTION ZONES — REMOVED (weak abs_ratio signal, visual noise)


        // ══════════════════════════════════════════════════════════════
        // RENDER LAYERS 1-6 (per-pane toggleable — bubbles)
        // ══════════════════════════════════════════════════════════════

        // Skip bubble rendering if toggled off for this pane
        const _drawBubbles = !_paneOverlay || _paneOverlay.bubbles;

        // LAYER 0.25: BOOK IMBALANCE GRADIENT — REMOVED (DOM shifts every tick, visual noise)

        // LAYER 0.5: MICRO-OFI HEATMAP — REMOVED (per-price coloring, visual noise)

        const _tPhaseA1 = performance.now();
        _drawTimings.phaseA += (_tPhaseA1 - _tPhaseA0);

        // ══════════════════════════════════════════════════════════════
        // LAYER 1: VOLUME BUBBLES — clean green/red circles at >2σ
        // Shows where significant trade volume happened on each candle.
        // Green = buy dominant, red = sell dominant. Size = σ distance.
        // ══════════════════════════════════════════════════════════════

        if (_drawBubbles && buyBubbles.length + sellBubbles.length > 0) {
            ctx.save();
            // Buy bubbles (green)
            for (const b of buyBubbles) {
                if (b.y === null || b.y === undefined || isNaN(b.y)) continue;
                if (b.y < -10 || b.y > mediaSize.height + 10) continue;
                ctx.fillStyle = `rgba(0, 230, 118, ${b.opacity * 0.7})`;
                ctx.beginPath();
                ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
                ctx.fill();
                // Border
                ctx.strokeStyle = `rgba(0, 200, 83, ${Math.min(b.opacity + 0.1, 0.9)})`;
                ctx.lineWidth = 1;
                ctx.stroke();
            }
            // Sell bubbles (red)
            for (const b of sellBubbles) {
                if (b.y === null || b.y === undefined || isNaN(b.y)) continue;
                if (b.y < -10 || b.y > mediaSize.height + 10) continue;
                ctx.fillStyle = `rgba(255, 23, 68, ${b.opacity * 0.7})`;
                ctx.beginPath();
                ctx.arc(b.x, b.y, b.radius, 0, Math.PI * 2);
                ctx.fill();
                ctx.strokeStyle = `rgba(213, 0, 0, ${Math.min(b.opacity + 0.1, 0.9)})`;
                ctx.lineWidth = 1;
                ctx.stroke();
            }
            // Glow for institutional (>3σ)
            for (const b of glowBubbles) {
                if (b.y === null || b.y === undefined || isNaN(b.y)) continue;
                const isBuy = b.buyVol >= b.sellVol;
                ctx.fillStyle = isBuy
                    ? `rgba(0, 230, 118, ${b.opacity * 0.12})`
                    : `rgba(255, 23, 68, ${b.opacity * 0.12})`;
                ctx.beginPath();
                ctx.arc(b.x, b.y, b.radius + 3, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.restore();
        }

        // WebGL frame for overlays
        const _useWebGL = _drawBubbles && typeof WebGLOverlay !== 'undefined' && WebGLOverlay.isReady();
        if (_useWebGL) {
            WebGLOverlay.beginFrame();
            WebGLOverlay.flush();
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 8: ABSORPTION BUBBLES (L2 DOM snapshot vs trade tape)
        //
        // Data source: bar.originalData.absorption from l2_worker.py v2 engine
        // Each entry is a CLUSTER anchor (adjacent levels pre-merged by backend).
        //
        // Tiering (from refill_ratio = refilled / traded):
        //   FORTRESS (tier 3) — refill ≥ 0.7, traded ≥ 100 → bright glow, large
        //   SOLID    (tier 2) — refill ≥ 0.5, traded ≥ 50  → medium glow
        //   HELD     (tier 1) — refill ≥ 0.3, traded ≥ 30  → basic bubble
        //   FAKE     (fake=true) — pull_ratio ≥ 0.6       → dashed red X (spoof)
        //
        // Color:
        //   bid side = cyan (passive buyers absorbing)
        //   ask side = magenta (passive sellers absorbing)
        // Contract count shown inside bubble (total_traded).
        // ══════════════════════════════════════════════════════════════

        if (_drawBubbles) {
            // Compute visible price range from all bars (clamp absorption to candle range)
            let _visHigh = -Infinity, _visLow = Infinity;
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar?.originalData) continue;
                const h = bar.originalData.h || bar.originalData.high;
                const l = bar.originalData.l || bar.originalData.low;
                if (h > _visHigh) _visHigh = h;
                if (l < _visLow) _visLow = l;
            }
            const _visRange = _visHigh - _visLow;
            const _visMargin = Math.max(_visRange * 0.15, 2.0);

            // Collect all absorption entries across visible bars, dedupe by price
            // (latest bar wins for each price level since absorption is cumulative)
            const _absMap = {}; // {priceStr: {data, barX, barIdx}}
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar?.originalData?.absorption) continue;
                const abs = bar.originalData.absorption;
                for (const priceStr in abs) {
                    const entry = abs[priceStr];
                    if (!entry) continue;
                    // Gate: tier >= 1 OR explicitly flagged fake (show spoofs too)
                    if (!entry.fake && (entry.tier || 0) < 1) continue;
                    // Clamp to visible candle range — absorption data is global, skip far prices
                    const p = parseFloat(priceStr);
                    if (isFinite(_visHigh) && isFinite(_visLow)) {
                        if (p > _visHigh + _visMargin || p < _visLow - _visMargin) continue;
                    }
                    _absMap[priceStr] = { data: entry, barX: bar.x, barIdx: i };
                }
            }

            const _absEntries = Object.values(_absMap);

            // ── Bar gap — horizontal offset so bubbles sit to the RIGHT of the
            // candle body, never on top of it. Computed once per frame from
            // the visible bar spacing. Fallback to 8px when only one bar visible.
            let _barGap = 8;
            if (d.bars.length >= 2) {
                const _b0 = d.bars[0]?.x || 0;
                const _b1 = d.bars[1]?.x || 0;
                const _dx = Math.abs(_b1 - _b0);
                if (_dx > 0) _barGap = Math.max(_dx * 0.55, 8);
            }

            // ── Horizontal zone bands — multi-level cluster defense zones ──
            // Faint tinted band across the full chart width at each cluster's
            // price span, so traders can see candles approaching / inside a
            // defended zone (the most valuable MM signal when scanning).
            // Skipped when VP Intel pane owns the zone-band rendering (with range label).
            const _vpIntelActiveZ = (typeof VolumeProfileOverlay !== 'undefined' && typeof VolumeProfileOverlay.isIntelActive === 'function') ? VolumeProfileOverlay.isIntelActive() : false;
            if (_showAbsZoneBand && !_vpIntelActiveZ && _absEntries.length > 0) {
                ctx.save();
                for (const { data: abs } of _absEntries) {
                    if (abs.fake) continue;
                    const cs = abs.cluster_size || 1;
                    if (cs < 2) continue; // single-level = no band
                    const prices = abs.cluster_prices || [];
                    if (prices.length < 2) continue;
                    let pLo = Infinity, pHi = -Infinity;
                    for (const p of prices) {
                        if (p < pLo) pLo = p;
                        if (p > pHi) pHi = p;
                    }
                    const yLo = priceConverter(pLo);
                    const yHi = priceConverter(pHi);
                    if (yLo == null || yHi == null || isNaN(yLo) || isNaN(yHi)) continue;
                    const top = Math.min(yLo, yHi);
                    const bandH = Math.max(Math.abs(yLo - yHi) + 3, 4);
                    const tier = abs.tier || 0;
                    const isBid = abs.side === 'bid';
                    const rgb = isBid ? [0, 220, 240] : [240, 60, 180];
                    const fillAlpha = tier >= 3 ? 0.09 : tier >= 2 ? 0.06 : 0.04;
                    const lineAlpha = tier >= 3 ? 0.35 : tier >= 2 ? 0.25 : 0.18;
                    // Tint band
                    ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${fillAlpha})`;
                    ctx.fillRect(0, top, mediaSize.width, bandH);
                    // Top/bottom edge lines (thin)
                    ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${lineAlpha})`;
                    ctx.lineWidth = 0.5;
                    ctx.setLineDash([4, 3]);
                    ctx.beginPath();
                    ctx.moveTo(0, top); ctx.lineTo(mediaSize.width, top);
                    ctx.moveTo(0, top + bandH); ctx.lineTo(mediaSize.width, top + bandH);
                    ctx.stroke();
                    ctx.setLineDash([]);
                }
                ctx.restore();
            }

            if (_absEntries.length > 0) {
                // Sort by tier DESC so FORTRESS > SOLID > HELD render last
                // and the vertical dedup keeps the strongest tag.
                const _sortedAbs = Object.entries(_absMap).sort((a, b) => {
                    return (b[1].data.tier || 0) - (a[1].data.tier || 0);
                });
                let _lastAbsTagY = -Infinity;
                ctx.save();
                for (const [priceStr, entry] of _sortedAbs) {
                    const { data: abs, barX } = entry;

                    const price = parseFloat(priceStr);
                    if (isNaN(price)) continue;
                    const y = priceConverter(price);
                    if (y === null || y === undefined || isNaN(y)) continue;
                    if (y < -10 || y > mediaSize.height + 10) continue;

                    const tier = abs.tier || 0;
                    const isFake = !!abs.fake;
                    const isBid = abs.side === 'bid';

                    // Bubble offset — sit to the right of the candle body.
                    // Radius is computed below; use generous r≈15 hint for right-edge skip.
                    const bubbleX = barX + _barGap;
                    // Right-edge skip — better to drop the bubble than stomp the axis.
                    if (bubbleX > mediaSize.width - 18) continue;

                    // ── FAKE walls: dashed red X (spoof warning) ──
                    if (isFake) {
                        const sz = 7;
                        ctx.strokeStyle = 'rgba(255,80,80,0.75)';
                        ctx.lineWidth = 1.5;
                        ctx.setLineDash([3, 2]);
                        ctx.beginPath();
                        ctx.moveTo(bubbleX - sz, y - sz); ctx.lineTo(bubbleX + sz, y + sz);
                        ctx.moveTo(bubbleX + sz, y - sz); ctx.lineTo(bubbleX - sz, y + sz);
                        ctx.stroke();
                        ctx.setLineDash([]);
                        continue;
                    }

                    // Color: cyan for bid (passive buyers), magenta for ask (passive sellers)
                    const rgb = isBid ? [0, 220, 240] : [240, 60, 180];

                    // Radius: tier-scaled, HELD=6, SOLID=9, FORTRESS=13
                    const baseR = tier >= 3 ? 13 : tier >= 2 ? 9 : 6;
                    // Boost by refill_ratio conviction (up to +2px)
                    const refill = abs.refill_ratio || 0;
                    const r = baseR + Math.min(refill * 2, 2);

                    // Opacity: tier-based
                    const alpha = tier >= 3 ? 0.90 : tier >= 2 ? 0.75 : 0.55;

                    // ── FORTRESS: pulsing outer ring ──
                    if (tier >= 3) {
                        ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.12)`;
                        ctx.beginPath();
                        ctx.arc(bubbleX, y, r + 5, 0, Math.PI * 2);
                        ctx.fill();
                    }

                    // ── SOLID/FORTRESS: inner glow ──
                    if (tier >= 2) {
                        ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha * 0.12})`;
                        ctx.beginPath();
                        ctx.arc(bubbleX, y, r + 2, 0, Math.PI * 2);
                        ctx.fill();
                    }

                    // Filled bubble — translucent so candles show through
                    ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha * 0.35})`;
                    ctx.beginPath();
                    ctx.arc(bubbleX, y, r, 0, Math.PI * 2);
                    ctx.fill();

                    // Border — brightened to compensate for translucent fill
                    ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${Math.min(alpha + 0.30, 0.98)})`;
                    ctx.lineWidth = tier >= 3 ? 2 : tier >= 2 ? 1.5 : 1;
                    ctx.stroke();

                    // ── Refill-class pip (colored dot on NE edge of bubble) ──
                    // Derive from refill_ratio: ≥0.8 instant (green), ≥0.5 fast (yellow), else slow (red)
                    if (_showRefillPip && tier >= 1) {
                        const rr = abs.refill_ratio || 0;
                        const pipColor = rr >= 0.8 ? 'rgba(40,255,140,0.95)' :
                                         rr >= 0.5 ? 'rgba(255,220,40,0.92)' :
                                                     'rgba(255,80,80,0.88)';
                        const pipR = Math.max(2, Math.min(r * 0.28, 3.5));
                        // NE edge, offset along 45°
                        const off = r * 0.72;
                        ctx.fillStyle = pipColor;
                        ctx.beginPath();
                        ctx.arc(bubbleX + off, y - off, pipR, 0, Math.PI * 2);
                        ctx.fill();
                        // Thin outline for contrast
                        ctx.strokeStyle = 'rgba(0,0,0,0.45)';
                        ctx.lineWidth = 0.75;
                        ctx.stroke();
                    }

                    // ── FORTRESS/SOLID/HELD text tag on LEFT edge column ──
                    // 14px vertical dedup — sorted by tier DESC above, so the
                    // strongest bubble in a vertical neighborhood keeps the tag.
                    // Skipped when VP Intel pane owns the pill badge rendering.
                    const _vpIntelActive = (typeof VolumeProfileOverlay !== 'undefined' && typeof VolumeProfileOverlay.isIntelActive === 'function') ? VolumeProfileOverlay.isIntelActive() : false;
                    if (_showAbsTag && !_vpIntelActive && tier >= 1 && Math.abs(y - _lastAbsTagY) >= 14) {
                        const tagText = tier >= 3 ? 'FORTRESS' : tier >= 2 ? 'SOLID' : 'HELD';
                        const tagAlpha = tier >= 3 ? 0.95 : tier >= 2 ? 0.80 : 0.60;
                        ctx.save();
                        ctx.font = `bold ${tier >= 3 ? 8 : 7}px "JetBrains Mono", monospace`;
                        const tm = ctx.measureText(tagText);
                        const tagX = 4; // Left margin
                        const tagY = y;
                        const tagW = tm.width + 6;
                        // Pill background
                        ctx.fillStyle = 'rgba(0,0,0,0.72)';
                        ctx.fillRect(tagX - 1, tagY - 6, tagW, 12);
                        // Border matches bubble color
                        ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${tagAlpha})`;
                        ctx.lineWidth = 0.75;
                        ctx.strokeRect(tagX - 1, tagY - 6, tagW, 12);
                        // Text
                        ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${Math.min(tagAlpha + 0.05, 1)})`;
                        ctx.textAlign = 'left';
                        ctx.textBaseline = 'middle';
                        ctx.fillText(tagText, tagX + 2, tagY);
                        // Leader dot bridging pill → bubble
                        const leaderFromX = tagX + tagW + 3;
                        const leaderToX = bubbleX - r - 2;
                        if (leaderToX > leaderFromX + 6) {
                            ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${tagAlpha * 0.45})`;
                            ctx.lineWidth = 0.5;
                            ctx.setLineDash([2, 3]);
                            ctx.beginPath();
                            ctx.moveTo(leaderFromX, tagY);
                            ctx.lineTo(leaderToX, tagY);
                            ctx.stroke();
                            ctx.setLineDash([]);
                        }
                        ctx.restore();
                        _lastAbsTagY = y;
                    }

                    // Cluster indicator: small dash above bubble if multi-level
                    if ((abs.cluster_size || 1) > 1) {
                        ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.85)`;
                        ctx.lineWidth = 1.5;
                        ctx.beginPath();
                        const dashY = y - r - 3;
                        ctx.moveTo(bubbleX - r * 0.5, dashY);
                        ctx.lineTo(bubbleX + r * 0.5, dashY);
                        ctx.stroke();
                    }

                    // Contract count: use total_traded (the real volume that hit this level)
                    const contracts = abs.total_traded || (abs.buy_vol + abs.sell_vol) || 0;
                    if (contracts > 0 && r >= 6) {
                        ctx.font = `${Math.max(8, Math.min(r * 0.85, 12))}px "JetBrains Mono", monospace`;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        ctx.fillStyle = 'rgba(255,255,255,0.95)';
                        const txt = contracts >= 1000 ? (contracts / 1000).toFixed(1) + 'k' : String(contracts);
                        ctx.fillText(txt, bubbleX, y);
                    }
                }
                ctx.restore();
            }
        }


        // ══════════════════════════════════════════════════════════════
        // LAYER 10: EXHAUSTION BUBBLES
        //
        // When depth_deltas shows net negative (orders pulled > loaded)
        // at the candle's close price, the candle is exhausting.
        // Render a ring bubble (hollow, to distinguish from absorption fill).
        //
        // Purple ring = buy exhaustion (buyers drying up → short signal)
        // Orange ring = sell exhaustion (sellers drying up → long signal)
        //
        // Size scales with |netDelta|. Ring is placed at the price level
        // with the strongest pull (dominant_price), or candle close as fallback.
        // ══════════════════════════════════════════════════════════════

        if (_drawBubbles) {
            // Recompute bar gap for this block (separate scope from Layer 9).
            let _exhBarGap = 8;
            if (d.bars.length >= 2) {
                const _b0 = d.bars[0]?.x || 0;
                const _b1 = d.bars[1]?.x || 0;
                const _dx = Math.abs(_b1 - _b0);
                if (_dx > 0) _exhBarGap = Math.max(_dx * 0.55, 8);
            }
            ctx.save();
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar?.originalData) continue;
                const deltas = bar.originalData.depth_deltas;
                if (!deltas) continue;

                const close = bar.originalData.c || bar.originalData.close;
                const open = bar.originalData.o || bar.originalData.open;
                if (!close || !open) continue;

                // Sum all depth deltas for this bar + track dominant-pull price
                let netDelta = 0;
                let deltaCount = 0;
                let dominantPrice = null;
                let dominantMag = 0;
                for (const pk in deltas) {
                    const dv = deltas[pk];
                    netDelta += dv;
                    deltaCount++;
                    if (Math.abs(dv) > dominantMag) {
                        dominantMag = Math.abs(dv);
                        dominantPrice = parseFloat(pk);
                    }
                }
                if (deltaCount === 0) continue;

                // Noise gate
                const avgDelta = Math.abs(netDelta) / deltaCount;
                if (avgDelta < 10) continue;

                const isBullCandle = close >= open;
                const isBuyExhaustion = isBullCandle && netDelta < -50;
                const isSellExhaustion = !isBullCandle && netDelta > 50;
                if (!isBuyExhaustion && !isSellExhaustion) continue;

                // Place bubble at dominant-pull price (fallback: close)
                const bubblePrice = (dominantPrice != null && isFinite(dominantPrice)) ? dominantPrice : close;
                const y = priceConverter(bubblePrice);
                if (y == null || isNaN(y)) continue;
                if (y < -10 || y > mediaSize.height + 10) continue;

                // Size scales with |netDelta| — larger pull = larger ring
                // Base 6px, +1px per 50 lots, capped at 14px
                const r = Math.min(14, 6 + Math.abs(netDelta) / 50);

                // Offset bubble to the right of the candle body
                const bubbleX = bar.x + _exhBarGap;
                // Right-edge skip — drop off-screen bubbles rather than stomp axis
                if (bubbleX > mediaSize.width - r - 4) continue;

                // Color: purple for buy exhaustion, orange for sell exhaustion
                const rgb = isBuyExhaustion ? [160, 80, 220] : [255, 160, 40];
                const alpha = Math.min(0.90, 0.45 + Math.abs(netDelta) / 400);

                // ── Outer faint fill (ring halo) ──
                ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha * 0.15})`;
                ctx.beginPath();
                ctx.arc(bubbleX, y, r + 2, 0, Math.PI * 2);
                ctx.fill();

                // ── Ring outline (hollow — distinguishes from absorption) ──
                ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha})`;
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.arc(bubbleX, y, r, 0, Math.PI * 2);
                ctx.stroke();

                // ── Inner dot marker ──
                ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha * 0.85})`;
                ctx.beginPath();
                ctx.arc(bubbleX, y, Math.max(2, r * 0.30), 0, Math.PI * 2);
                ctx.fill();

                // ── Directional tail arrow (down for buy exh, up for sell exh) ──
                ctx.strokeStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha * 0.9})`;
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                if (isBuyExhaustion) {
                    // ▼ short-bias arrow below ring
                    ctx.moveTo(bubbleX - 3, y + r + 2);
                    ctx.lineTo(bubbleX,     y + r + 6);
                    ctx.lineTo(bubbleX + 3, y + r + 2);
                } else {
                    // ▲ long-bias arrow above ring
                    ctx.moveTo(bubbleX - 3, y - r - 2);
                    ctx.lineTo(bubbleX,     y - r - 6);
                    ctx.lineTo(bubbleX + 3, y - r - 2);
                }
                ctx.stroke();
            }
            ctx.restore();
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


    }); } catch(e) { console.error('[V3-draw] error:', e); } // close useMediaCoordinateSpace

    // Profiler removed — console.log in draw() causes DevTools repaint lag

};  // close draw()

console.log('[V2.1+Fix1] Integration patch loaded. Patched: VolumeBubbleRenderer.draw()');
console.log('[V2.1+Fix1] Fix 1: 1.5σ hard floor + Wilson CI (95%) for all directional prints');
console.log('[V2.1+Fix1] Fix 1: Absorption walls tier≥2 bypass at 1.0σ (direction-agnostic)');
console.log('[V2.1+Fix1] Modules: Kalman, Hawkes, Absorption, Dominance, Decay, Regression, CumlDelta, Exhaustion, Sweep');
console.log('[V2.1+Fix1] Diagnostics: window._v2Debug | noise: window._v2Debug.noiseFilter');

})(); // end IIFE
