/**
 * PinPane — End-of-day pin convergence prediction.
 *
 * Backed by /api/intel/pin/<ticker> (REST snapshot + history) and
 * Socket.IO 'intel:pin_update' (push every 15s last hour, 60s otherwise).
 *
 * Pin = strike where 0DTE price is mechanically pulled toward at expiry due
 * to dealer Γ exposure. Pin pull strengthens dramatically in the last 30 min
 * as gamma exposure peaks. This pane:
 *
 *   1. HEADER          — spot, time-remaining, pin estimate, confidence
 *   2. PIN BARS         — per-strike pin_probability bars (within ATM ±$15)
 *                          with score component breakdown on hover
 *   3. WAREHOUSE TABLE  — per-strike dealer position (dn_gamma + OI)
 *   4. EXPECTED CLOSE  — weighted-mean pin + 95% CI band
 *   5. TRAJECTORY      — sparkline of pin estimate over last 60-120 min
 *
 * Anti-theater: every rendered number traces to a backend field documented in
 * docs/MEASURED_VALUES.md → "Phase 2: Pin Convergence". Score weights are
 * CONFIGURED with TODO to upgrade to MEASURED via outcome ledger after 2 weeks.
 */
window.PinPane = (() => {
    'use strict';

    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _pollTimer = null;
    let _ageTickTimer = null;
    let _pushHandler = null;

    let _state = null;             // last full state object from REST or push

    // CONFIGURED — UI cadence (matches socket push) + display caps
    const REST_POLL_MS         = 30000;   // drift-correction polling
    const TICKER               = 'QQQ';   // single-underlying for v1

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('pin-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'pin-styles';
        _styleEl.textContent = `
            .pin-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            /* ── Header ─────────────────────────────────────── */
            .pin-header { display:grid; grid-template-columns: auto auto 1fr auto auto;
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                color:rgba(160,170,200,.6); border-bottom:1px solid rgba(255,255,255,.04);
                padding-bottom:5px; }
            .pin-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; white-space:nowrap; }
            .pin-stat { display:flex; flex-direction:column; gap:1px; min-width:60px; }
            .pin-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .pin-stat .val { color:rgba(220,230,250,.95); font-weight:700; font-size:11px; }
            .pin-stat .val.warn { color:#ffb366; }
            .pin-stat .val.hot { color:#66cc99; }
            .pin-stat .val.up { color:#85b6e6; }
            .pin-stat .val.dn { color:#e69580; }
            .pin-tr-bar { height:3px; background:rgba(255,255,255,.06); border-radius:2px;
                position:relative; overflow:hidden; min-width:120px; }
            .pin-tr-bar > .fill { position:absolute; top:0; left:0; height:100%;
                background:linear-gradient(90deg,#85b6e6,#ffb366,#e69580);
                transition:width 0.5s ease; }

            /* ── Expected close + CI band ───────────────────── */
            .pin-target { display:flex; flex-direction:column; gap:4px;
                background:rgba(255,255,255,.025); border-radius:4px; padding:8px;
                border-left:3px solid #85b6e6; }
            .pin-target.high-conf { border-left-color:#66cc99; }
            .pin-target.low-conf { border-left-color:#ffb366; }
            .pin-target-label { font-size:8px; color:rgba(140,150,180,.6);
                text-transform:uppercase; letter-spacing:.6px; }
            .pin-target-row { display:flex; align-items:baseline; justify-content:space-between;
                gap:10px; }
            .pin-target-est { font-size:18px; font-weight:700; color:rgba(220,230,250,.98);
                font-family:'JetBrains Mono',monospace; }
            .pin-target-spot { font-size:10px; color:rgba(160,170,200,.55); }
            .pin-target-ci { font-size:9px; color:rgba(160,170,200,.65);
                font-family:'JetBrains Mono',monospace; }
            .pin-target-bias { font-size:9px; font-weight:700; padding:2px 6px;
                border-radius:2px; letter-spacing:.5px; }
            .pin-target-bias.up { background:rgba(102,204,153,.18); color:#85e0a3; }
            .pin-target-bias.dn { background:rgba(204,102,119,.18); color:#e69aa5; }
            .pin-target-bias.flat { background:rgba(170,180,210,.1); color:rgba(170,180,210,.6); }

            /* ── Walls overlay row ──────────────────────────── */
            .pin-walls { display:flex; gap:6px; flex-wrap:wrap; padding:3px 0;
                font-size:8px; }
            .pin-wall-chip { padding:2px 6px; border-radius:2px;
                background:rgba(255,255,255,.04); color:rgba(180,190,220,.7);
                display:flex; gap:3px; align-items:baseline; }
            .pin-wall-chip .name { color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.4px; }
            .pin-wall-chip .v { color:rgba(220,230,250,.9); font-weight:600; }

            /* ── Pin probability bars ───────────────────────── */
            .pin-bars-section { flex:1; display:flex; flex-direction:column;
                min-height:0; gap:1px; overflow-y:auto; }
            .pin-bars-title { display:flex; justify-content:space-between;
                font-size:9px; font-weight:700; color:rgba(180,190,220,.6);
                text-transform:uppercase; letter-spacing:.6px;
                padding:4px 2px 2px 2px; position:sticky; top:0; background:#070a14;
                border-bottom:1px solid rgba(255,255,255,.04); }
            .pin-bar-row { display:grid; grid-template-columns: 50px 1fr 50px 38px;
                gap:6px; align-items:center; padding:2px 4px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.02); }
            .pin-bar-row.atm { background:rgba(133,182,230,.05); }
            .pin-bar-row.is-pin { background:rgba(255,179,102,.06); }
            .pin-bar-row .strike { font-family:'JetBrains Mono',monospace;
                color:rgba(220,230,250,.85); font-weight:600; }
            .pin-bar-row .bar-host { position:relative; height:14px;
                background:rgba(255,255,255,.025); border-radius:2px; overflow:hidden; }
            .pin-bar-row .bar { position:absolute; top:0; bottom:0; left:0;
                background:rgba(133,182,230,.55); border-radius:2px;
                transition:width 0.4s ease; }
            .pin-bar-row.is-pin .bar { background:rgba(255,179,102,.7); }
            .pin-bar-row .bar-text { position:absolute; right:4px; top:0; bottom:0;
                display:flex; align-items:center; font-size:8px;
                color:rgba(255,255,255,.85); font-weight:700; }
            .pin-bar-row .oi { font-size:8px; color:rgba(160,170,200,.55);
                text-align:right; font-family:'JetBrains Mono',monospace; }
            .pin-bar-row .gamma { font-size:8px; text-align:right;
                font-family:'JetBrains Mono',monospace; }
            .pin-bar-row .gamma.pos { color:#85b6e6; }
            .pin-bar-row .gamma.neg { color:#e69580; }
            .pin-bar-row .gamma.zero { color:rgba(170,180,210,.3); }

            /* ── Trajectory sparkline ───────────────────────── */
            .pin-traj { padding:4px 2px; border-top:1px solid rgba(255,255,255,.04); }
            .pin-traj-title { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.4px; margin-bottom:2px; }
            .pin-traj-canvas { width:100%; height:36px; display:block; }

            /* ── Empty / loading ────────────────────────────── */
            .pin-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="pin-wrap">
            <div class="pin-header" data-fld="header">
                <div class="pin-title">🎯 PIN CONVERGENCE</div>
                <div class="pin-stat">
                    <span class="lbl">spot</span>
                    <span class="val" data-fld="hdr_spot">—</span>
                </div>
                <div class="pin-stat">
                    <span class="lbl">time → close</span>
                    <span class="val" data-fld="hdr_tr">—</span>
                    <div class="pin-tr-bar"><div class="fill" data-fld="hdr_tr_bar" style="width:0%"></div></div>
                </div>
                <div class="pin-stat">
                    <span class="lbl">conf</span>
                    <span class="val" data-fld="hdr_conf">—</span>
                </div>
                <div class="pin-stat">
                    <span class="lbl">data age</span>
                    <span class="val" data-fld="hdr_age">—</span>
                </div>
            </div>

            <div class="pin-target" data-fld="target_host">
                <div class="pin-target-label">Expected close (DERIVED — weighted mean)</div>
                <div class="pin-target-row">
                    <span class="pin-target-est" data-fld="tgt_est">—</span>
                    <span class="pin-target-bias flat" data-fld="tgt_bias">—</span>
                </div>
                <div class="pin-target-row">
                    <span class="pin-target-spot" data-fld="tgt_spot">spot —</span>
                    <span class="pin-target-ci" data-fld="tgt_ci">95% CI: —</span>
                </div>
                <div class="pin-walls" data-fld="walls"></div>
            </div>

            <div class="pin-bars-section" data-fld="bars_host">
                <div class="pin-empty">Waiting for first compute…</div>
            </div>

            <div class="pin-traj">
                <div class="pin-traj-title" data-fld="traj_title">PIN TRAJECTORY (last <span data-fld="traj_n">0</span> samples)</div>
                <canvas class="pin-traj-canvas" data-fld="traj_canvas"></canvas>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────

    function _ageStr(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) return '—';
        if (seconds < 60) return Math.floor(seconds) + 's';
        if (seconds < 3600) return Math.floor(seconds/60) + 'm ' + (Math.floor(seconds)%60) + 's';
        return Math.floor(seconds/3600) + 'h ' + Math.floor((seconds%3600)/60) + 'm';
    }

    function _confidenceLabel(c) {
        if (!Number.isFinite(c)) return '—';
        const pct = (c * 100).toFixed(0);
        return `${pct}%`;
    }

    function _confClass(c) {
        if (!Number.isFinite(c)) return '';
        if (c >= 0.30) return 'hot';
        if (c >= 0.18) return 'up';
        return 'warn';
    }

    function _trProgressPct(tr_sec) {
        // Session length 6.5h = 23,400s — show fraction elapsed
        const total = 23400;
        const elapsed = total - Math.max(0, tr_sec);
        return Math.max(0, Math.min(100, (elapsed / total) * 100));
    }

    function _signedTxt(n, decimals) {
        if (!Number.isFinite(n)) return '—';
        const sign = n > 0 ? '+' : (n < 0 ? '−' : '');
        return sign + Math.abs(n).toFixed(decimals);
    }

    // ── Rendering ───────────────────────────────────────────────────

    function _renderHeader() {
        if (!_slot || !_state) return;
        const set = (sel, txt, cls) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (!el) return;
            el.textContent = txt;
            if (cls !== undefined) {
                el.className = cls ? ('val ' + cls) : 'val';
            }
        };
        set('hdr_spot', '$' + (Number.isFinite(_state.spot) && _state.spot > 0 ? _state.spot.toFixed(2) : '—'));
        set('hdr_tr', _ageStr(_state.time_remaining_sec));
        const trBar = _slot.querySelector('[data-fld="hdr_tr_bar"]');
        if (trBar) trBar.style.width = _trProgressPct(_state.time_remaining_sec) + '%';
        set('hdr_conf', _confidenceLabel(_state.pin_confidence), _confClass(_state.pin_confidence));
        // Data age (server_time - data_ts)
        const age = (_state.data_ts && _state.server_time)
            ? Math.max(0, _state.server_time - _state.data_ts) : 0;
        set('hdr_age', _ageStr(age) + ' ago', age > 30 ? 'warn' : '');
    }

    function _renderTarget() {
        if (!_slot || !_state) return;
        const tgtHost = _slot.querySelector('[data-fld="target_host"]');
        const est = _state.pin_estimate;
        const spot = _state.spot;
        const conf = _state.pin_confidence;

        const estEl = _slot.querySelector('[data-fld="tgt_est"]');
        const spotEl = _slot.querySelector('[data-fld="tgt_spot"]');
        const ciEl = _slot.querySelector('[data-fld="tgt_ci"]');
        const biasEl = _slot.querySelector('[data-fld="tgt_bias"]');

        if (!Number.isFinite(est) || est === null || est === undefined) {
            if (estEl) estEl.textContent = (_state.reason ? '—' : 'computing…');
            if (spotEl) spotEl.textContent = 'spot —';
            if (ciEl) ciEl.textContent = '95% CI: —';
            if (biasEl) { biasEl.textContent = (_state.reason || '—'); biasEl.className = 'pin-target-bias flat'; }
            return;
        }

        if (estEl) estEl.textContent = '$' + est.toFixed(2);
        if (spotEl) spotEl.textContent = `spot ${Number.isFinite(spot) ? '$' + spot.toFixed(2) : '—'}`;
        if (ciEl) {
            ciEl.textContent = `95% CI: $${(_state.ci_low || 0).toFixed(2)} – $${(_state.ci_high || 0).toFixed(2)}`;
        }

        // Bias arrow vs spot
        if (biasEl && Number.isFinite(spot) && spot > 0) {
            const diff = est - spot;
            if (Math.abs(diff) < 0.10) {
                biasEl.textContent = '→ AT PIN';
                biasEl.className = 'pin-target-bias flat';
            } else if (diff > 0) {
                biasEl.textContent = `↑ +$${diff.toFixed(2)}`;
                biasEl.className = 'pin-target-bias up';
            } else {
                biasEl.textContent = `↓ −$${Math.abs(diff).toFixed(2)}`;
                biasEl.className = 'pin-target-bias dn';
            }
        }

        // High-conf vs low-conf left-border accent
        if (tgtHost) {
            tgtHost.classList.remove('high-conf', 'low-conf');
            if (Number.isFinite(conf)) {
                if (conf >= 0.30) tgtHost.classList.add('high-conf');
                else if (conf < 0.15) tgtHost.classList.add('low-conf');
            }
        }

        // Walls overlay
        const wallsEl = _slot.querySelector('[data-fld="walls"]');
        if (wallsEl) {
            const w = _state.walls || {};
            const chips = [
                ['max pain', w.max_pain],
                ['γ flip',   w.gamma_flip],
                ['call wall', w.call_wall],
                ['put wall', w.put_wall],
            ].filter(([_, v]) => Number.isFinite(v) && v > 0);
            wallsEl.innerHTML = chips.map(([nm, v]) =>
                `<div class="pin-wall-chip"><span class="name">${nm}</span><span class="v">$${v.toFixed(0)}</span></div>`
            ).join('');
        }
    }

    function _renderBars() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="bars_host"]');
        if (!host) return;
        const strikes = _state.strikes || [];
        if (!strikes.length) {
            host.innerHTML = `
                <div class="pin-bars-title">
                    <span>STRIKE PIN PROBABILITY</span>
                    <span>—</span>
                </div>
                <div class="pin-empty">${_state.reason ? 'No data: ' + _state.reason : 'Awaiting data…'}</div>`;
            return;
        }
        // Sort high-prob first for visual clarity
        const sorted = strikes.slice().sort((a,b) => b.pin_probability - a.pin_probability);
        const maxProb = sorted[0] ? sorted[0].pin_probability : 0;
        const pinEst = _state.pin_estimate;
        const spot = _state.spot;

        const titleHtml = `
            <div class="pin-bars-title">
                <span>STRIKE PIN PROBABILITY (within ±$${(_state.analysis_band_dollars || 15).toFixed(0)} of ATM)</span>
                <span>${strikes.length} strikes</span>
            </div>`;

        // Render in strike-ascending order (more intuitive for traders)
        const byStrike = strikes.slice().sort((a,b) => a.K - b.K);
        const rows = byStrike.map(s => {
            const isAtm = Number.isFinite(spot) && Math.abs(s.K - spot) < 0.5;
            const isPin = Number.isFinite(pinEst) && Math.abs(s.K - pinEst) < 0.5;
            const widthPct = maxProb > 0 ? (s.pin_probability / maxProb * 100) : 0;
            const oiTotal = s.oi_total || 0;
            const dnG = s.dn_gamma || 0;
            const gCls = dnG > 0 ? 'pos' : (dnG < 0 ? 'neg' : 'zero');
            const probPct = (s.pin_probability * 100).toFixed(1);
            const cls = ['pin-bar-row',
                        isAtm ? 'atm' : '',
                        isPin ? 'is-pin' : ''].filter(Boolean).join(' ');
            const tooltip = [
                `K=${s.K.toFixed(2)}`,
                `pin_prob=${probPct}%`,
                `gamma_score=${(s.gamma_score*100).toFixed(0)}%`,
                `distance_score=${(s.distance_score*100).toFixed(0)}%`,
                `oi_score=${(s.oi_score*100).toFixed(0)}%`,
                `time_amp=${s.time_amplifier.toFixed(2)}x`,
                `OI ${oiTotal}, dn_gamma ${dnG.toFixed(2)}`
            ].join(' · ');
            return `
                <div class="${cls}" title="${tooltip}">
                    <span class="strike">$${s.K.toFixed(2)}${isAtm?' •':''}</span>
                    <span class="bar-host">
                        <span class="bar" style="width:${widthPct.toFixed(1)}%"></span>
                        <span class="bar-text">${probPct}%</span>
                    </span>
                    <span class="oi">${oiTotal.toLocaleString()}</span>
                    <span class="gamma ${gCls}">${_signedTxt(dnG, 0)}</span>
                </div>
            `;
        }).join('');
        host.innerHTML = titleHtml + rows;
    }

    function _renderTrajectory() {
        if (!_slot || !_state) return;
        const canvas = _slot.querySelector('[data-fld="traj_canvas"]');
        const tnEl = _slot.querySelector('[data-fld="traj_n"]');
        const hist = _state.history || [];
        if (tnEl) tnEl.textContent = String(hist.length);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        // Match canvas drawing-buffer to display size
        const dpr = window.devicePixelRatio || 1;
        const w = canvas.clientWidth * dpr;
        const h = canvas.clientHeight * dpr;
        if (canvas.width !== w || canvas.height !== h) {
            canvas.width = w; canvas.height = h;
        }
        ctx.clearRect(0,0,w,h);
        if (hist.length < 2) {
            ctx.fillStyle = 'rgba(140,150,180,.35)';
            ctx.font = `${10*dpr}px JetBrains Mono`;
            ctx.fillText('history accumulating…', 4*dpr, 14*dpr);
            return;
        }
        // Plot pin_estimate over time + CI band
        const pins = hist.map(p => p.pin_estimate).filter(Number.isFinite);
        const lows = hist.map(p => p.ci_low).filter(Number.isFinite);
        const highs = hist.map(p => p.ci_high).filter(Number.isFinite);
        if (!pins.length) return;
        const minY = Math.min(...lows, ...pins) - 0.5;
        const maxY = Math.max(...highs, ...pins) + 0.5;
        const yRange = Math.max(0.001, maxY - minY);
        const xStep = w / Math.max(1, hist.length - 1);

        // CI band (filled polygon)
        ctx.beginPath();
        for (let i = 0; i < hist.length; i++) {
            const p = hist[i];
            if (!Number.isFinite(p.ci_high)) continue;
            const x = i * xStep;
            const y = h - ((p.ci_high - minY) / yRange) * h;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        for (let i = hist.length - 1; i >= 0; i--) {
            const p = hist[i];
            if (!Number.isFinite(p.ci_low)) continue;
            const x = i * xStep;
            const y = h - ((p.ci_low - minY) / yRange) * h;
            ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.fillStyle = 'rgba(133,182,230,.10)';
        ctx.fill();

        // Pin estimate line
        ctx.beginPath();
        for (let i = 0; i < hist.length; i++) {
            const p = hist[i];
            if (!Number.isFinite(p.pin_estimate)) continue;
            const x = i * xStep;
            const y = h - ((p.pin_estimate - minY) / yRange) * h;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = '#ffb366';
        ctx.lineWidth = 1.5 * dpr;
        ctx.stroke();

        // Spot trace (lighter)
        ctx.beginPath();
        for (let i = 0; i < hist.length; i++) {
            const p = hist[i];
            if (!Number.isFinite(p.spot)) continue;
            const x = i * xStep;
            const y = h - ((p.spot - minY) / yRange) * h;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = 'rgba(133,182,230,.6)';
        ctx.lineWidth = 1 * dpr;
        ctx.stroke();
    }

    function _renderAll() {
        _renderHeader();
        _renderTarget();
        _renderBars();
        _renderTrajectory();
    }

    // ── Data flow ───────────────────────────────────────────────────

    function _onPinUpdate(state) {
        if (!state) return;
        // Push events don't carry history (server-side state cache only emits
        // current snapshot). Preserve any history we already have from REST.
        if (_state && Array.isArray(_state.history) && !state.history) {
            // Append the new snapshot to existing history (keep frontend-side
            // continuity until next REST poll syncs full server-side history)
            const hist = _state.history.slice();
            if (Number.isFinite(state.pin_estimate)) {
                hist.push({
                    ts: state.server_time,
                    spot: state.spot,
                    pin_estimate: state.pin_estimate,
                    pin_confidence: state.pin_confidence,
                    ci_low: state.ci_low,
                    ci_high: state.ci_high,
                });
                while (hist.length > 480) hist.shift();
            }
            state.history = hist;
        }
        _state = state;
        _renderAll();
    }

    async function _refreshREST() {
        if (_destroyed) return;
        try {
            const r = await _authFetch(`/api/intel/pin/${TICKER}`);
            if (!r.ok) return;
            const j = await r.json();
            if (j && typeof j === 'object') {
                _state = j;
                _renderAll();
            }
        } catch (_) {}
    }

    function _tickAge() {
        if (_destroyed || !_state) return;
        // Decrement display value of time_remaining locally (don't wait for
        // next push to update a stale countdown)
        if (Number.isFinite(_state.time_remaining_sec) && _state.time_remaining_sec > 0) {
            _state.time_remaining_sec = Math.max(0, _state.time_remaining_sec - 1);
        }
        _renderHeader();
    }

    // ── Lifecycle ───────────────────────────────────────────────────

    function init(slotEl) {
        _slot = slotEl;
        _destroyed = false;
        _state = null;
        _injectStyles();
        _buildShell();

        if (window.AltarisEvents) {
            _pushHandler = (d) => _onPinUpdate(d);
            window.AltarisEvents.on('socket:intel:pin_update', _pushHandler);
        }

        // Initial REST snapshot + drift-correction poll
        _refreshREST();
        _pollTimer = setInterval(_refreshREST, REST_POLL_MS);
        _ageTickTimer = setInterval(_tickAge, 1000);
    }

    function destroy() {
        _destroyed = true;
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = null;
        if (_ageTickTimer) clearInterval(_ageTickTimer);
        _ageTickTimer = null;
        if (window.AltarisEvents && _pushHandler) {
            window.AltarisEvents.off('socket:intel:pin_update', _pushHandler);
            _pushHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
