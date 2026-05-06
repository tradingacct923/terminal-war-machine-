/**
 * SviPane — Volatility Surface Residual (Phase 20A)
 *
 * Backed by /api/intel/svi/<ticker>?exp=YYYY-MM-DD (connectors/svi_surface.py).
 * Calibrates Gatheral SVI to live Schwab chain, surfaces per-strike residuals
 * (observed IV vs SVI fit) and a vega-weighted aggregate z-score.
 *
 * Lifted from Nguyen (2025) "Regime-Adaptive Volatility Surface Arbitrage" with
 * our total-variance objective fix for small-T (0DTE) numerical stability.
 *
 * Renders three sections:
 *   1. Header — ticker / expiry selector + RMSE / pass status
 *   2. Smile chart — observed (dots) vs SVI fit (line) by log-moneyness
 *   3. Residual table — per-strike (K, side, iv_obs, iv_fit, residual_bp, vega)
 *
 * Every number traces to a SOURCE: SVI fit comes from connectors/svi_surface.py,
 * observed IV from Schwab `volatility` field, residuals are DERIVED.
 */
window.SviPane = (() => {
    'use strict';

    let _slot = null;
    let _refreshTimer = null;
    let _currentTicker = 'QQQ';
    let _currentExp = '';     // empty → server picks nearest
    let _availableExps = [];

    const REFRESH_MS = 30_000;   // 30s — calibration is ~150ms; not worth faster

    // ── CSS injection (scoped) ──
    function _injectCss() {
        if (document.getElementById('svi-pane-css')) return;
        const css = `
            .svi-pane { display:flex; flex-direction:column; height:100%;
                        font-family:'Roboto Mono',monospace; font-size:11px;
                        color:#cfd5e1; background:#0d1018; overflow:hidden; }
            .svi-header { display:flex; align-items:center; gap:8px;
                           padding:6px 10px; background:#10131c;
                           border-bottom:1px solid #1f2535; flex-shrink:0; }
            .svi-title { font-weight:700; color:#80d4ff; letter-spacing:0.5px; }
            .svi-pill { padding:2px 6px; border-radius:3px; background:#1a1f2e;
                        font-size:10px; color:#9aa3b8; }
            .svi-pill.pass { background:#0e3d1f; color:#7dde9b; }
            .svi-pill.fail { background:#3d0e0e; color:#de7d7d; }
            .svi-pill.warn { background:#3d2f0e; color:#ffd577; }
            .svi-exp-select { background:#0d1018; border:1px solid #1f2535;
                              color:#cfd5e1; padding:2px 6px; font-family:inherit;
                              font-size:10px; cursor:pointer; }
            .svi-stats { display:flex; gap:14px; margin-left:auto; font-size:10px; }
            .svi-stat-label { color:#6a7080; margin-right:3px; }
            .svi-stat-val { color:#cfd5e1; font-weight:600; }
            .svi-stat-val.pos { color:#ff7d7d; }
            .svi-stat-val.neg { color:#7dde9b; }
            .svi-stat-val.neutral { color:#9aa3b8; }

            .svi-body { display:flex; flex:1; min-height:0; }
            .svi-chart { flex:1.4; padding:6px; min-width:0; position:relative; }
            .svi-chart canvas { width:100%; height:100%; display:block; }
            .svi-table-wrap { flex:1; min-width:0; overflow-y:auto;
                              border-left:1px solid #1f2535; }
            .svi-table { width:100%; border-collapse:collapse; font-size:10px; }
            .svi-table th { position:sticky; top:0; background:#10131c;
                            color:#9aa3b8; font-weight:500; padding:3px 4px;
                            text-align:right; border-bottom:1px solid #1f2535;
                            font-size:9px; letter-spacing:0.3px; }
            .svi-table th:first-child, .svi-table td:first-child { text-align:left; }
            .svi-table td { padding:2px 4px; text-align:right;
                            border-bottom:1px solid #15192466;
                            font-variant-numeric:tabular-nums; }
            .svi-table tr.atm td { background:#1a1f2e44; }
            .svi-table td.resid-pos { color:#ff7d7d; font-weight:600; }
            .svi-table td.resid-neg { color:#7dde9b; font-weight:600; }
            .svi-table td.resid-zero { color:#6a7080; }
            .svi-table td.side-call { color:#80d4ff; }
            .svi-table td.side-put { color:#ff9c80; }

            .svi-loading { display:flex; align-items:center; justify-content:center;
                           height:100%; color:#6a7080; font-style:italic; }
            .svi-error { padding:10px; color:#de7d7d; font-size:10px; }
        `;
        const style = document.createElement('style');
        style.id = 'svi-pane-css';
        style.textContent = css;
        document.head.appendChild(style);
    }

    function _renderShell() {
        _slot.innerHTML = `
            <div class="svi-pane">
                <div class="svi-header">
                    <span class="svi-title">SVI</span>
                    <span class="svi-pill" id="svi-ticker">${_currentTicker}</span>
                    <select class="svi-exp-select" id="svi-exp"></select>
                    <span class="svi-pill" id="svi-rmse">RMSE —</span>
                    <span class="svi-pill" id="svi-arb">ARB —</span>
                    <div class="svi-stats">
                        <span><span class="svi-stat-label">spot</span><span class="svi-stat-val" id="svi-spot">—</span></span>
                        <span><span class="svi-stat-label">samples</span><span class="svi-stat-val" id="svi-n">—</span></span>
                        <span><span class="svi-stat-label">agg.resid</span><span class="svi-stat-val" id="svi-agg">—</span></span>
                        <span><span class="svi-stat-label">z (20d)</span><span class="svi-stat-val" id="svi-z">—</span></span>
                    </div>
                </div>
                <div class="svi-body">
                    <div class="svi-chart"><canvas id="svi-canvas"></canvas></div>
                    <div class="svi-table-wrap">
                        <table class="svi-table" id="svi-table">
                            <thead><tr>
                                <th>K</th>
                                <th>side</th>
                                <th>iv obs</th>
                                <th>iv fit</th>
                                <th>resid (bp)</th>
                                <th>vega·w</th>
                            </tr></thead>
                            <tbody id="svi-tbody">
                                <tr><td colspan="6" class="svi-loading">Loading…</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        `;
        // Wire expiry change handler
        const sel = _slot.querySelector('#svi-exp');
        sel.addEventListener('change', () => {
            _currentExp = sel.value;
            _refresh();
        });
    }

    // ── Canvas rendering ──
    function _drawChart(state) {
        const canvas = _slot.querySelector('#svi-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        const cssW = canvas.clientWidth || 400;
        const cssH = canvas.clientHeight || 250;
        canvas.width = cssW * dpr;
        canvas.height = cssH * dpr;
        ctx.scale(dpr, dpr);
        ctx.clearRect(0, 0, cssW, cssH);

        if (!state || !state.strikes || state.strikes.length < 2) {
            ctx.fillStyle = '#6a7080';
            ctx.font = '11px Roboto Mono';
            ctx.fillText('No data', 10, 20);
            return;
        }

        const strikes = state.strikes;
        const ks = strikes.map(s => s.k);
        const ivs = strikes.flatMap(s => [s.iv_obs, s.iv_fit]);
        const kMin = Math.min(...ks), kMax = Math.max(...ks);
        const ivMin = Math.min(...ivs) * 0.95;
        const ivMax = Math.max(...ivs) * 1.02;

        const padL = 38, padR = 8, padT = 8, padB = 22;
        const plotW = cssW - padL - padR;
        const plotH = cssH - padT - padB;

        const xPx = (k) => padL + ((k - kMin) / (kMax - kMin || 1)) * plotW;
        const yPx = (iv) => padT + (1 - (iv - ivMin) / (ivMax - ivMin || 1)) * plotH;

        // Gridlines + axes
        ctx.strokeStyle = '#1f2535';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(padL, padT); ctx.lineTo(padL, padT + plotH);
        ctx.lineTo(padL + plotW, padT + plotH);
        ctx.stroke();

        // Y-axis labels (3 ticks)
        ctx.fillStyle = '#6a7080';
        ctx.font = '9px Roboto Mono';
        ctx.textAlign = 'right';
        for (let i = 0; i <= 3; i++) {
            const iv = ivMin + (ivMax - ivMin) * i / 3;
            const y = yPx(iv);
            ctx.fillText((iv * 100).toFixed(1) + '%', padL - 3, y + 3);
            if (i > 0 && i < 3) {
                ctx.strokeStyle = '#1f253544';
                ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
            }
        }

        // X-axis label (k=0 = ATM)
        ctx.textAlign = 'center';
        ctx.fillText('k=' + kMin.toFixed(3), padL, padT + plotH + 12);
        ctx.fillText('k=0 (ATM)', xPx(0), padT + plotH + 12);
        ctx.fillText('k=' + kMax.toFixed(3), padL + plotW, padT + plotH + 12);

        // ATM vertical line
        ctx.strokeStyle = '#3a4055';
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(xPx(0), padT); ctx.lineTo(xPx(0), padT + plotH);
        ctx.stroke();
        ctx.setLineDash([]);

        // SVI fit line (smooth — re-compute from params on a dense grid)
        const params = state.params;
        if (params && params.b !== undefined) {
            ctx.strokeStyle = '#80d4ff';
            ctx.lineWidth = 1.8;
            ctx.beginPath();
            const T = state.T_years;
            const N = 80;
            for (let i = 0; i <= N; i++) {
                const k = kMin + (kMax - kMin) * i / N;
                const xi = k - params.m;
                const w = params.a + params.b * (params.rho * xi + Math.sqrt(xi*xi + params.sigma*params.sigma));
                const iv = Math.sqrt(Math.max(w, 1e-10) / T);
                const x = xPx(k), y = yPx(iv);
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }
            ctx.stroke();
        }

        // Observed IV dots (calls = blue, puts = orange)
        for (const s of strikes) {
            ctx.fillStyle = s.side === 'call' ? '#80d4ff' : '#ff9c80';
            ctx.beginPath();
            ctx.arc(xPx(s.k), yPx(s.iv_obs), 2.2, 0, Math.PI * 2);
            ctx.fill();
        }

        // Legend
        ctx.font = '9px Roboto Mono';
        ctx.textAlign = 'left';
        ctx.fillStyle = '#80d4ff'; ctx.fillText('● calls', padL + 5, padT + 10);
        ctx.fillStyle = '#ff9c80'; ctx.fillText('● puts',  padL + 50, padT + 10);
        ctx.strokeStyle = '#80d4ff'; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(padL + 90, padT + 7); ctx.lineTo(padL + 110, padT + 7); ctx.stroke();
        ctx.fillStyle = '#80d4ff'; ctx.fillText('SVI fit', padL + 113, padT + 10);
    }

    function _renderTable(state) {
        const tbody = _slot.querySelector('#svi-tbody');
        if (!state || !state.strikes) {
            tbody.innerHTML = '<tr><td colspan="6" class="svi-loading">No data</td></tr>';
            return;
        }
        const spot = state.spot || 0;
        const html = state.strikes.map(s => {
            const isATM = Math.abs(s.K - spot) < 0.5;
            const residClass = s.residual_bp > 5 ? 'resid-pos' :
                                s.residual_bp < -5 ? 'resid-neg' : 'resid-zero';
            return `<tr class="${isATM ? 'atm' : ''}">
                <td>${s.K.toFixed(2)}</td>
                <td class="side-${s.side}">${s.side}</td>
                <td>${(s.iv_obs * 100).toFixed(2)}%</td>
                <td>${(s.iv_fit * 100).toFixed(2)}%</td>
                <td class="${residClass}">${s.residual_bp >= 0 ? '+' : ''}${s.residual_bp.toFixed(1)}</td>
                <td>${s.vega_weight.toFixed(2)}</td>
            </tr>`;
        }).join('');
        tbody.innerHTML = html;
    }

    function _renderHeader(state) {
        const $ = (id) => _slot.querySelector('#' + id);
        $('svi-spot').textContent = '$' + (state.spot || 0).toFixed(2);
        $('svi-n').textContent = state.samples_used || 0;
        const rmse = state.rmse_bp || 0;
        const rmseEl = $('svi-rmse');
        rmseEl.textContent = `RMSE ${rmse.toFixed(1)}bp`;
        rmseEl.className = 'svi-pill ' + (state.pass_rmse ? 'pass' : 'fail');

        const arbEl = $('svi-arb');
        arbEl.textContent = state.butterfly_arb ? 'ARB ⚠' : 'ARB ✓';
        arbEl.className = 'svi-pill ' + (state.butterfly_arb ? 'fail' : 'pass');

        const agg = state.aggregate_residual;
        const aggEl = $('svi-agg');
        if (agg !== null && agg !== undefined) {
            aggEl.textContent = `${agg >= 0 ? '+' : ''}${agg.toFixed(1)}bp`;
            aggEl.className = 'svi-stat-val ' + (agg > 5 ? 'pos' : agg < -5 ? 'neg' : 'neutral');
        } else {
            aggEl.textContent = '—';
            aggEl.className = 'svi-stat-val neutral';
        }

        const z = state.aggregate_z;
        const zEl = $('svi-z');
        if (z !== null && z !== undefined) {
            zEl.textContent = `${z >= 0 ? '+' : ''}${z.toFixed(2)}`;
            zEl.className = 'svi-stat-val ' + (Math.abs(z) > 1.5 ? (z > 0 ? 'pos' : 'neg') : 'neutral');
        } else {
            zEl.textContent = `— (${state.aggregate_z_window || 0}/${state.aggregate_z_window_max || 20})`;
            zEl.className = 'svi-stat-val neutral';
        }

        // Update expiry dropdown if it changed
        if (state.exp_date && state.exp_date !== _currentExp) {
            _currentExp = state.exp_date;
        }
    }

    function _renderError(msg) {
        const tbody = _slot.querySelector('#svi-tbody');
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="6" class="svi-error">${msg}</td></tr>`;
        }
    }

    async function _populateExpDropdown() {
        // Probe /api/chain to get available expirations
        try {
            const r = await window.authFetch('/api/chain?ticker=' + _currentTicker);
            if (!r.ok) return;
            const data = await r.json();
            _availableExps = (data.expirations || []).map(e => ({
                date: e.date, dte: e.dte, label: e.label,
            }));
            const sel = _slot.querySelector('#svi-exp');
            sel.innerHTML = _availableExps.slice(0, 8).map(e =>
                `<option value="${e.date}">${e.label} (${e.dte}DTE)</option>`
            ).join('');
            if (_currentExp) sel.value = _currentExp;
            else if (_availableExps.length > 0) {
                _currentExp = _availableExps[0].date;
                sel.value = _currentExp;
            }
        } catch (_) {}
    }

    async function _refresh() {
        if (!_slot) return;
        try {
            const url = '/api/intel/svi/' + _currentTicker +
                        (_currentExp ? '?exp=' + _currentExp : '');
            const r = await window.authFetch(url);
            if (!r.ok) {
                _renderError(`Server returned ${r.status}. (Endpoint pending server restart?)`);
                return;
            }
            const state = await r.json();
            if (state.error) {
                _renderError(state.error);
                return;
            }
            _renderHeader(state);
            _drawChart(state);
            _renderTable(state);
        } catch (e) {
            _renderError(`Fetch failed: ${e.message}`);
        }
    }

    function init(slotEl) {
        _slot = slotEl;
        _injectCss();
        _renderShell();

        // First populate expirations, then fetch SVI
        _populateExpDropdown().then(() => {
            _refresh();
        });

        // Periodic refresh
        _refreshTimer = setInterval(_refresh, REFRESH_MS);
    }

    function destroy() {
        if (_refreshTimer) {
            clearInterval(_refreshTimer);
            _refreshTimer = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
