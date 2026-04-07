// ═══════════════════════════════════════════════════════════════════════════════
// SIGMA ENGINE v2 — EWMA Log-Transform Statistical Core + Iceberg Clustering
// ═══════════════════════════════════════════════════════════════════════════════
//
// Senior quant upgrade over v1:
//   1. EWMA (exponentially weighted) replaces simple mean — recency matters
//   2. Time-density clustering — detects iceberg/algo execution (many small clips)
//   3. Regime-aware thresholds — σ multiplier adjusts with market regime
//   4. Side-aware distributions — separate buy vs sell σ tracking
//   5. classifyTrade() — returns { tier, pctl, side } for any trade
//
// Key outputs (unchanged API for downstream consumers):
//   noiseFloor      = exp(μ + noiseσ·σ) - 1     → dynamic noise gate
//   sigThreshold    = exp(μ + sigσ·σ) - 1        → significant trade
//   instThreshold   = exp(μ + instσ·σ) - 1       → institutional outlier
//   marketVolatility = σ / max(μ, 0.01)          → regime-adaptive decay
//
// New outputs:
//   classifyTrade(vol, side) → { tier, pctl }
//   lastCluster             → { price, side, totalVol, count, duration } | null
// ═══════════════════════════════════════════════════════════════════════════════

'use strict';

