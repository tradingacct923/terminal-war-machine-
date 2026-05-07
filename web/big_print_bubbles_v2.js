/**
 * BIG PRINT BUBBLES — Premium signal-only volume bubbles
 *
 * Renders ONLY when a real signal fires:
 *   🔵 BLUE       block (institutional absorbed; book ≥ 2× size)
 *   🟠 ORANGE     sweep (consumed multiple levels in <200ms)
 *   🟣 MAGENTA    aggression (top-decile size, isolated)
 *   🟡 GOLD       FORTRESS / SOLID absorption (refill ≥ P75 + traded ≥ P50)
 *   🟨 YELLOW     exhaustion bar (4-cond pivot)
 *
 * Data sources (all backend-computed, zero magic numbers):
 *   - window._bigPrintMap         from Socket.IO 'big_print' (l2_worker._emit_big_print)
 *   - window._v2AbsBuffer         from candle_enriched.absorption (l2_worker absorption v2)
 *   - bar.originalData.sweeps     from candle_enriched.sweeps    (l2_worker._detect_sweep)
 *   - bar-level delta/range/vol   computed in renderer from bar OHLC
 *
 * Premium disc visual:
 *   Layer 0: dim halo (NO shadowBlur — uses larger semi-transparent disc)
 *   Layer 1: radial-gradient main disc (lighter center → base color edge)
 *   Layer 2: crisp 1.5px solid border in border color
 *   Layer 3: subtle white inner reflection arc (top-left)
 *   Layer 4: bold white number with dark drop shadow
 *
 * Anchors as a LightweightCharts custom series. Must be added by the chart
 * host (AtraxLiveChart) after createChart, then fed the same OHLC data as
 * the candlestick series so bar.x positions are populated.
 */
