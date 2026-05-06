/**
 * WingTrackerPane — 0DTE far-OTM call/put aggressor flow.
 *
 * Backed by /api/intel/wing_tracker + Socket.IO 'intel:wing_update' (push
 * every 5s during RTH).
 *
 * Renders:
 *   1. HEADER          — spot, dte, session age, regime chip + strength
 *   2. REGIME BANNER   — NORMAL / ACTIVE / EXTREME + rationale
 *   3. ZONE BARS       — ATM / NEAR_WING / DEEP_WING / TAIL with call/put split
 *                        + buy/sell aggressor counts per zone
 *   4. TOP STRIKES     — 10 most active wing strikes with aggressor skew
 *   5. RECENT PRINTS   — last 20 wing prints (live ticker tape)
 *
 * Zone classification (DERIVED):
 *   ATM        |K − spot| ≤ 1.0% × spot
 *   NEAR_WING  1.0% < |K − spot| ≤ 2.5%
 *   DEEP_WING  2.5% < |K − spot| ≤ 5.0%
 *   TAIL       |K − spot| > 5.0%
 *
 * Anti-theater: every value traces to backend field documented in
 * docs/MEASURED_VALUES.md → "Phase 6: 0DTE Wing Tracker".
 */
window.WingTrackerPane = (() => {
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
        if (document.getElementById('wing-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'wing-styles';
        _styleEl.textContent = `
            .wing-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            .wing-header { display:grid;
                grid-template-columns: auto 1fr auto auto auto;
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:5px; }
            .wing-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; }
            .wing-stat { display:flex; flex-direction:column; gap:1px; }
            .wing-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .wing-stat .val { color:rgba(220,230,250,.95); font-weight:700;
                font-size:11px; font-variant-numeric: tabular-nums; }
            .wing-stat .val.up    { color:#85e0a3; }
            .wing-stat .val.dn    { color:#e69aa5; }
            .wing-stat .val.warn  { color:#ffb366; }
            .wing-stat .sub { font-size:8px; color:rgba(140,150,180,.5); }

            /* ── Regime banner ─────────────────────────────────── */
            .wing-regime { background:rgba(255,255,255,.025); border-radius:4px;
                padding:7px 10px; border-left:3px solid rgba(140,150,180,.3);
                display:flex; flex-direction:column; gap:3px; }
            .wing-regime.r-normal  { border-left-color:rgba(170,180,210,.4); }
            .wing-regime.r-active  { border-left-color:#e6a85c;
                background:linear-gradient(90deg, rgba(230,168,92,.10), rgba(255,255,255,.025) 70%); }
            .wing-regime.r-extreme { border-left-color:#cc6677;
                background:linear-gradient(90deg, rgba(204,102,119,.18), rgba(255,255,255,.025) 70%); }
            .wing-regime.r-no-data { border-left-color:rgba(170,180,210,.2); }
            .wing-regime-row { display:flex; align-items:baseline;
                justify-content:space-between; gap:10px; }
            .wing-regime-tag { font-weight:700; font-size:13px; letter-spacing:.6px;
                padding:3px 9px; border-radius:3px; }
            .wing-regime-tag.normal   { background:rgba(170,180,210,.10); color:rgba(180,190,210,.7); }
            .wing-regime-tag.active   { background:rgba(230,168,92,.18); color:#ffc285; }
            .wing-regime-tag.extreme  { background:rgba(204,102,119,.22); color:#ff9aa8; }
            .wing-regime-tag.no-data  { background:rgba(170,180,210,.06); color:rgba(140,150,180,.5); }
            .wing-regime-strength { font-size:10px; color:rgba(160,170,200,.7);
                font-family:'JetBrains Mono',monospace; }
            .wing-regime-rationale { font-size:9px; color:rgba(170,180,210,.7);
                line-height:1.35; }

            /* ── Zone bars ─────────────────────────────────────── */
            .wing-zones { background:rgba(255,255,255,.02); border-radius:3px;
                padding:6px 8px; display:flex; flex-direction:column; gap:4px; }
            .wing-zones-hdr { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px; }
            .wing-zone-row { display:grid;
                grid-template-columns: 80px 1fr 1fr 50px;
                gap:6px; align-items:center; font-size:9px;
                padding:3px 0;
                border-bottom:1px dashed rgba(255,255,255,.025); }
            .wing-zone-row:last-child { border-bottom:none; }
            .wing-zone-row .name {
                color:rgba(180,190,220,.85); font-weight:700; font-size:9px; }
            .wing-zone-row .name.atm        { color:rgba(220,230,250,.95); }
            .wing-zone-row .name.near       { color:#85b6e6; }
            .wing-zone-row .name.deep       { color:#a89eff; }
            .wing-zone-row .name.tail       { color:#ff9aa8; }
            .wing-zone-row .bar-cell { display:flex; flex-direction:column; gap:1px; }
            .wing-zone-row .bar-cell .lbl { font-size:7px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.4px; }
            .wing-zone-row .bar { height:7px;
                background:rgba(255,255,255,.04); border-radius:1px;
                position:relative; overflow:hidden; }
            .wing-zone-row .bar > .fill { position:absolute; top:0; bottom:0;
                left:0; transition:width .3s ease; }
            .wing-zone-row .bar.call > .fill { background:#85e0a3; }
            .wing-zone-row .bar.put  > .fill { background:#e69aa5; }
            .wing-zone-row .vol {
                font-family:'JetBrains Mono',monospace; font-weight:700;
                color:rgba(220,230,250,.95); font-size:10px;
                text-align:right; font-variant-numeric: tabular-nums; }

            /* ── Top strikes table ─────────────────────────────── */
            .wing-strikes { background:rgba(255,255,255,.02); border-radius:3px;
                padding:5px 7px; display:flex; flex-direction:column; gap:2px; }
            .wing-strikes-hdr { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px; padding-bottom:2px;
                border-bottom:1px solid rgba(255,255,255,.04); }
            .wing-strike-row {
                display:grid; grid-template-columns:
                    20px 60px 1fr 50px 50px 35px 50px;
                gap:4px; align-items:center; font-size:9px;
                font-family:'JetBrains Mono',monospace;
                font-variant-numeric: tabular-nums;
                padding:2px 0; }
            .wing-strike-row .side { font-weight:700; }
            .wing-strike-row .side.c { color:#85e0a3; }
            .wing-strike-row .side.p { color:#e69aa5; }
            .wing-strike-row .strike { color:rgba(220,230,250,.95); font-weight:700; }
            .wing-strike-row .skew-bar {
                height:6px; background:rgba(255,255,255,.04);
                border-radius:1px; position:relative; overflow:hidden; }
            .wing-strike-row .skew-bar > .fill { position:absolute;
                top:0; bottom:0; left:50%; transform-origin:left center;
                transition:transform .25s ease, background .25s ease; }
            .wing-strike-row .vol  { color:rgba(220,230,250,.95);
                text-align:right; font-weight:600; }
            .wing-strike-row .skew { font-size:8px;
                text-align:right; color:rgba(160,170,200,.7); }
            .wing-strike-row .skew.up { color:#85e0a3; }
            .wing-strike-row .skew.dn { color:#e69aa5; }
            .wing-strike-row .zone {
                font-size:7px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.3px;
                text-align:right; }
            .wing-strike-row .dist  { color:rgba(160,170,200,.6);
                text-align:right; font-size:8px; }

            /* ── Recent prints ─────────────────────────────────── */
            .wing-prints {
                flex:1 1 auto; min-height:60px;
                background:rgba(255,255,255,.015); border-radius:3px;
                padding:5px 7px; display:flex; flex-direction:column; gap:2px;
                overflow:hidden; }
            .wing-prints-hdr { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px;
                padding-bottom:2px;
                border-bottom:1px solid rgba(255,255,255,.04); }
            .wing-prints-scroll { flex:1 1 auto; overflow:hidden; }
            .wing-print-row {
                display:grid; grid-template-columns:
                    50px 18px 50px 60px 30px 60px 30px;
                gap:4px; align-items:center;
                font-size:8px;
                font-family:'JetBrains Mono',monospace;
                font-variant-numeric: tabular-nums;
                padding:1px 0;
                border-bottom:1px dashed rgba(255,255,255,.020); }
            .wing-print-row:last-child { border-bottom:none; }
            .wing-print-row .ts { color:rgba(140,150,180,.6); }
            .wing-print-row .side { font-weight:700; }
            .wing-print-row .side.c { color:#85e0a3; }
            .wing-print-row .side.p { color:#e69aa5; }
            .wing-print-row .strike { color:rgba(220,230,250,.95); font-weight:700; }
            .wing-print-row .dist { color:rgba(160,170,200,.6); }
            .wing-print-row .size { color:rgba(220,230,250,.95);
                font-weight:600; text-align:right; }
            .wing-print-row .premium { color:rgba(160,170,200,.7); text-align:right; }
            .wing-print-row .aggr { font-weight:700; text-align:right; }
            .wing-print-row .aggr.buy  { color:#85e0a3; }
            .wing-print-row .aggr.sell { color:#e69aa5; }

            .wing-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="wing-wrap">
            <div class="wing-header">
                <div class="wing-title">🪶 0DTE WING TRACKER</div>
                <div></div>
                <div class="wing-stat">
                    <span class="lbl">spot</span>
                    <span class="val" data-fld="hdr_spot">—</span>
                </div>
                <div class="wing-stat">
                    <span class="lbl">0DTE</span>
                    <span class="val" data-fld="hdr_dte">—</span>
                    <span class="sub" data-fld="hdr_age">—</span>
                </div>
                <div class="wing-stat">
                    <span class="lbl">regime</span>
                    <span class="val" data-fld="hdr_regime">—</span>
                </div>
            </div>

            <div class="wing-regime" data-fld="regime_host">
                <div class="wing-empty">Awaiting first 0DTE prints…</div>
            </div>

            <div class="wing-zones">
                <div class="wing-zones-hdr">Zone volume (calls vs puts)</div>
                <div data-fld="zones_host"></div>
            </div>

            <div class="wing-strikes">
                <div class="wing-strikes-hdr">
                    Top active wing strikes · BUY/SELL aggressor skew
                </div>
                <div data-fld="strikes_host"></div>
            </div>

            <div class="wing-prints">
                <div class="wing-prints-hdr">Recent wing prints</div>
                <div class="wing-prints-scroll" data-fld="prints_host"></div>
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
    function _fmtClock(ts) {
        if (!Number.isFinite(ts) || ts === 0) return '—';
        const d = new Date(ts * 1000);
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        const ss = String(d.getSeconds()).padStart(2, '0');
        return `${hh}:${mm}:${ss}`;
    }
    function _regimeDisplay(r) {
        const map = {
            'NORMAL':  { tag:'normal',  wrap:'r-normal',  label:'· NORMAL' },
            'ACTIVE':  { tag:'active',  wrap:'r-active',  label:'△ ACTIVE' },
            'EXTREME': { tag:'extreme', wrap:'r-extreme', label:'🔥 EXTREME' },
            'NO_DATA': { tag:'no-data', wrap:'r-no-data', label:'… NO DATA' },
        };
        return map[r] || map['NO_DATA'];
    }

    // ── Renderers ──────────────────────────────────────────────────────

    function _renderHeader() {
        if (!_slot || !_state) return;
        const set = (sel, txt, cls) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (!el) return;
            el.textContent = txt;
            if (cls !== undefined) el.className = cls ? ('val ' + cls) : 'val';
        };
        const setSub = (sel, txt) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (el) el.textContent = txt;
        };
        set('hdr_spot', _state.spot ? _fmtUsd(_state.spot) : '—');
        set('hdr_dte', _state.dte_key || '—');
        const ageS = _state.session_age_sec || 0;
        const ageStr = ageS >= 3600
            ? `${(ageS/3600).toFixed(1)}h`
            : ageS >= 60
                ? `${(ageS/60).toFixed(0)}m`
                : `${Math.round(ageS)}s`;
        setSub('hdr_age', `age ${ageStr}`);
        const r = _state.regime || 'NO_DATA';
        const cls = r === 'EXTREME' ? 'dn' : (r === 'ACTIVE' ? 'warn' : '');
        set('hdr_regime', r, cls);
    }

    function _renderRegime() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="regime_host"]');
        if (!host) return;
        const r = _state.regime || 'NO_DATA';
        const display = _regimeDisplay(r);
        host.className = 'wing-regime ' + display.wrap;
        if (r === 'NO_DATA') {
            host.innerHTML = `<div class="wing-empty">${_state.rationale || 'awaiting data'}</div>`;
            return;
        }
        const strength = Number.isFinite(_state.regime_strength)
            ? (_state.regime_strength * 100).toFixed(0) + '%' : '—';
        const ndd = Number.isFinite(_state.net_dealer_delta_est_shares)
            ? _state.net_dealer_delta_est_shares : null;
        const nddTxt = (ndd === null) ? ''
            : ` · est dealer Δ ${ndd > 0 ? '+' : ''}${_fmtNum(ndd)} sh`;
        host.innerHTML = `
            <div class="wing-regime-row">
                <span class="wing-regime-tag ${display.tag}">${display.label}</span>
                <span class="wing-regime-strength">strength ${strength}${nddTxt}</span>
            </div>
            <div class="wing-regime-rationale">${_state.rationale || ''}</div>
        `;
    }

    function _renderZones() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="zones_host"]');
        if (!host) return;
        const zones = _state.zones || {};
        const order = ['ATM', 'NEAR_WING', 'DEEP_WING', 'TAIL'];

        // Find max total volume for bar normalization
        let maxVol = 0;
        for (const z of order) {
            const v = (zones[z] || {}).total_volume || 0;
            if (v > maxVol) maxVol = v;
        }
        if (maxVol < 1) maxVol = 1;

        const nameClass = {
            'ATM':       'atm',
            'NEAR_WING':'near',
            'DEEP_WING':'deep',
            'TAIL':     'tail',
        };
        const labelMap = {
            'ATM':       'ATM',
            'NEAR_WING': 'NEAR',
            'DEEP_WING': 'DEEP',
            'TAIL':      'TAIL',
        };

        host.innerHTML = order.map(z => {
            const data = zones[z] || {};
            const callV = data.call_volume || 0;
            const putV = data.put_volume || 0;
            const totalV = data.total_volume || 0;
            const callPct = (callV / maxVol) * 100;
            const putPct = (putV / maxVol) * 100;
            const buys = data.buy_count || 0;
            const sells = data.sell_count || 0;
            return `<div class="wing-zone-row">
                <span class="name ${nameClass[z]}">${labelMap[z]}</span>
                <div class="bar-cell">
                    <span class="lbl">CALL ${_fmtNum(callV)}</span>
                    <div class="bar call"><div class="fill" style="width:${callPct.toFixed(1)}%"></div></div>
                </div>
                <div class="bar-cell">
                    <span class="lbl">PUT  ${_fmtNum(putV)}</span>
                    <div class="bar put"><div class="fill" style="width:${putPct.toFixed(1)}%"></div></div>
                </div>
                <span class="vol">${_fmtNum(totalV)}</span>
            </div>`;
        }).join('');
    }

    function _renderStrikes() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="strikes_host"]');
        if (!host) return;
        const strikes = _state.top_strikes || [];
        if (!strikes.length) {
            host.innerHTML = `<div class="wing-empty">no active wing strikes yet</div>`;
            return;
        }
        host.innerHTML = strikes.map(s => {
            const skew = Number.isFinite(s.aggressor_skew) ? s.aggressor_skew : 0;
            const skewPct = Math.abs(skew) * 50;
            const skewOffset = skew < 0 ? -skewPct : 0;
            const skewColor = skew > 0.10 ? '#85e0a3'
                              : skew < -0.10 ? '#e69aa5'
                              : 'rgba(170,180,210,.4)';
            const skewCls = skew > 0.10 ? 'up' : (skew < -0.10 ? 'dn' : '');
            const distTxt = Number.isFinite(s.dist_pct) ? _fmtPct(s.dist_pct) : '—';
            return `<div class="wing-strike-row">
                <span class="side ${s.side === 'C' ? 'c' : 'p'}">${s.side}</span>
                <span class="strike">$${(s.strike || 0).toFixed(2)}</span>
                <div class="skew-bar"><div class="fill" style="
                    width:${skewPct.toFixed(1)}%;
                    transform:translateX(${skewOffset}%);
                    background:${skewColor};
                "></div></div>
                <span class="vol">${_fmtNum(s.volume || 0)}</span>
                <span class="skew ${skewCls}">${(skew >= 0 ? '+' : '') + skew.toFixed(2)}</span>
                <span class="zone">${(s.zone || '').replace('_WING','')}</span>
                <span class="dist">${distTxt}</span>
            </div>`;
        }).join('');
    }

    function _renderPrints() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="prints_host"]');
        if (!host) return;
        const prints = _state.recent_prints || [];
        if (!prints.length) {
            host.innerHTML = `<div class="wing-empty">no wing prints yet</div>`;
            return;
        }
        // Render last 20 newest-first
        const ordered = prints.slice().reverse();
        host.innerHTML = ordered.map(p => {
            const sideCls = p.side === 'C' ? 'c' : 'p';
            const aggrCls = p.aggressor === 'BUY' ? 'buy' : 'sell';
            const distTxt = Number.isFinite(p.dist_pct) ? _fmtPct(p.dist_pct) : '—';
            return `<div class="wing-print-row">
                <span class="ts">${_fmtClock(p.ts)}</span>
                <span class="side ${sideCls}">${p.side}</span>
                <span class="strike">$${(p.strike || 0).toFixed(2)}</span>
                <span class="dist">${distTxt}</span>
                <span class="size">${_fmtNum(p.size)}</span>
                <span class="premium">$${_fmtNum(p.premium || 0)}</span>
                <span class="aggr ${aggrCls}">${p.aggressor}</span>
            </div>`;
        }).join('');
    }

    function _renderAll() {
        _renderHeader();
        _renderRegime();
        _renderZones();
        _renderStrikes();
        _renderPrints();
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
            const r = await _authFetch('/api/intel/wing_tracker');
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
            window.AltarisEvents.on('socket:intel:wing_update', _pushHandler);
        }

        _refreshREST();
        _pollTimer = setInterval(_refreshREST, REST_POLL_MS);
    }

    function destroy() {
        _destroyed = true;
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = null;
        if (window.AltarisEvents && _pushHandler) {
            window.AltarisEvents.off('socket:intel:wing_update', _pushHandler);
            _pushHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