const SigmaEngine = {
    // ── EWMA parameters ──
    _halfLife: 50,                // half-life in number of samples
    _alpha: 0,                    // computed from half-life
    _ewmaMean: 0,                 // EWMA of log-volumes
    _ewmaVar: 0,                  // EWMA of variance
    _initialized: false,
    _sampleCount: 0,

    // ── Side-aware EWMA ──
    _buyEwmaMean: 0, _buyEwmaVar: 0, _buyInit: false, _buyCount: 0,
    _sellEwmaMean: 0, _sellEwmaVar: 0, _sellInit: false, _sellCount: 0,

    // ── Rolling reservoir for percentile lookups ──
    _reservoir: [],
    _RESERVOIR_SIZE: 500,
    _reservoirDirty: false,
    _sortedCache: [],

    // ── Computed statistics (public API — backwards compatible) ──
    logAvg: 0,
    logStddev: 1,
    noiseFloor: 1,
    sigThreshold: 5,
    instThreshold: 999999,     // safe initial (Infinity breaks JSON.stringify)
    marketVolatility: 1.0,

    // ── Regime-adaptive σ multipliers ──
    _regime: 'transition',
    _REGIME_SIGMA: {
        'pin_mean_revert':      { noise: 1.2, sig: 2.0, inst: 3.0 },
        'long_gamma_stable':    { noise: 1.0, sig: 1.5, inst: 3.0 },
        'transition':           { noise: 1.0, sig: 1.5, inst: 2.5 },
        'short_gamma_volatile': { noise: 0.8, sig: 1.2, inst: 2.0 },
        'crash_tail_risk':      { noise: 0.6, sig: 1.0, inst: 1.8 },
    },

    // ── Time-density clustering (Iceberg Detector) ──
    _recentTrades: [],             // { price, side, ts, vol }
    _CLUSTER_WINDOW_MS: 500,       // trades within 500ms on same side = cluster
    _CLUSTER_MIN_CLIPS: 3,         // minimum clips to form a cluster
    lastCluster: null,             // latest detected cluster

    // ── Per-price absorption tracking (fed from data.abs) ──
    _absorption: {},

    // ── Throttle ──
    _lastRecompute: 0,
    _RECOMPUTE_INTERVAL: 300,      // recompute every 300ms (was 500)

    // ══════════════════════════════════════════════════════════
    //  INITIALIZATION
    // ══════════════════════════════════════════════════════════

    _init() {
        // Compute EWMA alpha from half-life: α = 1 - exp(-ln(2) / halfLife)
        this._alpha = 1.0 - Math.exp(-Math.LN2 / this._halfLife);
    },

    // ══════════════════════════════════════════════════════════
    //  FEED TRADES — EWMA + Clustering
    // ══════════════════════════════════════════════════════════

    /**
     * Feed raw trades from WebSocket into the sigma buffer.
     * @param {Array} trades - array of {p, v, s} or {price, volume, side} trade objects
     */
    feedTrades(trades) {
        if (!trades || !trades.length) return;
        if (!this._alpha) this._init();

        const now = performance.now();

        for (const trade of trades) {
            const v = trade.v !== undefined ? trade.v : trade.vol || trade.volume || 1;
            if (v <= 0) continue;

            const logV = Math.log(v + 1);

            // ── Global EWMA update ──
            if (!this._initialized) {
                this._ewmaMean = logV;
                this._ewmaVar = 0;
                this._initialized = true;
            } else {
                const delta = logV - this._ewmaMean;
                this._ewmaMean += this._alpha * delta;
                this._ewmaVar = (1 - this._alpha) * (this._ewmaVar + this._alpha * delta * delta);
            }
            this._sampleCount++;

            // ── Side-aware EWMA ──
            const side = trade.s || trade.side || trade.spin;
            const isBuy = side === 'buy' || side === 'B' || side === 'b' || side > 0;
            if (isBuy) {
                if (!this._buyInit) {
                    this._buyEwmaMean = logV; this._buyEwmaVar = 0; this._buyInit = true;
                } else {
                    const d = logV - this._buyEwmaMean;
                    this._buyEwmaMean += this._alpha * d;
                    this._buyEwmaVar = (1 - this._alpha) * (this._buyEwmaVar + this._alpha * d * d);
                }
                this._buyCount++;
            } else {
                if (!this._sellInit) {
                    this._sellEwmaMean = logV; this._sellEwmaVar = 0; this._sellInit = true;
                } else {
                    const d = logV - this._sellEwmaMean;
                    this._sellEwmaMean += this._alpha * d;
                    this._sellEwmaVar = (1 - this._alpha) * (this._sellEwmaVar + this._alpha * d * d);
                }
                this._sellCount++;
            }

            // ── Reservoir (for percentile lookups) ──
            this._reservoir.push(v);
            if (this._reservoir.length > this._RESERVOIR_SIZE) {
                this._reservoir.shift();
            }
            this._reservoirDirty = true;

            // ── Time-density clustering ──
            const price = trade.p || trade.price || 0;
            const ts = trade.ts || trade.timestamp || now;
            this._recentTrades.push({ price, side: isBuy ? 'buy' : 'sell', ts, vol: v });
        }

        // Trim cluster window to last 2 seconds max
        const cutoff = now - 2000;
        while (this._recentTrades.length > 0 && this._recentTrades[0].ts < cutoff) {
            this._recentTrades.shift();
        }

        // ── Detect clusters ──
        this._detectCluster(now);

        // ── Throttled threshold recompute ──
        if (now - this._lastRecompute > this._RECOMPUTE_INTERVAL) {
            this._recompute();
            this._lastRecompute = now;
        }
    },

    // ══════════════════════════════════════════════════════════
    //  ICEBERG / ALGO CLUSTER DETECTION
    // ══════════════════════════════════════════════════════════

    _detectCluster(now) {
        if (this._recentTrades.length < this._CLUSTER_MIN_CLIPS) return;

        // Group trades within CLUSTER_WINDOW_MS by (price, side)
        const windowStart = now - this._CLUSTER_WINDOW_MS;
        const inWindow = this._recentTrades.filter(t => t.ts >= windowStart);

        if (inWindow.length < this._CLUSTER_MIN_CLIPS) return;

        // Group by side (ignore price — algos may walk the book)
        const buys = inWindow.filter(t => t.side === 'buy');
        const sells = inWindow.filter(t => t.side === 'sell');

        for (const group of [buys, sells]) {
            if (group.length < this._CLUSTER_MIN_CLIPS) continue;

            const totalVol = group.reduce((s, t) => s + t.vol, 0);
            const avgPrice = group.reduce((s, t) => s + t.price, 0) / group.length;
            const duration = group[group.length - 1].ts - group[0].ts;

            // Only flag if the SYNTHETIC total is significant
            const logTotal = Math.log(totalVol + 1);
            const sigma = this._getRegimeSigma();
            const clusterThreshold = Math.exp(this._ewmaMean + sigma.sig * Math.sqrt(Math.max(0, this._ewmaVar))) - 1;

            if (totalVol > clusterThreshold) {
                this.lastCluster = {
                    price: Math.round(avgPrice * 100) / 100,
                    side: group[0].side,
                    totalVol,
                    count: group.length,
                    duration: Math.round(duration),
                    ts: now,
                    type: duration < 100 ? 'algo_burst' : 'iceberg_clip',
                };
            }
        }
    },

    // ══════════════════════════════════════════════════════════
    //  REGIME MANAGEMENT
    // ══════════════════════════════════════════════════════════

    /**
     * Set the current market regime (fed from backend via Socket.IO).
     * @param {string} regime - one of the regime keys
     */
    setRegime(regime) {
        if (this._REGIME_SIGMA[regime]) {
            this._regime = regime;
            // Immediately recompute thresholds with new regime
            this._recompute();
        }
    },

    _getRegimeSigma() {
        return this._REGIME_SIGMA[this._regime] || this._REGIME_SIGMA['transition'];
    },

    // ══════════════════════════════════════════════════════════
    //  TRADE CLASSIFICATION — The Core MM Tool
    // ══════════════════════════════════════════════════════════

    /**
     * Classify a single trade by its volume relative to the current regime.
     * Uses side-aware distributions when available.
     *
     * @param {number} volume - raw trade volume
     * @param {string} side - 'buy' or 'sell'
     * @returns {{ tier: string, pctl: number, regime: string }}
     *   tier: 'noise' | 'sig' | 'inst' | 'whale'
     *   pctl: percentile rank (0-100)
     */
    classifyTrade(volume, side) {
        if (!this._initialized || this._sampleCount < 10) {
            return { tier: 'noise', pctl: 50, regime: this._regime };
        }

        // Use side-aware σ if enough samples, else fall back to global
        const isBuy = side === 'buy' || side === 'B' || side === 'b';
        let mean, variance;

        if (isBuy && this._buyCount > 30) {
            mean = this._buyEwmaMean;
            variance = this._buyEwmaVar;
        } else if (!isBuy && this._sellCount > 30) {
            mean = this._sellEwmaMean;
            variance = this._sellEwmaVar;
        } else {
            mean = this._ewmaMean;
            variance = this._ewmaVar;
        }

        const logV = Math.log(volume + 1);
        const std = Math.sqrt(Math.max(0, variance));
        const sigma = this._getRegimeSigma();

        // Percentile via reservoir
        const pctl = this._percentileOf(volume);

        // Tier via regime-adjusted σ
        const instLine = mean + sigma.inst * std;
        const sigLine = mean + sigma.sig * std;
        const noiseLine = mean + sigma.noise * std;

        let tier;
        if (logV >= instLine + std) {
            tier = 'whale';   // beyond institutional — true outlier
        } else if (logV >= instLine) {
            tier = 'inst';    // institutional
        } else if (logV >= sigLine) {
            tier = 'sig';     // significant but not institutional
        } else {
            tier = 'noise';   // noise
        }

        return { tier, pctl: Math.round(pctl), regime: this._regime };
    },

    // ── Non-parametric percentile from reservoir ──
    _percentileOf(value) {
        if (this._reservoir.length < 20) return 50;
        if (this._reservoirDirty) {
            this._sortedCache = [...this._reservoir].sort((a, b) => a - b);
            this._reservoirDirty = false;
        }
        const n = this._sortedCache.length;
        let lo = 0, hi = n;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (this._sortedCache[mid] < value) lo = mid + 1;
            else hi = mid;
        }
        return (lo / n) * 100;
    },

    // ══════════════════════════════════════════════════════════
    //  ABSORPTION TRACKING (unchanged API)
    // ══════════════════════════════════════════════════════════

    feedAbsorption(absData) {
        if (!absData) return;
        this._absorption = absData;
    },

    getAbsorption(priceStr) {
        return this._absorption[priceStr] || null;
    },

    checkAbsorption(priceStr) {
        const abs = this._absorption[priceStr];
        if (!abs || abs.hits < 3) return { isAbsorb: false, inertia: 0 };

        const aggVol = (abs.side === 'bid') ? (abs.sell_vol || 0) : (abs.buy_vol || 0);
        const passive = Math.max(abs.passive_consumed || 1, 1);
        const inertia = aggVol / passive;
        const isAbsorb = abs.score > 1.0 && inertia > 1.0;

        return { isAbsorb, inertia };
    },

    // ══════════════════════════════════════════════════════════
    //  THRESHOLD RECOMPUTE (publishes backwards-compatible API)
    // ══════════════════════════════════════════════════════════

    _recompute() {
        if (!this._initialized || this._sampleCount < 10) return;

        const logAvg = this._ewmaMean;
        const logStddev = Math.sqrt(Math.max(0, this._ewmaVar));
        const sigma = this._getRegimeSigma();

        // ── Publish (backwards compatible) ──
        this.logAvg = logAvg;
        this.logStddev = logStddev;

        this.noiseFloor = Math.exp(logAvg + sigma.noise * logStddev) - 1;
        this.sigThreshold = Math.exp(logAvg + sigma.sig * logStddev) - 1;
        this.instThreshold = Math.exp(logAvg + sigma.inst * logStddev) - 1;

        this.marketVolatility = logStddev / Math.max(logAvg, 0.01);
    },

    // ══════════════════════════════════════════════════════════
    //  DEBUG / DIAGNOSTICS
    // ══════════════════════════════════════════════════════════

    getStats() {
        return {
            regime: this._regime,
            sigmaMultipliers: this._getRegimeSigma(),
            sampleCount: this._sampleCount,
            buySamples: this._buyCount,
            sellSamples: this._sellCount,
            reservoirSize: this._reservoir.length,
            ewmaMean: this._ewmaMean,
            ewmaStd: Math.sqrt(Math.max(0, this._ewmaVar)),
            noiseFloor: this.noiseFloor,
            sigThreshold: this.sigThreshold,
            instThreshold: this.instThreshold,
            marketVolatility: this.marketVolatility,
            lastCluster: this.lastCluster,
        };
    },
};

window.SigmaEngine = SigmaEngine;
