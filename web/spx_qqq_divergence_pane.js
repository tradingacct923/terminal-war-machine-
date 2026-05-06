/**
 * SpxQqqDivergencePane — cross-asset dealer-regime comparator.
 *
 * Backed by /api/intel/spx_qqq_divergence + Socket.IO
 * 'intel:spx_qqq_divergence' (push every 10s during RTH).
 *
 * Renders:
 *   1. HEADER          — SPX/QQQ spots + verdict chip + strength
 *   2. VERDICT BANNER  — large verdict + rationale + alignment indicator
 *   3. COMPARISON TBL  — side-by-side SPX vs QQQ rows for regime/flip/hp/walls
 *   4. TRAJECTORY      — flip-distance % spread over last 60 min (canvas)
 *
 * Verdict states (DERIVED — see connectors/spx_qqq_divergence.py):
 *   ALIGNED_BULL          — both LONG_GAMMA (above flip)
 *   ALIGNED_BEAR          — both SHORT_GAMMA (below flip)
 *   DIVERGENT_REGIME      — opposite regimes (high signal value)
 *   DIVERGENT_MAGNITUDE   — same regime, magnitude ratio ≥2× (medium signal)
 *   NEUTRAL               — at-flip / unclear
 *   NO_DATA               — awaiting valid spot+flip on both
 *
 * Anti-theater: every value traces to backend field documented in
 * docs/MEASURED_VALUES.md → "Phase 4: SPX-vs-QQQ Divergence".
 */
