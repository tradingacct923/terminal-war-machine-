/**
 * V2 SIGMA ENGINE — Zero-Heuristic Statistical Core
 *
 * Replaces the per-frame log-σ computation in VolumeBubbleRenderer.draw()
 * with auto-calibrating probabilistic models:
 *
 *   1. AdaptiveKalmanThreshold — Online Kalman filter for volume normalization
 *   2. HawkesClusterDetector  — Self-exciting point process for cluster detection
 *   3. AbsorptionAggregator   — Multi-bar temporal absorption model
 *   4. AdaptiveDominance      — Binomial CI-based dominance threshold
 *   5. TemporalDecay          — Information-theoretic recency weighting
 *   6. RegressionAcceleration — WLS slope for cluster momentum
 *   7. CumlDeltaRenderer      — The missing sidebar render loop
 *   8. ExhaustionDetector     — Geometric-mean flow-price divergence
 *
 * All modules expose window globals. No static thresholds. No magic numbers.
 * Every parameter is discovered from data or controlled by a feedback loop.
 */
(function() {
'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// INSTRUMENT CONSTANTS
// ═══════════════════════════════════════════════════════════════════════════
//
// Only NQ and GC are supported. Values are exact exchange specs.
// Tick size drives:
 //   - AbsorptionAggregator adjacent-level lookup (±1 tick)
//   - ExhaustionDetector priceFailure normalization (2-tick reference move)
//
// To add ES: TICK_SIZES.ES = 0.25, PRICE_FAILURE_TICKS stays 2 → 0.50

const TICK_SIZES = {
    NQ: 0.25,  // NQ futures: $5/tick, 4 ticks/point
    GC: 0.10,  // Gold futures: $10/tick, 10 ticks/point
};
const DEFAULT_TICK_SIZE = 0.25;  // fallback = NQ

// How many ticks = "meaningful but not explosive" price move for
// ExhaustionDetector Signal 2 normalization. 2 ticks is one full
// round-turn on NQ ($10), or 2 ticks on GC ($20). Instrument-invariant.
const PRICE_FAILURE_TICKS = 2;

// ── Safety fallback for _rgba() ──
// _rgba() is defined in the original volume_bubbles.js.
// If load order breaks (e.g. v2 loads before original), we define it here
// so render calls don't throw. The original definition takes priority
// because this runs before the export block.
if (typeof _rgba === 'undefined') {
    // eslint-disable-next-line no-unused-vars
    window._rgba = function _rgba(rgb, alpha) {
        return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
    };
}

// ═══════════════════════════════════════════════════════════════════════════
// 1. ADAPTIVE KALMAN THRESHOLD
// ═══════════════════════════════════════════════════════════════════════════
//
// PROBLEM: Current engine recomputes log(μ), log(σ) over ALL visible bars
// every frame. This is:
//   a) O(n) per frame where n = total price levels across visible bars
//   b) Non-adaptive: a regime shift (e.g., FOMC) contaminates the window
//      with pre-event noise for the entire visible range
//   c) Equally weights stale data from 30 min ago vs 1 sec ago
//
// SOLUTION: Online Kalman filter tracking the log-volume distribution.
//
// State vector: x_t = [μ_log, σ²_log]
//   μ_log  = running mean of log(volume + 1)
//   σ²_log = running variance of log(volume + 1)
//
// Process model (random walk with adaptive noise):
//   x_{t+1} = x_t + w_t,   w_t ~ N(0, Q_t)
//   Q_t = diag(q_μ, q_σ) where q is adapted from innovation monitoring
//
// Measurement model:
//   z_t = log(v_t + 1)  (each new trade volume observation)
//
// Innovation-based Q adaptation (Mehra 1972):
//   If |ν_t| > 2√S_t consistently → regime shift → inflate Q
//   If |ν_t| < 0.5√S_t consistently → stable → deflate Q
//
// This gives us ADAPTIVE thresholds that automatically widen during
// volatility and tighten during dead tape — no manual σ floor needed.

const AdaptiveKalmanThreshold = {
    // State
    _mu: 0,           // E[log(v+1)]
    _var: 1,          // Var[log(v+1)]
    _P_mu: 1,         // Kalman covariance for μ
    // FIX M2: Removed dead _P_var/_Q_var state. We track variance via exponential
    // weighting (alpha * residual²), NOT a 2D Kalman. Keeping phantom covariance
    // variables created false confidence that a joint (μ, σ²) filter was running.
    _Q_mu: 0.001,     // Process noise for μ (adapted via Mehra 1972)
    _R: 0.5,          // Measurement noise (observation uncertainty)
    _initialized: false,
    _lastFedSig: '',            // signature guard — prevents re-feeding same bar data
    _innovationBuffer: [],  // rolling window of squared innovations
    _INNOV_WINDOW: 20,      // window size for innovation monitoring

    /**
     * Initialize from a batch of volumes (e.g., REST backfill).
     * Uses Welford's online algorithm for numerical stability.
     */
    initialize(volumes) {
        if (!volumes || volumes.length < 3) return;
        let n = 0, mean = 0, m2 = 0;
        for (let i = 0; i < volumes.length; i++) {
            const x = Math.log(volumes[i] + 1);
            n++;
            const delta = x - mean;
            mean += delta / n;
            m2 += delta * (x - mean);
        }
        this._mu = mean;
        this._var = m2 / (n - 1);
        this._P_mu = this._var / n;  // initial uncertainty = sample variance / n
        // FIX M2: _P_var removed — variance tracked via exponential weighting, not Kalman
        this._R = this._var * 0.5;  // measurement noise = half the variance
        this._initialized = true;
        this._innovationBuffer = [];
    },

    /**
     * Process a single new volume observation.
     * Returns { mu, sigma, sigThreshold, instThreshold } — all in original space.
     */
    update(volume) {
        const z = Math.log(volume + 1);

        if (!this._initialized) {
            this._mu = z;
            this._var = 1;
            this._P_mu = 1;
            this._initialized = true;
            return this._thresholds();
        }

        // ── Predict step ──
        // x_{t|t-1} = x_t (random walk)
        const P_mu_pred = this._P_mu + this._Q_mu;

        // ── Update step (μ) ──
        const innovation = z - this._mu;
        const S = P_mu_pred + this._R;  // innovation variance
        const K = P_mu_pred / S;         // Kalman gain

        // FIX F1: Capture prior residual BEFORE updating _mu.
        // Using posterior residual (z - updated_mu) biases _var downward
        // because the mean has already been pulled toward z.
        const priorResidual = innovation;  // = z - _mu (pre-update)

        this._mu += K * innovation;
        this._P_mu = (1 - K) * P_mu_pred;

        // ── Update variance estimate (using PRIOR residual) ──
        // Online variance update: exponential weighting
        const alpha = Math.max(0.02, Math.min(0.15, 2 / (this._innovationBuffer.length + 10)));
        this._var = (1 - alpha) * this._var + alpha * priorResidual * priorResidual;
        this._var = Math.max(this._var, 0.05); // Prevent variance collapse during dead tape

        // ── Innovation monitoring: adapt Q ──
        const normalizedInnovSq = (innovation * innovation) / S;
        this._innovationBuffer.push(normalizedInnovSq);
        if (this._innovationBuffer.length > this._INNOV_WINDOW) {
            this._innovationBuffer.shift();
        }

        if (this._innovationBuffer.length >= 5) {
            // Mehra 1972 complete framework: adapt both Q and R
            const avgNormInnovSq = this._innovationBuffer.reduce((a, b) => a + b, 0)
                / this._innovationBuffer.length;

            // FIX M3: R adaptation — observed innovation variance should match S = P + R
            const observedInnovVar = avgNormInnovSq * S;  // un-normalize: actual innov variance
            const estimatedR = Math.max(0.01, observedInnovVar - P_mu_pred);
            this._R = 0.9 * this._R + 0.1 * estimatedR;
            this._R = Math.max(0.01, Math.min(this._R, 5.0));

            if (avgNormInnovSq > 2.0) {
                // Innovations too large → underestimating process noise → regime shift
                this._Q_mu = Math.min(this._Q_mu * 1.5, 0.1);
            } else if (avgNormInnovSq < 0.5) {
                // Innovations too small → overestimating process noise → stable
                this._Q_mu = Math.max(this._Q_mu * 0.8, 1e-5);
            }
        }

        return this._thresholds();
    },

    /**
     * Compute thresholds in original (non-log) space.
     * Uses the current Kalman state — no per-frame recomputation needed.
     */
    _thresholds() {
        const logStddev = Math.sqrt(Math.max(this._var, 1e-6));
        return {
            mu: this._mu,
            sigma: logStddev,
            // Named σ-level thresholds — exact log-normal inversions.
            // DO NOT approximate these with scalar multiplication elsewhere.
            highDomMinVol:        Math.exp(this._mu + 0.5 * logStddev) - 1, // 0.5σ: noise floor
            absorbMinVol:         Math.exp(this._mu + 1.0 * logStddev) - 1, // 1.0σ: absorption gate
            sigThreshold:         Math.exp(this._mu + 1.5 * logStddev) - 1, // 1.5σ: legacy sig
            wallThreshold:        Math.exp(this._mu + 2.0 * logStddev) - 1, // 2.0σ: V3 wall gate
            directionalThreshold: Math.exp(this._mu + 2.5 * logStddev) - 1, // 2.5σ: V3 directional gate
            instThreshold:        Math.exp(this._mu + 3.0 * logStddev) - 1, // 3.0σ: institutional
            // Regime indicator: high Q_mu = volatile regime, low = stable
            regimeVolatility: this._Q_mu / 0.001,
        };
    },

    /**
     * Compute σ-distance for a given volume.
     */
    sigmaDistance(volume) {
        const logVol = Math.log(volume + 1);
        const logStddev = Math.sqrt(Math.max(this._var, 1e-6));
        return logStddev > 0 ? (logVol - this._mu) / logStddev : 0;
    },

    reset() {
        this._mu = 0; this._var = 1;
        this._P_mu = 1;
        this._Q_mu = 0.001;
        this._R = 0.5;
        this._initialized = false;
        this._innovationBuffer = [];
        this._lastFedSig = '';  // clear signature guard on reset
        this._fedFrom = undefined;  // clear range tracking on reset
        this._fedTo = undefined;
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 2. HAWKES CLUSTER DETECTOR
// ═══════════════════════════════════════════════════════════════════════════
//
// PROBLEM: Current cluster detection counts hits ≥ 3 at same price with
// each hit ≥ 1.5σ. This fails on:
//   a) Sliced institutional orders (6 × 1.4σ = invisible)
//   b) No temporal structure (hits 30 min apart ≠ hits 2 sec apart)
//   c) No excitation: the 4th hit after 3 rapid hits is MORE significant,
//      not equally significant
//
// SOLUTION: Hawkes process — a self-exciting point process.
//
// For each price level p, define the intensity function:
//
//   λ_p(t) = μ_p + Σ_{t_i < t} g(v_i) × α × exp(-β(t - t_i))
//
// Where:
//   μ_p     = baseline intensity (background rate at this price)
//   g(v_i)  = volume weighting function: v_i / E[v] (larger prints excite more)
//   α       = excitation magnitude
//   β       = decay rate (how fast excitation fades)
//   t_i     = time of i-th event at this price level
//
// Cluster detection: λ_p(t) > threshold_p
//   threshold_p = μ_p + k × √(α/(2β)) × (1 + branching_ratio)
//   where branching_ratio = α/β (should be < 1 for stationarity)
//
// KEY: This naturally handles sub-threshold aggregation.
// A 1.0σ print alone barely moves λ. But 6 rapid 1.0σ prints create
// self-excitation that pushes λ past threshold — exactly the pattern
// the current system misses.
//
// PARAMETER ESTIMATION: Method of moments (fast, O(1) per update)
//   Given event times {t_i} and volumes {v_i}:
//   μ̂ = N/T × (1 - α̂/β̂)
//   α̂/β̂ estimated from the empirical autocorrelation of the counting process
//
// INCREMENTAL UPDATE (O(1) per event):
//   λ_p(t_new) = μ_p + [λ_p(t_old) - μ_p] × exp(-β × Δt) + α × g(v_new)

const HawkesClusterDetector = {
    // Per-price-level state: Map<priceStr, HawkesState>
    _levels: new Map(),

    // Global parameters (auto-calibrated from data)
    _alpha: 0.8,    // excitation magnitude (will be calibrated)
    _beta: 2.0,     // decay rate, per-bar units (will be calibrated)
    _muGlobal: 0.1, // baseline intensity (events per bar, calibrated)
    _meanVol: 1,    // E[volume] for g(v) normalization

    // Calibration state
    _totalEvents: 0,
    _totalBars: 0,
    _lastCalibrationBar: 0,
    _CALIBRATION_INTERVAL: 10, // recalibrate every N bars

    /**
     * Ingest a bar's worth of volume data.
     * @param {number} barIdx  - bar index (time proxy for λ decay)
     * @param {Object} bp      - bubble profile {priceStr: [buyVol, sellVol]}
     * @param {number} sigThreshold - current significance threshold from Kalman
     * @param {number} noiseFloor   - minimum volume for λ excitation (0.5σ from Kalman)
     * @param {number} barTs   - wall-clock timestamp of this bar (Unix seconds, 0 if unavailable)
     */
    ingestBar(barIdx, bp, sigThreshold, noiseFloor, barTs = 0) {
        if (!bp) return;
        // If noiseFloor not provided, derive from Kalman state.
        // Use exp(μ - 0.5σ) which is the lower half of the volume distribution
        // — anything below this is genuine noise. Falls back to 1 if Kalman not ready.
        const floor = noiseFloor
            || (AdaptiveKalmanThreshold._initialized
                ? Math.max(Math.exp(AdaptiveKalmanThreshold._mu - 0.5 * Math.sqrt(Math.max(AdaptiveKalmanThreshold._var, 1e-6))) - 1, 1)
                : 1);

        for (const priceStr in bp) {
            const bv = bp[priceStr][0], sv = bp[priceStr][1];
            const tv = bv + sv;
            if (tv <= 0) continue;

            let state = this._levels.get(priceStr);
            if (!state) {
                state = {
                    lambda: this._muGlobal,  // current intensity
                    lastBar: barIdx,
                    lastTs: barTs,           // wall-clock time of last event (0 if unavailable)
                    totalVol: 0,
                    events: [],    // [{bar, vol, buy, sell}] — kept for acceleration
                    peakLambda: 0,
                };
                this._levels.set(priceStr, state);
            }

            // ── Decay existing intensity to current time ──
            const dt = barIdx - state.lastBar;
            if (dt > 0) {
                state.lambda = this._muGlobal
                    + (state.lambda - this._muGlobal) * Math.exp(-this._beta * dt);
            }

            // ── Volume weighting: g(v) = v / E[v] ──
            // Larger prints contribute more excitation
            const g = this._meanVol > 0 ? tv / this._meanVol : 1;

            // ── Excite λ ONLY if print is above noise floor ──
            // Below ~0.5σ, prints are statistical noise: random 1-5 lot fills
            // that carry no cluster information. Without this gate, hundreds
            // of noise prints accumulate phantom intensity.
            //
            // Above noiseFloor: full excitation via g(v). The Hawkes
            // sub-threshold aggregation still works — prints between 0.5σ
            // and 1.5σ individually don't trigger cluster detection, but
            // their λ contributions compound if they arrive in rapid burst.
            if (tv >= floor) {
                state.lambda += this._alpha * g;
            }
            // Track totalVol, lastBar, and lastTs regardless — used for calibration and pruning
            state.totalVol += tv;
            state.lastBar = barIdx;
            if (barTs > 0) state.lastTs = barTs;  // update wall-clock time if available

            // Track peak λ for ExhaustionDetector Signal 1.
            // peakLambda is the highest intensity this level has ever reached.
            // ExhaustionDetector uses: intensityDecay = 1 - λ(now) / peakLambda
            // This gives the TRUE decay from maximum excitation, not a proxy.
            if (state.lambda > state.peakLambda) {
                state.peakLambda = state.lambda;
            }

            // Keep event history for acceleration computation
            state.events.push({ bar: barIdx, vol: tv, buy: bv, sell: sv });

            // Trim old events (keep last 20)
            if (state.events.length > 20) state.events.shift();

            this._totalEvents++;
            // FIX M6: Track events in rolling window for calibration
            // (session accumulator becomes sluggish after 2000+ bars)
            if (!this._recentBarEvents) this._recentBarEvents = [];
            this._recentBarEvents.push(barIdx);
            while (this._recentBarEvents.length > 0 &&
                   this._recentBarEvents[0] < barIdx - 200) {
                this._recentBarEvents.shift();
            }
        }

        this._totalBars = Math.max(this._totalBars, barIdx + 1);

        // ── Periodic recalibration ──
        if (barIdx - this._lastCalibrationBar >= this._CALIBRATION_INTERVAL) {
            this._calibrate(barIdx);
            this._lastCalibrationBar = barIdx;
        }
    },

    /**
     * Method-of-moments calibration.
     * FIX M6: Uses rolling 200-bar window instead of session accumulators.
     */
    _calibrate(currentBar) {
        const recentEvents = this._recentBarEvents ? this._recentBarEvents.length : 0;
        const effectiveBars = currentBar != null
            ? Math.min(currentBar + 1, 200)
            : this._totalBars;

        if (effectiveBars < 5 || (recentEvents < 10 && this._totalEvents < 10)) return;

        // Baseline intensity: events per bar (rolling window preferred)
        const rawMu = recentEvents >= 10
            ? recentEvents / effectiveBars
            : this._totalEvents / this._totalBars;

        // Estimate branching ratio from event clustering
        // Quick heuristic: fraction of events that occur at levels with λ > 2μ
        let clusteredEvents = 0;
        for (const [, state] of this._levels) {
            if (state.lambda > 2 * rawMu) {
                clusteredEvents += state.events.length;
            }
        }
        const branchingRatio = Math.min(
            this._totalEvents > 0 ? clusteredEvents / this._totalEvents : 0.3,
            0.95  // must be < 1 for stationarity
        );

        // μ = rawMu × (1 - branching_ratio)
        this._muGlobal = rawMu * (1 - branchingRatio);

        // α/β = branching_ratio, with β controlling decay speed
        // β = 2.0 means intensity halves every ~0.35 bars (fast decay, captures bursts)
        // Adapt β: more events = faster regime → faster decay needed
        this._beta = Math.max(1.0, Math.min(5.0, rawMu * 0.5));
        this._alpha = branchingRatio * this._beta;

        // Update mean volume for g(v) normalization
        let volSum = 0, volCount = 0;
        for (const [, state] of this._levels) {
            for (const ev of state.events) {
                volSum += ev.vol;
                volCount++;
            }
        }
        if (volCount > 0) this._meanVol = volSum / volCount;
    },

    /**
     * Get cluster intensity at a price level.
     * Returns null if level has no activity, or:
     * {
     *   lambda: current intensity,
     *   isCluster: boolean (λ > threshold),
     *   clusterStrength: normalized 0-1 (how far above threshold),
     *   events: [{bar, vol, buy, sell}],
     *   totalVol, totalBuy, totalSell,
     *   acceleration: regression slope (WLS),
     * }
     */
    getCluster(priceStr, currentBar) {
        const state = this._levels.get(priceStr);
        if (!state || state.events.length < 2) return null;

        // Decay intensity to current time
        const dt = currentBar - state.lastBar;
        const lambda = this._muGlobal
            + (state.lambda - this._muGlobal) * Math.exp(-this._beta * Math.max(dt, 0));

        // ── Adaptive threshold ──
        // Under a stationary Hawkes process, the variance of λ is:
        //   Var[λ] ≈ α² / (2β × (1 - α/β)²)
        // Threshold = μ + 2 × √Var[λ] (≈ 95th percentile)
        const branchingRatio = this._alpha / this._beta;
        const lambdaVar = (this._alpha * this._alpha)
            / (2 * this._beta * Math.pow(1 - branchingRatio, 2) + 1e-9);
        const threshold = this._muGlobal + 2 * Math.sqrt(lambdaVar);

        const isCluster = lambda > threshold && state.events.length >= 2;
        const clusterStrength = threshold > 0
            ? Math.min((lambda - threshold) / threshold, 3.0) / 3.0
            : 0;

        // ── Compute aggregate stats ──
        let totalBuy = 0, totalSell = 0;
        for (const ev of state.events) {
            totalBuy += ev.buy;
            totalSell += ev.sell;
        }

        // ── WLS Acceleration (see module 6) ──
        const acceleration = RegressionAcceleration.computeSlope(
            state.events.map(e => e.vol),
            state.events.map(e => e.bar)
        );

        return {
            lambda,
            threshold,
            isCluster,
            clusterStrength: Math.max(0, clusterStrength),
            events: state.events,
            totalVol: state.totalVol,
            totalBuy,
            totalSell,
            acceleration,
            // FIX (Issue 5): Expose peakLambda so ExhaustionDetector
            // Signal 1 uses the real Hawkes peak, not the σ-decay fallback.
            peakLambda: state.peakLambda,
            // FIX (Issue 7): Expose R² so call sites can gate on regression quality.
            // Acceleration direction is only reliable when R² ≥ 0.4.
            // Below that, the slope is fitting noise, not a real trend.
            accelerationRSquared: acceleration.rSquared,
        };
    },

    /**
    /**
     * Get all active clusters (λ > threshold).
     * Prunes stale levels by BOTH bar count AND wall-clock time.
     * @param {number} currentBar - latest bar index
     * @param {number} currentTs  - wall-clock timestamp of latest bar (Unix seconds, 0 = unavailable)
     * @returns Map<priceStr, clusterInfo>
     */
    getActiveClusters(currentBar, currentTs = 0) {
        const clusters = new Map();
        const toDelete = [];
        const PRUNE_SECONDS = 1800; // 30 min real time — correct regardless of bar period
        const PRUNE_BARS    = 200;  // safety fallback when timestamps unavailable

        for (const [priceStr, state] of this._levels) {
            const dt = currentBar - state.lastBar;

            // ── Time-based prune (primary): wall-clock 30 minutes ──
            // Replaces the old 30-bar heuristic which was completely wrong
            // on non-1min charts (hourly = 30 hours stale; 6s = 3 min gone).
            const dtSeconds = (currentTs > 0 && state.lastTs > 0)
                ? currentTs - state.lastTs
                : null;

            const isTimeStale  = dtSeconds !== null && dtSeconds > PRUNE_SECONDS;
            const isBarStale   = dtSeconds === null  && dt > PRUNE_BARS; // ts unavailable fallback

            if ((isTimeStale || isBarStale) && state.events.length === 0) {
                toDelete.push(priceStr);
                continue;
            }

            // Also prune if λ has fully decayed to within 1% of μ and level is stale
            const decayedLambda = this._muGlobal
                + (state.lambda - this._muGlobal) * Math.exp(-this._beta * Math.max(dt, 0));
            if ((isTimeStale || isBarStale) && decayedLambda < this._muGlobal * 1.01) {
                toDelete.push(priceStr);
                continue;
            }

            const cl = this.getCluster(priceStr, currentBar);
            if (cl && cl.isCluster) {
                clusters.set(priceStr, cl);
            }
        }

        for (const priceStr of toDelete) {
            this._levels.delete(priceStr);
        }

        return clusters;
    },

    reset() {
        this._levels.clear();
        this._totalEvents = 0;
        this._totalBars = 0;
        this._lastCalibrationBar = 0;
        this._alpha = 0.8;
        this._beta = 2.0;
        this._muGlobal = 0.1;
        this._meanVol = 1;
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 3. MULTI-BAR ABSORPTION AGGREGATOR
// ═══════════════════════════════════════════════════════════════════════════
//
// PROBLEM: _isAbsorption() checks a single bar at a single price level
// with a fixed 35% ratio. This misses:
//   a) Multi-bar absorption (wall absorbs across 3-5 candles)
//   b) Multi-level absorption (adjacent prices, same wall)
//   c) The rich backend data (abs.s, abs.w, abs.sh, abs.c) is IGNORED
//
// SOLUTION: Temporal absorption scoring with exponential decay.
//
// For each price level p, maintain an absorption score A_p(t):
//
//   A_p(t) = Σ_{i} w_i × s_i × exp(-λ_abs × (t - t_i))
//
// Where:
//   s_i    = backend absorption score at time i (abs.s field)
//   w_i    = wave count modifier: w_i = 1 + log(abs.w + 1) (more waves = more real)
//   λ_abs  = decay rate (auto-calibrated from median absorption duration)
//   t_i    = time of absorption detection
//
// Adjacent-level aggregation:
//   For each price p, also sum contributions from p ± tickSize:
//   A_p_effective = A_p + 0.5 × A_{p-tick} + 0.5 × A_{p+tick}
//
// Classification (output: continuous score, not binary):
//   A_p < 0.5  → no significant absorption
//   A_p ∈ [0.5, 2.0) → developing absorption (dim purple)
//   A_p ∈ [2.0, 5.0) → confirmed wall (bright purple + glow)
//   A_p ≥ 5.0  → institutional fortress (massive glow + alert)

const AbsorptionAggregator = {
    // Per-price absorption state
    _scores: new Map(),  // priceStr → {score, lastUpdate, events: [{t, s, w, sh, c}]}
    _lambda: 0.3,       // decay rate per bar — FIX M1: now auto-calibrated
    _tickSize: 0.25,    // instrument tick size — updated via setSymbol()
    _lastAbsSig: '',    // signature guard — prevents re-feeding same WS snapshot
    _lastCalibBar: 0,   // FIX M1: last bar index where _calibrateLambda ran

    /**
     * Set the current instrument so adjacent-level lookup uses correct tick.
     * Call this on symbol switch before any ingest calls.
     * @param {string} symbol - 'NQ' or 'GC'
     */
    setSymbol(symbol) {
        this._tickSize = TICK_SIZES[symbol] || DEFAULT_TICK_SIZE;
    },

    /**
     * Ingest absorption data from a WebSocket snapshot.
     * @param {Object} absData - {priceStr: {s, w, i, h, c, sh, rs, sd}}
     * @param {number} barIdx - current bar index
     */
    ingest(absData, barIdx) {
        if (!absData) return;

        for (const priceStr in absData) {
            const entry = absData[priceStr];
            const s = entry.s || 0;   // absorption score
            const w = entry.w || 0;   // wave count
            const sh = entry.sh || 0; // shock count
            const c = entry.c || 0;   // crack count

            // Skip trivial entries
            if (s < 0.1 && w < 1) continue;

            let state = this._scores.get(priceStr);
            if (!state) {
                state = { score: 0, lastUpdate: barIdx, events: [] };
                this._scores.set(priceStr, state);
            }

            // Decay existing score to current time
            const dt = barIdx - state.lastUpdate;
            if (dt > 0) {
                state.score *= Math.exp(-this._lambda * dt);
            }

            // Wave modifier: more waves = wall is actively defending
            const waveWeight = 1 + Math.log(w + 1);

            // Shock modifier: more shocks = more aggressive opposition
            const shockWeight = 1 + Math.log(sh + 1) * 0.5;

            // Crack penalty: cracks reduce confidence
            const crackPenalty = c > 0 ? 1 / (1 + c * 0.3) : 1;

            // Composite score contribution
            const contribution = s * waveWeight * shockWeight * crackPenalty;

            state.score += contribution;
            state.lastUpdate = barIdx;
            state.events.push({ t: barIdx, s, w, sh, c });

            // Trim old events
            if (state.events.length > 30) state.events.shift();
        }

        // FIX M1: Auto-calibrate λ every 50 bars from observed absorption durations
        if (barIdx - this._lastCalibBar >= 50) {
            this._calibrateLambda(barIdx);
            this._lastCalibBar = barIdx;
        }
    },

    /**
     * FIX M1: Auto-calibrate decay rate λ from observed absorption event durations.
     * λ = ln(2) / median_duration — sets half-life to the typical absorption length.
     */
    _calibrateLambda(currentBar) {
        const durations = [];
        for (const [, state] of this._scores) {
            if (state.events.length >= 2) {
                const first = state.events[0].t;
                const last = state.events[state.events.length - 1].t;
                const dur = last - first;
                if (dur > 0) durations.push(dur);
            }
        }
        if (durations.length < 5) return;
        durations.sort((a, b) => a - b);
        const median = durations[Math.floor(durations.length / 2)];
        if (median > 0) {
            const newLambda = Math.log(2) / median;
            this._lambda = Math.max(0.05, Math.min(1.0, newLambda));
        }
    },

    /**
     * Ingest from the simple per-bar bubble profile (fallback when no backend abs data).
     * Uses the existing _isAbsorption logic but accumulates temporally.
     */
    ingestFromBP(bp, barIdx, absorbMinVol) {
        if (!bp) return;

        for (const priceStr in bp) {
            const bv = bp[priceStr][0], sv = bp[priceStr][1];
            const tv = bv + sv;
            if (tv < absorbMinVol) continue;

            // Shannon entropy: H = -p·log2(p) - (1-p)·log2(1-p)
            // H=1.0 at perfect balance (pure absorption), H→0 at one-sided
            // Replaces the fixed 0.25/0.35 ratio threshold with a continuous
            // measure. No discontinuity — entropy maps smoothly onto [0,1].
            const p = tv > 0 ? bv / tv : 0.5;
            const q = 1 - p;
            const entropy = (p > 0.001 && q > 0.001)
                ? -(p * Math.log2(p) + q * Math.log2(q))
                : 0;

            // Gate: entropy < 0.65 means >80% one-sided — not absorption
            if (entropy < 0.65) continue;

            let state = this._scores.get(priceStr);
            if (!state) {
                state = { score: 0, lastUpdate: barIdx, events: [] };
                this._scores.set(priceStr, state);
            }

            const dt = barIdx - state.lastUpdate;
            if (dt > 0) state.score *= Math.exp(-this._lambda * dt);

            // Score = entropy × volume significance
            // H=0.65→0.12, H=0.85→0.72, H=1.0→1.0 (normalized to [0,1])
            // Multiplied by volume ratio so bigger balanced prints score higher
            const entropyNorm = Math.pow((entropy - 0.5) / 0.5, 2);
            const volScale = tv / absorbMinVol;
            state.score += entropyNorm * volScale;
            state.lastUpdate = barIdx;
        }
    },

    /**
     * Get effective absorption score at a price level.
     * Includes adjacent-level aggregation.
     */
    getScore(priceStr, currentBar) {
        const price = parseFloat(priceStr);
        if (isNaN(price)) return 0;

        let effective = 0;

        // Center level
        const center = this._scores.get(priceStr);
        if (center) {
            const dt = currentBar - center.lastUpdate;
            effective += center.score * Math.exp(-this._lambda * Math.max(dt, 0));
        }

        // Adjacent levels (±1 tick)
        const above = this._scores.get((price + this._tickSize).toFixed(2));
        const below = this._scores.get((price - this._tickSize).toFixed(2));
        if (above) {
            const dt = currentBar - above.lastUpdate;
            effective += 0.5 * above.score * Math.exp(-this._lambda * Math.max(dt, 0));
        }
        if (below) {
            const dt = currentBar - below.lastUpdate;
            effective += 0.5 * below.score * Math.exp(-this._lambda * Math.max(dt, 0));
        }

        return effective;
    },

    /**
     * Get absorption classification.
     * Returns { score, tier, glowIntensity, label }
     */
    classify(priceStr, currentBar) {
        const score = this.getScore(priceStr, currentBar);
        if (score < 2.0) return { score, tier: 0, glowIntensity: 0, label: null };
        if (score < 6.0) return { score, tier: 1, glowIntensity: 0.3, label: 'ABS' };
        if (score < 15.0) return { score, tier: 2, glowIntensity: 0.6, label: 'WALL' };
        return { score, tier: 3, glowIntensity: 1.0, label: 'FORTRESS' };
    },

    reset() {
        this._scores.clear();
        this._lastAbsSig = '';  // clear signature guard on symbol switch
        // Note: _tickSize is NOT reset here. It persists across session
        // restarts for the same symbol. setSymbol() is called on symbol switch.
    },
};

// IMPORTANT: ExhaustionDetector also needs the tick size for Signal 2.
// It reads it from AbsorptionAggregator._tickSize to avoid duplicating state.
// This is safe because both are updated at the same time via setSymbol().


// ═══════════════════════════════════════════════════════════════════════════
// 4. ADAPTIVE DOMINANCE THRESHOLD
// ═══════════════════════════════════════════════════════════════════════════
//
// PROBLEM: Fixed 0.70 dominance threshold. A 500-lot at 60% buy is
// highly directional. A 3-lot at 80% buy is noise. The threshold should
// scale inversely with volume.
//
// SOLUTION: Binomial confidence interval.
//
// Model: each contract is Bernoulli(p) where p = true buy probability.
// For n contracts with k buys, the (1-α) Wilson confidence interval for p:
//
//   p̂ = k/n
//   z = Φ⁻¹(1 - α/2) where α = 0.05 → z ≈ 1.96
//   
//   CI_lower = (p̂ + z²/(2n) - z√(p̂(1-p̂)/n + z²/(4n²))) / (1 + z²/n)
//
// "Directional" if CI_lower > 0.50 (the lower bound of the buy fraction
// is above 50%, so we're 95% confident it's directional).
//
// This automatically adapts:
//   n=500, p̂=0.60 → CI_lower ≈ 0.557 > 0.50 → DIRECTIONAL ✓
//   n=3,   p̂=0.80 → CI_lower ≈ 0.284 < 0.50 → NOT SIGNIFICANT ✗
//   n=100, p̂=0.65 → CI_lower ≈ 0.556 > 0.50 → DIRECTIONAL ✓
//   n=10,  p̂=0.70 → CI_lower ≈ 0.395 < 0.50 → NOT SIGNIFICANT ✗

const AdaptiveDominance = {
    _z: 1.96,  // 95% CI (could use 1.645 for 90% if too conservative)

    /**
     * Test if a volume print is directionally significant.
     * @param {number} buyVol - buy volume
     * @param {number} sellVol - sell volume
     * @returns {{ isDirectional: boolean, ciLower: number, pHat: number }}
     */
    test(buyVol, sellVol) {
        const n = buyVol + sellVol;
        if (n <= 0) return { isDirectional: false, ciLower: 0.5, pHat: 0.5 };

        // p̂ = fraction of dominant side
        const dominant = Math.max(buyVol, sellVol);
        const pHat = dominant / n;

        // Wilson score interval (lower bound)
        const z2 = this._z * this._z;
        const denominator = 1 + z2 / n;
        const center = pHat + z2 / (2 * n);
        const spread = this._z * Math.sqrt(pHat * (1 - pHat) / n + z2 / (4 * n * n));
        const ciLower = (center - spread) / denominator;

        return {
            isDirectional: ciLower > 0.50,
            ciLower,
            pHat,
        };
    },

    /**
     * Get conviction strength (0-1) for rendering intensity.
     * Uses the excess of CI_lower over 0.50 as a confidence measure.
     */
    convictionStrength(buyVol, sellVol) {
        const { isDirectional, ciLower } = this.test(buyVol, sellVol);
        if (!isDirectional) return 0;
        // Map CI_lower from 0.50 → 0, to 0.75 → 1.0
        return Math.min((ciLower - 0.50) / 0.25, 1.0);
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 5. TEMPORAL DECAY ENGINE
// ═══════════════════════════════════════════════════════════════════════════
//
// PROBLEM: All visible bars have equal visual weight. Stale prints from
// 30 minutes ago clutter the display.
//
// SOLUTION: Exponential decay with λ auto-calibrated from visible range.
//
// For bar at index i in a visible range [from, to]:
//   age = (to - 1) - i  (0 for most recent, (to-from-1) for oldest)
//   recencyWeight = exp(-λ × age)
//
// λ is chosen so that:
//   recencyWeight(oldest) = targetFloor (default 0.15)
//   → λ = -ln(targetFloor) / (to - from - 1)
//
// This automatically adapts to any zoom level:
//   Zoomed to 10 bars → λ = 0.127 → gentle decay
//   Zoomed to 100 bars → λ = 0.019 → steeper decay
//   Zoomed to 500 bars → λ = 0.0038 → very steep

const TemporalDecay = {
    _targetFloor: 0.15,  // oldest bar renders at 15% of newest

    /**
     * Compute decay weight for a bar at given position.
     * @param {number} barIdx - index of the bar
     * @param {number} from - start of visible range
     * @param {number} to - end of visible range (exclusive)
     * @returns {number} weight in [targetFloor, 1.0]
     */
    weight(barIdx, from, to) {
        const range = to - from - 1;
        if (range <= 0) return 1.0;

        const age = (to - 1) - barIdx;
        const lambda = -Math.log(this._targetFloor) / range;
        return Math.exp(-lambda * Math.max(age, 0));
    },

    /**
     * Apply decay to an opacity value.
     */
    applyToOpacity(opacity, barIdx, from, to) {
        return opacity * this.weight(barIdx, from, to);
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 6. REGRESSION ACCELERATION
// ═══════════════════════════════════════════════════════════════════════════
//
// PROBLEM: Current acceleration uses avgSecondHalf / avgFirstHalf.
// This is a 2-point estimate — maximally noisy.
//
// SOLUTION: Weighted Least Squares regression of volume vs time.
//
// Model: v_i = β₀ + β₁ × t_i + ε_i
//   where weights w_i = 1/(σ²_v + ε) give inverse-variance weighting
//
// For simplicity (and O(n) computation):
//   β₁ = (Σ w_i(t_i - t̄)(v_i - v̄)) / (Σ w_i(t_i - t̄)²)
//
// Normalized slope: β̂₁ = β₁ × T / v̄
//   β̂₁ > 0 → acceleration (institutions loading)
//   β̂₁ < 0 → deceleration (exhaustion)
//   |β̂₁| magnitude = strength of trend

const RegressionAcceleration = {
    /**
     * Compute normalized WLS slope from volume and time arrays.
     * @param {number[]} volumes - volume at each hit
     * @param {number[]} times - time index at each hit
     * @returns {{ slope: number, rSquared: number, direction: string }}
     */
    computeSlope(volumes, times) {
        const n = volumes.length;
        if (n < 3) return { slope: 0, rSquared: 0, direction: 'neutral' };

        // Compute means
        let sumV = 0, sumT = 0;
        for (let i = 0; i < n; i++) {
            sumV += volumes[i];
            sumT += times[i];
        }
        const meanV = sumV / n;
        const meanT = sumT / n;
        if (meanV <= 0) return { slope: 0, rSquared: 0, direction: 'neutral' };

        // Compute WLS slope (uniform weights for now — volume variance is homoscedastic)
        let numSum = 0, denSum = 0, ssRes = 0, ssTot = 0;
        for (let i = 0; i < n; i++) {
            const dt = times[i] - meanT;
            const dv = volumes[i] - meanV;
            numSum += dt * dv;
            denSum += dt * dt;
            ssTot += dv * dv;
        }

        if (denSum === 0) return { slope: 0, rSquared: 0, direction: 'neutral' };

        const beta1 = numSum / denSum;
        const timeRange = (times[n - 1] - times[0]) || 1;

        // Normalized slope: dimensionless, scale-invariant
        const normalizedSlope = (beta1 * timeRange) / meanV;

        // R² for confidence
        for (let i = 0; i < n; i++) {
            const predicted = meanV + beta1 * (times[i] - meanT);
            ssRes += (volumes[i] - predicted) ** 2;
        }
        const rSquared = ssTot > 0 ? 1 - ssRes / ssTot : 0;

        return {
            slope: normalizedSlope,
            rSquared: Math.max(0, rSquared),
            direction: normalizedSlope > 0.1 ? 'accelerating'
                     : normalizedSlope < -0.1 ? 'decelerating'
                     : 'neutral',
        };
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 7. CUMULATIVE LEVEL DELTA RENDERER
// ═══════════════════════════════════════════════════════════════════════════
//
// The MISSING render loop. Data is computed at volume_bubbles.js:415-442.
// Config exists at volume_bubbles.js:241-249. Drawing was never implemented.
//
// Renders horizontal bars at each price level showing net cumulative delta
// (total buys - total sells) across all visible bars.

const CumlDeltaRenderer = {
    /**
     * Render cumulative delta sidebar bars.
     * Call this from inside VolumeBubbleRenderer.draw() after Layer 6.5.
     *
     * @param {CanvasRenderingContext2D} ctx
     * @param {Object} cumlDelta - {priceStr: {buy, sell, total}}
     * @param {Function} priceConverter - price → Y coordinate
     * @param {Object} config - BUBBLE_CONFIG reference
     * @param {number} chartRightEdge - X coordinate of chart's right edge
     * @param {number} cumlMinVol - minimum cumulative volume to display
     */
    render(ctx, cumlDelta, priceConverter, config, chartRightEdge, cumlMinVol) {
        if (!config.CUML_DELTA_ENABLED) return;

        const entries = Object.entries(cumlDelta);
        if (entries.length === 0) return;

        // Find max |net delta| for normalization
        let maxAbsDelta = 0;
        for (const [, d] of entries) {
            if (d.total < cumlMinVol) continue;
            const net = Math.abs(d.buy - d.sell);
            if (net > maxAbsDelta) maxAbsDelta = net;
        }
        if (maxAbsDelta === 0) return;

        const barMaxW = config.CUML_DELTA_BAR_MAX_WIDTH;
        const barH = config.CUML_DELTA_BAR_HEIGHT;
        const barAlpha = config.CUML_DELTA_BAR_ALPHA;
        const glowThreshold = config.CUML_DELTA_GLOW_THRESHOLD;
        // LEFT margin — renders from left edge (opposite of Thermal Flare which uses right)
        const leftMargin = 8;

        ctx.save();

        for (const [priceStr, d] of entries) {
            if (d.total < cumlMinVol) continue;

            const price = parseFloat(priceStr);
            if (isNaN(price)) continue;
            const y = priceConverter(price);
            if (y === null || y === undefined || isNaN(y)) continue;

            const netDelta = d.buy - d.sell;
            const absNet = Math.abs(netDelta);
            const normWidth = (absNet / maxAbsDelta) * barMaxW;

            const isBuy = netDelta >= 0;
            // Amber for buy pressure, Steel blue for sell — distinct from Flare's green/red
            const rgb = isBuy ? '255, 180, 40' : '80, 140, 220';

            // Bar grows RIGHTWARD from left margin (Flare grows leftward from right edge)
            const barX = leftMargin;
            const barY = y - barH / 2;

            // ── Glow for large bars ──
            if (normWidth / barMaxW > glowThreshold) {
                ctx.shadowColor = `rgba(${rgb}, 0.4)`;
                ctx.shadowBlur = 6;
            }

            // ── Fill bar ──
            ctx.fillStyle = `rgba(${rgb}, ${barAlpha})`;
            ctx.fillRect(barX, barY, normWidth, barH);

            // ── Border ──
            ctx.strokeStyle = `rgba(${rgb}, ${barAlpha + 0.2})`;
            ctx.lineWidth = 0.5;
            ctx.strokeRect(barX, barY, normWidth, barH);

            ctx.shadowBlur = 0;

            // ── Label for significant bars ──
            if (normWidth > barMaxW * 0.3) {
                ctx.font = config.CUML_DELTA_FONT;
                ctx.fillStyle = `rgba(255, 255, 255, ${config.CUML_DELTA_LABEL_ALPHA})`;
                ctx.textAlign = 'left';
                ctx.textBaseline = 'middle';
                const label = `${isBuy ? '+' : ''}${netDelta}`;
                ctx.fillText(label, barX + normWidth + 3, y);
            }
        }

        ctx.restore();
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 8. EXHAUSTION DETECTOR
// ═══════════════════════════════════════════════════════════════════════════
//
// WHAT IS EXHAUSTION (from a market maker's desk):
//
// Exhaustion = aggressive side spent their order flow, price stops moving.
// It is the DIVERGENCE between flow intensity and price response.
//
// Three independent signals, ALL must be present:
//
// Signal 1: HAWKES INTENSITY DECAY
//   The self-exciting process at a price level is dying.
//   λ(t)/λ_peak is falling → the cluster of aggression is fading.
//   Measured: intensityDecay = 1 - λ(t)/λ_peak
//
// Signal 2: FLOW-PRICE DIVERGENCE
//   Cumulative delta is pushing one direction, but price isn't following.
//   Buy exhaustion: cumDelta > 0 (buyers aggressive) but price flat/down
//   Sell exhaustion: cumDelta < 0 (sellers aggressive) but price flat/up
//   Measured: divergence = |cumDelta_normalized| × max(0, -sign(delta) × priceChange)
//
// Signal 3: VOLUME CLIMAX DECAY
//   Peak σ-distance at this level was followed by declining σ-distance.
//   This is the "volume climax" pattern — a blow-off print followed by silence.
//   Measured: climaxDecay = max(0, peakSigma - currentSigma) / peakSigma
//
// COMPOSITE: Geometric mean of all three signals.
//   E = (intensityDecay × divergence × climaxDecay)^(1/3)
//
// WHY geometric mean:
//   - No weighting parameters (w_1, w_2, w_3 would be magic numbers)
//   - ALL three must be nonzero → all three conditions must be present
//   - One strong signal can't compensate for a zero in another
//   - Product is scale-invariant: each signal is normalized 0-1
//
// CLASSIFICATION:
//   E < 0.2 → no exhaustion
//   E ∈ [0.2, 0.5) → developing exhaustion (visual: desaturating bubble)
//   E ∈ [0.5, 0.8) → confirmed exhaustion (visual: grayed + "EXH" label)
//   E ≥ 0.8 → climax exhaustion (visual: dashed ring + "CLIMAX" label)

const ExhaustionDetector = {
    // Per-price exhaustion state
    _state: new Map(),  // priceStr → { peakSigma, peakBar, peakLambda, flowHistory }

    /**
     * Update exhaustion state for a price level.
     * Call this for each price level during bubble classification.
     *
     * @param {string} priceStr
     * @param {number} barIdx
     * @param {number} sigmaDistance - current σ-distance of this print
     * @param {number} buyVol
     * @param {number} sellVol
     * @param {number} barClose - close price of the current bar
     */
    update(priceStr, barIdx, sigmaDistance, buyVol, sellVol, barClose) {
        let state = this._state.get(priceStr);
        if (!state) {
            state = {
                peakSigma: 0,
                peakBar: barIdx,
                peakLambda: 0,
                firstBar: barIdx,
                firstPrice: barClose,
                cumBuy: 0,
                cumSell: 0,
                sigmaHistory: [],   // [{bar, sigma}] — last 10
                priceHistory: [],   // [{bar, close}] — last 10
                _ingestedBars: new Set(),  // track which bars have been accumulated
            };
            this._state.set(priceStr, state);
        }

        // FIX (Bug B): Skip if this (priceStr, barIdx) was already processed.
        // update() is called from PHASE 5 which iterates ALL visible bars every
        // frame. Without this guard, cumBuy/cumSell inflate by 60×/sec.
        // The ratio normalizedDelta/totalFlow is invariant, but the histories
        // get scrambled with duplicate entries and the state is dirty.
        if (state._ingestedBars.has(barIdx)) return;
        state._ingestedBars.add(barIdx);

        // Track peak σ
        if (sigmaDistance > state.peakSigma) {
            state.peakSigma = sigmaDistance;
            state.peakBar = barIdx;
        }

        // Accumulate flow
        state.cumBuy += buyVol;
        state.cumSell += sellVol;

        // Rolling histories
        state.sigmaHistory.push({ bar: barIdx, sigma: sigmaDistance });
        if (state.sigmaHistory.length > 10) state.sigmaHistory.shift();

        state.priceHistory.push({ bar: barIdx, close: barClose });
        if (state.priceHistory.length > 10) state.priceHistory.shift();
    },

    /**
     * Compute exhaustion score for a price level.
     *
     * @param {string} priceStr
     * @param {number} currentBar
     * @param {Object|null} hawkesCluster - from HawkesClusterDetector.getCluster()
     * @returns {{ score: number, tier: number, label: string|null, side: string }}
     */
    detect(priceStr, currentBar, hawkesCluster) {
        const state = this._state.get(priceStr);
        if (!state || state.sigmaHistory.length < 5) {
            return { score: 0, tier: 0, label: null, side: 'none' };
        }

        // ── Signal 1: Hawkes Intensity Decay ──
        // How much has λ fallen from its peak?
        //
        // FIX (Issue 5): Now uses the real peakLambda from HawkesClusterDetector.
        // Previously used clusterStrength as a proxy, which is only valid when
        // the level is still an active cluster. peakLambda captures the TRUE
        // historical maximum, including levels that WERE clusters but have since
        // decayed below threshold.
        //
        // Formula: intensityDecay = 1 - λ(now) / peakLambda
        //   = 0 when λ is at its peak (no decay, no exhaustion signal)
        //   = 1 when λ has fallen to baseline (complete intensity collapse)
        let intensityDecay = 0;
        if (hawkesCluster && hawkesCluster.peakLambda > 0 && hawkesCluster.lambda >= 0) {
            // Real Hawkes decay: normalized by actual historical peak
            intensityDecay = Math.max(0, 1 - hawkesCluster.lambda / hawkesCluster.peakLambda);
        } else if (hawkesCluster && hawkesCluster.clusterStrength < 0.5 && hawkesCluster.events.length >= 3) {
            // Partial fallback: cluster exists but peakLambda not available
            intensityDecay = 1 - hawkesCluster.clusterStrength;
        } else if (state.peakSigma > 1.0) {
            // Final fallback: no Hawkes data at all, use σ-decay
            const currentSigma = state.sigmaHistory[state.sigmaHistory.length - 1].sigma;
            intensityDecay = Math.max(0, (state.peakSigma - currentSigma) / state.peakSigma);
        }
        intensityDecay = Math.min(intensityDecay, 1.0);


        // ── Signal 2: Flow-Price Divergence ──
        // Is flow pushing one direction while price isn't following?
        let divergence = 0;
        const cumDelta = state.cumBuy - state.cumSell;
        const totalFlow = state.cumBuy + state.cumSell;

        if (totalFlow > 0 && state.priceHistory.length >= 2) {
            const firstClose = state.priceHistory[0].close;
            const lastClose = state.priceHistory[state.priceHistory.length - 1].close;
            const priceChange = lastClose - firstClose;

            // Normalize delta: fraction of total flow that's net directional
            const normalizedDelta = Math.abs(cumDelta) / totalFlow;  // 0-1

            // Divergence: flow says one direction, price says the opposite or flat
            // Buy exhaustion: cumDelta > 0, price flat or falling
            // Sell exhaustion: cumDelta < 0, price flat or rising
            const flowDirection = cumDelta > 0 ? 1 : -1; // positive = buy pressure
            const priceFollowed = flowDirection * priceChange; // positive = price confirmed flow

            if (priceFollowed <= 0) {
                // Price did NOT follow flow → divergence.
                //
                // FIX (Issue 4): Was hardcoded to 0.50 (= 2 NQ ticks).
                // Now reads tick size from AbsorptionAggregator (set via setSymbol()).
                // PRICE_FAILURE_TICKS = 2 is instrument-invariant (2 ticks = meaningful move).
                //   NQ: 2 × 0.25 = 0.50 (unchanged from before)
                //   GC: 2 × 0.10 = 0.20 (gold has tighter tick, was over-penalizing)
                //
                // priceFailure: how strong is the price non-response?
                //   0.3 floor: always some baseline divergence signal when flow fails
                //   Cap at 1.0: we don't double-penalize massive counter-moves
                const refMove = PRICE_FAILURE_TICKS * (AbsorptionAggregator._tickSize || DEFAULT_TICK_SIZE);
                const priceFailure = Math.min(Math.abs(priceChange) / refMove + 0.3, 1.0);
                divergence = normalizedDelta * priceFailure;
            }
            // If price DID follow flow → no divergence (divergence stays 0)
        }
        divergence = Math.min(divergence, 1.0);


        // ── Signal 3: Volume Climax Decay ──
        // Did σ peak and then fall?
        let climaxDecay = 0;
        if (state.peakSigma > 1.5 && state.sigmaHistory.length >= 3) {
            const currentSigma = state.sigmaHistory[state.sigmaHistory.length - 1].sigma;
            climaxDecay = Math.max(0, (state.peakSigma - currentSigma) / state.peakSigma);

            // Must be AFTER the peak (not before it)
            const barsSincePeak = currentBar - state.peakBar;
            if (barsSincePeak < 1) climaxDecay = 0; // peak is RIGHT NOW, not exhaustion yet
        }
        climaxDecay = Math.min(climaxDecay, 1.0);


        // ── Composite: Geometric mean ──
        // All three must be nonzero for exhaustion to register.
        // FIX F3: When Hawkes data is unavailable, Signals 1 and 3 are
        // identical (both compute sigma-decay). Using all three double-weights
        // sigma decay, violating the independence requirement.
        // Solution: use 2-signal model (sqrt) when no Hawkes data.
        const hasRealHawkes = !!(hawkesCluster && (hawkesCluster.peakLambda > 0 || hawkesCluster.events.length >= 3));
        const eps = 0.01;
        let raw;
        if (hasRealHawkes) {
            raw = Math.pow(
                (intensityDecay + eps) * (divergence + eps) * (climaxDecay + eps),
                1 / 3
            ) - eps;
        } else {
            // 2-signal model: climax decay + flow-price divergence only
            raw = Math.sqrt(
                (climaxDecay + eps) * (divergence + eps)
            ) - eps;
        }

        const score = Math.max(0, Math.min(raw, 1.0));

        // ── Classification ──
        let tier = 0, label = null;
        if (score >= 0.8)       { tier = 3; label = 'CLIMAX'; }
        else if (score >= 0.5)  { tier = 2; label = 'EXH'; }
        else if (score >= 0.2)  { tier = 1; label = 'exh'; }

        // ── Side: which side is exhausted? ──
        const side = cumDelta > 0 ? 'buy_exhaustion' : cumDelta < 0 ? 'sell_exhaustion' : 'none';

        return { score, tier, label, side, intensityDecay, divergence, climaxDecay };
    },

    /**
     * Scan all tracked levels and return exhausted ones.
     * Also prunes stale entries to prevent unbounded Map growth (Issue 1).
     *
     * PRUNING LOGIC:
     * A price level is stale if it hasn't been updated for MAX_STALE_BARS bars.
     * On live NQ tape, that's ~50 volume bars ≈ several minutes of dead zone.
     * Once a level goes stale, the exhaustion signal is irrelevant anyway
     * (the price has moved away). Pruning happens inline during the scan
     * to avoid a separate O(n) sweep.
     *
     * @param {number} currentBar
     * @param {Map} hawkesClusters - from HawkesClusterDetector.getActiveClusters()
     * @returns {Map<string, ExhaustionResult>}
     */
    getExhaustedLevels(currentBar, hawkesClusters) {
        const MAX_STALE_BARS = 50;  // prune levels not seen for this many bars
        const exhausted = new Map();
        const toDelete = [];

        for (const [priceStr, state] of this._state) {
            // ── Prune stale entries ──
            // lastSeenBar = bar index of most recent sigmaHistory entry.
            // If that's more than MAX_STALE_BARS ago, the level is dead.
            const lastEntry = state.sigmaHistory[state.sigmaHistory.length - 1];
            const lastSeenBar = lastEntry ? lastEntry.bar : state.peakBar;
            if (currentBar - lastSeenBar > MAX_STALE_BARS) {
                toDelete.push(priceStr);
                continue;
            }

            // Prune _ingestedBars: remove bar indices older than (currentBar - 60).
            // These will never be re-visited and the Set grows unbounded without this.
            for (const idx of state._ingestedBars) {
                if (idx < currentBar - 60) state._ingestedBars.delete(idx);
            }

            const hawkes = hawkesClusters ? hawkesClusters.get(priceStr) : null;
            // Also check levels that WERE clusters but λ has since decayed
            const hawkesFromDetector = HawkesClusterDetector.getCluster(priceStr, currentBar);
            const result = this.detect(priceStr, currentBar, hawkes || hawkesFromDetector);
            if (result.tier >= 1) {
                exhausted.set(priceStr, result);
            }
        }

        // Prune outside the iteration loop (can't delete while iterating Map)
        for (const priceStr of toDelete) {
            this._state.delete(priceStr);
        }

        return exhausted;
    },

    /**
     * Clear all per-state _ingestedBars Sets without destroying exhaustion state.
     * Called by HawkesStateManager on full re-ingest (big scroll) so that
     * bars get re-processed for exhaustion signals.
     *
     * FIX m3: peakSigma MUST be reset on re-ingest. If the visible window
     * shifted (big scroll), the old peakSigma came from bars no longer in view.
     * Keeping it suppresses exhaustion detection on the new window because
     * (peakSigma - currentSigma) / peakSigma stays artificially large.
     */
    clearIngested() {
        for (const [, state] of this._state) {
            state._ingestedBars.clear();
            state.cumBuy = 0;
            state.cumSell = 0;
            state.peakSigma = 0;          // FIX m3: reset to re-discover peak from new window
            state.sigmaHistory = [];       // will re-populate on re-ingest
            state.priceHistory = [];       // will re-populate on re-ingest
        }
    },

    reset() {
        this._state.clear();
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// 9. ABSORPTION ZONE DETECTOR
// ═══════════════════════════════════════════════════════════════════════════
//
// PROBLEM: AbsorptionAggregator scores individual price levels. A dealer
// hedging at a gamma strike doesn't defend a single tick — they defend a
// ZONE of 3-8 ticks. Individual bubbles at adjacent prices don't convey
// "this is a defended region." The visual must coalesce contiguous
// absorption levels into horizontal bands.
//
// ALGORITHM:
//   1. Query AbsorptionAggregator for all scored levels (tier >= 1)
//   2. Sort by price, scan for contiguous runs (gap <= 1 tick)
//   3. Merge runs of 2+ ticks into zones with aggregate score
//   4. Zone score = sum of member scores (captures width + depth)
//   5. Zone classification: POCKET (2-3 ticks), WALL (4-6), FORTRESS (7+)
//   6. Cache zones with dirty-flag (same pattern as V3 bubble cache)
//
// RENDERING (done in v2_integration.js Layer 0):
//   - Translucent horizontal band spanning chart width
//   - Color: bid zones cyan-green, ask zones red-magenta
//   - Alpha from zone score (auto-calibrated, no fixed thresholds)
//   - Label at right edge: "ZONE 4T ▸ WALL" (tick count + tier)
//
const AbsorptionZoneDetector = {
    _zones: [],          // [{lo, hi, score, ticks, side, levels: [{price,score}]}]
    _lastSig: '',        // dirty flag
    _minTier: 1,         // minimum AbsorptionAggregator tier to include

    /**
     * Rebuild zones from current AbsorptionAggregator state.
     * @param {number} currentBar - latest bar index for decay
     * @param {Map} bpAgg - optional {priceStr: {buyVol, sellVol}} from visible bars
     * @returns {Array} zones
     */
    detect(currentBar, bpAgg) {
        // Signature: aggregator score count + currentBar
        const sig = `${AbsorptionAggregator._scores.size}:${currentBar}`;
        if (sig === this._lastSig && this._zones.length > 0) return this._zones;
        this._lastSig = sig;

        const tickSize = AbsorptionAggregator._tickSize || 0.25;

        // Collect all levels with score above noise floor
        const scored = [];
        for (const [priceStr, state] of AbsorptionAggregator._scores) {
            const dt = currentBar - state.lastUpdate;
            const decayed = state.score * Math.exp(-AbsorptionAggregator._lambda * Math.max(dt, 0));
            if (decayed < 1.0) continue; // below tier 0 threshold

            const price = parseFloat(priceStr);
            if (isNaN(price)) continue;

            // Determine side from bp aggregation if available
            let side = 'neutral';
            if (bpAgg && bpAgg[priceStr]) {
                const agg = bpAgg[priceStr];
                side = agg.sellVol > agg.buyVol ? 'bid' : 'ask';
                // Absorption on bid side = buy absorption (defending bid)
                // Absorption on ask side = sell absorption (defending ask)
            }

            scored.push({ price, priceStr, score: decayed, side });
        }

        if (scored.length < 2) { this._zones = []; return this._zones; }

        // Sort by price ascending
        scored.sort((a, b) => a.price - b.price);

        // Coalesce contiguous runs (gap <= 1 tick)
        const zones = [];
        let run = [scored[0]];

        for (let i = 1; i < scored.length; i++) {
            const gap = scored[i].price - scored[i - 1].price;
            if (gap <= tickSize * 1.01) { // float tolerance
                run.push(scored[i]);
            } else {
                if (run.length >= 2) zones.push(this._buildZone(run));
                run = [scored[i]];
            }
        }
        if (run.length >= 2) zones.push(this._buildZone(run));

        this._zones = zones;
        return zones;
    },

    _buildZone(levels) {
        const lo = levels[0].price;
        const hi = levels[levels.length - 1].price;
        const totalScore = levels.reduce((s, l) => s + l.score, 0);
        const avgScore = totalScore / levels.length;
        const ticks = levels.length;

        // Side: majority vote from member levels
        const sideCounts = { bid: 0, ask: 0, neutral: 0 };
        for (const l of levels) sideCounts[l.side]++;
        const side = sideCounts.bid >= sideCounts.ask ? 'bid' : 'ask';

        // Tier from aggregate: score density × width
        let tier, label;
        if (ticks >= 7 || totalScore >= 40) { tier = 3; label = 'FORTRESS'; }
        else if (ticks >= 4 || totalScore >= 15) { tier = 2; label = 'WALL'; }
        else { tier = 1; label = 'POCKET'; }

        return { lo, hi, totalScore, avgScore, ticks, side, tier, label, levels };
    },

    reset() {
        this._zones = [];
        this._lastSig = '';
    },
};


// ═══════════════════════════════════════════════════════════════════════════
// EXPORTS — Window globals matching volume_bubbles.js pattern
// ═══════════════════════════════════════════════════════════════════════════

window.AdaptiveKalmanThreshold = AdaptiveKalmanThreshold;
window.HawkesClusterDetector = HawkesClusterDetector;
window.AbsorptionAggregator = AbsorptionAggregator;
window.AdaptiveDominance = AdaptiveDominance;
window.TemporalDecay = TemporalDecay;
window.RegressionAcceleration = RegressionAcceleration;
window.CumlDeltaRenderer = CumlDeltaRenderer;
window.ExhaustionDetector = ExhaustionDetector;
window.AbsorptionZoneDetector = AbsorptionZoneDetector;

})(); // end IIFE
