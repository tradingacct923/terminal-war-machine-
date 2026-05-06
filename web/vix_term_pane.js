/**
 * VixTermPane — Cross-Asset Vol Regime Dashboard.
 *
 * Backed by /api/intel/vix_term + Socket.IO 'intel:vix_term' (push every 10s
 * during RTH).
 *
 * Renders:
 *   1. HEADER          — VIX, VIX1D, VVIX, SKEW + regime chip + strength
 *   2. REGIME BANNER   — large regime classifier + rationale
 *   3. CURVE STRIP     — front-vs-30d ratio + cross-asset bar comparison
 *   4. SPREAD TABLE    — VXN/RVX/VXD/VXEEM minus VIX, plus ratios
 *   5. TRAJECTORY      — VIX line + regime-tinted dots over last 60 min
 *
 * Regime states (DERIVED — see connectors/vix_term_structure.py):
 *   CALM_CONTANGO        — vol-selling envelope
 *   NORMAL               — mixed signals
 *   TECH_DIVERGENCE      — VXN vs VIX gap ≥4 points
 *   ELEVATED             — VIX 22-30 OR SKEW ≥145
 *   STRESS_CONTANGO      — VIX ≥30, term stress
 *   STRESS_BACKWARDATION — VIX ≥22 + VIX1D > VIX (event/gap risk)
 *   VVIX_DIVERGENCE      — VVIX/VIX ≥9 with VIX <18 (institutional tail-bid)
 *   NO_DATA              — no live VIX feed
 *
 * Anti-theater: every value traces to backend field documented in
 * docs/MEASURED_VALUES.md → "Phase 5: VIX Regime / Cross-Asset Vol Dashboard".
 */
