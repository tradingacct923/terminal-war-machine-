/**
 * HedgeForecastPane — Γ-pressure × velocity OBSERVABLE state (descriptive only).
 *
 * Backed by /api/intel/hedge_forecast/<ticker> + Socket.IO 'intel:hedge_forecast'
 * (push every 5s during RTH). Both endpoints now ship observable-only payload.
 *
 * 2026-05-04 — Directional prediction REMOVED from this pane.
 * Audit on n=1,910 paired forecast/observation records (today's
 * hedge_forecast_paired ledger) showed:
 *   - sign_match rate: 53.30% (Wilson 95% CI [51-56])
 *   - majority-class baseline: 62.72% (constant "positive")
 *   - calibration_ratio median: 0.002 (forecast 500× too large)
 *   - Pearson(forecast, observed): r = -0.01 (zero correlation)
 * The model UNDERPERFORMS coin-flip-against-base-rate by 9.4 percentage
 * points. Same root cause as sweep: modern MMs run net-delta-neutral books
 * and don't hedge sweep-by-sweep. See /tmp/hedge_forecaster_audit.py.
 *
 * What we keep displaying (all observable, all factual):
 *   1. HEADER          — spot, velocity (per-sec + CV), distance to flip
 *   2. Γ-PRESSURE      — hp_gamma_shares_1pct (DERIVED but factual:
 *                        shares/1% spot move from greek_surface)
 *   3. OBSERVED TAPE   — last 5 min equity flow + print count
 *
 * What we deliberately don't show: forecasts.{5,15,30}min.{shares,side,
 * confidence}. The disk ledgers (hedge_forecast_outcomes_*.jsonl,
 * hedge_forecast_paired_*.jsonl) keep writing those for future analysis.
 */
