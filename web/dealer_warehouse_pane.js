/**
 * DealerWarehousePane — per-strike commitment quality scorer.
 *
 * Backed by /api/intel/dealer_warehouse + Socket.IO 'intel:dealer_warehouse'
 * (push every 10s during RTH).
 *
 * Renders:
 *   1. HEADER          — spot, contract count, strike count, total posted
 *   2. TOP COMMITTED   — 5 strikes where dealers are GENUINELY defending
 *   3. TOP PHANTOM     — 5 strikes with high posted time but low fill rate
 *   4. ALL STRIKES     — full per-strike table sorted by distance to spot
 *
 * Classification (DERIVED):
 *   COMMITTED  posted ≥60s AND catch_rate ≥0.05/s → real defense
 *   PHANTOM    posted ≥120s AND catch_rate <0.005/s → HFT phantom depth
 *   ACTIVE     low posted but catch_rate ≥0.05/s → in-and-out aggressive
 *   INACTIVE   low everywhere
 *
 * Why this matters: Pin Convergence uses this to upgrade `warehouse_strength`
 * from oi_score proxy to MEASURED commitment. A strike with high γ × OI but
 * PHANTOM depth = paper wall. COMMITTED depth = real wall.
 *
 * Anti-theater: every value traces to backend field documented in
 * docs/MEASURED_VALUES.md → "Phase 8: Dealer Warehouse Quality".
 */
