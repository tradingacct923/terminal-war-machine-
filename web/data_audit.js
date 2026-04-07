/**
 * Data Integrity & Pipeline Latency Auditor
 *
 * Extracted from volume_bubbles.js for maintainability.
 * Non-destructive monitoring: wraps the existing Socket.IO handler.
 *
 * Console API:
 *   startDataAudit()  — begin logging every 5s
 *   stopDataAudit()   — stop logging
 *
 * Exports: window.DataAudit, window.startDataAudit, window.stopDataAudit
 */

// DATA INTEGRITY & PIPELINE LATENCY AUDITOR
// ═══════════════════════════════════════════════════════════════════════════════
//
// Wraps the Socket.IO dom_snapshot handler to measure:
//   1. Pipeline latency (JSON parse → SigmaEngine → PressureField → DOM update)
//   2. Sequence gap detection (dropped/reordered packets)
//   3. Trade throughput (cumulative trade count)
//
// Console API:
//   startDataAudit()   — begin logging every 5s
//   stopDataAudit()    — stop logging
//
// The audit is NON-DESTRUCTIVE: it wraps the existing _sio handler
// and delegates to `_initDomWebSocket` for all actual processing.
// ═══════════════════════════════════════════════════════════════════════════════

const DataAudit = {
    _active: false,
    _intervalId: null,
    _tradeCount: 0,
    _droppedPackets: 0,
    _lastSeq: -1,
    _totalLatency: 0,
    _latencySamples: 0,
    _maxLatency: 0,

    start() {
        if (this._active) {
            console.log('[DataAudit] Already running');
            return;
        }
        this._active = true;
        this._tradeCount = 0;
        this._droppedPackets = 0;
        this._lastSeq = -1;
        this._totalLatency = 0;
        this._latencySamples = 0;
        this._maxLatency = 0;

        console.log('%c 🛰️ DATA INTEGRITY AUDIT ACTIVE', 'color: #00e5ff; font-weight: bold;');
        console.log('%c   Monitoring pipeline latency, sequence gaps, trade throughput', 'color: #888;');

        // Reporting loop — every 5 seconds
        this._intervalId = setInterval(() => {
            if (!this._active) return;
            const avgLat = this._latencySamples > 0
                ? (this._totalLatency / this._latencySamples).toFixed(3)
                : '0.000';
            const maxLat = this._maxLatency.toFixed(3);
            const health = this._droppedPackets === 0
                ? '💯 PERFECT'
                : `❌ ${this._droppedPackets} GAPS`;
            const latColor = parseFloat(avgLat) < 2 ? 'color: #00ff00' : 'color: #ffcc00';
            const wsState = DOM2D._wsActive ? '🟢 WS' : '🔴 REST';

            console.log(
                `%c [AUDIT] ${wsState} | Trades: ${this._tradeCount} | Avg: ${avgLat}ms | Peak: ${maxLat}ms | ${health}`,
                latColor
            );

            // Reset per-interval counters (cumulative trade count persists)
            this._totalLatency = 0;
            this._latencySamples = 0;
            this._maxLatency = 0;
        }, 5000);
    },

    stop() {
        if (!this._active) return;
        this._active = false;
        if (this._intervalId) {
            clearInterval(this._intervalId);
            this._intervalId = null;
        }
        console.log('[DataAudit] Stopped');
    },

    // Called from the instrumented dom_snapshot handler
    measure(data, startTime) {
        if (!this._active) return;
        const endTime = performance.now();
        const latency = endTime - startTime;

        this._totalLatency += latency;
        this._latencySamples++;
        if (latency > this._maxLatency) this._maxLatency = latency;

        // Trade counting
        if (data.trades && data.trades.length) {
            this._tradeCount += data.trades.length;
        }

        // Sequence gap detection (if backend sends seq IDs)
        if (data.seq !== undefined) {
            if (this._lastSeq !== -1 && data.seq !== this._lastSeq + 1) {
                this._droppedPackets++;
                console.warn(
                    `%c ⚠️ DATA GAP: Expected seq ${this._lastSeq + 1}, got ${data.seq}`,
                    'color: orange'
                );
            }
            this._lastSeq = data.seq;
        }

        // Latency spike alert (> 8ms = GC likely)
        if (latency > 8) {
            console.warn(
                `%c ⚡ LATENCY SPIKE: ${latency.toFixed(1)}ms (possible GC pause)`,
                'color: #ff8800'
            );
        }
    },
};

window.DataAudit = DataAudit;
window.startDataAudit = () => DataAudit.start();
window.stopDataAudit  = () => DataAudit.stop();