window.SpxQqqDivergencePane = (() => {
    'use strict';

    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _pollTimer = null;
    let _pushHandler = null;
    let _resizeObs = null;

    let _state = null;

    // CONFIGURED — UI cadence; live pushes carry true freshness
    const REST_POLL_MS = 30000;     // drift-correction polling

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('spxqqq-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'spxqqq-styles';
        _styleEl.textContent = `
            .spxqqq-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            .spxqqq-header { display:grid; grid-template-columns: auto 1fr auto auto auto;
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:5px; }
            .spxqqq-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; }
            .spxqqq-stat { display:flex; flex-direction:column; gap:1px; }
            .spxqqq-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .spxqqq-stat .val { color:rgba(220,230,250,.95); font-weight:700; font-size:11px; }
            .spxqqq-stat .val.up { color:#85b6e6; }
            .spxqqq-stat .val.dn { color:#e69580; }
            .spxqqq-stat .val.warn { color:#ffb366; }
            .spxqqq-stat .val.ok { color:#66cc99; }
            .spxqqq-stat .sub { font-size:8px; color:rgba(140,150,180,.5); }

            /* ── Verdict banner ──────────────────────────────────── */
            .spxqqq-verdict { background:rgba(255,255,255,.025); border-radius:4px;
                padding:8px 10px; border-left:3px solid rgba(140,150,180,.3);
                display:flex; flex-direction:column; gap:4px; }
            .spxqqq-verdict.v-aligned-bull {
                border-left-color:#66cc99;
                background:linear-gradient(90deg, rgba(102,204,153,.10), rgba(255,255,255,.025) 70%); }
            .spxqqq-verdict.v-aligned-bear {
                border-left-color:#cc6677;
                background:linear-gradient(90deg, rgba(204,102,119,.10), rgba(255,255,255,.025) 70%); }
            .spxqqq-verdict.v-divergent-regime {
                border-left-color:#e6a85c;
                background:linear-gradient(90deg, rgba(230,168,92,.14), rgba(255,255,255,.025) 70%); }
            .spxqqq-verdict.v-divergent-magnitude {
                border-left-color:#a89eff;
                background:linear-gradient(90deg, rgba(168,158,255,.10), rgba(255,255,255,.025) 70%); }
            .spxqqq-verdict.v-neutral { border-left-color:rgba(170,180,210,.3); }
            .spxqqq-verdict.v-no-data { border-left-color:rgba(170,180,210,.2); }
            .spxqqq-verdict-row { display:flex; align-items:baseline;
                justify-content:space-between; gap:10px; }
            .spxqqq-verdict-tag { font-weight:700; font-size:13px; letter-spacing:.6px;
                padding:3px 9px; border-radius:3px; }
            .spxqqq-verdict-tag.aligned-bull { background:rgba(102,204,153,.18); color:#85e0a3; }
            .spxqqq-verdict-tag.aligned-bear { background:rgba(204,102,119,.18); color:#e69aa5; }
            .spxqqq-verdict-tag.divergent-regime { background:rgba(230,168,92,.20); color:#ffc285; }
            .spxqqq-verdict-tag.divergent-magnitude { background:rgba(168,158,255,.18); color:#bcb3ff; }
            .spxqqq-verdict-tag.neutral { background:rgba(170,180,210,.10); color:rgba(180,190,210,.6); }
            .spxqqq-verdict-tag.no-data { background:rgba(170,180,210,.06); color:rgba(140,150,180,.5); }
            .spxqqq-verdict-strength { font-size:10px; color:rgba(160,170,200,.7);
                font-family:'JetBrains Mono',monospace; }
            .spxqqq-verdict-rationale { font-size:9px; color:rgba(170,180,210,.7);
                line-height:1.35; }

            /* ── Comparison table ─────────────────────────────────── */
            .spxqqq-table { background:rgba(255,255,255,.02); border-radius:3px;
                padding:5px 7px; display:grid;
                grid-template-columns: 1fr auto auto;
                gap:3px 14px; font-size:9px; }
            .spxqqq-table .hdr { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:2px;
                margin-bottom:2px; }
            .spxqqq-table .lbl { color:rgba(160,170,200,.7); }
            .spxqqq-table .val { font-family:'JetBrains Mono',monospace;
                color:rgba(220,230,250,.95); font-weight:700; text-align:right;
                font-variant-numeric: tabular-nums; }
            .spxqqq-table .val.up { color:#85b6e6; }
            .spxqqq-table .val.dn { color:#e69580; }
            .spxqqq-table .val.long-gamma { color:#85e0a3; }
            .spxqqq-table .val.short-gamma { color:#ffb88a; }
            .spxqqq-table .val.dim { color:rgba(170,180,210,.4); }

            /* ── Trajectory canvas ───────────────────────────────── */
            .spxqqq-traj-wrap { flex:1 1 auto; min-height:80px;
                background:rgba(255,255,255,.015); border-radius:3px;
                padding:5px 7px; display:flex; flex-direction:column; gap:3px;
                overflow:hidden; }
            .spxqqq-traj-head { display:flex; justify-content:space-between;
                font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px; }
            .spxqqq-traj-canvas { flex:1 1 auto; width:100%; min-height:60px;
                display:block; }
            .spxqqq-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="spxqqq-wrap">
            <div class="spxqqq-header">
                <div class="spxqqq-title">⚖️ SPX vs QQQ DIVERGENCE</div>
                <div></div>
                <div class="spxqqq-stat">
                    <span class="lbl">spx</span>
                    <span class="val" data-fld="hdr_spx">—</span>
                    <span class="sub" data-fld="hdr_spx_sub">— vs flip</span>
                </div>
                <div class="spxqqq-stat">
                    <span class="lbl">qqq</span>
                    <span class="val" data-fld="hdr_qqq">—</span>
                    <span class="sub" data-fld="hdr_qqq_sub">— vs flip</span>
                </div>
                <div class="spxqqq-stat">
                    <span class="lbl">strength</span>
                    <span class="val" data-fld="hdr_strength">—</span>
                </div>
            </div>

            <div class="spxqqq-verdict" data-fld="verdict_host">
                <div class="spxqqq-empty">Awaiting first compute…</div>
            </div>

            <div class="spxqqq-table" data-fld="cmp_host"></div>

            <div class="spxqqq-traj-wrap">
                <div class="spxqqq-traj-head">
                    <span>Flip-distance % spread (QQQ − SPX) over last 60 min</span>
                    <span data-fld="traj_summary">—</span>
                </div>
                <canvas class="spxqqq-traj-canvas" data-fld="traj_canvas"></canvas>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────

    function _fmtPct(v) {
        if (!Number.isFinite(v)) return '—';
        return (v >= 0 ? '+' : '') + v.toFixed(3) + '%';
    }

    function _fmtUsd(v) {
        if (!Number.isFinite(v)) return '—';
        return '$' + v.toFixed(2);
    }

    function _fmtSize(n) {
        if (!Number.isFinite(n)) return '—';
        const a = Math.abs(n);
        const sign = n < 0 ? '−' : (n > 0 ? '+' : '');
        if (a >= 1e9) return sign + (a/1e9).toFixed(2) + 'B';
        if (a >= 1e6) return sign + (a/1e6).toFixed(2) + 'M';
        if (a >= 1e3) return sign + (a/1e3).toFixed(1) + 'K';
        return sign + Math.round(a).toString();
    }

    function _verdictDisplay(verdict) {
        const map = {
            'ALIGNED_BULL':         { tag:'aligned-bull', wrap:'v-aligned-bull', label:'⬆ ALIGNED LONG-Γ' },
            'ALIGNED_BEAR':         { tag:'aligned-bear', wrap:'v-aligned-bear', label:'⬇ ALIGNED SHORT-Γ' },
            'DIVERGENT_REGIME':     { tag:'divergent-regime', wrap:'v-divergent-regime', label:'⚡ DIVERGENT REGIMES' },
            'DIVERGENT_MAGNITUDE':  { tag:'divergent-magnitude', wrap:'v-divergent-magnitude', label:'◐ DIVERGENT MAGNITUDE' },
            'NEUTRAL':              { tag:'neutral', wrap:'v-neutral', label:'· NEUTRAL' },
            'NO_DATA':              { tag:'no-data', wrap:'v-no-data', label:'… NO DATA' },
        };
        return map[verdict] || map['NO_DATA'];
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
        const spx = _state.spx || {};
        const qqq = _state.qqq || {};

        set('hdr_spx', spx.spot ? _fmtUsd(spx.spot) : '—',
            spx.regime === 'LONG_GAMMA' ? 'up' : (spx.regime === 'SHORT_GAMMA' ? 'dn' : ''));
        setSub('hdr_spx_sub',
            spx.distance_to_flip_pct !== null && spx.distance_to_flip_pct !== undefined
              ? `${_fmtPct(spx.distance_to_flip_pct)} vs flip`
              : '— vs flip');

        set('hdr_qqq', qqq.spot ? _fmtUsd(qqq.spot) : '—',
            qqq.regime === 'LONG_GAMMA' ? 'up' : (qqq.regime === 'SHORT_GAMMA' ? 'dn' : ''));
        setSub('hdr_qqq_sub',
            qqq.distance_to_flip_pct !== null && qqq.distance_to_flip_pct !== undefined
              ? `${_fmtPct(qqq.distance_to_flip_pct)} vs flip`
              : '— vs flip');

        const div = _state.divergence || {};
        const strength = Number.isFinite(div.strength) ? (div.strength * 100).toFixed(0) + '%' : '—';
        const sCls = div.strength > 0.66 ? 'warn' : (div.strength > 0.33 ? '' : 'ok');
        set('hdr_strength', strength, sCls);
    }

    function _renderVerdict() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="verdict_host"]');
        if (!host) return;
        const div = _state.divergence || {};
        if (!div.verdict || div.verdict === 'NO_DATA') {
            host.className = 'spxqqq-verdict v-no-data';
            host.innerHTML = `<div class="spxqqq-empty">${div.rationale || 'awaiting data'}</div>`;
            return;
        }
        const v = _verdictDisplay(div.verdict);
        host.className = 'spxqqq-verdict ' + v.wrap;
        const strengthPct = Number.isFinite(div.strength) ? (div.strength * 100).toFixed(0) : '—';
        const ratioTxt = Number.isFinite(div.magnitude_ratio)
            ? `mag ratio ${div.magnitude_ratio.toFixed(2)}×`
            : '';
        const distTxt = Number.isFinite(div.flip_distance_diff_pct)
            ? `Δflip ${_fmtPct(div.flip_distance_diff_pct)}`
            : '';
        const subParts = [strengthPct !== '—' ? `strength ${strengthPct}%` : '',
                          ratioTxt, distTxt].filter(Boolean).join(' · ');
        host.innerHTML = `
            <div class="spxqqq-verdict-row">
                <span class="spxqqq-verdict-tag ${v.tag}">${v.label}</span>
                <span class="spxqqq-verdict-strength">${subParts}</span>
            </div>
            <div class="spxqqq-verdict-rationale">${div.rationale || ''}</div>
        `;
    }

    function _renderComparison() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="cmp_host"]');
        if (!host) return;
        const spx = _state.spx || {};
        const qqq = _state.qqq || {};

        const regimeCls = (r) => r === 'LONG_GAMMA' ? 'long-gamma'
                                : r === 'SHORT_GAMMA' ? 'short-gamma' : 'dim';
        const distCls = (v) => Number.isFinite(v) ? (v >= 0 ? 'up' : 'dn') : 'dim';
        const numCls = (v) => Number.isFinite(v) && v !== 0
                              ? (v > 0 ? 'up' : 'dn') : 'dim';

        // Build rows
        const row = (lbl, spxVal, spxCls, qqqVal, qqqCls) => `
            <div class="lbl">${lbl}</div>
            <div class="val ${spxCls || 'dim'}">${spxVal}</div>
            <div class="val ${qqqCls || 'dim'}">${qqqVal}</div>
        `;

        host.innerHTML = `
            <div class="hdr"></div>
            <div class="hdr" style="text-align:right; color:rgba(180,200,240,.7);">SPX</div>
            <div class="hdr" style="text-align:right; color:rgba(220,200,160,.7);">QQQ</div>

            ${row('regime',
                  spx.regime || '—', regimeCls(spx.regime),
                  qqq.regime || '—', regimeCls(qqq.regime))}

            ${row('γ flip',
                  Number.isFinite(spx.gamma_flip) ? _fmtUsd(spx.gamma_flip) : '—', '',
                  Number.isFinite(qqq.gamma_flip) ? _fmtUsd(qqq.gamma_flip) : '—', '')}

            ${row('dist to flip',
                  _fmtPct(spx.distance_to_flip_pct), distCls(spx.distance_to_flip_pct),
                  _fmtPct(qqq.distance_to_flip_pct), distCls(qqq.distance_to_flip_pct))}

            ${row('hp_γ_shares /1%',
                  _fmtSize(spx.hp_gamma_shares_1pct), numCls(spx.hp_gamma_shares_1pct),
                  _fmtSize(qqq.hp_gamma_shares_1pct), numCls(qqq.hp_gamma_shares_1pct))}

            ${row('net dealer Γ$',
                  _fmtSize(spx.net_dealer_gamma_dollars), numCls(spx.net_dealer_gamma_dollars),
                  _fmtSize(qqq.net_dealer_gamma_dollars), numCls(qqq.net_dealer_gamma_dollars))}

            ${row('call wall',
                  Number.isFinite(spx.call_wall) ? _fmtUsd(spx.call_wall) : '—', '',
                  Number.isFinite(qqq.call_wall) ? _fmtUsd(qqq.call_wall) : '—', '')}

            ${row('put wall',
                  Number.isFinite(spx.put_wall) ? _fmtUsd(spx.put_wall) : '—', '',
                  Number.isFinite(qqq.put_wall) ? _fmtUsd(qqq.put_wall) : '—', '')}

            ${row('PCR (OI)',
                  Number.isFinite(spx.pcr_oi) ? spx.pcr_oi.toFixed(2) : '—',
                  Number.isFinite(spx.pcr_oi) ? (spx.pcr_oi > 1 ? 'dn' : 'up') : 'dim',
                  Number.isFinite(qqq.pcr_oi) ? qqq.pcr_oi.toFixed(2) : '—',
                  Number.isFinite(qqq.pcr_oi) ? (qqq.pcr_oi > 1 ? 'dn' : 'up') : 'dim')}

            ${row('strikes tracked',
                  spx.strike_count || '—', 'dim',
                  qqq.strike_count || '—', 'dim')}
        `;
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
            Number.isFinite(s.flip_distance_diff_pct) && Number.isFinite(s.ts));
        const sumEl = _slot.querySelector('[data-fld="traj_summary"]');
        if (history.length < 2) {
            if (sumEl) sumEl.textContent = `${history.length} samples`;
            ctx.font = '9px JetBrains Mono';
            ctx.fillStyle = 'rgba(140,150,180,.4)';
            ctx.textAlign = 'center';
            ctx.fillText('awaiting samples', W / 2, H / 2);
            return;
        }

        // Determine y range — use symmetric scale around 0
        let maxAbs = 0;
        for (const s of history) {
            maxAbs = Math.max(maxAbs, Math.abs(s.flip_distance_diff_pct));
        }
        if (maxAbs < 0.05) maxAbs = 0.05;          // ensure non-trivial scale
        const padTop = 6, padBot = 6;
        const yPx = (v) => {
            const norm = (v / maxAbs);             // -1..1
            return (H / 2) - norm * ((H - padTop - padBot) / 2);
        };

        // x-axis range
        const t0 = history[0].ts;
        const t1 = history[history.length - 1].ts;
        const dt = Math.max(1, t1 - t0);
        const xPx = (ts) => ((ts - t0) / dt) * (W - 8) + 4;

        // Zero baseline
        ctx.strokeStyle = 'rgba(255,255,255,.06)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, H / 2);
        ctx.lineTo(W, H / 2);
        ctx.stroke();

        // Trajectory line
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#e6a85c';
        ctx.beginPath();
        for (let i = 0; i < history.length; i++) {
            const x = xPx(history[i].ts);
            const y = yPx(history[i].flip_distance_diff_pct);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Verdict highlight markers (color dots at sample points)
        const colorByVerdict = {
            'ALIGNED_BULL':        '#66cc99',
            'ALIGNED_BEAR':        '#cc6677',
            'DIVERGENT_REGIME':    '#e6a85c',
            'DIVERGENT_MAGNITUDE': '#a89eff',
            'NEUTRAL':             'rgba(170,180,210,.5)',
            'NO_DATA':             'rgba(140,150,180,.3)',
        };
        // Show every Nth dot to avoid crowding
        const stride = Math.max(1, Math.floor(history.length / 30));
        for (let i = 0; i < history.length; i += stride) {
            const s = history[i];
            const c = colorByVerdict[s.verdict] || '#888';
            ctx.fillStyle = c;
            ctx.beginPath();
            ctx.arc(xPx(s.ts), yPx(s.flip_distance_diff_pct), 1.6, 0, Math.PI * 2);
            ctx.fill();
        }

        // Axis labels (top/bottom y range)
        ctx.font = '8px JetBrains Mono';
        ctx.fillStyle = 'rgba(140,150,180,.6)';
        ctx.textAlign = 'left';
        ctx.fillText(`+${maxAbs.toFixed(2)}%`, 2, padTop + 4);
        ctx.fillText(`−${maxAbs.toFixed(2)}%`, 2, H - 2);
        ctx.textAlign = 'right';
        ctx.fillText('0%', W - 4, H / 2 - 2);

        if (sumEl) {
            const lastDiff = history[history.length - 1].flip_distance_diff_pct;
            sumEl.textContent = `${history.length} samples · current ${_fmtPct(lastDiff)}`;
        }
    }

    function _renderAll() {
        _renderHeader();
        _renderVerdict();
        _renderComparison();
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
            const r = await _authFetch('/api/intel/spx_qqq_divergence');
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
            window.AltarisEvents.on('socket:intel:spx_qqq_divergence', _pushHandler);
        }

        // Re-render trajectory canvas on resize
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
            window.AltarisEvents.off('socket:intel:spx_qqq_divergence', _pushHandler);
            _pushHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