window.VixTermPane = (() => {
    'use strict';

    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _pollTimer = null;
    let _pushHandler = null;
    let _resizeObs = null;

    let _state = null;

    const REST_POLL_MS = 30000;       // CONFIGURED — drift-correction polling

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('vixterm-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'vixterm-styles';
        _styleEl.textContent = `
            .vixterm-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            .vixterm-header { display:grid;
                grid-template-columns: auto 1fr repeat(4, auto);
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:5px; }
            .vixterm-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; }
            .vixterm-stat { display:flex; flex-direction:column; gap:1px; }
            .vixterm-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .vixterm-stat .val { color:rgba(220,230,250,.95); font-weight:700;
                font-size:11px; font-variant-numeric: tabular-nums; }
            .vixterm-stat .val.calm   { color:#85e0a3; }
            .vixterm-stat .val.normal { color:rgba(220,230,250,.95); }
            .vixterm-stat .val.elev   { color:#ffb88a; }
            .vixterm-stat .val.stress { color:#e69aa5; }
            .vixterm-stat .val.warn   { color:#ffb366; }
            .vixterm-stat .sub { font-size:8px; color:rgba(140,150,180,.5); }

            /* ── Regime banner ─────────────────────────────────── */
            .vixterm-regime { background:rgba(255,255,255,.025); border-radius:4px;
                padding:8px 10px; border-left:3px solid rgba(140,150,180,.3);
                display:flex; flex-direction:column; gap:4px; }
            .vixterm-regime.r-calm-contango       { border-left-color:#66cc99;
                background:linear-gradient(90deg, rgba(102,204,153,.10), rgba(255,255,255,.025) 70%); }
            .vixterm-regime.r-normal              { border-left-color:rgba(170,180,210,.4); }
            .vixterm-regime.r-tech-divergence     { border-left-color:#a89eff;
                background:linear-gradient(90deg, rgba(168,158,255,.10), rgba(255,255,255,.025) 70%); }
            .vixterm-regime.r-elevated            { border-left-color:#e6a85c;
                background:linear-gradient(90deg, rgba(230,168,92,.10), rgba(255,255,255,.025) 70%); }
            .vixterm-regime.r-stress-contango     { border-left-color:#e69080;
                background:linear-gradient(90deg, rgba(230,144,128,.14), rgba(255,255,255,.025) 70%); }
            .vixterm-regime.r-stress-backwardation { border-left-color:#cc6677;
                background:linear-gradient(90deg, rgba(204,102,119,.18), rgba(255,255,255,.025) 70%); }
            .vixterm-regime.r-vvix-divergence     { border-left-color:#7ec4ff;
                background:linear-gradient(90deg, rgba(126,196,255,.10), rgba(255,255,255,.025) 70%); }
            .vixterm-regime.r-no-data             { border-left-color:rgba(170,180,210,.2); }
            .vixterm-regime-row { display:flex; align-items:baseline;
                justify-content:space-between; gap:10px; }
            .vixterm-regime-tag { font-weight:700; font-size:13px; letter-spacing:.6px;
                padding:3px 9px; border-radius:3px; }
            .vixterm-regime-tag.calm-contango    { background:rgba(102,204,153,.18); color:#85e0a3; }
            .vixterm-regime-tag.normal           { background:rgba(170,180,210,.10); color:rgba(180,190,210,.7); }
            .vixterm-regime-tag.tech-divergence  { background:rgba(168,158,255,.18); color:#bcb3ff; }
            .vixterm-regime-tag.elevated         { background:rgba(230,168,92,.18); color:#ffc285; }
            .vixterm-regime-tag.stress-contango  { background:rgba(230,144,128,.20); color:#ffb098; }
            .vixterm-regime-tag.stress-backwardation { background:rgba(204,102,119,.22); color:#ff9aa8; }
            .vixterm-regime-tag.vvix-divergence  { background:rgba(126,196,255,.18); color:#a8d6ff; }
            .vixterm-regime-tag.no-data          { background:rgba(170,180,210,.06); color:rgba(140,150,180,.5); }
            .vixterm-regime-strength { font-size:10px; color:rgba(160,170,200,.7);
                font-family:'JetBrains Mono',monospace; }
            .vixterm-regime-rationale { font-size:9px; color:rgba(170,180,210,.7);
                line-height:1.35; }

            /* ── Cross-asset vol bars ────────────────────────────── */
            .vixterm-cross { background:rgba(255,255,255,.02); border-radius:3px;
                padding:6px 8px; display:flex; flex-direction:column; gap:4px; }
            .vixterm-cross-hdr { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px; }
            .vixterm-cross-bars { display:flex; flex-direction:column; gap:3px; }
            .vixterm-bar-row { display:grid;
                grid-template-columns: 50px 1fr 60px 50px;
                gap:6px; align-items:center; font-size:9px; }
            .vixterm-bar-row .name { color:rgba(180,190,220,.7);
                font-weight:600; font-size:9px; }
            .vixterm-bar-row .bar { height:10px; background:rgba(255,255,255,.04);
                border-radius:1px; position:relative; overflow:hidden; }
            .vixterm-bar-row .bar > .fill { position:absolute; top:0; bottom:0;
                left:0; transition:width .3s ease, background .3s ease; }
            .vixterm-bar-row .val {
                font-family:'JetBrains Mono',monospace; font-weight:700;
                color:rgba(220,230,250,.95); text-align:right;
                font-variant-numeric: tabular-nums; }
            .vixterm-bar-row .spread {
                font-family:'JetBrains Mono',monospace;
                color:rgba(160,170,200,.7); font-size:8px; text-align:right;
                font-variant-numeric: tabular-nums; }
            .vixterm-bar-row .spread.pos { color:#e69aa5; }
            .vixterm-bar-row .spread.neg { color:#85e0a3; }

            /* ── Spread / ratios row ─────────────────────────────── */
            .vixterm-ratios { display:grid; grid-template-columns: repeat(3, 1fr);
                gap:5px; }
            .vixterm-ratio-cell { background:rgba(255,255,255,.02);
                border-radius:3px; padding:5px 7px; display:flex;
                flex-direction:column; gap:1px; }
            .vixterm-ratio-cell .k { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px; }
            .vixterm-ratio-cell .v { font-size:13px; font-weight:700;
                font-family:'JetBrains Mono',monospace; color:rgba(220,230,250,.95);
                font-variant-numeric: tabular-nums; }
            .vixterm-ratio-cell .v.up    { color:#e69aa5; }
            .vixterm-ratio-cell .v.dn    { color:#85e0a3; }
            .vixterm-ratio-cell .v.warn  { color:#ffb366; }
            .vixterm-ratio-cell .sub { font-size:7px; color:rgba(140,150,180,.5); }

            /* ── Trajectory canvas ───────────────────────────────── */
            .vixterm-traj-wrap { flex:1 1 auto; min-height:80px;
                background:rgba(255,255,255,.015); border-radius:3px;
                padding:5px 7px; display:flex; flex-direction:column; gap:3px;
                overflow:hidden; }
            .vixterm-traj-head { display:flex; justify-content:space-between;
                font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px; }
            .vixterm-traj-canvas { flex:1 1 auto; width:100%; min-height:60px;
                display:block; }
            .vixterm-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="vixterm-wrap">
            <div class="vixterm-header">
                <div class="vixterm-title">📈 VOL REGIME</div>
                <div></div>
                <div class="vixterm-stat">
                    <span class="lbl">VIX</span>
                    <span class="val" data-fld="hdr_vix">—</span>
                </div>
                <div class="vixterm-stat">
                    <span class="lbl">VIX1D</span>
                    <span class="val" data-fld="hdr_vix1d">—</span>
                </div>
                <div class="vixterm-stat">
                    <span class="lbl">VVIX</span>
                    <span class="val" data-fld="hdr_vvix">—</span>
                </div>
                <div class="vixterm-stat">
                    <span class="lbl">SKEW</span>
                    <span class="val" data-fld="hdr_skew">—</span>
                </div>
            </div>

            <div class="vixterm-regime" data-fld="regime_host">
                <div class="vixterm-empty">Awaiting first compute…</div>
            </div>

            <div class="vixterm-cross">
                <div class="vixterm-cross-hdr">Cross-asset 30d vol vs VIX</div>
                <div class="vixterm-cross-bars" data-fld="cross_host"></div>
            </div>

            <div class="vixterm-ratios">
                <div class="vixterm-ratio-cell">
                    <span class="k">VIX1D / VIX</span>
                    <span class="v" data-fld="ratio_v1d">—</span>
                    <span class="sub">&gt;1 = backwardation</span>
                </div>
                <div class="vixterm-ratio-cell">
                    <span class="k">VVIX / VIX</span>
                    <span class="v" data-fld="ratio_vvix">—</span>
                    <span class="sub">&gt;9 = institutional bid</span>
                </div>
                <div class="vixterm-ratio-cell">
                    <span class="k">10y yield</span>
                    <span class="v" data-fld="ratio_tnx">—</span>
                    <span class="sub">$TNX × 10</span>
                </div>
            </div>

            <div class="vixterm-traj-wrap">
                <div class="vixterm-traj-head">
                    <span>VIX over last 60 min · regime-tinted dots</span>
                    <span data-fld="traj_summary">—</span>
                </div>
                <canvas class="vixterm-traj-canvas" data-fld="traj_canvas"></canvas>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────
    function _fmtNum(v, dp = 2) {
        if (!Number.isFinite(v)) return '—';
        return v.toFixed(dp);
    }
    function _fmtSigned(v, dp = 2) {
        if (!Number.isFinite(v)) return '—';
        return (v >= 0 ? '+' : '') + v.toFixed(dp);
    }
    function _vixLevelClass(vix) {
        if (!Number.isFinite(vix)) return '';
        if (vix < 16) return 'calm';
        if (vix < 22) return 'normal';
        if (vix < 30) return 'elev';
        return 'stress';
    }
    function _regimeDisplay(regime) {
        const map = {
            'CALM_CONTANGO':        { tag:'calm-contango',    wrap:'r-calm-contango',    label:'⬇ CALM CONTANGO' },
            'NORMAL':               { tag:'normal',           wrap:'r-normal',           label:'· NORMAL' },
            'TECH_DIVERGENCE':      { tag:'tech-divergence',  wrap:'r-tech-divergence',  label:'◑ TECH DIVERGENCE' },
            'ELEVATED':             { tag:'elevated',         wrap:'r-elevated',         label:'△ ELEVATED' },
            'STRESS_CONTANGO':      { tag:'stress-contango',  wrap:'r-stress-contango',  label:'⚠ STRESS CONTANGO' },
            'STRESS_BACKWARDATION': { tag:'stress-backwardation', wrap:'r-stress-backwardation', label:'🔥 STRESS BACKWARDATION' },
            'VVIX_DIVERGENCE':      { tag:'vvix-divergence',  wrap:'r-vvix-divergence',  label:'◐ VVIX DIVERGENCE' },
            'NO_DATA':              { tag:'no-data',          wrap:'r-no-data',          label:'… NO DATA' },
        };
        return map[regime] || map['NO_DATA'];
    }

    // ── Renderers ──────────────────────────────────────────────────────

    function _renderHeader() {
        if (!_slot || !_state) return;
        const tickers = _state.tickers || {};
        const set = (sel, txt, cls) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (!el) return;
            el.textContent = txt;
            if (cls !== undefined) el.className = cls ? ('val ' + cls) : 'val';
        };
        const vix = tickers.VIX;
        set('hdr_vix', _fmtNum(vix), _vixLevelClass(vix));
        set('hdr_vix1d', _fmtNum(tickers.VIX1D));
        set('hdr_vvix', _fmtNum(tickers.VVIX));
        // SKEW level color: <135 normal, 135-145 warn, >145 elev
        const skew = tickers.SKEW;
        let skewCls = '';
        if (Number.isFinite(skew)) {
            if (skew >= 145) skewCls = 'elev';
            else if (skew >= 135) skewCls = 'warn';
        }
        set('hdr_skew', _fmtNum(skew, 1), skewCls);
    }

    function _renderRegime() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="regime_host"]');
        if (!host) return;
        const r = _state.regime || 'NO_DATA';
        const display = _regimeDisplay(r);
        host.className = 'vixterm-regime ' + display.wrap;
        if (r === 'NO_DATA') {
            host.innerHTML = `<div class="vixterm-empty">${_state.rationale || 'awaiting data'}</div>`;
            return;
        }
        const strength = Number.isFinite(_state.regime_strength)
            ? (_state.regime_strength * 100).toFixed(0) + '%' : '—';
        host.innerHTML = `
            <div class="vixterm-regime-row">
                <span class="vixterm-regime-tag ${display.tag}">${display.label}</span>
                <span class="vixterm-regime-strength">strength ${strength}</span>
            </div>
            <div class="vixterm-regime-rationale">${_state.rationale || ''}</div>
        `;
    }

    function _renderCrossAsset() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="cross_host"]');
        if (!host) return;
        const tickers = _state.tickers || {};
        const spreads = _state.spreads || {};
        const vix = tickers.VIX;
        if (!Number.isFinite(vix)) {
            host.innerHTML = `<div class="vixterm-empty">no VIX</div>`;
            return;
        }

        // Rows: (name, value-key in tickers, spread-key in spreads)
        const rows = [
            ['VIX',   'VIX',   null],
            ['VXN',   'VXN',   'vxn_minus_vix'],
            ['RVX',   'RVX',   'rvx_minus_vix'],
            ['VXD',   'VXD',   'vxd_minus_vix'],
            ['VXEEM', 'VXEEM', 'vxeem_minus_vix'],
            ['OVX',   'OVX',   null],
            ['GVZ',   'GVZ',   null],
        ];

        // For bar normalization, find the max vol value across visible tickers
        let maxVal = vix;
        for (const r of rows) {
            const v = tickers[r[1]];
            if (Number.isFinite(v) && v > maxVal) maxVal = v;
        }
        if (maxVal <= 0) maxVal = 1;

        host.innerHTML = rows.map(r => {
            const name = r[0];
            const val = tickers[r[1]];
            const spreadKey = r[2];
            const spread = spreadKey ? spreads[spreadKey] : null;
            if (!Number.isFinite(val)) {
                return `<div class="vixterm-bar-row">
                    <span class="name">${name}</span>
                    <div class="bar"><div class="fill" style="width:0%"></div></div>
                    <span class="val">—</span>
                    <span class="spread">—</span>
                </div>`;
            }
            const widthPct = (val / maxVal) * 100;
            // Color by name
            const colorMap = {
                'VIX':   '#e6a85c',
                'VXN':   '#a89eff',
                'RVX':   '#7ec4ff',
                'VXD':   '#85b6e6',
                'VXEEM': '#ffb88a',
                'OVX':   '#ffd180',
                'GVZ':   '#ffe17a',
            };
            const color = colorMap[name] || '#888';
            const spreadCls = !Number.isFinite(spread) ? '' : (spread > 0 ? 'pos' : 'neg');
            const spreadTxt = !Number.isFinite(spread) ? '' : _fmtSigned(spread);
            return `<div class="vixterm-bar-row">
                <span class="name">${name}</span>
                <div class="bar"><div class="fill" style="width:${widthPct.toFixed(1)}%; background:${color};"></div></div>
                <span class="val">${val.toFixed(2)}</span>
                <span class="spread ${spreadCls}">${spreadTxt}</span>
            </div>`;
        }).join('');
    }

    function _renderRatios() {
        if (!_slot || !_state) return;
        const ratios = _state.ratios || {};
        const tickers = _state.tickers || {};
        const set = (sel, txt, cls) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (!el) return;
            el.textContent = txt;
            if (cls !== undefined) el.className = cls ? ('v ' + cls) : 'v';
        };
        const v1d = ratios.vix1d_over_vix;
        set('ratio_v1d',
            Number.isFinite(v1d) ? v1d.toFixed(3) + '×' : '—',
            !Number.isFinite(v1d) ? '' : (v1d > 1.05 ? 'up' : (v1d < 0.95 ? 'dn' : '')));
        const vvixR = ratios.vvix_over_vix;
        set('ratio_vvix',
            Number.isFinite(vvixR) ? vvixR.toFixed(2) + '×' : '—',
            !Number.isFinite(vvixR) ? '' : (vvixR >= 9 ? 'warn' : ''));
        const tnx = tickers.TNX;
        // $TNX is yield × 10 — display as actual yield (4.50% etc)
        set('ratio_tnx',
            Number.isFinite(tnx) ? (tnx / 10).toFixed(2) + '%' : '—',
            '');
    }

    function _renderTrajectory() {
        if (!_slot || !_state) return;
        const cv = _slot.querySelector('[data-fld="traj_canvas"]');
        if (!cv) return;
        const dpr = window.devicePixelRatio || 1;
        const W = Math.max(40, cv.clientWidth | 0);
        const H = Math.max(40, cv.clientHeight | 0);
        if (cv.width !== W * dpr) cv.width = W * dpr;
        if (cv.height !== H * dpr) cv.height = H * dpr;
        const ctx = cv.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, W, H);

        const history = (_state.history || []).filter(s =>
            Number.isFinite(s.vix) && Number.isFinite(s.ts));
        const sumEl = _slot.querySelector('[data-fld="traj_summary"]');
        if (history.length < 2) {
            if (sumEl) sumEl.textContent = `${history.length} samples`;
            ctx.font = '9px JetBrains Mono';
            ctx.fillStyle = 'rgba(140,150,180,.4)';
            ctx.textAlign = 'center';
            ctx.fillText('awaiting samples', W / 2, H / 2);
            return;
        }

        // y range — symmetric padding around min/max with floor at 10/ceiling at 50
        let lo = Infinity, hi = -Infinity;
        for (const s of history) {
            if (s.vix < lo) lo = s.vix;
            if (s.vix > hi) hi = s.vix;
        }
        const span = Math.max(0.5, hi - lo);
        lo -= span * 0.10;
        hi += span * 0.10;
        const padTop = 8, padBot = 8;
        const yPx = (v) => padTop + ((hi - v) / (hi - lo)) * (H - padTop - padBot);

        const t0 = history[0].ts;
        const t1 = history[history.length - 1].ts;
        const dt = Math.max(1, t1 - t0);
        const xPx = (ts) => ((ts - t0) / dt) * (W - 8) + 4;

        // Threshold lines: VIX=16 (calm boundary) and VIX=22 (elevated)
        const thresholds = [16, 22, 30];
        ctx.lineWidth = 1;
        for (const t of thresholds) {
            if (t < lo || t > hi) continue;
            const y = yPx(t);
            ctx.strokeStyle = t === 16 ? 'rgba(102,204,153,.18)'
                              : t === 22 ? 'rgba(230,168,92,.20)'
                              : 'rgba(204,102,119,.20)';
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(W, y);
            ctx.stroke();
        }

        // VIX line
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#e6a85c';
        ctx.beginPath();
        for (let i = 0; i < history.length; i++) {
            const x = xPx(history[i].ts);
            const y = yPx(history[i].vix);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Regime-tinted dots
        const colorByRegime = {
            'CALM_CONTANGO':        '#66cc99',
            'NORMAL':                'rgba(170,180,210,.5)',
            'TECH_DIVERGENCE':       '#a89eff',
            'ELEVATED':              '#e6a85c',
            'STRESS_CONTANGO':       '#e69080',
            'STRESS_BACKWARDATION':  '#cc6677',
            'VVIX_DIVERGENCE':       '#7ec4ff',
            'NO_DATA':               'rgba(140,150,180,.3)',
        };
        const stride = Math.max(1, Math.floor(history.length / 30));
        for (let i = 0; i < history.length; i += stride) {
            const s = history[i];
            const c = colorByRegime[s.regime] || '#888';
            ctx.fillStyle = c;
            ctx.beginPath();
            ctx.arc(xPx(s.ts), yPx(s.vix), 1.6, 0, Math.PI * 2);
            ctx.fill();
        }

        // Axis labels
        ctx.font = '8px JetBrains Mono';
        ctx.fillStyle = 'rgba(140,150,180,.6)';
        ctx.textAlign = 'left';
        ctx.fillText(hi.toFixed(2), 2, padTop + 4);
        ctx.fillText(lo.toFixed(2), 2, H - 2);

        if (sumEl) {
            const last = history[history.length - 1];
            const change = history.length >= 2 ? (last.vix - history[0].vix) : 0;
            sumEl.textContent = `${history.length} samples · ${_fmtSigned(change)} over window`;
        }
    }

    function _renderAll() {
        _renderHeader();
        _renderRegime();
        _renderCrossAsset();
        _renderRatios();
        _renderTrajectory();
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
            const r = await _authFetch('/api/intel/vix_term');
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
            window.AltarisEvents.on('socket:intel:vix_term', _pushHandler);
        }

        try {
            if (typeof ResizeObserver !== 'undefined') {
                _resizeObs = new ResizeObserver(() => _renderTrajectory());
                _resizeObs.observe(_slot);
            }
        } catch (_) {}

        _refreshREST();
        _pollTimer = setInterval(_refreshREST, REST_POLL_MS);
    }

    function destroy() {
        _destroyed = true;
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = null;
        if (_resizeObs) { try { _resizeObs.disconnect(); } catch (_) {} _resizeObs = null; }
        if (window.AltarisEvents && _pushHandler) {
            window.AltarisEvents.off('socket:intel:vix_term', _pushHandler);
            _pushHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