(function() {
'use strict';

// ── Color palette (matches the spec from MM design session) ──
const COLORS = {
    block:      { rgb: [30, 144, 255],   border: [22, 110, 200],  halo: 0.18 },
    sweep:      { rgb: [255, 122, 0],    border: [200, 95, 0],    halo: 0.22 },
    aggression: { rgb: [255, 20, 147],   border: [200, 16, 117],  halo: 0.16 },
    real_abs:   { rgb: [255, 184, 28],   border: [205, 145, 0],   halo: 0.24 }, // gold — most important signal
    exhaustion: { rgb: [255, 231, 0],    border: [200, 180, 0],   halo: 0.16 },
};

// ── Numerical-sanity constants (all categorized) ──
// CONFIGURED: 5-min baseline window already on backend (_PRINT_RING_SEC)
// STRUCTURAL: top-decile threshold (90th pct of rolling)
// DERIVED: 2× book ratio for absorption margin
// DERIVED: -1σ in log-space for exhaustion volume drop
// All other "constants" are visual styling (radius scaling, opacity, font sizes).
const PRIORITY = { sweep: 5, block: 4, real_abs: 3, aggression: 2, exhaustion: 1 };

// Renders one premium disc + label
// borderWidth: 2.5 for "important" classes (block/sweep/real_abs), 1.5 for others
// extreme: true → adds a concentric outer ring (top 1% of rolling 5-min)
function _drawPremiumDisc(ctx, cx, cy, r, color, label, mediaSize, borderWidth, extreme) {
    if (cy < -r * 2 || cy > mediaSize.height + r * 2) return;
    if (cx < -r * 2 || cx > mediaSize.width + r * 2) return;
    const [rR, gG, bB] = color.rgb;
    const [bdR, bdG, bdB] = color.border;
    const bw = borderWidth || 1.5;

    // Layer −1: EXTREME emphasis — top 1% of rolling 5-min distribution
    // Two concentric rings + bright outer halo. Visually unmistakable
    // for genuinely extraordinary prints regardless of regime.
    // STRUCTURAL: percentile-derived flag from backend (no σ assumption).
    if (extreme) {
        // Bright outer halo ring
        ctx.beginPath();
        ctx.arc(cx, cy, r + 16, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${rR},${gG},${bB},0.20)`;
        ctx.fill();
        // Outer crisp ring (white-ish for max contrast)
        ctx.beginPath();
        ctx.arc(cx, cy, r + 14, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(255,255,255,0.55)`;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        // Inner color ring (matches signal class)
        ctx.beginPath();
        ctx.arc(cx, cy, r + 10, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${rR},${gG},${bB},0.95)`;
        ctx.lineWidth = 2.5;
        ctx.stroke();
    }

    // Layer 0: halo (larger dim disc — NO shadowBlur per CLAUDE.md)
    ctx.beginPath();
    ctx.arc(cx, cy, r + 6, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${rR},${gG},${bB},${color.halo})`;
    ctx.fill();

    // Layer 1: radial-gradient main disc — minimal lighten preserves hue
    // Opacity tuned so candles remain readable through the bubble (~50% see-through
    // at edge). Matches the reference image's "translucent over price action" look.
    const grad = ctx.createRadialGradient(
        cx - r * 0.3, cy - r * 0.3, 0,
        cx, cy, r
    );
    const lighten = (v) => Math.min(255, Math.round(v + 15));
    grad.addColorStop(0, `rgba(${lighten(rR)},${lighten(gG)},${lighten(bB)},0.65)`);
    grad.addColorStop(0.7, `rgba(${rR},${gG},${bB},0.50)`);
    grad.addColorStop(1, `rgba(${bdR},${bdG},${bdB},0.45)`);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = grad;
    ctx.fill();

    // Layer 2: crisp solid border (variable width — emphasizes important signals)
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(${bdR},${bdG},${bdB},0.98)`;
    ctx.lineWidth = bw;
    ctx.stroke();

    // Layer 3: subtle inner reflection (3D feel)
    if (r >= 14) {
        ctx.beginPath();
        ctx.arc(cx - r * 0.25, cy - r * 0.25, r * 0.4, Math.PI * 0.7, Math.PI * 1.3);
        ctx.strokeStyle = 'rgba(255,255,255,0.25)';
        ctx.lineWidth = 1;
        ctx.stroke();
    }

    // Layer 4: bold white number
    if (r >= 12 && label) {
        const fontSize = r < 20 ? 10 : r < 32 ? 12 : 14;
        ctx.font = `700 ${fontSize}px "Inter", "JetBrains Mono", "SF Mono", monospace`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        // Drop shadow
        ctx.fillStyle = 'rgba(0,0,0,0.65)';
        ctx.fillText(label, cx + 0.5, cy + 1);
        // Main text
        ctx.fillStyle = 'rgba(255,255,255,1)';
        ctx.fillText(label, cx, cy);
    }
}

// Edge marker for off-screen bubbles — small disc with directional arrow
// at the top or bottom of the chart when a signal's price is outside the
// visible viewport. Lets the user know signals exist even when zoomed away.
function _drawEdgeMarker(ctx, cx, cy, color, label, dir) {
    const [rR, gG, bB] = color.rgb;
    const [bdR, bdG, bdB] = color.border;
    // Smaller filled disc
    ctx.beginPath();
    ctx.arc(cx, cy, 9, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${rR},${gG},${bB},0.75)`;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, cy, 9, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(${bdR},${bdG},${bdB},0.95)`;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    // Direction arrow inside disc
    ctx.font = '700 12px "Inter", "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = 'rgba(255,255,255,0.95)';
    ctx.fillText(dir === 'above' ? '↑' : '↓', cx, cy);
    // Label tag above or below the disc
    if (label) {
        ctx.font = '700 9px "Inter", "JetBrains Mono", monospace';
        const labelY = dir === 'above' ? cy + 16 : cy - 16;
        ctx.fillStyle = 'rgba(0,0,0,0.7)';
        ctx.fillText(label, cx + 0.5, labelY + 0.5);
        ctx.fillStyle = `rgba(${rR + 30},${gG + 30},${bB + 30},1)`;
        ctx.fillText(label, cx, labelY);
    }
}

// Render either the full premium disc OR an edge marker based on y position.
// Always shows SOMETHING — never silently drops a bubble for being off-screen.
function _drawDiscOrEdgeMarker(ctx, cx, cy, r, color, label, mediaSize, borderW, extreme) {
    if (cy >= 0 && cy <= mediaSize.height) {
        _drawPremiumDisc(ctx, cx, cy, r, color, label, mediaSize, borderW, extreme);
    } else if (cy < 0) {
        _drawEdgeMarker(ctx, cx, 14, color, label, 'above');
    } else {
        _drawEdgeMarker(ctx, cx, mediaSize.height - 14, color, label, 'below');
    }
}

// Format volume number for the bubble label
function _fmtVol(v) {
    if (v >= 10000) return Math.round(v / 1000) + 'k';
    if (v >= 1000) return (v / 1000).toFixed(1) + 'k';
    return String(Math.round(v));
}

// Bubble radius from print size relative to recent-distribution P90.
// Sizing balances two needs:
//   1. Big prints must stand out — log-σ scaling on size/P90 ratio
//   2. Bubbles must NOT engulf adjacent candles — clamped to barSpacing
//
// The clamp uses barSpacing × 3.5 as the max radius:
//   - barSpacing 3px (very zoomed out)  → max ~10px (tight, doesn't smother chart)
//   - barSpacing 8px (typical)          → max ~28px (visible, not overwhelming)
//   - barSpacing 12px (Morning NQ Scalp)→ max 42px
//   - barSpacing 20px+ (zoomed in)      → max 50px (hard ceiling)
//
// Min radius enforces visibility at extreme zoom-out. STRUCTURAL = bound to
// data-derived bar width, no magic absolute thresholds.
//
//   ratio=1 (at P90)      → 14px disc (× sizeScale × clamp)
//   ratio=2 (top 5%)      → ~22px
//   ratio=4 (top 1%)      → ~30px
//   ratio=8+ (extreme)    → 38px–50px cap
function _radiusForSize(size, p90, barSpacing, sizeScale) {
    const baseP90 = (!p90 || p90 < 1) ? Math.max(size, 1) : p90;
    const ratio = size / baseP90;
    let r = (14 + Math.log2(Math.max(ratio, 1)) * 8) * (sizeScale || 1.0);
    // Zoom-aware clamp — bubble can't exceed ~3.5× the candle's pixel width,
    // so it never visually overwhelms adjacent candles.
    const bs = barSpacing || 12;
    const maxByBar = Math.max(12, Math.min(50, bs * 3.5));
    const minVisible = Math.max(8, Math.min(14, bs * 1.0));
    return Math.min(Math.max(r, minVisible), maxByBar);
}

// Read user settings from window.BubbleSettings (populated by the panel).
// All defaults preserve current behavior if panel hasn't loaded yet.
function _settings() {
    const s = window.BubbleSettings || {};
    return {
        showBlock:      s.showBlock      !== false,
        showSweep:      s.showSweep      !== false,
        showAggression: s.showAggression !== false,
        showRealAbs:    s.showRealAbs    !== false,
        showExhaustion: s.showExhaustion !== false,
        sizeScale:      Math.max(0.3, Math.min(2.5, +s.sizeScale || 1.0)),
        minSize:        Math.max(0, +s.minSize || 0),
    };
}

// Subscribe to candle_enriched events to build a bp-by-bar lookup.
// This gives the renderer access to REAL per-price buy/sell volume per bar
// (used for true delta + stacked imbalance instead of OHLC-derived proxies).
// Wired once on script load.
(function _wireEnrichedListener() {
    if (typeof window.AltarisEvents === 'undefined') {
        // Bus not ready yet — retry shortly
        setTimeout(_wireEnrichedListener, 500);
        return;
    }
    if (window._bpByBar_wired) return;
    window._bpByBar_wired = true;
    if (!window._bpByBar) window._bpByBar = {};
    // DST-aware: delegates to bootstrap helper. Computed lazily on each
    // event so a long-lived session that crosses a DST transition picks
    // up the new offset without a page reload (the helper caches per-min).
    const _etOffS = () => (typeof window._getETOffsetSec === 'function')
        ? window._getETOffsetSec() : 14400;
    window.AltarisEvents.on('data:candle:enriched', (ev) => {
        try {
            if (!ev || !ev.bp || ev.symbol !== 'NQ') return;
            // Backend emits the bar boundary as `time` (UTC seconds — see
            // l2_worker.py:1310). Chart bar.time is ET seconds (post-offset).
            const utcSec = ev.time || ev.t || 0;
            const etSec = utcSec - _etOffS();
            window._bpByBar[etSec] = ev.bp;
            // Hard cap on map size — drop the oldest keys when over.
            // The earlier "if length > 200, drop entries older than 1hr"
            // pattern grew unboundedly on fast-bar timeframes (1s/15s
            // bars produce >200 keys/hour, all newer than the 1hr cutoff,
            // so nothing was deleted).
            const KEYS_CAP = 600;
            const keys = Object.keys(window._bpByBar);
            if (keys.length > KEYS_CAP) {
                const sorted = keys.map(Number).sort((a, b) => a - b);
                const dropCount = sorted.length - KEYS_CAP;
                for (let i = 0; i < dropCount; i++) {
                    delete window._bpByBar[sorted[i]];
                }
            }
        } catch (_) {}
    });
})();

// Compute exhaustion bars (4-condition test) over visible range
// Returns: Set<barIdx> for bars that are "exhausting"
//   1) prior 3 bars same REAL delta sign (trend established) — uses bp Σ(buy−sell)
//   2) log(vol) drop ≥ 1σ vs prior 3 bars (in log-space)
//   3) range[t] ≤ median(range[t-3..t-1])  (range compression)
//   4) sign(delta[t]) flipped OR |delta[t]| < 25th pct of prior 3
function _computeExhaustionBars(bars, from, to) {
    const exhSet = new Set();
    if (to - from < 4) return exhSet;
    const bpByBar = window._bpByBar || {};
    // Pre-compute per-bar REAL delta + vol + range (uses bp when available, falls
    // back to OHLC proxy only for history bars without bp — backfill candles)
    const stats = [];
    for (let i = from; i < to; i++) {
        const bar = bars[i];
        if (!bar) { stats.push(null); continue; }
        const tSec = bar.time || bar.originalData?.time || 0;
        const o = bar.originalData?.open ?? 0;
        const c = bar.originalData?.close ?? 0;
        const v = bar.originalData?.volume ?? 0;
        const h = bar.originalData?.high ?? 0;
        const l = bar.originalData?.low ?? 0;
        let delta;
        const bp = bpByBar[tSec];
        if (bp) {
            // REAL delta from per-price buy/sell aggregation
            let buy = 0, sell = 0;
            for (const k in bp) {
                const e = bp[k];
                if (e && e.length >= 2) {
                    buy  += e[0] || 0;
                    sell += e[1] || 0;
                }
            }
            delta = buy - sell;
        } else {
            // Fallback for history bars without bp (backfill, server restart, etc.)
            const dirSign = (c >= o) ? 1 : -1;
            const range = Math.max(h - l, 0.001);
            const closePos = (c - l) / range;
            delta = v * (2 * closePos - 1);
        }
        stats.push({ delta, vol: v, range: h - l });
    }
    // Visible-range medians (for range compress test)
    const allRanges = stats.filter(s => s).map(s => s.range);
    const allLogVols = stats.filter(s => s && s.vol > 0).map(s => Math.log(s.vol + 1));
    if (allLogVols.length < 5) return exhSet;
    const logMu = allLogVols.reduce((a, b) => a + b, 0) / allLogVols.length;
    const logVar = allLogVols.reduce((s, v) => s + (v - logMu) ** 2, 0) / allLogVols.length;
    const logSig = Math.sqrt(logVar);
    if (logSig <= 0) return exhSet;

    for (let i = from + 3; i < to; i++) {
        const idx = i - from;
        const cur = stats[idx];
        const p1 = stats[idx - 1], p2 = stats[idx - 2], p3 = stats[idx - 3];
        if (!cur || !p1 || !p2 || !p3) continue;
        // 1) trend established
        const sigs = [Math.sign(p3.delta), Math.sign(p2.delta), Math.sign(p1.delta)];
        if (sigs[0] === 0 || sigs[0] !== sigs[1] || sigs[1] !== sigs[2]) continue;
        const trendDir = sigs[0];
        // 2) volume drop ≥ 1σ in log-space
        const curLogVol = Math.log(cur.vol + 1);
        if (curLogVol > logMu - logSig) continue;
        // 3) range compression
        const priorRanges = [p1.range, p2.range, p3.range].sort((a, b) => a - b);
        const medRange = priorRanges[1];
        if (cur.range > medRange) continue;
        // 4) pivot: delta sign flipped OR magnitude very small
        const curSign = Math.sign(cur.delta);
        const priorMags = [Math.abs(p1.delta), Math.abs(p2.delta), Math.abs(p3.delta)].sort((a, b) => a - b);
        const p25 = priorMags[0];
        const pivot = (curSign !== trendDir) || (Math.abs(cur.delta) < p25);
        if (!pivot) continue;
        exhSet.add(i);
    }
    return exhSet;
}

class BigPrintBubbleRenderer {
    constructor() {
        this._data = null;
        this._activeSymbol = 'NQ';
    }
    update(data) { this._data = data; }
    setSymbol(sym) { if (sym) this._activeSymbol = sym; }

    draw(target, priceConverter) {
        const d = this._data;
        // Earliest-possible trace — fires BEFORE any early returns so we can
        // see whether draw() is even being called and what state d is in.
        window._BUBBLE_TRACE = {
            phase: 'entered_draw',
            d_exists: !!d,
            d_bars_len: (d && d.bars) ? d.bars.length : 'no_bars_field',
            d_keys: d ? Object.keys(d) : [],
            visibleRange: d ? d.visibleRange : null,
            version: 'v2_per_bar_dedup',
        };
        if (!d || !d.bars || d.bars.length === 0) return;
        const sym = this._activeSymbol;

        try { target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
            const { from, to } = d.visibleRange;
            if (to - from < 1) return;
            // Read live settings + chart zoom level
            const settings = _settings();
            const barSpacing = (typeof d.barSpacing === 'number' && d.barSpacing > 0) ? d.barSpacing : 12;

            // ⚠ Timezone: bar.originalData.time is in ET seconds (LWC
            // convention, post ET_OFFSET subtraction in AtraxLiveChart.onHistory).
            // big_print event ts is UTC ms (from l2_worker._emit_big_print).
            //
            // TIMEFRAME-AGNOSTIC: build a sorted list of bars and use binary
            // search to find the bar that CONTAINS each event's timestamp.
            // Works for any timeframe (1s, 1m, 5m, 15m, 1h, …) because we
            // map "event time falls in this bar's time range" not "event
            // minute-bucket equals bar minute-bucket".
            //
            // DST-aware: source of truth lives in window._getETOffsetSec
            // (bootstrap helper). EDT = 4*3600, EST = 5*3600.
            const ET_OFFSET_MS = ((typeof window._getETOffsetSec === 'function')
                ? window._getETOffsetSec() : 14400) * 1000;
            const sortedBars = [];
            // ⚠ LWC v4 STRIPS the `time` field from originalData (since time
            // is already used as the bar's logical key). In the renderer, the
            // bar object has:
            //   bar.time          = logical bar INDEX (1, 2, 3, …)
            //   bar.x             = pixel X coordinate (this is correct)
            //   bar.originalData  = {open, high, low, close} — NO time field!
            //   bar.wb            = sometimes present (live-updated bars only)
            // To recover the actual UTC seconds we look at the candle series's
            // own data array — its index aligns with bar.time perfectly. The
            // candle series is stored at `window.AtraxLiveChart._series` and
            // its `.data()[barIdx].time` gives us ET-seconds (post-offset).
            const _candleSeries = window.AtraxLiveChart && window.AtraxLiveChart._series;
            const _candleData = _candleSeries ? _candleSeries.data() : null;
            for (let i = from; i < to; i++) {
                const bar = d.bars[i];
                if (!bar) continue;
                // Try originalData.time, then bar.wb (live update), then
                // candle-series lookup by index. Last resort is bar.time
                // which is just the index but we sanity-guard < 100M.
                let tSec = bar.originalData?.time
                        || bar.wb
                        || (_candleData && _candleData[bar.time] ? _candleData[bar.time].time : 0)
                        || 0;
                // Sanity: real epoch seconds are > ~10^9. If tSec is small (<10^8),
                // it's an index masquerading as a timestamp — skip it.
                if (tSec > 100_000_000) sortedBars.push({ x: bar.x, time: tSec, idx: i });
            }
            sortedBars.sort((a, b) => a.time - b.time);  // defensive — usually pre-sorted by LWC

            // Binary-search: largest bar.time ≤ event_time, with tolerance.
            // ⚠ Without the tolerance, events whose timestamp is AFTER the last
            // visible bar all match that last bar (binary search returns the
            // largest ≤ target). At zoom levels where many recent events are
            // off-screen-right, this caused all their bubbles to stack at the
            // rightmost visible bar's X position. The tolerance forces a null
            // return when the event is beyond a reasonable proximity, so those
            // bubbles drop out of the visible render entirely.
            //
            // STRUCTURAL: tolerance = 1.5× 1m bar duration = 90s. Higher TFs
            // (5m, 15m) will have proportionally larger gaps between bars and
            // would need a TF-aware tolerance — this fix is sufficient for 1m
            // and degrades gracefully on other TFs.
            const BAR_TIME_TOLERANCE_S = 90;
            const _findContainingBar = (eventTsMs) => {
                if (sortedBars.length === 0) return null;
                const eventSec = (eventTsMs - ET_OFFSET_MS) / 1000;
                let lo = 0, hi = sortedBars.length - 1, best = null;
                while (lo <= hi) {
                    const mid = (lo + hi) >> 1;
                    if (sortedBars[mid].time <= eventSec) {
                        best = sortedBars[mid];
                        lo = mid + 1;
                    } else {
                        hi = mid - 1;
                    }
                }
                // If event is more than tolerance past the matched bar, it's
                // off-screen-right — don't anchor it (would cause column stacking).
                if (best && (eventSec - best.time) > BAR_TIME_TOLERANCE_S) return null;
                return best;
            };

            ctx.save();

            // ════════════════════════════════════════════════════════════
            // LAYER 0: BAR-LEVEL SIGNALS (absorption / exhaustion / aggression)
            //
            // PRIMARY signal layer — bar-close detected events. Each bar produces
            // at most 3 signals (one per type). Renders at the signal's price.
            //
            // SPATIAL DEDUP: when the detector fires on consecutive bars at
            // narrow zoom, bubbles overlap visually into vertical columns.
            // Solution: build a flat list of all signals, sort time-DESCENDING
            // (newest first) so recent activity dominates, and skip any new
            // bubble whose center falls within ~1.4× radius of a previously-
            // rendered bubble of the same color/type. Also caps total visible
            // bar-signal bubbles at 24 to prevent visual chaos.
            // ════════════════════════════════════════════════════════════
            const barSignalMap = window._barSignalMap || {};
            const _flatSigs = [];
            // DEBUG counters — surface why bubbles drop out at narrow zoom
            const _trace = { total: 0, wrong_sym: 0, no_bar: 0, ok: 0 };
            for (const k in barSignalMap) {
                _trace.total++;
                const sig = barSignalMap[k];
                if (!sig || sig.symbol !== sym) { _trace.wrong_sym++; continue; }
                const bar = _findContainingBar(((sig.t || 0) * 1000));
                if (!bar) { _trace.no_bar++; continue; }
                _trace.ok++;
                const barX = bar.x;
                const ts = sig.t || 0;
                // ────────────────────────────────────────────────────────────
                // ABSORPTION — PREMIUM-ONLY filter (context layer).
                //
                // Backtest (n=522 fires): detector fires on 87% of bars.
                // Painted as-is, the chart becomes a noise wall — every
                // candle is covered. The trader can't tell which level was
                // actually significant.
                //
                // Two filters narrow the rendered set to bars where the
                // microstructure was genuinely extreme:
                //   1. refill_class ∈ {instant, fast}   — MM same-tick / 1-2-tick refill
                //   2. strength ≥ 2.5                   — top 1/3 of fires by z-score
                //   ── OR ──
                //   strength ≥ 3.5                      — extreme outlier (top ~10%)
                //
                // Result: ~15-25 bubbles visible per session instead of 120+,
                // each marking a level where MM defense was unambiguous.
                // The remaining 80%+ of "DEF" fires still log to disk for
                // later analysis but DO NOT paint, preserving the chart.
                //
                // Label reads "DEF {vol}" — same contextual meaning, no
                // directional implication.
                // ────────────────────────────────────────────────────────────
                if (sig.absorption && sig.absorption.length > 0 && settings.showRealAbs) {
                    for (const abs of sig.absorption.slice(0, 1)) {
                        const strength = abs.strength || 0;
                        const refillClass = abs.refill_class;
                        const premium = (refillClass === 'instant' || refillClass === 'fast') && strength >= 2.5;
                        const extreme = strength >= 3.5;
                        if (!premium && !extreme) continue;
                        _flatSigs.push({
                            type:    'absorption',
                            ts:      ts,
                            x:       barX,
                            price:   abs.price,
                            label:   `DEF ${_fmtVol(abs.volume)}`,
                            color:   COLORS.real_abs,
                            volume:  abs.volume,
                            strength: strength,
                            extreme: extreme,
                            borderW: extreme ? 3.5 : 2.5,
                        });
                    }
                }
                // EXHAUSTION — currently 0 fires in 9hr window. When it does
                // fire, the side IS directional by definition (sell_exhaustion
                // = sellers gave up = expect bounce up). Keep arrows here.
                if (sig.exhaustion && settings.showExhaustion) {
                    const exh = sig.exhaustion;
                    _flatSigs.push({
                        type:    'exhaustion',
                        ts:      ts,
                        x:       barX,
                        price:   exh.price,
                        label:   `${exh.side === 'sell_exhaustion' ? '▼' : '▲'}${_fmtVol(exh.volume)}`,
                        color:   COLORS.exhaustion,
                        volume:  exh.volume,
                        strength: exh.strength || 0,
                        extreme: (exh.strength || 0) >= 0.5,
                        borderW: 2.5,
                    });
                }
                // CLASSICAL ABSORPTION — added 2026-05-06.
                // Delta-price divergence: one-sided flow that didn't move price.
                // Signal: take the OPPOSITE side (buyers absorbed → SHORT).
                // Label: ⊘<vol> (circle-slash = "stuck")
                //   ⊘B<vol>  buyers absorbed, expect DOWN
                //   ⊘S<vol>  sellers absorbed, expect UP
                if (sig.classical_absorption && (settings.showRealAbs || settings.showClassicalAbs !== false)) {
                    const cabs = sig.classical_absorption;
                    const isBuy = cabs.side === 'classical_buy_absorbed';
                    const strength = cabs.strength || 0;
                    _flatSigs.push({
                        type:    'classical_absorption',
                        ts:      ts,
                        x:       barX,
                        price:   cabs.price,
                        label:   `⊘${isBuy ? 'B' : 'S'}${_fmtVol(cabs.volume)}`,
                        color:   isBuy ? COLORS.sell : COLORS.buy,   // INVERTED — signal_dir
                        volume:  cabs.volume,
                        strength: strength,
                        extreme: strength >= 100,                    // +100 strength = climactic
                        borderW: strength >= 100 ? 3.0 : 2.0,
                    });
                }
                // AGGRESSION — sweep direction IS the trade tape side. The 1
                // fire we have (09:36 EDT, 27,378.75) hit its directional
                // target (+3.50 pts in next bar). Keep arrow.
                if (sig.aggression && (settings.showSweep || settings.showAggression)) {
                    const agg = sig.aggression;
                    _flatSigs.push({
                        type:    'aggression',
                        ts:      ts,
                        x:       barX,
                        price:   agg.price,
                        label:   `${agg.side === 'buy_aggression' ? '↑' : '↓'}${_fmtVol(agg.volume)}·${agg.levels}L`,
                        color:   COLORS.sweep,
                        volume:  agg.volume,
                        strength: agg.strength || 0,
                        extreme: (agg.strength || 0) >= 0.6,
                        borderW: 2.5,
                    });
                }
            }

            // Sort: STRONGEST first (so when we hit the cap we keep the
            // most significant fires). Ties broken by recency.
            _flatSigs.sort((a, b) => (b.strength - a.strength) || (b.ts - a.ts));

            // ════════════════════════════════════════════════════════════════
            // PER-BAR DEDUP + STRENGTH-FIRST CAP
            //
            // After the premium filter (strength ≥ 2.5 + fast refill, OR
            // strength ≥ 3.5 alone), most sessions yield 15-25 absorption
            // bubbles. We hard-cap at 30 so even a heavy MM-defense session
            // never exceeds a readable density. Bubbles are sorted by strength
            // DESC, so the cap drops the WEAKEST premium fires first if there
            // are too many.
            //
            // Per-bar X-bucket dedup (4-px buckets) prevents double-rendering
            // at the same bar position but allows many bubbles across the
            // visible window.
            // ════════════════════════════════════════════════════════════════
            const _renderedSigs = [];
            const _seenBuckets = new Set();   // `${type}|${xBucket}` keys
            const MAX_BAR_BUBBLES = 30;
            const X_BUCKET_PX = 4;
            const _battleMap = window._battleStateMap || {};
            for (const s of _flatSigs) {
                if (_renderedSigs.length >= MAX_BAR_BUBBLES) break;
                const y = priceConverter(s.price);
                if (y === null || y === undefined || isNaN(y)) continue;
                const xBucket = Math.round(s.x / X_BUCKET_PX);
                const key = `${s.type}|${xBucket}`;
                if (_seenBuckets.has(key)) continue;
                _seenBuckets.add(key);
                const r = _radiusForSize(s.volume, s.volume / Math.max(s.strength, 0.5),
                                         barSpacing, settings.sizeScale);
                _drawDiscOrEdgeMarker(ctx, s.x, y, r, s.color, s.label, mediaSize, s.borderW, s.extreme);
                _renderedSigs.push({ type: s.type, x: s.x, y: y, r: r });

                // ── BATTLE STATE DOT — adjacent to absorption bubbles only ──
                // Look up battle verdict for this bar+price. Renders a small
                // colored dot to the RIGHT of the absorption bubble:
                //   tier A+ ABSORBER_WINS_HIGH → 2 green dots (highest conviction)
                //   tier A  ABSORBER_WINS      → 1 green dot
                //   tier B  AGGRESSOR_WINS     → 1 red dot (INVERSE direction trade)
                if (s.type === 'absorption') {
                    const bsKey = `${sym}:1m:${s.ts}`;
                    const bs = _battleMap[bsKey];
                    if (bs && bs.label && bs.label !== 'NO_SIGNAL') {
                        // Match by price too — bar can have multiple absorption levels
                        if (Math.abs((bs.K || 0) - s.price) <= 0.5) {
                            const dotR = Math.max(2, Math.min(4, r * 0.25));
                            const gap = r + dotR + 2;
                            const dotColor = (bs.tier === 'B') ? '#ff4d4d' : '#28d97a';
                            const isDouble = (bs.tier === 'A+');
                            ctx.save();
                            ctx.fillStyle = dotColor;
                            ctx.beginPath();
                            ctx.arc(s.x + gap, y, dotR, 0, Math.PI * 2);
                            ctx.fill();
                            if (isDouble) {
                                ctx.beginPath();
                                ctx.arc(s.x + gap + dotR * 2 + 1, y, dotR, 0, Math.PI * 2);
                                ctx.fill();
                            }
                            ctx.restore();
                        }
                    }
                }
            }
            // DEBUG: expose render trace for inspection from devtools console
            window._BUBBLE_TRACE = {
                ...(_trace),
                flatSigs: _flatSigs.length,
                rendered: _renderedSigs.length,
                visibleBars: sortedBars.length,
                d_bars_len: d.bars ? d.bars.length : 'no_bars',
                d_from: from, d_to: to,
                first_bar_keys: d.bars && d.bars[from] ? Object.keys(d.bars[from]) : null,
                first_bar_origData_time: d.bars && d.bars[from] && d.bars[from].originalData ? d.bars[from].originalData.time : null,
                first_bar_time: d.bars && d.bars[from] ? d.bars[from].time : null,
                first_bar_x: d.bars && d.bars[from] ? d.bars[from].x : null,
                version: 'v2_per_bar_dedup',
            };

            // ════════════════════════════════════════════════════════════
            // LAYER 1: Big print events (block / sweep / aggression)
            //
            // DISABLED — superseded by bar-level signals (LAYER 0 above).
            // Print-level events created visual chaos overlapping bar signals
            // at the same (bar, price). Bar signals are senior-grade with
            // multi-leg confirmation; print-level is the early naive system.
            // The big_print socket events still flow into window._bigPrintMap
            // (preserved for future use / forensics) but are no longer rendered.
            const _BIG_PRINT_LAYER_ENABLED = false;
            if (_BIG_PRINT_LAYER_ENABLED) {
            //
            // Aggregation pipeline (timeframe-agnostic):
            //   1. Walk window._bigPrintMap (filtered by symbol + class toggles)
            //   2. For each event, find containing bar via binary search
            //   3. Bucket by `${barIdx}:${priceKey}` — multiple events at the
            //      same (bar, price) coalesce: sum sizes, keep highest-priority
            //      class (sweep > block > aggression)
            //   4. Render one disc per bucket
            //
            // This means on 5m bars, all 5 minutes of events at e.g. 27420
            // collapse into one disc with summed contract count, classified
            // by the most aggressive print in that window. On 1m bars,
            // typically 1 event per (bar,price) — same as before.
            // ════════════════════════════════════════════════════════════
            const bpMap = window._bigPrintMap || {};
            const eventsByBucket = new Map(); // `${barIdx}:${price}` → aggregated event
            for (const k in bpMap) {
                const ev = bpMap[k];
                if (!ev || ev.symbol !== sym) continue;
                if (ev.size < settings.minSize) continue;
                if (ev.classification === 'block'      && !settings.showBlock)      continue;
                if (ev.classification === 'sweep'      && !settings.showSweep)      continue;
                if (ev.classification === 'aggression' && !settings.showAggression) continue;
                if (!COLORS[ev.classification]) continue;
                const bar = _findContainingBar(ev.ts);
                if (!bar) continue;
                const priceKey = (+ev.price).toFixed(2);
                const bucketKey = `${bar.idx}:${priceKey}`;
                const existing = eventsByBucket.get(bucketKey);
                // Level-defended upgrade: if this print landed on a FORTRESS or
                // SOLID level (per absorption v2 engine), the cross-stream signal
                // is way stronger than just "big print." We surface this with the
                // gold (real_abs) color regardless of base classification — the
                // base class is preserved in `base_class` so MM still knows
                // whether it was a block/sweep/aggression that hit defense.
                const isAtDefense = ev.at_level_tier === 'FORTRESS' || ev.at_level_tier === 'SOLID';
                if (!existing) {
                    eventsByBucket.set(bucketKey, {
                        x: bar.x,
                        price: ev.price,
                        size: ev.size,
                        classification: ev.classification,
                        base_class: ev.classification,
                        at_level_tier: ev.at_level_tier || null,
                        refill_class: ev.refill_class || null,
                        p90: ev.p90 || ev.size,
                        // Extreme survives across aggregation: if ANY event in
                        // the bucket was top-1% (P99), the bucket is extreme.
                        extreme: !!ev.extreme,
                        // Mark for visual upgrade in render loop
                        defended: isAtDefense,
                    });
                } else {
                    existing.size += ev.size;
                    if (PRIORITY[ev.classification] > PRIORITY[existing.classification]) {
                        existing.classification = ev.classification;
                        existing.base_class = ev.classification;
                    }
                    if ((ev.p90 || 0) > existing.p90) existing.p90 = ev.p90;
                    if (ev.extreme) existing.extreme = true;
                    if (isAtDefense) {
                        existing.defended = true;
                        existing.at_level_tier = ev.at_level_tier;
                    }
                    if (ev.refill_class && !existing.refill_class) {
                        existing.refill_class = ev.refill_class;
                    }
                }
            }

            // ── COALESCE adjacent prices in same bar ──
            // Real footprint chart behavior: a sweep across 10 ticks should
            // appear as ONE principal bubble at the most-traded level, not 10
            // separate stacked dots. Group buckets where bar matches AND prices
            // are within ±COALESCE_TICKS, sum sizes, anchor at price with
            // greatest individual volume (the "principal").
            //
            // STRUCTURAL: the ±2 tick range means a "cluster" = within 0.5pt
            // on NQ (which is exactly the spread of a typical fast sweep).
            // Larger ranges would over-merge unrelated activity.
            const COALESCE_TICK_RANGE = 2;  // ±2 ticks (NQ tick=0.25 → ±0.5pt)
            const NQ_TICK = 0.25;
            const coalesced = new Map();
            for (const [key, agg] of eventsByBucket) {
                const [barIdx, priceStr] = key.split(':');
                const price = parseFloat(priceStr);
                let mergedKey = null;
                // Find existing cluster within ±COALESCE_TICKS in same bar
                for (const [k2, a2] of coalesced) {
                    const [b2] = k2.split(':');
                    if (b2 !== barIdx) continue;
                    if (Math.abs(a2.price - price) <= COALESCE_TICK_RANGE * NQ_TICK) {
                        mergedKey = k2;
                        break;
                    }
                }
                if (mergedKey) {
                    const a2 = coalesced.get(mergedKey);
                    a2.size += agg.size;
                    // Track per-level sizes to find the principal (max-volume tick)
                    if (!a2._levels) a2._levels = new Map();
                    a2._levels.set(agg.price, (a2._levels.get(agg.price) || 0) + agg.size);
                    if (!a2._levels.has(a2.price)) a2._levels.set(a2.price, agg.size);
                    // Recompute principal: anchor at price with largest size
                    let maxSize = 0, maxPrice = a2.price;
                    for (const [p, s] of a2._levels) {
                        if (s > maxSize) { maxSize = s; maxPrice = p; }
                    }
                    a2.price = maxPrice;
                    // Highest priority class wins
                    if (PRIORITY[agg.classification] > PRIORITY[a2.classification]) {
                        a2.classification = agg.classification;
                    }
                    if (agg.extreme) a2.extreme = true;
                    if (agg.defended) a2.defended = true;
                    if (agg.at_level_tier && !a2.at_level_tier) a2.at_level_tier = agg.at_level_tier;
                } else {
                    coalesced.set(key, { ...agg, _levels: new Map([[agg.price, agg.size]]) });
                }
            }

            // ── PER-BAR CAP ──
            // Footprint reading: at most 2 bubbles per bar (best buy-side
            // signal + best sell-side signal). Prevents vertical columns
            // when a bar has activity at many prices. The 2 surviving
            // bubbles per bar are the largest by volume.
            //
            // STRUCTURAL: 2 = one for buyers, one for sellers. Reflects the
            // actual binary aggressor structure of every print.
            const PER_BAR_CAP = 2;
            const groupsByBar = new Map();  // barIdx → array of buckets
            for (const agg of coalesced.values()) {
                const barK = Object.keys(agg).find(k => false) || agg._barIdx;  // not used
                // Recover barIdx from coalesced — we already had it as the leading
                // segment of the bucket key, but we lost it through the spread.
                // Easier: recompute via x → barIdx is implicit; for cap, just use
                // a (barX, side) tuple which uniquely identifies which bar this is in.
                const groupKey = `${agg.x}`;
                if (!groupsByBar.has(groupKey)) groupsByBar.set(groupKey, []);
                groupsByBar.get(groupKey).push(agg);
            }
            const capped = [];
            for (const [_, bucketList] of groupsByBar) {
                // Sort by size DESC, keep top-N
                bucketList.sort((a, b) => b.size - a.size);
                for (const a of bucketList.slice(0, PER_BAR_CAP)) capped.push(a);
            }
            // Render aggregated buckets in priority order so important classes
            // sit on top when multiple discs collide visually.
            const bucketsByPriority = capped
                .sort((a, b) => (PRIORITY[a.classification] || 0) - (PRIORITY[b.classification] || 0));
            for (const agg of bucketsByPriority) {
                // Level-defended upgrade: render as gold (real_abs) when print
                // fell on a FORTRESS/SOLID level. Block/sweep BASE class still
                // preserved in agg.base_class for hover/tooltip info, but the
                // visual color emphasizes "this hit defense" — the strongest
                // MM signal we can derive from the available L2 data.
                const renderClass = agg.defended ? 'real_abs' : agg.classification;
                const color = COLORS[renderClass];
                if (!color) continue;
                if (renderClass === 'real_abs' && !settings.showRealAbs) continue;
                const y = priceConverter(agg.price);
                if (y === null || y === undefined || isNaN(y)) continue;
                const r = _radiusForSize(agg.size, agg.p90, barSpacing, settings.sizeScale);
                // Defended levels and block/sweep get thicker borders
                const isHighSig = renderClass === 'real_abs' ||
                                  agg.classification === 'block' ||
                                  agg.classification === 'sweep';
                const borderW = isHighSig ? 2.5 : 1.5;
                _drawDiscOrEdgeMarker(ctx, agg.x, y, r, color, _fmtVol(agg.size), mediaSize, borderW, agg.extreme);
            }
            }  // close if (_BIG_PRINT_LAYER_ENABLED)

            // ════════════════════════════════════════════════════════════
            // LAYER 2: Real absorption (FORTRESS / SOLID gold discs)
            // From window._v2AbsBuffer (l2_worker absorption v2 → app.js bridge)
            // {symbol: {priceStr: {tier, label, score, total_traded, ...}}}
            // ════════════════════════════════════════════════════════════
            if (settings.showRealAbs) {
                const absAll = window._v2AbsBuffer || {};
                const absForSym = absAll[sym] || {};
                const absKeys = Object.keys(absForSym);
                if (absKeys.length > 0 && d.bars.length > 0) {
                    const lastBar = d.bars[to - 1] || d.bars[d.bars.length - 1];
                    const anchorX = lastBar.x;
                    const tradedVals = absKeys.map(k => absForSym[k]?.total_traded || 0).filter(v => v > 0).sort((a, b) => a - b);
                    const tradedP90 = tradedVals.length >= 5 ? tradedVals[Math.floor(tradedVals.length * 0.9)] : Math.max(...tradedVals, 1);
                    for (const priceStr of absKeys) {
                        const abs = absForSym[priceStr];
                        if (!abs) continue;
                        const tier = abs.tier || 0;
                        if (tier < 2) continue;  // FORTRESS or SOLID only
                        if ((abs.total_traded || 0) < settings.minSize) continue;
                        const price = parseFloat(priceStr);
                        if (isNaN(price)) continue;
                        const y = priceConverter(price);
                        if (y === null || y === undefined || isNaN(y)) continue;
                        const r = _radiusForSize(abs.total_traded || 0, tradedP90, barSpacing, settings.sizeScale);
                        const label = _fmtVol(abs.total_traded || 0);
                        // Real abs gets the thickest border — most important signal
                        _drawDiscOrEdgeMarker(ctx, anchorX, y, r, COLORS.real_abs, label, mediaSize, 2.5);
                    }
                }
            }

            // ════════════════════════════════════════════════════════════
            // LAYER 3: Exhaustion bars (yellow)
            // 4-condition: trend established + vol drop + range compress + pivot
            // ════════════════════════════════════════════════════════════
            if (settings.showExhaustion) {
                const exhSet = _computeExhaustionBars(d.bars, from, to);
                for (const idx of exhSet) {
                    const bar = d.bars[idx];
                    if (!bar?.originalData) continue;
                    const close = bar.originalData.close ?? bar.originalData.c;
                    const high = bar.originalData.high ?? bar.originalData.h;
                    const low = bar.originalData.low ?? bar.originalData.l;
                    const open = bar.originalData.open ?? bar.originalData.o;
                    if (!Number.isFinite(close)) continue;
                    const midPrice = (high + low + close + open) / 4;
                    const y = priceConverter(midPrice);
                    if (y === null || y === undefined || isNaN(y)) continue;
                    const vol = bar.originalData.volume ?? bar.originalData.v ?? 0;
                    if (vol < settings.minSize) continue;
                    const r = _radiusForSize(vol, Math.max(vol, 100), barSpacing, settings.sizeScale);
                    _drawDiscOrEdgeMarker(ctx, bar.x, y, r, COLORS.exhaustion, _fmtVol(vol), mediaSize, 1.5);
                }
            }

            ctx.restore();
        }); } catch (e) {
            console.warn('[BigPrintBubbles] draw error:', e);
        }
    }
}

class BigPrintBubbleSeries {
    constructor() { this._renderer = new BigPrintBubbleRenderer(); }
    renderer() { return this._renderer; }
    update(data, _options) { this._renderer.update(data); }
    priceValueBuilder(plotRow) { return [plotRow.close || 0]; }
    isWhitespace(data) { return !data || data.close === undefined; }
    // ⚠ Empty defaultOptions() leaves the custom series with a null
    // priceScale → LWC's render pipeline silently skips it (priceValueBuilder
    // and isWhitespace get called during data ingest, but renderer().draw()
    // never fires). Returning the same shape as a built-in series's defaults
    // gives LWC a valid scale wiring so the render loop actually invokes us.
    defaultOptions() {
        return {
            priceScaleId: 'right',
            visible: true,
            lastValueVisible: false,
            priceLineVisible: false,
            priceLineSource: 0,
            priceLineWidth: 1,
            priceLineColor: '',
            priceLineStyle: 2,
            baseLineVisible: false,
            baseLineColor: '#B2B5BE',
            baseLineWidth: 1,
            baseLineStyle: 0,
            // minMove of 0.01 (cent-scale) works for every supported symbol.
            // Custom overlay series doesn't drive scale labels — it shares
            // the candle series's price scale — so this only affects grid
            // resolution. The previous 0.25 was NQ-tick-specific and left
            // QQQ/SPY's $0.01-tick prices misaligned by quarter-point
            // increments. NQ rendering is unaffected (0.01 ≤ 0.25, so
            // any NQ price aligns to cent-level just fine).
            priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
            title: '',
            color: 'transparent',
        };
    }
    setSymbol(sym) { this._renderer.setSymbol(sym); }
}

// Public API
window.BigPrintBubbleSeries = BigPrintBubbleSeries;
window.BigPrintBubbleRenderer = BigPrintBubbleRenderer;

console.log('[BigPrintBubbles] loaded — premium signal-only renderer ready');

})();