window.HedgeForecastPane = (() => {
    'use strict';

    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _pollTimer = null;
    let _pushHandler = null;

    let _state = null;

    // CONFIGURED — UI cadence; live pushes carry true freshness
    const REST_POLL_MS = 30000;     // drift-correction polling
    const TICKER       = 'QQQ';

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('hedgefc-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'hedgefc-styles';
        _styleEl.textContent = `
            .hedgefc-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            .hedgefc-header { display:grid; grid-template-columns: auto 1fr auto auto auto;
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:5px; }
            .hedgefc-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; }
            .hedgefc-stat { display:flex; flex-direction:column; gap:1px; }
            .hedgefc-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .hedgefc-stat .val { color:rgba(220,230,250,.95); font-weight:700; font-size:11px; }
            .hedgefc-stat .val.up { color:#85b6e6; }
            .hedgefc-stat .val.dn { color:#e69580; }
            .hedgefc-stat .val.warn { color:#ffb366; }
            .hedgefc-stat .val.ok { color:#66cc99; }
            .hedgefc-stat .sub { font-size:8px; color:rgba(140,150,180,.5); }

            /* ── Primary forecast card ───────────────────────── */
            .hedgefc-primary { background:rgba(255,255,255,.025); border-radius:4px;
                padding:10px; border-left:3px solid rgba(140,150,180,.3);
                display:flex; flex-direction:column; gap:6px; }
            .hedgefc-primary.dir-buy {
                border-left-color:#66cc99;
                background:linear-gradient(90deg, rgba(102,204,153,.08), rgba(255,255,255,.025) 65%); }
            .hedgefc-primary.dir-sell {
                border-left-color:#cc6677;
                background:linear-gradient(90deg, rgba(204,102,119,.08), rgba(255,255,255,.025) 65%); }
            .hedgefc-primary-label { font-size:8px; color:rgba(140,150,180,.6);
                text-transform:uppercase; letter-spacing:.6px; }
            .hedgefc-primary-row { display:flex; align-items:baseline;
                justify-content:space-between; gap:10px; }
            .hedgefc-primary-tag { font-weight:700; font-size:14px; letter-spacing:.6px;
                padding:3px 9px; border-radius:3px; }
            .hedgefc-primary-tag.buy { background:rgba(102,204,153,.18); color:#85e0a3; }
            .hedgefc-primary-tag.sell { background:rgba(204,102,119,.18); color:#e69aa5; }
            .hedgefc-primary-tag.flat { background:rgba(170,180,210,.1); color:rgba(170,180,210,.5); }
            .hedgefc-primary-shares { font-size:22px; font-weight:700;
                color:rgba(220,230,250,.98); font-family:'JetBrains Mono',monospace; }
            .hedgefc-primary-shares.dir-buy { color:#85e0a3; }
            .hedgefc-primary-shares.dir-sell { color:#e69aa5; }
            .hedgefc-primary-conf { font-size:9px; color:rgba(160,170,200,.65); }

            /* ── Window bars ─────────────────────────────────── */
            .hedgefc-windows { display:grid; grid-template-columns: repeat(3, 1fr);
                gap:6px; padding:4px 0; }
            .hedgefc-win { background:rgba(255,255,255,.02); border-radius:3px;
                padding:6px; display:flex; flex-direction:column; gap:3px;
                border:1px solid rgba(255,255,255,.04); }
            .hedgefc-win.dir-buy { border-color:rgba(102,204,153,.3); }
            .hedgefc-win.dir-sell { border-color:rgba(204,102,119,.3); }
            .hedgefc-win-head { display:flex; justify-content:space-between;
                align-items:center; }
            .hedgefc-win-tag { font-size:8px; font-weight:700; letter-spacing:.4px;
                color:rgba(180,190,220,.7); }
            .hedgefc-win-conf { font-size:7px; color:rgba(140,150,180,.55); }
            .hedgefc-win-shares { font-size:13px; font-weight:700;
                font-family:'JetBrains Mono',monospace; }
            .hedgefc-win-shares.up { color:#85b6e6; }
            .hedgefc-win-shares.dn { color:#e69580; }
            .hedgefc-win-shares.flat { color:rgba(170,180,210,.4); }
            .hedgefc-win-delta { font-size:8px; color:rgba(160,170,200,.5); }
            .hedgefc-win-bar { height:4px; background:rgba(255,255,255,.05);
                border-radius:2px; position:relative; overflow:hidden; }
            .hedgefc-win-bar > .fill { position:absolute; top:0; bottom:0;
                left:50%; transform-origin:left center; transition:transform .4s ease,
                background .4s ease; }

            /* ── Calibration row ─────────────────────────────── */
            .hedgefc-calib { background:rgba(255,255,255,.02); border-radius:3px;
                padding:6px; display:grid; grid-template-columns: 1fr 1fr 1fr;
                gap:8px; font-size:9px; }
            .hedgefc-calib-cell { display:flex; flex-direction:column; gap:2px; }
            .hedgefc-calib-cell .k { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.4px; }
            .hedgefc-calib-cell .v { font-size:11px; font-weight:700;
                color:rgba(220,230,250,.95); font-family:'JetBrains Mono',monospace; }
            .hedgefc-calib-cell .v.up { color:#85b6e6; }
            .hedgefc-calib-cell .v.dn { color:#e69580; }
            .hedgefc-calib-cell .v.flat { color:rgba(170,180,210,.4); }
            .hedgefc-calib-cell .sub { font-size:7px; color:rgba(140,150,180,.5); }

            .hedgefc-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="hedgefc-wrap">
            <div class="hedgefc-header">
                <div class="hedgefc-title">🔮 HEDGE FORECASTER</div>
                <div></div>
                <div class="hedgefc-stat">
                    <span class="lbl">spot</span>
                    <span class="val" data-fld="hdr_spot">—</span>
                </div>
                <div class="hedgefc-stat">
                    <span class="lbl">velocity</span>
                    <span class="val" data-fld="hdr_vel">—</span>
                    <span class="sub" data-fld="hdr_vel_sub">cv —</span>
                </div>
                <div class="hedgefc-stat">
                    <span class="lbl">to flip</span>
                    <span class="val" data-fld="hdr_flip">—</span>
                </div>
            </div>

            <div class="hedgefc-primary" data-fld="primary_host">
                <div class="hedgefc-empty">Awaiting first compute…</div>
            </div>

            <div class="hedgefc-windows" data-fld="windows_host"></div>

            <div class="hedgefc-calib" data-fld="calib_host">
                <div class="hedgefc-calib-cell">
                    <span class="k">Predicted (5min)</span>
                    <span class="v" data-fld="calib_pred">—</span>
                    <span class="sub">DERIVED hp_γ × ΔS</span>
                </div>
                <div class="hedgefc-calib-cell">
                    <span class="k">Observed actual</span>
                    <span class="v" data-fld="calib_actual">—</span>
                    <span class="sub" data-fld="calib_actual_sub">last 5min equity tape</span>
                </div>
                <div class="hedgefc-calib-cell">
                    <span class="k">Calibration ratio</span>
                    <span class="v" data-fld="calib_ratio">—</span>
                    <span class="sub">target → 1.0×</span>
                </div>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────

    function _fmtSize(n) {
        if (!Number.isFinite(n)) return '—';
        const a = Math.abs(n);
        if (a >= 1e6) return (n/1e6).toFixed(2) + 'M';
        if (a >= 1e3) return (n/1e3).toFixed(1) + 'K';
        return Math.round(n).toString();
    }

    function _fmtSigned(n) {
        if (!Number.isFinite(n)) return '—';
        const sign = n > 0 ? '+' : (n < 0 ? '−' : '');
        return sign + _fmtSize(Math.abs(n));
    }

    function _fmtVelocity(v_per_sec) {
        if (!Number.isFinite(v_per_sec)) return '—';
        // Convert to $/min for display
        const v_per_min = v_per_sec * 60;
        const sign = v_per_min > 0 ? '+' : (v_per_min < 0 ? '−' : '');
        return `${sign}$${Math.abs(v_per_min).toFixed(2)}/min`;
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
        set('hdr_spot', _state.spot ? '$' + _state.spot.toFixed(2) : '—');
        const vel = _state.velocity_per_sec;
        if (Number.isFinite(vel)) {
            set('hdr_vel', _fmtVelocity(vel),
                vel > 0 ? 'up' : (vel < 0 ? 'dn' : ''));
        } else {
            set('hdr_vel', '—');
        }

        const cv = _state.velocity_cv;
        const cvSubEl = _slot.querySelector('[data-fld="hdr_vel_sub"]');
        if (cvSubEl) {
            if (Number.isFinite(cv)) {
                cvSubEl.textContent = `cv ${cv.toFixed(2)}${_state.velocity_stable ? ' (stable)' : ' (noisy)'}`;
            } else {
                cvSubEl.textContent = `cv —`;
            }
        }

        const dist = _state.distance_to_flip;
        if (Number.isFinite(dist)) {
            set('hdr_flip', `$${dist.toFixed(2)}`,
                dist > 30 ? 'ok' : (dist > 10 ? '' : 'warn'));
        } else {
            set('hdr_flip', '—');
        }
    }

    function _renderPrimary() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="primary_host"]');
        if (!host) return;

        // 2026-05-04 — predictive forecast removed (zero edge over base rate).
        // Replaced with descriptive Γ-pressure observation: how many shares
        // dealers would need to hedge per 1% spot move (a structural fact
        // computed from the streamed Γ chain, not a prediction of behavior).
        const hpShares = _state.hp_gamma_shares_1pct;
        const vel = _state.velocity_per_sec;
        if (!Number.isFinite(hpShares) || hpShares === null) {
            host.className = 'hedgefc-primary';
            host.innerHTML = `<div class="hedgefc-empty">${_state.reason ? 'No data: ' + _state.reason : 'Awaiting Γ-pressure…'}</div>`;
            return;
        }

        // Regime label from sign of hp_gamma_shares_1pct:
        //   > 0 → short-γ regime (dealers BUY rallies, SELL dips → momentum amplifier)
        //   < 0 → long-γ regime  (dealers SELL rallies, BUY dips → mean-reverter)
        const isLongGamma = hpShares < 0;
        const regimeTxt = isLongGamma ? 'long-γ regime (mean-reverting)'
                                       : 'short-γ regime (momentum-amplifying)';
        const regimeCls = isLongGamma ? 'dir-sell' : 'dir-buy';
        const sharesAbs = _fmtSize(Math.abs(hpShares));
        const velTxt = Number.isFinite(vel) ? _fmtVelocity(vel) : '—';

        host.className = 'hedgefc-primary ' + regimeCls;
        host.innerHTML = `
            <div class="hedgefc-primary-label">Γ-pressure (observable, not a prediction)</div>
            <div class="hedgefc-primary-row">
                <span class="hedgefc-primary-tag ${isLongGamma ? 'sell' : 'buy'}">${regimeTxt}</span>
                <span class="hedgefc-primary-conf">spot velocity ${velTxt}${_state.velocity_stable ? ' (stable)' : ' (noisy)'}</span>
            </div>
            <div class="hedgefc-primary-row">
                <span class="hedgefc-primary-shares ${regimeCls}">${sharesAbs} shares / 1% spot</span>
                <span class="hedgefc-primary-conf">structural Γ-exposure from streaming chain</span>
            </div>
        `;
    }

    function _renderWindows() {
        // 2026-05-04 — 5/15/30-min predictive bars removed (zero-edge audit).
        // Hide the row entirely. Disk ledgers keep predictions for offline
        // analysis; the live UI no longer renders directional forecasts.
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="windows_host"]');
        if (!host) return;
        host.innerHTML = '';
        host.style.display = 'none';
    }

    function _renderCalibration() {
        // 2026-05-04 — only render the OBSERVED equity tape over last 5min.
        // Predicted + calibration_ratio cells removed — predictions had zero
        // edge over base rate (53.3% sign_match vs 62.7% majority baseline,
        // calibration_ratio median 0.002). Observed is a fact: it's the
        // sum of signed equity prints over [now-300, now] from Tradier WS.
        if (!_slot || !_state) return;
        const set = (sel, txt, cls) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (!el) return;
            el.textContent = txt;
            if (cls !== undefined) el.className = cls ? ('v ' + cls) : 'v';
        };

        const obs = _state.observed_5min_actual;
        const obsCount = _state.observed_5min_count || 0;

        set('calib_actual',
            Number.isFinite(obs) ? _fmtSigned(obs) : '—',
            obs > 0 ? 'up' : (obs < 0 ? 'dn' : 'flat'));

        const subEl = _slot.querySelector('[data-fld="calib_actual_sub"]');
        if (subEl) subEl.textContent = `${obsCount} prints / last 5min equity tape`;

        // Hide the predicted + ratio cells entirely (they're predictive)
        for (const fld of ['calib_pred', 'calib_ratio']) {
            const cell = _slot.querySelector(`[data-fld="${fld}"]`);
            if (cell) {
                const wrap = cell.closest('.hedgefc-calib-cell');
                if (wrap) wrap.style.display = 'none';
            }
        }
    }

    function _renderAll() {
        _renderHeader();
        _renderPrimary();
        _renderWindows();
        _renderCalibration();
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
            const r = await _authFetch(`/api/intel/hedge_forecast/${TICKER}`);
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
            window.AltarisEvents.on('socket:intel:hedge_forecast', _pushHandler);
        }

        _refreshREST();
        _pollTimer = setInterval(_refreshREST, REST_POLL_MS);
    }

    function destroy() {
        _destroyed = true;
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = null;
        if (window.AltarisEvents && _pushHandler) {
            window.AltarisEvents.off('socket:intel:hedge_forecast', _pushHandler);
            _pushHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