window.DealerWarehousePane = (() => {
    'use strict';

    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _pollTimer = null;
    let _pushHandler = null;

    let _state = null;

    const REST_POLL_MS = 30000;     // CONFIGURED — drift-correction polling

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('warehouse-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'warehouse-styles';
        _styleEl.textContent = `
            .wh-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            .wh-header { display:grid;
                grid-template-columns: auto 1fr auto auto auto auto;
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:5px; }
            .wh-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; }
            .wh-stat { display:flex; flex-direction:column; gap:1px; }
            .wh-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .wh-stat .val { color:rgba(220,230,250,.95); font-weight:700;
                font-size:11px; font-variant-numeric: tabular-nums; }
            .wh-stat .sub { font-size:8px; color:rgba(140,150,180,.5); }

            /* ── Lists (committed / phantom) ───────────────────── */
            .wh-lists { display:grid; grid-template-columns: 1fr 1fr; gap:6px; }
            .wh-list { background:rgba(255,255,255,.02); border-radius:3px;
                padding:5px 7px; display:flex; flex-direction:column; gap:2px; }
            .wh-list-hdr { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px;
                padding-bottom:2px;
                border-bottom:1px solid rgba(255,255,255,.04);
                display:flex; align-items:center; justify-content:space-between; }
            .wh-list-hdr.committed { color:#85e0a3; }
            .wh-list-hdr.phantom   { color:#a89eff; }
            .wh-list-row {
                display:grid;
                grid-template-columns: 16px 56px 1fr 50px 38px;
                gap:4px; align-items:center; font-size:9px;
                font-family:'JetBrains Mono',monospace;
                font-variant-numeric: tabular-nums;
                padding:2px 0; }
            .wh-list-row .side { font-weight:700; }
            .wh-list-row .side.c { color:#85e0a3; }
            .wh-list-row .side.p { color:#e69aa5; }
            .wh-list-row .strike { color:rgba(220,230,250,.95); font-weight:700; }
            .wh-list-row .bar { height:6px; background:rgba(255,255,255,.04);
                border-radius:1px; position:relative; overflow:hidden; }
            .wh-list-row .bar > .fill { position:absolute; top:0; bottom:0;
                left:0; transition:width .3s ease, background .3s ease; }
            .wh-list-row .bar.committed > .fill { background:#85e0a3; }
            .wh-list-row .bar.phantom   > .fill { background:#a89eff; }
            .wh-list-row .score { color:rgba(220,230,250,.95);
                font-weight:600; text-align:right; }
            .wh-list-row .dist { color:rgba(160,170,200,.7); text-align:right;
                font-size:8px; }

            /* ── All strikes table ─────────────────────────────── */
            .wh-all { flex:1 1 auto; min-height:80px;
                background:rgba(255,255,255,.015); border-radius:3px;
                padding:5px 7px; display:flex; flex-direction:column;
                overflow:hidden; }
            .wh-all-hdr {
                display:grid;
                grid-template-columns: 16px 60px 50px 60px 50px 50px 60px 40px 50px;
                gap:4px; font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px;
                padding:2px 0;
                border-bottom:1px solid rgba(255,255,255,.04); }
            .wh-all-hdr > * { text-align:right; }
            .wh-all-hdr > :first-child,
            .wh-all-hdr > :nth-child(2) { text-align:left; }
            .wh-all-scroll { flex:1 1 auto; overflow:auto; }
            .wh-all-row {
                display:grid;
                grid-template-columns: 16px 60px 50px 60px 50px 50px 60px 40px 50px;
                gap:4px; align-items:center;
                font-size:9px;
                font-family:'JetBrains Mono',monospace;
                font-variant-numeric: tabular-nums;
                padding:2px 0;
                border-bottom:1px dashed rgba(255,255,255,.020); }
            .wh-all-row > * { text-align:right; }
            .wh-all-row > :first-child,
            .wh-all-row > :nth-child(2) { text-align:left; }
            .wh-all-row:last-child { border-bottom:none; }
            .wh-all-row .side { font-weight:700; }
            .wh-all-row .side.c { color:#85e0a3; }
            .wh-all-row .side.p { color:#e69aa5; }
            .wh-all-row .strike { color:rgba(220,230,250,.95); font-weight:700; }
            .wh-all-row .dist.up { color:#85b6e6; }
            .wh-all-row .dist.dn { color:#e69580; }
            .wh-all-row .dist.dim { color:rgba(170,180,210,.5); }
            .wh-all-row .class {
                font-weight:700; font-size:8px; padding:1px 4px;
                border-radius:2px; }
            .wh-all-row .class.committed { background:rgba(133,224,163,.18); color:#85e0a3; }
            .wh-all-row .class.phantom   { background:rgba(168,158,255,.18); color:#bcb3ff; }
            .wh-all-row .class.active    { background:rgba(255,209,128,.18); color:#ffd180; }
            .wh-all-row .class.inactive  { background:rgba(170,180,210,.10); color:rgba(180,190,210,.5); }
            .wh-all-row .ndim {
                color:rgba(160,170,200,.7); font-size:8px; }
            .wh-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="wh-wrap">
            <div class="wh-header">
                <div class="wh-title">🛡 DEALER WAREHOUSE</div>
                <div></div>
                <div class="wh-stat">
                    <span class="lbl">spot</span>
                    <span class="val" data-fld="hdr_spot">—</span>
                </div>
                <div class="wh-stat">
                    <span class="lbl">contracts</span>
                    <span class="val" data-fld="hdr_contracts">—</span>
                </div>
                <div class="wh-stat">
                    <span class="lbl">strikes</span>
                    <span class="val" data-fld="hdr_strikes">—</span>
                </div>
                <div class="wh-stat">
                    <span class="lbl">posted</span>
                    <span class="val" data-fld="hdr_posted">—</span>
                    <span class="sub" data-fld="hdr_caught">— caught</span>
                </div>
            </div>

            <div class="wh-lists">
                <div class="wh-list">
                    <div class="wh-list-hdr committed">
                        <span>🛡 TOP COMMITTED</span>
                        <span style="color:rgba(140,150,180,.55); text-transform:none">caught×rate</span>
                    </div>
                    <div data-fld="committed_host">
                        <div class="wh-empty">awaiting capture data…</div>
                    </div>
                </div>
                <div class="wh-list">
                    <div class="wh-list-hdr phantom">
                        <span>👻 TOP PHANTOM</span>
                        <span style="color:rgba(140,150,180,.55); text-transform:none">post w/ no fills</span>
                    </div>
                    <div data-fld="phantom_host">
                        <div class="wh-empty">awaiting capture data…</div>
                    </div>
                </div>
            </div>

            <div class="wh-all">
                <div class="wh-all-hdr">
                    <span></span>
                    <span>strike</span>
                    <span>dist</span>
                    <span>class</span>
                    <span>posted</span>
                    <span>caught</span>
                    <span>at-top</span>
                    <span>rate/s</span>
                    <span>venue</span>
                </div>
                <div class="wh-all-scroll" data-fld="all_host">
                    <div class="wh-empty">awaiting capture data…</div>
                </div>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────
    function _fmtNum(n) {
        if (!Number.isFinite(n)) return '—';
        const a = Math.abs(n);
        if (a >= 1e6) return (n/1e6).toFixed(2) + 'M';
        if (a >= 1e3) return (n/1e3).toFixed(1) + 'K';
        return Math.round(n).toString();
    }
    function _fmtPct(v) {
        if (!Number.isFinite(v)) return '—';
        return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
    }
    function _fmtUsd(v) {
        if (!Number.isFinite(v)) return '—';
        return '$' + v.toFixed(2);
    }
    function _fmtTime(s) {
        if (!Number.isFinite(s)) return '—';
        if (s >= 3600) return (s/3600).toFixed(1) + 'h';
        if (s >= 60)   return (s/60).toFixed(1)   + 'm';
        return Math.round(s) + 's';
    }
    function _classCls(c) {
        const m = {
            'COMMITTED': 'committed',
            'PHANTOM':   'phantom',
            'ACTIVE':    'active',
            'INACTIVE':  'inactive',
        };
        return m[c] || 'inactive';
    }

    // ── Renderers ──────────────────────────────────────────────────────

    function _renderHeader() {
        if (!_slot || !_state) return;
        const set = (sel, txt) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (el) el.textContent = txt;
        };
        set('hdr_spot', _state.spot ? _fmtUsd(_state.spot) : '—');
        set('hdr_contracts', String(_state.contract_count || 0));
        set('hdr_strikes', String(_state.strike_count || 0));
        const tots = _state.totals || {};
        set('hdr_posted', _fmtTime(tots.posted_time_s || 0));
        set('hdr_caught', `${_fmtNum(tots.caught_at_top || 0)} caught at top`);
    }

    function _renderRankList(hostSel, rows, cls) {
        const host = _slot.querySelector(`[data-fld="${hostSel}"]`);
        if (!host) return;
        if (!rows || !rows.length) {
            host.innerHTML = `<div class="wh-empty">none yet</div>`;
            return;
        }
        // Find max score for normalization
        const scoreKey = cls === 'committed' ? 'commitment_score' : 'phantom_score';
        let maxScore = 0;
        for (const r of rows) {
            const s = r[scoreKey] || 0;
            if (s > maxScore) maxScore = s;
        }
        if (maxScore < 0.001) maxScore = 1;
        host.innerHTML = rows.map(r => {
            const score = r[scoreKey] || 0;
            const widthPct = (score / maxScore) * 100;
            const sideCls = r.side === 'C' ? 'c' : 'p';
            const distTxt = Number.isFinite(r.dist_pct) ? _fmtPct(r.dist_pct) : '—';
            return `<div class="wh-list-row">
                <span class="side ${sideCls}">${r.side}</span>
                <span class="strike">$${(r.K || 0).toFixed(2)}</span>
                <div class="bar ${cls}"><div class="fill" style="width:${widthPct.toFixed(1)}%"></div></div>
                <span class="score">${_fmtNum(score)}</span>
                <span class="dist">${distTxt}</span>
            </div>`;
        }).join('');
    }

    function _renderAllStrikes() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="all_host"]');
        if (!host) return;
        const rows = _state.strikes || [];
        if (!rows.length) {
            host.innerHTML = `<div class="wh-empty">${_state.reason || 'awaiting capture data'}</div>`;
            return;
        }
        host.innerHTML = rows.map(r => {
            const sideCls = r.side === 'C' ? 'c' : 'p';
            const dist = Number.isFinite(r.dist_pct) ? r.dist_pct : null;
            const distCls = dist === null ? 'dim' : (dist >= 0 ? 'up' : 'dn');
            const distTxt = dist === null ? '—' : _fmtPct(dist);
            const cls = r.classification || 'INACTIVE';
            const clsLabel = cls === 'COMMITTED' ? 'CMT'
                            : cls === 'PHANTOM'  ? 'PHM'
                            : cls === 'ACTIVE'   ? 'ACT'
                            : 'IDLE';
            return `<div class="wh-all-row">
                <span class="side ${sideCls}">${r.side}</span>
                <span class="strike">$${(r.K || 0).toFixed(2)}</span>
                <span class="dist ${distCls}">${distTxt}</span>
                <span class="class ${_classCls(cls)}">${clsLabel}</span>
                <span class="ndim">${_fmtTime(r.posted_time_s || 0)}</span>
                <span class="ndim">${_fmtNum(r.caught_count || 0)}</span>
                <span class="ndim">${_fmtNum(r.caught_at_top || 0)}</span>
                <span class="ndim">${(r.catch_rate || 0).toFixed(3)}</span>
                <span class="ndim">${r.top_exch || ''}</span>
            </div>`;
        }).join('');
    }

    function _renderAll() {
        _renderHeader();
        _renderRankList('committed_host', _state.top_committed || [], 'committed');
        _renderRankList('phantom_host',   _state.top_phantom   || [], 'phantom');
        _renderAllStrikes();
    }

    // ── Data flow ──────────────────────────────────────────────────────

    function _onPushUpdate(state) {
        if (!state) return;
        _state = state;
        _renderAll();
    }

    async function _refreshREST() {
        if (_destroyed) return;
        try {
            const r = await _authFetch('/api/intel/dealer_warehouse');
            if (!r.ok) return;
            const j = await r.json();
            if (j && typeof j === 'object') {
                _state = j;
                _renderAll();
            }
        } catch (_) {}
    }

    // ── Lifecycle ──────────────────────────────────────────────────────

    function init(slotEl) {
        _slot = slotEl;
        _destroyed = false;
        _state = null;
        _injectStyles();
        _buildShell();

        if (window.AltarisEvents) {
            _pushHandler = (d) => _onPushUpdate(d);
            window.AltarisEvents.on('socket:intel:dealer_warehouse', _pushHandler);
        }

        _refreshREST();
        _pollTimer = setInterval(_refreshREST, REST_POLL_MS);
    }

    function destroy() {
        _destroyed = true;
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = null;
        if (window.AltarisEvents && _pushHandler) {
            window.AltarisEvents.off('socket:intel:dealer_warehouse', _pushHandler);
            _pushHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
