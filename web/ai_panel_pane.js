/**
 * AIPanel — 0DTHERO-style alert matrix + sparkline headers + message log.
 *
 * Top: 4×4 matrix of {SPX,SPY,QQQ,NDX} × {Flow Cross, Flow Div., Key Level,
 *   Spike/Dump}. Each column header carries a 60s sparkline:
 *     - SPX/SPY/QQQ → cumulative signed-$ flow (from flow_update)
 *     - NDX         → WGC sign trajectory (from ndx_wgc)
 *
 * NDX column semantics (differs from index/ETF tickers):
 *     Flow Cross  → dealer gamma regime (DAMP=bullish / AMPL=bearish / NEUTRAL=none)
 *     Flow Div.   → offline (no equivalent signal)
 *     Key Level   → NDX coverage % (offline until walls are complete)
 *     Spike/Dump  → ampl_cluster active flash (warning)
 *
 * Bottom: message log, newest-first, cap MAX_LOG_ROWS.
 */
const AIPanel = (() => {
    'use strict';

    const TICKERS = ['SPX', 'SPY', 'QQQ', 'NDX'];
    const FLOW_TICKERS = new Set(['SPX', 'SPY', 'QQQ']);  // driven by flow_update
    const ROWS    = ['Flow Cross', 'Flow Div.', 'Key Level', 'Spike/Dump'];
    const TYPE_TO_ROW = {
        flow_cross:       'Flow Cross',
        flow_divergence:  'Flow Div.',
        flow_convergence: 'Flow Div.',
        key_level:        'Key Level',
        wall_proximity:   'Key Level',    // flash cell when spot is within 0.3% of a wall
        spike:            'Spike/Dump',
        dump:             'Spike/Dump',
        bullish_volume:   'Spike/Dump',
        ndx_regime_flip:  'Flow Cross',   // routes into NDX Flow Cross cell
        ampl_cluster:     'Spike/Dump',   // routes into NDX Spike/Dump cell
    };
    const ROW_TO_FIELD = {
        'Flow Cross': 'flow_cross',
        'Flow Div.':  'flow_divergence',
        'Key Level':  'key_level',
        'Spike/Dump': 'spike_dump',
    };
    // Which cells have backend signals today.
    //   SPX/SPY/QQQ: all four rows wired.
    //   NDX:          Flow Cross (regime) + Spike/Dump (cluster) only.
    const WIRED_MATRIX = {
        SPX: new Set(['flow_cross', 'flow_divergence', 'key_level', 'spike_dump']),
        SPY: new Set(['flow_cross', 'flow_divergence', 'key_level', 'spike_dump']),
        QQQ: new Set(['flow_cross', 'flow_divergence', 'key_level', 'spike_dump']),
        NDX: new Set(['flow_cross', 'spike_dump']),
    };

    const MAX_LOG_ROWS = 50;
    const SPARK_SAMPLES = 120;                     // 60s at 0.5s emit cadence
    const AMPL_CLUSTER_FLASH_SEC = 300;            // cluster tag stays lit 5 min

    // Measured 5-min hit rates from logs/alert_outcomes.jsonl (n=4070, window 2026-04-23..05-04).
    // Source: /tmp/analyze_flow_div.py — kept here so users see WHY some alerts are dim.
    // Update when re-measured. Lower = less predictive edge. <50 = no edge (random or worse).
    const EDGE_HIT_RATE = {
        flow_divergence:  64.7,
        flow_convergence: 46.4,
        key_level:        44.1,
        spike:            44.1,
        flow_cross:       42.9,
        dump:             42.1,
        bullish_volume:   25.0,
        wall_proximity:    7.3,    // catastrophically bad — gamma-regime gate added in alert_engine
        ndx_regime_flip:   0,      // unmeasured (NDX-only)
        ampl_cluster:      0,
    };
    const EDGE_DIM_THRESHOLD  = 50.0;   // hit rate under this = no measurable edge
    const EDGE_FILTER_KEY     = 'ai-panel-edge-filter';
    const EDGE_FILTER_MODES   = ['show', 'dim', 'hide'];   // cycle in this order

    let _container = null;
    let _matrixEl = null;
    let _logListEl = null;
    let _tsEl = null;
    let _zeroEl = null;
    let _styleEl  = null;
    let _unsubAlert    = null;
    let _unsubFlow     = null;
    let _wgcSocketOff  = null;
    let _state    = {};
    let _mag      = {};
    let _logRows  = [];
    let _clockTimer = 0;
    // Sparkline buffers: ticker -> { samples: number[], lastVal: number, label: string }
    let _spark   = {};
    // NDX regime snapshot from ndx_wgc event.
    let _wgc     = null;
    let _amplClusterUntil = 0;  // epoch-sec; while now<this, NDX Spike/Dump = warning
    let _filterMode = 'dim';   // 'show' | 'dim' | 'hide' — persisted in localStorage

    function _injectStyles() {
        if (document.getElementById('aipanel-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'aipanel-styles';
        _styleEl.textContent = `
            .ap-wrap { height:100%; display:flex; flex-direction:column; background:#0a0d14; color:rgba(220,225,235,.9);
                       font-family:'JetBrains Mono','Share Tech Mono',monospace; }
            .ap-panel { margin:10px 10px 6px 10px; padding:10px 14px 12px; border:1px solid rgba(255,255,255,.06);
                        border-radius:8px; background:#0b0f1a; }
            .ap-hdr  { display:flex; align-items:baseline; padding:4px 0 8px; gap:12px;
                       border-bottom:1px solid rgba(255,180,80,.18); }
            .ap-brand   { font-size:11px; font-weight:700; color:#d8c78c; letter-spacing:1.1px; }
            .ap-nav     { font-size:10px; color:#c9b27a; letter-spacing:.8px; text-transform:uppercase; }
            .ap-ts      { margin-left:auto; font-size:9.5px; color:#c9b27a; letter-spacing:.3px; white-space:nowrap; }
            .ap-link    { color:#c9b27a; opacity:.65; cursor:pointer; font-size:11px; }
            .ap-matrix-wrap { padding:10px 0 0; }
            .ap-matrix { width:100%; border-collapse:separate; border-spacing:0 6px; font-size:10.5px; }
            .ap-matrix th, .ap-matrix td { padding:4px 6px; text-align:center; vertical-align:middle; }
            .ap-matrix th { font-size:10px; font-weight:600; color:#e6e6ea; letter-spacing:.5px; padding-bottom:6px; }
            .ap-matrix .ap-row-label { text-align:left; color:#d8c78c; font-size:11px; letter-spacing:.3px;
                                       padding-left:2px; padding-right:14px; white-space:nowrap; }
            .ap-matrix .ap-row-label::before { content:'> '; color:#c9b27a; opacity:.85; }

            /* Sparkline header: ticker name + 60s canvas + current value. */
            .ap-thead-ticker { font-size:10px; font-weight:700; color:#e6e6ea; letter-spacing:.7px; margin-bottom:2px; }
            .ap-spark { display:block; margin:0 auto; width:72px; height:22px; }
            .ap-spark-val { font-size:9px; font-weight:600; letter-spacing:.3px; line-height:1.1; margin-top:2px;
                            color:rgba(220,225,235,.75); white-space:nowrap; }
            .ap-spark-val.bull { color:#1fd17a; }
            .ap-spark-val.bear { color:#e03060; }
            .ap-spark-val.warn { color:#f0a040; }

            .ap-cell { display:inline-flex; flex-direction:column; align-items:center; justify-content:center; gap:2px; }
            .ap-dot { display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px;
                      border-radius:50%; font-size:10px; font-weight:700; line-height:1;
                      background:rgba(90,95,115,.5); border:1px solid rgba(90,95,115,.5); color:transparent; }
            .ap-dot-bullish { background:#1fd17a; border-color:#1fd17a; color:#0a0d14;
                              box-shadow:0 0 8px rgba(31,209,122,.5); }
            .ap-dot-bearish { background:#e03060; border-color:#e03060; color:#0a0d14;
                              box-shadow:0 0 8px rgba(224,48,96,.5); }
            .ap-dot-none    { background:rgba(90,95,115,.55); border-color:rgba(90,95,115,.55); }
            .ap-dot-offline { background:transparent; border-color:rgba(120,128,148,.55); }
            .ap-dot-warning { background:#f0a040; border-color:#f0a040; box-shadow:0 0 8px rgba(240,160,64,.5); }
            .ap-mag { font-size:8.5px; font-weight:600; letter-spacing:.2px; line-height:1; min-height:10px; }
            .ap-mag-bull { color:#1fd17a; }
            .ap-mag-bear { color:#e03060; }
            .ap-mag-warn { color:#f0a040; }

            .ap-log-panel { flex:1; display:flex; flex-direction:column; margin:4px 10px 10px 10px; padding:10px 14px 8px;
                            border:1px solid rgba(255,255,255,.06); border-radius:8px; background:#0b0f1a;
                            min-height:0; }
            .ap-prompt { display:flex; align-items:center; gap:6px; padding-bottom:6px;
                         border-bottom:1px solid rgba(255,180,80,.12); font-size:11px; color:#c9b27a; }
            .ap-prompt .caret { display:inline-block; width:8px; height:12px; background:#b58a45;
                                animation: ap-blink 1s steps(1) infinite; }
            @keyframes ap-blink { 50% { opacity:0; } }
            .ap-log-list { flex:1; overflow-y:auto; overflow-x:hidden; padding:6px 0 0; min-height:0; }
            .ap-log-list::-webkit-scrollbar { width:3px; }
            .ap-log-list::-webkit-scrollbar-thumb { background:rgba(181,138,69,.3); border-radius:2px; }
            .ap-log-row { display:flex; gap:10px; font-size:10.5px; line-height:1.7; padding:1px 0; color:#d8c78c; }
            .ap-log-row .ap-log-prefix { color:#b58a45; opacity:.85; }
            .ap-log-row.bull .ap-log-msg { color:#1fd17a; }
            .ap-log-row.bear .ap-log-msg { color:#e03060; }
            .ap-log-row.warn .ap-log-msg { color:#f0a040; }
            .ap-log-row .ap-log-ts { margin-left:auto; color:rgba(181,138,69,.8); white-space:nowrap; }
            .ap-log-footer { display:flex; align-items:center; padding-top:6px;
                             border-top:1px solid rgba(255,255,255,.04); font-size:9.5px;
                             color:rgba(181,138,69,.7); letter-spacing:.6px; }
            .ap-log-footer .ap-zero { margin-left:auto; display:inline-flex; align-items:center; gap:5px; color:#1fd17a; }
            .ap-log-footer .ap-zero::before { content:''; width:7px; height:7px; border-radius:50%; background:#1fd17a;
                                              box-shadow:0 0 6px rgba(31,209,122,.6); }

            /* Edge filter — measured hit-rate badge on each log row + filter toggle button. */
            .ap-edge-badge { display:inline-block; margin-right:4px; padding:0 4px; border-radius:2px;
                             font-size:9px; font-weight:600; line-height:1.4; letter-spacing:.2px; min-width:24px;
                             text-align:center; vertical-align:middle; }
            .ap-edge-badge.ap-edge-good { background:rgba(31,209,122,.18); color:#1fd17a;
                                          border:1px solid rgba(31,209,122,.35); }
            .ap-edge-badge.ap-edge-bad  { background:rgba(224,48,96,.12); color:#e03060;
                                          border:1px solid rgba(224,48,96,.30); }
            .ap-edge-badge.ap-edge-mid  { background:rgba(240,160,64,.10); color:#f0a040;
                                          border:1px solid rgba(240,160,64,.25); }
            /* Dim mode: low-edge rows are 35% opacity (still readable, de-emphasized). */
            .ap-log-list.ap-filter-dim  .ap-log-row.ap-low-edge { opacity:.35; }
            /* Hide mode: low-edge rows removed from view entirely. */
            .ap-log-list.ap-filter-hide .ap-log-row.ap-low-edge { display:none; }
            /* Filter toggle pill in the header. */
            .ap-filter-btn { padding:1px 6px; border-radius:3px; font-size:9px; font-weight:700;
                             letter-spacing:.6px; cursor:pointer; user-select:none;
                             border:1px solid rgba(181,138,69,.35); color:#c9b27a;
                             background:rgba(181,138,69,.08); margin-left:6px; }
            .ap-filter-btn:hover { background:rgba(181,138,69,.18); color:#e6d090; }
            .ap-filter-btn[data-mode="hide"] { color:#1fd17a; border-color:rgba(31,209,122,.35);
                                               background:rgba(31,209,122,.08); }
            .ap-filter-btn[data-mode="show"] { color:#e03060; border-color:rgba(224,48,96,.30);
                                               background:rgba(224,48,96,.06); }
        `;
        document.head.appendChild(_styleEl);
    }

    function _fmtTime(tsSec) {
        try {
            const d = new Date((tsSec || 0) * 1000);
            const h = d.getHours();
            const m = String(d.getMinutes()).padStart(2, '0');
            const s = String(d.getSeconds()).padStart(2, '0');
            const ap = h >= 12 ? 'PM' : 'AM';
            const h12 = h % 12 === 0 ? 12 : h % 12;
            return `${String(h12).padStart(2,'0')}:${m}:${s}${ap}`;
        } catch (e) { return ''; }
    }

    function _fmtHeaderTs() {
        const d = new Date();
        const mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
        const h = d.getHours(); const m = String(d.getMinutes()).padStart(2,'0');
        const ap = h >= 12 ? 'PM' : 'AM';
        const h12 = h % 12 === 0 ? 12 : h % 12;
        return `${mo}. ${d.getDate()} ${d.getFullYear()} ${h12}:${m} ${ap}`;
    }

    function _fmtFlowVal(v) {
        if (!Number.isFinite(v)) return '';
        const a = Math.abs(v);
        const s = v >= 0 ? '+$' : '-$';
        if (a >= 1e9) return `${s}${(a / 1e9).toFixed(2)}B`;
        if (a >= 1e6) return `${s}${(a / 1e6).toFixed(2)}M`;
        if (a >= 1e3) return `${s}${(a / 1e3).toFixed(0)}k`;
        return `${s}${a.toFixed(0)}`;
    }

    function _fmtWgcVal(wgc) {
        if (!wgc || typeof wgc.wgc_sign !== 'number') return '';
        const pct = wgc.wgc_sign * 100;
        const regime = String(wgc.regime || 'NEUTRAL');
        const sign = pct >= 0 ? '+' : '';
        return `${regime} ${sign}${pct.toFixed(1)}%`;
    }

    function _dotIcon(kind) {
        if (kind === 'bullish') return '↑';
        if (kind === 'bearish') return '↓';
        return '';
    }

    function _cellKind(ticker, metricField) {
        const wired = WIRED_MATRIX[ticker];
        if (!wired || !wired.has(metricField)) return 'offline';

        // NDX special-case: regime dot + cluster warning come from _wgc state,
        // not the generic _state alert buffer.
        if (ticker === 'NDX') {
            if (metricField === 'flow_cross') {
                const r = _wgc && _wgc.regime;
                if (r === 'DAMP') return 'bullish';
                if (r === 'AMPL') return 'bearish';
                return 'none';
            }
            if (metricField === 'spike_dump') {
                const now = Date.now() / 1000;
                return now < _amplClusterUntil ? 'warning' : 'none';
            }
            return 'offline';
        }

        const st = _state[ticker] || {};
        const v = st[metricField];
        if (v === 'bullish' || v === 'bearish' || v === 'warning') return v;
        return 'none';
    }

    function _buildSkeleton() {
        const tickerHeaders = TICKERS.map(t =>
            `<th>
                <div class="ap-thead-ticker">${t}</div>
                <canvas class="ap-spark" data-ticker="${t}" width="144" height="44"></canvas>
                <div class="ap-spark-val" data-spark-val="${t}">—</div>
            </th>`).join('');
        const thead = `<tr><th></th>${tickerHeaders}</tr>`;

        const tbody = ROWS.map(row => {
            const field = ROW_TO_FIELD[row];
            const cells = TICKERS.map(t =>
                `<td><span class="ap-cell">` +
                    `<span class="ap-dot" data-ticker="${t}" data-row="${row}" data-field="${field}">&nbsp;</span>` +
                    `<span class="ap-mag" data-ticker="${t}" data-row="${row}"></span>` +
                `</span></td>`
            ).join('');
            return `<tr><td class="ap-row-label">${row}</td>${cells}</tr>`;
        }).join('');
        return `
            <div class="ap-wrap">
                <div class="ap-panel">
                    <div class="ap-hdr">
                        <span class="ap-brand">0DTHERO AI</span>
                        <span class="ap-nav">ALERTS</span>
                        <span class="ap-filter-btn" id="ap-filter-btn" data-mode="${_filterMode}"
                              title="Cycle: show all → dim low-edge → hide low-edge. Edge measured from 4070 outcomes; <50% hit-rate at 5min = low-edge.">
                            ${_filterLabel(_filterMode)}
                        </span>
                        <span class="ap-ts" id="ap-ts">${_fmtHeaderTs()}</span>
                        <span class="ap-link" title="open full view">&#x2197;</span>
                    </div>
                    <div class="ap-matrix-wrap">
                        <table class="ap-matrix">
                            <thead>${thead}</thead>
                            <tbody>${tbody}</tbody>
                        </table>
                    </div>
                </div>
                <div class="ap-log-panel">
                    <div class="ap-prompt"><span>&gt;</span><span class="caret"></span></div>
                    <div class="ap-log-list" id="ap-log-list"></div>
                    <div class="ap-log-footer">
                        <span>MESSAGE LOG</span>
                        <span class="ap-zero" id="ap-zero">Zero</span>
                    </div>
                </div>
            </div>`;
    }

    function _fmtMag(mag) {
        if (!mag) return '';
        const m = Number(mag.magnitude_m);
        const sig = Number(mag.sigma);
        if (Number.isFinite(m) && Math.abs(m) >= 0.01) {
            const sign = m >= 0 ? '+' : '';
            return Math.abs(m) >= 1
                ? `${sign}${m.toFixed(0)}M`
                : `${sign}${m.toFixed(1)}M`;
        }
        if (Number.isFinite(sig) && Math.abs(sig) >= 0.1) {
            const sign = sig >= 0 ? '+' : '';
            return `${sign}${sig.toFixed(1)}σ`;
        }
        return '';
    }

    function _paintCell(ticker, row) {
        if (!_matrixEl) return;
        const dot = _matrixEl.querySelector(`.ap-dot[data-ticker="${ticker}"][data-row="${row}"]`);
        if (!dot) return;
        const field = ROW_TO_FIELD[row];
        const kind  = _cellKind(ticker, field);
        dot.className = 'ap-dot ap-dot-' + kind;
        dot.textContent = _dotIcon(kind);
        const magEl = _matrixEl.querySelector(`.ap-mag[data-ticker="${ticker}"][data-row="${row}"]`);
        if (magEl) {
            const m = (_mag[ticker] || {})[field];
            const lit = kind === 'bullish' || kind === 'bearish' || kind === 'warning';
            const text = lit ? _fmtMag(m) : '';
            magEl.textContent = text;
            let cls = 'ap-mag';
            if (kind === 'bullish') cls += ' ap-mag-bull';
            else if (kind === 'bearish') cls += ' ap-mag-bear';
            else if (kind === 'warning') cls += ' ap-mag-warn';
            magEl.className = cls;
        }
    }

    function _applyState() {
        for (const ticker of TICKERS) {
            for (const row of ROWS) _paintCell(ticker, row);
        }
    }

    /* ─── Sparklines ──────────────────────────────────────────────────── */

    function _drawSpark(canvas, samples, opts) {
        if (!canvas || !samples) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        // Match backing store to CSS size × devicePixelRatio so strokes stay
        // crisp on retina. Without this the spark renders at raw CSS pixels
        // and lines look fuzzy.
        const dpr = window.devicePixelRatio || 1;
        const cssW = canvas.clientWidth || canvas.width;
        const cssH = canvas.clientHeight || canvas.height;
        const bw = Math.max(1, Math.round(cssW * dpr));
        const bh = Math.max(1, Math.round(cssH * dpr));
        if (canvas.width !== bw) canvas.width = bw;
        if (canvas.height !== bh) canvas.height = bh;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const W = cssW, H = cssH;
        ctx.clearRect(0, 0, W, H);
        if (samples.length < 2) return;

        let min = samples[0], max = samples[0];
        for (let i = 1; i < samples.length; i++) {
            const v = samples[i];
            if (v < min) min = v;
            if (v > max) max = v;
        }
        if (min > 0) min = 0;
        if (max < 0) max = 0;
        const range = (max - min) || 1;
        const xs = W / Math.max(samples.length - 1, 1);
        const toY = (v) => H - ((v - min) / range) * H;
        const zeroY = toY(0);

        // 0-line baseline
        ctx.strokeStyle = 'rgba(140,150,180,.22)';
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(W, zeroY); ctx.stroke();
        ctx.setLineDash([]);

        const last = samples[samples.length - 1];
        const colorBull = (opts && opts.bull) || '#1fd17a';
        const colorBear = (opts && opts.bear) || '#e03060';
        const strokeColor = last > 0 ? colorBull : last < 0 ? colorBear : 'rgba(140,150,180,.6)';

        // Area fill (subtle)
        ctx.beginPath();
        ctx.moveTo(0, zeroY);
        for (let i = 0; i < samples.length; i++) {
            ctx.lineTo(i * xs, toY(samples[i]));
        }
        ctx.lineTo((samples.length - 1) * xs, zeroY);
        ctx.closePath();
        ctx.fillStyle = last > 0
            ? 'rgba(31,209,122,.12)'
            : last < 0
                ? 'rgba(224,48,96,.12)'
                : 'rgba(140,150,180,.08)';
        ctx.fill();

        // Main line
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        for (let i = 0; i < samples.length; i++) {
            const x = i * xs, y = toY(samples[i]);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Endpoint dot
        const ex = (samples.length - 1) * xs;
        const ey = toY(last);
        ctx.fillStyle = strokeColor;
        ctx.beginPath(); ctx.arc(ex, ey, 2.2, 0, Math.PI * 2); ctx.fill();
    }

    function _renderSparks() {
        if (!_matrixEl) return;
        for (const t of TICKERS) {
            const canvas = _matrixEl.querySelector(`canvas.ap-spark[data-ticker="${t}"]`);
            const valEl  = _matrixEl.querySelector(`[data-spark-val="${t}"]`);
            const s = _spark[t];
            if (s && s.samples) _drawSpark(canvas, s.samples, null);
            if (valEl) {
                if (t === 'NDX') {
                    valEl.textContent = _fmtWgcVal(_wgc) || '—';
                    const reg = _wgc && _wgc.regime;
                    valEl.className = 'ap-spark-val ' + (reg === 'DAMP' ? 'bull' : reg === 'AMPL' ? 'bear' : '');
                } else {
                    const v = s && s.lastVal;
                    valEl.textContent = (Number.isFinite(v) && v !== 0) ? _fmtFlowVal(v) : '—';
                    valEl.className = 'ap-spark-val ' + (v > 0 ? 'bull' : v < 0 ? 'bear' : '');
                }
            }
        }
    }

    /* ─── Event handlers ──────────────────────────────────────────────── */

    function _onFlowUpdate(data) {
        if (!data || !Array.isArray(data.tickers)) return;
        for (const t of data.tickers) {
            const tk = t.ticker;
            if (!FLOW_TICKERS.has(tk)) continue;
            if (!_spark[tk]) _spark[tk] = { samples: [], lastVal: 0 };
            const v = Number(t.cum_signed_all || 0);
            _spark[tk].samples.push(v);
            if (_spark[tk].samples.length > SPARK_SAMPLES) _spark[tk].samples.shift();
            _spark[tk].lastVal = v;
        }
        _renderSparks();
    }

    function _onWgc(w) {
        if (!w || typeof w.wgc_sign !== 'number') return;
        _wgc = w;
        if (!_spark.NDX) _spark.NDX = { samples: [], lastVal: 0 };
        const v = Number(w.wgc_sign || 0);
        _spark.NDX.samples.push(v);
        if (_spark.NDX.samples.length > SPARK_SAMPLES) _spark.NDX.samples.shift();
        _spark.NDX.lastVal = v;
        _renderSparks();
        _paintCell('NDX', 'Flow Cross');
    }

    function _filterLabel(mode) {
        if (mode === 'hide') return 'EDGE◉';   // hide low-edge — strictest
        if (mode === 'dim')  return 'EDGE◐';   // dim low-edge — default
        return 'EDGE○';                         // show all — no filter
    }

    function _edgeClass(rate) {
        if (!Number.isFinite(rate) || rate <= 0) return '';     // unmeasured — no badge
        if (rate >= 60) return 'ap-edge-good';
        if (rate >= EDGE_DIM_THRESHOLD) return 'ap-edge-mid';
        return 'ap-edge-bad';
    }

    function _isLowEdge(type) {
        const r = EDGE_HIT_RATE[type];
        return Number.isFinite(r) && r > 0 && r < EDGE_DIM_THRESHOLD;
    }

    function _renderLogRows() {
        if (!_logListEl) return;
        const frag = _logRows.map(r => {
            const cls = r.direction === 'bullish' ? 'bull'
                      : r.direction === 'bearish' ? 'bear'
                      : r.direction === 'warning' ? 'warn' : '';
            const msg = String(r.msg || '');
            const time = r.time || '';
            const rate = EDGE_HIT_RATE[r.type];
            const lowEdge = _isLowEdge(r.type) ? ' ap-low-edge' : '';
            const badge = (Number.isFinite(rate) && rate > 0)
                ? `<span class="ap-edge-badge ${_edgeClass(rate)}" title="measured 5-min hit rate from logs/alert_outcomes.jsonl">${rate.toFixed(0)}%</span>`
                : '';
            return `<div class="ap-log-row ${cls}${lowEdge}">` +
                   `<span class="ap-log-prefix">&gt;</span>` +
                   badge +
                   `<span class="ap-log-msg">${msg}</span>` +
                   `<span class="ap-log-ts">${time}</span>` +
                   `</div>`;
        }).join('');
        _logListEl.innerHTML = frag;
        _applyFilterMode();
    }

    function _applyFilterMode() {
        if (!_logListEl) return;
        _logListEl.classList.remove('ap-filter-show', 'ap-filter-dim', 'ap-filter-hide');
        _logListEl.classList.add('ap-filter-' + _filterMode);
    }

    function _cycleFilterMode() {
        const idx = EDGE_FILTER_MODES.indexOf(_filterMode);
        _filterMode = EDGE_FILTER_MODES[(idx + 1) % EDGE_FILTER_MODES.length];
        try { localStorage.setItem(EDGE_FILTER_KEY, _filterMode); } catch (_) {}
        const btn = _container && _container.querySelector('#ap-filter-btn');
        if (btn) {
            btn.textContent = _filterLabel(_filterMode);
            btn.setAttribute('data-mode', _filterMode);
        }
        _applyFilterMode();
    }

    function _restoreFilterMode() {
        try {
            const saved = localStorage.getItem(EDGE_FILTER_KEY);
            if (saved && EDGE_FILTER_MODES.includes(saved)) _filterMode = saved;
        } catch (_) {}
    }

    function _formatMsg(alert) {
        if (alert.label) return alert.label;
        const mag = alert.magnitude_m;
        const bucket = alert.bucket ? ` [${alert.bucket}]` : '';
        const sign = (mag != null && mag >= 0) ? '+' : '';
        const magStr = (mag != null) ? ` ${sign}${mag.toFixed(2)}M` : '';
        return (alert.ticker || '') + ' ' + (alert.type || '') + magStr + bucket;
    }

    function _addLogEntry(alert) {
        const msg = _formatMsg(alert);
        const time = _fmtTime(alert.ts);
        const prev = _logRows[0];
        if (prev && prev.msg === msg && prev.time === time) return;
        _logRows.unshift({ msg, time, direction: alert.direction, type: alert.type });
        if (_logRows.length > MAX_LOG_ROWS) _logRows.length = MAX_LOG_ROWS;
        _renderLogRows();
    }

    function _onAlert(alert) {
        if (!alert || !_container) return;
        const tk = alert.ticker;
        const type = alert.type;
        const row = TYPE_TO_ROW[type];
        const dir = alert.direction || 'none';

        // NDX-side alerts drive the warning flash + log.
        if (type === 'ampl_cluster') {
            _amplClusterUntil = (Number(alert.ts) || Date.now() / 1000) + AMPL_CLUSTER_FLASH_SEC;
            if (!_mag.NDX) _mag.NDX = {};
            _mag.NDX.spike_dump = {
                magnitude_m: Number(alert.magnitude_m || 0),
                sigma:       Number(alert.sigma || 0),
                ts:          Number(alert.ts || 0),
            };
            _paintCell('NDX', 'Spike/Dump');
        }

        if (tk && row && TICKERS.includes(tk)) {
            // Don't let generic flow_cross routing clobber NDX regime; that's
            // sourced from _wgc directly. The ndx_regime_flip still flows through
            // for logging + magnitude.
            if (tk === 'NDX' && type !== 'ndx_regime_flip' && type !== 'ampl_cluster') {
                // no-op on NDX cells
            } else {
                const field = ROW_TO_FIELD[row];
                if (tk !== 'NDX') {
                    if (!_state[tk]) _state[tk] = {};
                    _state[tk][field] = dir;
                }
                if (!_mag[tk]) _mag[tk] = {};
                _mag[tk][field] = {
                    magnitude_m: Number(alert.magnitude_m || 0),
                    sigma:       Number(alert.sigma || 0),
                    ts:          Number(alert.ts || 0),
                };
                _paintCell(tk, row);
            }
        }
        _addLogEntry(alert);
    }

    function _tickClock() {
        if (_tsEl) _tsEl.textContent = _fmtHeaderTs();
        // Also re-evaluate NDX cluster flash decay.
        const now = Date.now() / 1000;
        if (_amplClusterUntil > 0 && now >= _amplClusterUntil) {
            _amplClusterUntil = 0;
            _paintCell('NDX', 'Spike/Dump');
        }
    }

    function _getReplayDate() {
        try {
            const u = new URLSearchParams(window.location.search);
            const d = u.get('replay_date') || '';
            return /^\d{8}$/.test(d) ? d : '';
        } catch (_) { return ''; }
    }

    function _hydrate() {
        const tok = sessionStorage.getItem('greeks-auth') || '';
        const hdrs = tok ? { 'X-Auth-Token': tok } : {};
        const replayDate = _getReplayDate();

        if (replayDate) {
            if (_tsEl) _tsEl.textContent = 'replay ' + replayDate;
            fetch('/api/alerts/history?date=' + replayDate + '&last_n=500', { headers: hdrs })
                .then(r => r.json())
                .then(d => {
                    if (!_container) return;
                    const alerts = (d && d.alerts) || [];
                    _logRows = alerts.slice(-MAX_LOG_ROWS).reverse().map(a => ({
                        msg: _formatMsg(a), time: _fmtTime(a.ts), direction: a.direction, type: a.type,
                    }));
                    _renderLogRows();
                })
                .catch(() => {});
            return;
        }

        fetch('/api/alerts/state', { headers: hdrs })
            .then(r => r.json())
            .then(d => {
                if (!_container) return;
                if (d && d.tickers) { _state = d.tickers; _applyState(); }
            })
            .catch(() => {});

        // Hydrate last-known NDX WGC so the regime cell isn't blank until the
        // next socket emit (which only fires when single-name walls refresh).
        fetch('/api/ndx_wgc', { headers: hdrs })
            .then(r => r.json())
            .then(d => {
                if (!_container) return;
                if (d && d.regime) _onWgc(d);
            })
            .catch(() => {});

        fetch('/api/_debug/alert_log', { headers: hdrs })
            .then(r => r.json())
            .then(d => {
                if (!_container) return;
                const alerts = (d && d.alerts) || [];
                const latest = {};
                for (const a of alerts) {
                    const row = TYPE_TO_ROW[a.type];
                    if (!row) continue;
                    const tk = a.ticker;
                    if (!TICKERS.includes(tk)) continue;
                    const field = ROW_TO_FIELD[row];
                    const key = tk + '|' + field;
                    if (!latest[key] || (a.ts || 0) > (latest[key].ts || 0)) latest[key] = a;
                }
                for (const [key, a] of Object.entries(latest)) {
                    const [tk, field] = key.split('|');
                    if (!_mag[tk]) _mag[tk] = {};
                    _mag[tk][field] = {
                        magnitude_m: Number(a.magnitude_m || 0),
                        sigma:       Number(a.sigma || 0),
                        ts:          Number(a.ts || 0),
                    };
                }
                _applyState();
                const fromServer = alerts.slice(-MAX_LOG_ROWS).reverse().map(a => ({
                    msg: _formatMsg(a), time: _fmtTime(a.ts), direction: a.direction, type: a.type,
                }));
                const liveSeen = new Set(_logRows.map(r => r.msg + '|' + r.time));
                const merged = _logRows.slice();
                for (const r of fromServer) {
                    if (!liveSeen.has(r.msg + '|' + r.time)) merged.push(r);
                }
                _logRows = merged.slice(0, MAX_LOG_ROWS);
                _renderLogRows();
            })
            .catch(() => {});
    }

    function init(slotEl) {
        _restoreFilterMode();
        _injectStyles();
        _container = slotEl;
        _container.innerHTML = _buildSkeleton();
        _matrixEl  = _container.querySelector('.ap-matrix');
        _logListEl = _container.querySelector('#ap-log-list');
        _tsEl      = _container.querySelector('#ap-ts');
        _zeroEl    = _container.querySelector('#ap-zero');
        _state     = {};
        _mag       = {};
        _logRows   = [];
        _spark     = {};
        _wgc       = null;
        _amplClusterUntil = 0;
        _applyState();
        _renderSparks();
        _applyFilterMode();
        _hydrate();
        _clockTimer = setInterval(_tickClock, 30_000);

        const filterBtn = _container.querySelector('#ap-filter-btn');
        if (filterBtn) filterBtn.addEventListener('click', _cycleFilterMode);

        if (window.AltarisEvents && typeof window.AltarisEvents.on === 'function') {
            const alertHandler = (a) => _onAlert(a);
            window.AltarisEvents.on('data:flow:alert', alertHandler);
            _unsubAlert = () => {
                try { window.AltarisEvents.off('data:flow:alert', alertHandler); } catch (_) {}
            };

            const flowHandler = (d) => _onFlowUpdate(d);
            window.AltarisEvents.on('data:flow:update', flowHandler);
            _unsubFlow = () => {
                try { window.AltarisEvents.off('data:flow:update', flowHandler); } catch (_) {}
            };
        }
        // NDX WGC comes straight off the socket; no AltarisEvents fan-out yet.
        if (window._sio && typeof window._sio.on === 'function') {
            const wgcHandler = (w) => _onWgc(w);
            window._sio.on('ndx_wgc', wgcHandler);
            _wgcSocketOff = () => {
                try { window._sio.off('ndx_wgc', wgcHandler); } catch (_) {}
            };
        }
    }

    function destroy() {
        if (_unsubAlert)  { try { _unsubAlert();  } catch (_) {} _unsubAlert  = null; }
        if (_unsubFlow)   { try { _unsubFlow();   } catch (_) {} _unsubFlow   = null; }
        if (_wgcSocketOff){ try { _wgcSocketOff();} catch (_) {} _wgcSocketOff= null; }
        if (_clockTimer)  { clearInterval(_clockTimer); _clockTimer = 0; }
        _container = null;
        _matrixEl = null;
        _logListEl = null;
        _tsEl = null;
        _zeroEl = null;
        _state = {};
        _mag = {};
        _logRows = [];
        _spark = {};
        _wgc = null;
        _amplClusterUntil = 0;
    }

    return { init, destroy };
})();
window.AIPanel = AIPanel;
