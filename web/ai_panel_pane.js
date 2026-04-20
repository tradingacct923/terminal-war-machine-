/**
 * AIPanel — 0DT-Hero-style alert matrix + message log.
 *
 * Top: 4×3 matrix showing {SPX, SPY, QQQ} × {Flow Cross, Flow Divergence,
 *   Key Level, Spike/Dump} as colored dots (green=bullish, red=bearish,
 *   dark=none). One glance = directional bias across all three indices.
 * Bottom: scrolling message log, newest first, capped at 50 rows. Format
 *   mirrors Schwab's AlertEngine labels: "> SPY dump -102.17M [all exp]".
 *
 * Lifecycle:
 *   init(slotEl): hydrate matrix via /api/alerts/state, log via
 *                 /api/_debug/alert_log, subscribe to data:flow:alert bus.
 *   destroy(): unsubscribe, clear state. Call during pane unmount.
 */
const AIPanel = (() => {
    'use strict';

    const TICKERS = ['SPX', 'SPY', 'QQQ'];
    const ROWS = ['Flow Cross', 'Flow Divergence', 'Key Level', 'Spike/Dump'];
    const TYPE_TO_ROW = {
        flow_cross:       'Flow Cross',
        flow_divergence:  'Flow Divergence',
        flow_convergence: 'Flow Divergence',
        key_level:        'Key Level',
        spike:            'Spike/Dump',
        dump:             'Spike/Dump',
        bullish_volume:   'Spike/Dump',
    };
    const ROW_TO_FIELD = {
        'Flow Cross':      'flow_cross',
        'Flow Divergence': 'flow_divergence',
        'Key Level':       'key_level',
        'Spike/Dump':      'spike_dump',
    };
    const MAX_LOG_ROWS = 50;

    let _container = null;
    let _matrixEl = null;
    let _logEl    = null;
    let _styleEl  = null;
    let _unsub    = null;
    let _state    = {};   // {ticker: {flow_cross: 'bullish'|'bearish'|'none', ...}}
    let _logRows  = [];

    function _injectStyles() {
        if (document.getElementById('aipanel-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'aipanel-styles';
        _styleEl.textContent = `
            .ap-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; color:rgba(220,225,235,.9);
                       font-family:'JetBrains Mono','Share Tech Mono',monospace; }
            .ap-hdr  { display:flex; align-items:center; padding:6px 10px; border-bottom:1px solid rgba(255,255,255,.04); gap:8px; }
            .ap-title { font-size:10px; font-weight:700; color:rgba(220,225,235,.9); letter-spacing:.8px; text-transform:uppercase; }
            .ap-sub   { font-size:8.5px; color:rgba(140,150,180,.55); margin-left:8px; letter-spacing:.5px; }
            .ap-ts    { font-size:8.5px; color:rgba(140,150,180,.55); margin-left:auto; }
            .ap-matrix-wrap { padding:10px 14px 4px 14px; }
            .ap-matrix { width:100%; border-collapse:collapse; font-size:10px; }
            .ap-matrix th, .ap-matrix td { padding:5px 8px; text-align:center; }
            .ap-matrix th { font-size:9px; font-weight:600; color:rgba(180,190,220,.65); letter-spacing:.6px; border-bottom:1px solid rgba(255,255,255,.04); }
            .ap-matrix .ap-row-label { text-align:left; color:rgba(200,210,230,.75); font-size:9.5px; letter-spacing:.3px; }
            .ap-matrix .ap-row-label::before { content: '> '; color: rgba(124,90,247,.55); }
            .ap-dot { display:inline-block; width:14px; height:14px; border-radius:50%; line-height:14px;
                      background:rgba(60,65,85,.45); border:1px solid rgba(90,100,125,.3); }
            .ap-dot-bullish { background:#1fd17a; border-color:#26c981; box-shadow:0 0 6px rgba(31,209,122,.5); }
            .ap-dot-bearish { background:#e03060; border-color:#d42a58; box-shadow:0 0 6px rgba(224,48,96,.5); }
            .ap-dot-none    { background:rgba(60,65,85,.45); }
            .ap-log { flex:1; overflow-y:auto; overflow-x:hidden; padding:6px 12px 10px 12px; border-top:1px solid rgba(255,255,255,.04); }
            .ap-log::-webkit-scrollbar { width:3px; }
            .ap-log::-webkit-scrollbar-thumb { background:rgba(124,90,247,.3); border-radius:2px; }
            .ap-log-row { display:flex; gap:8px; font-size:9.5px; line-height:1.6; padding:1px 0; color:rgba(220,225,235,.85); }
            .ap-log-row .ap-log-prefix { color:rgba(124,90,247,.6); }
            .ap-log-row.bull .ap-log-msg { color:#1fd17a; }
            .ap-log-row.bear .ap-log-msg { color:#e03060; }
            .ap-log-row .ap-log-ts { margin-left:auto; color:rgba(140,150,180,.5); }
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
            return `${h12}:${m}:${s}${ap}`;
        } catch (e) {
            return '';
        }
    }

    function _buildSkeleton() {
        const thead = '<tr><th></th>' +
            TICKERS.map(t => `<th>${t}</th>`).join('') + '</tr>';
        const tbody = ROWS.map(row =>
            `<tr><td class="ap-row-label">${row}</td>` +
            TICKERS.map(t => `<td><span class="ap-dot ap-dot-none" data-ticker="${t}" data-row="${row}">&nbsp;</span></td>`).join('') +
            `</tr>`).join('');
        return `
            <div class="ap-wrap">
                <div class="ap-hdr">
                    <span class="ap-title">AI Panel</span>
                    <span class="ap-sub">Alerts · Message Log</span>
                    <span class="ap-ts" id="ap-ts">loading…</span>
                </div>
                <div class="ap-matrix-wrap">
                    <table class="ap-matrix">
                        <thead>${thead}</thead>
                        <tbody>${tbody}</tbody>
                    </table>
                </div>
                <div class="ap-log" id="ap-log"></div>
            </div>`;
    }

    function _paintCell(ticker, row, direction) {
        const dot = _matrixEl && _matrixEl.querySelector(
            `.ap-dot[data-ticker="${ticker}"][data-row="${row}"]`);
        if (!dot) return;
        dot.classList.remove('ap-dot-bullish', 'ap-dot-bearish', 'ap-dot-none');
        dot.classList.add('ap-dot-' + (direction || 'none'));
    }

    function _applyState() {
        for (const ticker of TICKERS) {
            const st = _state[ticker] || {};
            for (const row of ROWS) {
                const field = ROW_TO_FIELD[row];
                _paintCell(ticker, row, st[field] || 'none');
            }
        }
    }

    function _renderLogRows() {
        if (!_logEl) return;
        const frag = _logRows.map(r => {
            const cls = r.direction === 'bullish' ? 'bull' : r.direction === 'bearish' ? 'bear' : '';
            return `<div class="ap-log-row ${cls}">` +
                   `<span class="ap-log-prefix">&gt;</span>` +
                   `<span class="ap-log-msg">${r.msg}</span>` +
                   `<span class="ap-log-ts">${r.time}</span>` +
                   `</div>`;
        }).join('');
        _logEl.innerHTML = frag;
    }

    function _formatMsg(alert) {
        // Backend `label` already contains magnitude + bucket for most alert
        // types (e.g. "SPY dump -102.17M [all exp]"). Use it when present;
        // only synthesize a fallback if label is missing. Never append mag/
        // bucket twice.
        if (alert.label) return alert.label;
        const mag = alert.magnitude_m;
        const bucket = alert.bucket ? ` [${alert.bucket}]` : '';
        const sign = (mag != null && mag >= 0) ? '+' : '';
        const magStr = (mag != null) ? ` ${sign}${mag.toFixed(2)}M` : '';
        return (alert.ticker || '') + ' ' + (alert.type || '') + magStr + bucket;
    }

    function _addLogEntry(alert) {
        _logRows.unshift({
            msg: _formatMsg(alert),
            time: _fmtTime(alert.ts),
            direction: alert.direction,
        });
        if (_logRows.length > MAX_LOG_ROWS) _logRows.length = MAX_LOG_ROWS;
        _renderLogRows();
    }

    function _onAlert(alert) {
        if (!alert || !_container) return;
        const tk = alert.ticker;
        const row = TYPE_TO_ROW[alert.type];
        const dir = alert.direction || 'none';
        if (tk && row && TICKERS.includes(tk)) {
            if (!_state[tk]) _state[tk] = {};
            _state[tk][ROW_TO_FIELD[row]] = dir;
            _paintCell(tk, row, dir);
        }
        _addLogEntry(alert);
        const ts = document.getElementById('ap-ts');
        if (ts) ts.textContent = 'live';
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

        // Replay mode: skip the live matrix (it's always 'now'), paint log
        // from the historical endpoint instead.
        if (replayDate) {
            const ts = document.getElementById('ap-ts');
            if (ts) ts.textContent = 'replay ' + replayDate;
            fetch('/api/alerts/history?date=' + replayDate + '&last_n=500',
                  { headers: hdrs })
                .then(r => r.json())
                .then(d => {
                    if (!_container) return;
                    const alerts = (d && d.alerts) || [];
                    _logRows = [];
                    for (const a of alerts.slice(-MAX_LOG_ROWS).reverse()) {
                        _logRows.push({
                            msg: _formatMsg(a),
                            time: _fmtTime(a.ts),
                            direction: a.direction,
                        });
                    }
                    _renderLogRows();
                })
                .catch(() => {});
            return;
        }

        // Matrix state (authoritative — overwrites per-event local state)
        fetch('/api/alerts/state', { headers: hdrs })
            .then(r => r.json())
            .then(d => {
                if (!_container) return;
                if (d && d.tickers) {
                    _state = d.tickers;
                    _applyState();
                }
                const ts = document.getElementById('ap-ts');
                if (ts) ts.textContent = d && d.ready ? 'live' : 'warming';
            })
            .catch(() => {});

        // Seed log. Preserve any live alerts that arrived between init and
        // this response (hydration race): prepend existing _logRows after
        // the server history so newest-first order is retained.
        fetch('/api/_debug/alert_log', { headers: hdrs })
            .then(r => r.json())
            .then(d => {
                if (!_container) return;
                const alerts = (d && d.alerts) || [];
                const fromServer = [];
                for (const a of alerts.slice(-MAX_LOG_ROWS).reverse()) {
                    fromServer.push({
                        msg: _formatMsg(a),
                        time: _fmtTime(a.ts),
                        direction: a.direction,
                    });
                }
                // Any live rows that arrived during fetch live at top of _logRows.
                // Merge: live rows first (newest), then server history (older).
                // De-duplicate by (msg, time) to avoid double-showing the same
                // alert if both streams saw it.
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
        _injectStyles();
        _container = slotEl;
        _container.innerHTML = _buildSkeleton();
        _matrixEl = _container.querySelector('.ap-matrix');
        _logEl    = _container.querySelector('#ap-log');
        _state    = {};
        _logRows  = [];
        _applyState();
        _hydrate();
        if (window.AltarisEvents && typeof window.AltarisEvents.on === 'function') {
            const handler = (alert) => _onAlert(alert);
            window.AltarisEvents.on('data:flow:alert', handler);
            _unsub = () => {
                try { window.AltarisEvents.off('data:flow:alert', handler); } catch (_) {}
            };
        }
    }

    function destroy() {
        if (_unsub) { try { _unsub(); } catch (_) {} _unsub = null; }
        _container = null;
        _matrixEl = null;
        _logEl = null;
        _state = {};
        _logRows = [];
    }

    return { init, destroy };
})();
