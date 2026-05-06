/**
 * GammaSkylinePane — per-strike dealer Γ$ "city skyline" Canvas2D viz.
 *
 * Backed by /api/intel/gamma_skyline + Socket.IO 'intel:gamma_skyline'
 * (push every 5s during RTH).
 *
 * Renders:
 *   1. HEADER          — spot, Σ hp_γ_shares /1%, dn_gamma_max_abs, regime hint
 *   2. STATS STRIP     — flip · max_pain · call/put walls · gamma walls
 *   3. SKYLINE CANVAS  — vertical bars per strike from zero baseline
 *      sign convention: positive dn_gamma → green/up (dealer NET LONG γ → SELL on rise)
 *                       negative dn_gamma → red/down (dealer NET SHORT γ → BUY on rise)
 *      overlays: spot crosshair, gamma_flip line, call_wall / put_wall ticks
 *   4. STRIKE TOOLTIP  — hover-strike inspector (oi_call/put, hp_γ shares)
 *
 * Design discipline: every bar height is normalized by `dn_gamma_max_abs`
 * (returned by backend) to keep view consistent across regime regimes.
 *
 * Anti-theater: every value traces to backend field documented in
 * docs/MEASURED_VALUES.md → "Phase 7: Gamma Skyline".
 */
window.GammaSkylinePane = (() => {
    'use strict';

    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _pollTimer = null;
    let _pushHandler = null;
    let _resizeObs = null;
    let _hoverK = null;
    let _moveHandler = null;
    let _leaveHandler = null;

    let _state = null;

    const REST_POLL_MS = 30000;     // CONFIGURED — drift-correction polling

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('skyline-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'skyline-styles';
        _styleEl.textContent = `
            .sky-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            .sky-header { display:grid;
                grid-template-columns: auto 1fr auto auto auto;
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:5px; }
            .sky-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; }
            .sky-stat { display:flex; flex-direction:column; gap:1px; }
            .sky-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .sky-stat .val { color:rgba(220,230,250,.95); font-weight:700;
                font-size:11px; font-variant-numeric: tabular-nums; }
            .sky-stat .val.up   { color:#85e0a3; }
            .sky-stat .val.dn   { color:#e69aa5; }
            .sky-stat .val.warn { color:#ffb366; }
            .sky-stat .sub { font-size:8px; color:rgba(140,150,180,.5); }

            /* ── Stats strip ─────────────────────────────────── */
            .sky-strip { background:rgba(255,255,255,.02); border-radius:3px;
                padding:5px 8px; display:grid;
                grid-template-columns: repeat(5, 1fr);
                gap:6px; font-size:9px; }
            .sky-strip .cell { display:flex; flex-direction:column; gap:1px; }
            .sky-strip .cell .k { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.4px; }
            .sky-strip .cell .v { font-size:11px; font-weight:700;
                font-family:'JetBrains Mono',monospace; color:rgba(220,230,250,.95);
                font-variant-numeric: tabular-nums; }
            .sky-strip .cell .v.flip       { color:#ffd180; }
            .sky-strip .cell .v.call       { color:#85e0a3; }
            .sky-strip .cell .v.put        { color:#e69aa5; }
            .sky-strip .cell .v.gamma-call { color:#a8e6c8; }
            .sky-strip .cell .v.gamma-put  { color:#e6a3b3; }
            .sky-strip .cell .v.dim        { color:rgba(170,180,210,.4); }

            /* ── Skyline canvas ─────────────────────────────── */
            .sky-canvas-wrap { flex:1 1 auto; min-height:140px;
                background:rgba(255,255,255,.015); border-radius:3px;
                padding:4px; position:relative; overflow:hidden; }
            .sky-canvas { width:100%; height:100%; display:block;
                cursor:crosshair; }
            .sky-tooltip {
                position:absolute; pointer-events:none;
                background:rgba(15,18,28,.95);
                border:1px solid rgba(255,255,255,.08);
                border-radius:3px;
                padding:5px 7px;
                font-size:9px; font-family:'JetBrains Mono',monospace;
                color:rgba(220,230,250,.95);
                font-variant-numeric: tabular-nums;
                line-height:1.4;
                white-space:nowrap;
                z-index:10;
                opacity:0; transition:opacity .15s ease;
            }
            .sky-tooltip.visible { opacity:1; }
            .sky-tooltip .tk { color:rgba(255,209,128,.95); font-weight:700; }
            .sky-tooltip .row { display:flex; gap:8px; justify-content:space-between; }
            .sky-tooltip .row .lbl { color:rgba(160,170,200,.7); }
            .sky-tooltip .pos { color:#85e0a3; }
            .sky-tooltip .neg { color:#e69aa5; }

            .sky-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="sky-wrap">
            <div class="sky-header">
                <div class="sky-title">🏙 GAMMA SKYLINE</div>
                <div></div>
                <div class="sky-stat">
                    <span class="lbl">spot</span>
                    <span class="val" data-fld="hdr_spot">—</span>
                </div>
                <div class="sky-stat">
                    <span class="lbl">Σ hp_γ /1%</span>
                    <span class="val" data-fld="hdr_hpg">—</span>
                </div>
                <div class="sky-stat">
                    <span class="lbl">strikes</span>
                    <span class="val" data-fld="hdr_n">—</span>
                    <span class="sub" data-fld="hdr_band">—</span>
                </div>
            </div>

            <div class="sky-strip">
                <div class="cell"><span class="k">flip</span>
                    <span class="v flip" data-fld="strip_flip">—</span></div>
                <div class="cell"><span class="k">call wall</span>
                    <span class="v call" data-fld="strip_call">—</span></div>
                <div class="cell"><span class="k">put wall</span>
                    <span class="v put" data-fld="strip_put">—</span></div>
                <div class="cell"><span class="k">γ-call wall</span>
                    <span class="v gamma-call" data-fld="strip_gcall">—</span></div>
                <div class="cell"><span class="k">γ-put wall</span>
                    <span class="v gamma-put" data-fld="strip_gput">—</span></div>
            </div>

            <div class="sky-canvas-wrap">
                <canvas class="sky-canvas" data-fld="canvas"></canvas>
                <div class="sky-tooltip" data-fld="tooltip"></div>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────
    function _fmtNum(n) {
        if (!Number.isFinite(n)) return '—';
        const a = Math.abs(n);
        const sign = n < 0 ? '−' : (n > 0 ? '+' : '');
        if (a >= 1e9) return sign + (a/1e9).toFixed(2) + 'B';
        if (a >= 1e6) return sign + (a/1e6).toFixed(2) + 'M';
        if (a >= 1e3) return sign + (a/1e3).toFixed(1) + 'K';
        return sign + Math.round(a).toString();
    }
    function _fmtUsd(v) {
        if (!Number.isFinite(v)) return '—';
        return '$' + v.toFixed(2);
    }
    function _fmtPct(v) {
        if (!Number.isFinite(v)) return '—';
        return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
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
        const tot = (_state.totals || {});
        const hpg = tot.hp_gamma_shares_1pct;
        set('hdr_hpg', _fmtNum(hpg) + ' sh',
            !Number.isFinite(hpg) ? '' : (hpg > 0 ? 'up' : (hpg < 0 ? 'dn' : '')));
        set('hdr_n', `${(_state.strikes || []).length}`);
        const lo = _state.band_low, hi = _state.band_high;
        setSub('hdr_band',
            (Number.isFinite(lo) && Number.isFinite(hi))
                ? `${_fmtUsd(lo)}–${_fmtUsd(hi)}` : '—');
    }

    function _renderStrip() {
        if (!_slot || !_state) return;
        const w = _state.walls || {};
        const set = (sel, val, cls) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (!el) return;
            const num = Number.isFinite(val) ? val : null;
            el.textContent = num !== null ? _fmtUsd(num) : '—';
            if (cls !== undefined) el.className = cls
                ? ('v ' + (num !== null ? cls : 'dim'))
                : 'v';
        };
        set('strip_flip',  w.gamma_flip,       'flip');
        set('strip_call',  w.call_wall,        'call');
        set('strip_put',   w.put_wall,         'put');
        set('strip_gcall', w.gamma_call_wall,  'gamma-call');
        set('strip_gput',  w.gamma_put_wall,   'gamma-put');
    }

    function _renderCanvas() {
        if (!_slot || !_state) return;
        const cv = _slot.querySelector('[data-fld="canvas"]');
        if (!cv) return;
        const dpr = window.devicePixelRatio || 1;
        const W = Math.max(40, cv.clientWidth | 0);
        const H = Math.max(40, cv.clientHeight | 0);
        if (cv.width !== W * dpr) cv.width = W * dpr;
        if (cv.height !== H * dpr) cv.height = H * dpr;
        const ctx = cv.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, W, H);

        const strikes = _state.strikes || [];
        if (!strikes.length) {
            ctx.font = '10px JetBrains Mono';
            ctx.fillStyle = 'rgba(140,150,180,.4)';
            ctx.textAlign = 'center';
            ctx.fillText(_state.reason || 'awaiting data', W / 2, H / 2);
            return;
        }

        const lo = _state.band_low;
        const hi = _state.band_high;
        if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return;

        const padTop = 14, padBot = 16;
        const padL = 4, padR = 4;
        const usableW = W - padL - padR;
        const usableH = H - padTop - padBot;
        const midY = padTop + usableH / 2;

        const xPx = (K) => padL + ((K - lo) / (hi - lo)) * usableW;

        // Bar geometry — width based on strike density
        const barWidth = Math.max(2, Math.min(14, usableW / strikes.length - 1));

        // ── Zero baseline ──
        ctx.strokeStyle = 'rgba(255,255,255,.10)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, midY);
        ctx.lineTo(W, midY);
        ctx.stroke();

        // ── Bars ──
        const halfH = usableH / 2;
        for (const s of strikes) {
            const dn = s.dn_gamma || 0;
            const norm = s.dn_gamma_norm || 0;        // [-1..1]
            const x = xPx(s.K);
            const barH = Math.abs(norm) * halfH;
            // Sign convention:
            //   dn_gamma > 0 → dealers LONG γ → bar UP, green
            //   dn_gamma < 0 → dealers SHORT γ → bar DOWN, red
            const isUp = dn > 0;
            const grad = ctx.createLinearGradient(0, midY, 0,
                isUp ? midY - barH : midY + barH);
            if (isUp) {
                grad.addColorStop(0,    'rgba(133,224,163,.40)');
                grad.addColorStop(0.5,  'rgba(133,224,163,.85)');
                grad.addColorStop(1,    'rgba(133,224,163,1)');
            } else {
                grad.addColorStop(0,    'rgba(230,154,165,.40)');
                grad.addColorStop(0.5,  'rgba(230,154,165,.85)');
                grad.addColorStop(1,    'rgba(230,154,165,1)');
            }
            ctx.fillStyle = grad;
            const rectX = x - barWidth / 2;
            if (isUp) {
                ctx.fillRect(rectX, midY - barH, barWidth, barH);
            } else {
                ctx.fillRect(rectX, midY,        barWidth, barH);
            }
            // Hover highlight
            if (_hoverK !== null && Math.abs(s.K - _hoverK) < 1e-6) {
                ctx.strokeStyle = 'rgba(255,255,255,.85)';
                ctx.lineWidth = 1;
                ctx.strokeRect(
                    rectX - 0.5,
                    isUp ? midY - barH - 0.5 : midY - 0.5,
                    barWidth + 1,
                    barH + 1
                );
            }
        }

        // ── Wall overlay lines ──
        const drawLine = (val, color, label) => {
            if (!Number.isFinite(val) || val < lo || val > hi) return;
            const x = xPx(val);
            ctx.strokeStyle = color;
            ctx.setLineDash([3, 2]);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x, padTop);
            ctx.lineTo(x, H - padBot);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.font = '8px JetBrains Mono';
            ctx.fillStyle = color;
            ctx.textAlign = 'center';
            ctx.fillText(label, x, padTop - 2);
        };
        const w = _state.walls || {};
        drawLine(w.put_wall,        'rgba(230,154,165,.7)', 'PUT');
        drawLine(w.call_wall,       'rgba(133,224,163,.7)', 'CALL');
        drawLine(w.gamma_put_wall,  'rgba(230,163,179,.55)','γP');
        drawLine(w.gamma_call_wall, 'rgba(168,230,200,.55)','γC');
        drawLine(w.gamma_flip,      'rgba(255,209,128,.85)','FLIP');

        // ── Spot crosshair (vertical) ──
        if (Number.isFinite(_state.spot) && _state.spot >= lo && _state.spot <= hi) {
            const x = xPx(_state.spot);
            ctx.strokeStyle = 'rgba(255,255,255,.85)';
            ctx.setLineDash([1, 2]);
            ctx.lineWidth = 1.2;
            ctx.beginPath();
            ctx.moveTo(x, padTop);
            ctx.lineTo(x, H - padBot);
            ctx.stroke();
            ctx.setLineDash([]);
            // Spot label
            ctx.font = 'bold 9px JetBrains Mono';
            ctx.fillStyle = 'rgba(255,255,255,.95)';
            ctx.textAlign = 'center';
            ctx.fillText(_fmtUsd(_state.spot), x, H - 4);
        }

        // ── Y-axis legend (signed) ──
        ctx.font = '8px JetBrains Mono';
        ctx.textAlign = 'left';
        ctx.fillStyle = 'rgba(133,224,163,.7)';
        ctx.fillText('+ dealer LONG γ', 4, padTop + 8);
        ctx.fillStyle = 'rgba(230,154,165,.7)';
        ctx.fillText('− dealer SHORT γ', 4, H - padBot - 2);

        // ── X-axis ticks (spot ± 5/10/20) ──
        ctx.font = '7px JetBrains Mono';
        ctx.fillStyle = 'rgba(140,150,180,.55)';
        ctx.textAlign = 'center';
        const xticks = [-20, -10, -5, 0, 5, 10, 20];
        for (const dx of xticks) {
            const K = _state.spot + dx;
            if (K < lo || K > hi) continue;
            ctx.fillText((dx >= 0 ? '+' : '') + dx, xPx(K), padTop + usableH + 8);
        }
    }

    function _renderTooltip(mouseX, mouseY) {
        if (!_slot || !_state) return;
        const tip = _slot.querySelector('[data-fld="tooltip"]');
        if (!tip) return;
        if (_hoverK === null) {
            tip.classList.remove('visible');
            return;
        }
        const strikes = _state.strikes || [];
        const s = strikes.find(x => Math.abs(x.K - _hoverK) < 1e-6);
        if (!s) {
            tip.classList.remove('visible');
            return;
        }
        const dnCls = s.dn_gamma > 0 ? 'pos' : 'neg';
        const hpCls = s.hp_gamma_shares_1pct > 0 ? 'pos' : 'neg';
        const dnTxt = (s.dn_gamma >= 0 ? '+' : '−') + Math.abs(s.dn_gamma).toLocaleString();
        const hpTxt = (s.hp_gamma_shares_1pct >= 0 ? '+' : '−') + _fmtNum(Math.abs(s.hp_gamma_shares_1pct));
        tip.innerHTML = `
            <div class="tk">$${s.K.toFixed(2)}${s.is_atm ? '  (ATM)' : ''}</div>
            <div class="row"><span class="lbl">dist</span>
                <span class="${s.dist_pct >= 0 ? 'pos' : 'neg'}">${_fmtPct(s.dist_pct)}</span></div>
            <div class="row"><span class="lbl">dn_γ$</span>
                <span class="${dnCls}">${dnTxt}</span></div>
            <div class="row"><span class="lbl">hp_γ /1%</span>
                <span class="${hpCls}">${hpTxt} sh</span></div>
            <div class="row"><span class="lbl">OI call</span>
                <span>${(s.oi_call || 0).toLocaleString()}</span></div>
            <div class="row"><span class="lbl">OI put</span>
                <span>${(s.oi_put || 0).toLocaleString()}</span></div>
        `;
        tip.classList.add('visible');
        // Position: nudge so it doesn't go off-screen
        const wrap = tip.parentElement;
        const wRect = wrap.getBoundingClientRect();
        const tipW = tip.offsetWidth || 140;
        const tipH = tip.offsetHeight || 90;
        let x = mouseX + 10;
        let y = mouseY + 10;
        if (x + tipW > wRect.width - 4) x = mouseX - tipW - 10;
        if (y + tipH > wRect.height - 4) y = mouseY - tipH - 10;
        if (x < 4) x = 4;
        if (y < 4) y = 4;
        tip.style.left = x + 'px';
        tip.style.top  = y + 'px';
    }

    function _onMouseMove(ev) {
        if (!_slot || !_state) return;
        const cv = _slot.querySelector('[data-fld="canvas"]');
        if (!cv) return;
        const r = cv.getBoundingClientRect();
        const mx = ev.clientX - r.left;
        const my = ev.clientY - r.top;
        const lo = _state.band_low, hi = _state.band_high;
        if (!Number.isFinite(lo) || !Number.isFinite(hi)) return;
        const padL = 4, padR = 4;
        const usableW = r.width - padL - padR;
        const xRel = (mx - padL) / usableW;
        if (xRel < 0 || xRel > 1) {
            if (_hoverK !== null) {
                _hoverK = null;
                _renderCanvas();
                _renderTooltip(mx, my);
            }
            return;
        }
        const K = lo + xRel * (hi - lo);
        // Snap to nearest strike
        const strikes = _state.strikes || [];
        if (!strikes.length) return;
        let best = strikes[0];
        let bestD = Math.abs(strikes[0].K - K);
        for (const s of strikes) {
            const d = Math.abs(s.K - K);
            if (d < bestD) { bestD = d; best = s; }
        }
        const newHover = best.K;
        if (newHover !== _hoverK) {
            _hoverK = newHover;
            _renderCanvas();
        }
        _renderTooltip(mx, my);
    }

    function _onMouseLeave() {
        if (_hoverK !== null) {
            _hoverK = null;
            _renderCanvas();
            const tip = _slot && _slot.querySelector('[data-fld="tooltip"]');
            if (tip) tip.classList.remove('visible');
        }
    }

    function _renderAll() {
        _renderHeader();
        _renderStrip();
        _renderCanvas();
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
            const r = await _authFetch('/api/intel/gamma_skyline');
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
        _hoverK = null;
        _injectStyles();
        _buildShell();

        if (window.AltarisEvents) {
            _pushHandler = (d) => _onPushUpdate(d);
            window.AltarisEvents.on('socket:intel:gamma_skyline', _pushHandler);
        }

        const cv = _slot.querySelector('[data-fld="canvas"]');
        if (cv) {
            _moveHandler = _onMouseMove;
            _leaveHandler = _onMouseLeave;
            cv.addEventListener('mousemove', _moveHandler);
            cv.addEventListener('mouseleave', _leaveHandler);
        }

        try {
            if (typeof ResizeObserver !== 'undefined') {
                _resizeObs = new ResizeObserver(() => _renderCanvas());
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
            window.AltarisEvents.off('socket:intel:gamma_skyline', _pushHandler);
            _pushHandler = null;
        }
        const cv = _slot && _slot.querySelector('[data-fld="canvas"]');
        if (cv) {
            if (_moveHandler) cv.removeEventListener('mousemove', _moveHandler);
            if (_leaveHandler) cv.removeEventListener('mouseleave', _leaveHandler);
        }
        _moveHandler = null;
        _leaveHandler = null;
        _hoverK = null;
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
