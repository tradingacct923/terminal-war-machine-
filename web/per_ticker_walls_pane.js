/**
 * PerTickerWallsPane — compact per-ticker gamma walls HUD
 *
 * Consumes the `single_name_walls` socket event (emitted every 6s from
 * schwab_bridge._single_name_refresh_loop). Displays one row per ticker
 * with spot, put_wall, call_wall, gamma_flip, and an above-flip indicator.
 *
 * The backend extracts walls from _single_name_greeks_cache (8,226 contracts
 * across the 8 NQ top weights + full 60-day expiration ladder).
 */
const PerTickerWallsPane = (() => {
    'use strict';

    let _container = null;
    let _tbody = null;
    let _styleEl = null;
    let _wallsHandler = null;
    let _wgcHandler = null;
    let _latestWalls = [];
    let _latestWgc = null;

    function _injectStyles() {
        if (document.getElementById('ptw-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'ptw-styles';
        _styleEl.textContent = `
            .ptw-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; }
            .ptw-hdr { display:flex; align-items:center; padding:4px 8px; border-bottom:1px solid rgba(255,255,255,.04); gap:6px; }
            .ptw-title { font-size:9px; font-weight:600; color:rgba(180,190,220,.7); letter-spacing:.8px; text-transform:uppercase; }
            .ptw-age { font-size:8px; color:rgba(140,150,180,.4); margin-left:auto; }
            .ptw-scroll { flex:1; overflow-y:auto; overflow-x:hidden; }
            .ptw-table { width:100%; border-collapse:collapse; font-size:9.5px; }
            .ptw-table thead { position:sticky; top:0; z-index:1; background:#0a0d18; }
            .ptw-table th { padding:4px 5px; text-align:left; font-size:7.5px; font-weight:500; color:rgba(140,150,180,.55);
                            border-bottom:1px solid rgba(255,255,255,.03); letter-spacing:.5px; text-transform:uppercase; }
            .ptw-table td { padding:4px 5px; border-bottom:1px solid rgba(255,255,255,.02); white-space:nowrap; }
            .ptw-ticker { color:#d8c78c; font-weight:600; }
            .ptw-spot  { color:rgba(220,225,235,.9); font-weight:600; }
            .ptw-pw    { color:#e03060; }
            .ptw-cw    { color:#1fd17a; }
            .ptw-flip  { color:rgba(168,85,247,.9); font-weight:600; }
            .ptw-gex   { color:rgba(124,90,247,.85); }
            .ptw-above { color:#1fd17a; font-weight:700; }
            .ptw-below { color:#e03060; font-weight:700; }
            .ptw-empty { color:rgba(140,150,180,.4); font-size:10px; padding:12px; text-align:center; }
            .ptw-row-dampening { background: linear-gradient(90deg, rgba(31,209,122,.04), transparent); }
            .ptw-row-amplifying { background: linear-gradient(90deg, rgba(224,48,96,.06), transparent); }

            .ptw-wgc { display:flex; align-items:center; gap:10px; padding:6px 8px; border-bottom:1px solid rgba(255,255,255,.05);
                        background:linear-gradient(90deg, rgba(124,90,247,.04), transparent); font-size:10px; }
            .ptw-wgc-label { color:rgba(180,190,220,.55); font-size:8px; letter-spacing:.8px; text-transform:uppercase; }
            .ptw-wgc-regime { font-weight:700; font-size:11px; padding:2px 6px; border-radius:3px; letter-spacing:.5px; }
            .ptw-wgc-damp { background:rgba(31,209,122,.18); color:#1fd17a; border:1px solid rgba(31,209,122,.4); }
            .ptw-wgc-ampl { background:rgba(224,48,96,.18); color:#e03060; border:1px solid rgba(224,48,96,.4); }
            .ptw-wgc-neutral { background:rgba(140,150,180,.12); color:rgba(180,190,220,.7); border:1px solid rgba(140,150,180,.3); }
            .ptw-wgc-sign { font-weight:600; font-family:inherit; }
            .ptw-wgc-bull { color:#1fd17a; }
            .ptw-wgc-bear { color:#e03060; }
            .ptw-wgc-stat { color:rgba(200,210,230,.75); font-size:9.5px; }
            .ptw-wgc-stat b { color:rgba(220,225,235,.95); font-weight:600; }
            .ptw-wgc-pipe { color:rgba(140,150,180,.3); }
            .ptw-wgc-hint { margin-left:auto; font-size:8.5px; color:rgba(140,150,180,.55); font-style:italic; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _renderWgc() {
        const wgcEl = _container && _container.querySelector('#ptw-wgc');
        if (!wgcEl) return;
        const w = _latestWgc;
        if (!w || typeof w.wgc_sign !== 'number') {
            wgcEl.innerHTML = `
                <span class="ptw-wgc-label">NDX-WGC</span>
                <span class="ptw-wgc-stat">awaiting composite…</span>`;
            return;
        }
        const regime = String(w.regime || 'NEUTRAL').toUpperCase();
        const regimeCls = regime === 'DAMP' ? 'ptw-wgc-damp'
                         : regime === 'AMPL' ? 'ptw-wgc-ampl'
                         : 'ptw-wgc-neutral';
        const signPct = (w.wgc_sign * 100);
        const signCls = signPct > 0 ? 'ptw-wgc-bull' : signPct < 0 ? 'ptw-wgc-bear' : '';
        const signStr = `${signPct >= 0 ? '+' : ''}${signPct.toFixed(1)}%`;
        const covered = (w.covered_weight * 100).toFixed(1);
        const ampl = w.ampl_count | 0;
        const damp = w.damp_count | 0;
        const netMw = Number(w.wgc_net_mw || 0);
        const netStr = `${netMw >= 0 ? '+' : ''}$${netMw.toFixed(1)}M`;
        const hint = regime === 'DAMP' ? 'fade breakouts · range-bound'
                    : regime === 'AMPL' ? 'chase breakouts · trend likely'
                    : 'regime unclear';
        wgcEl.innerHTML = `
            <span class="ptw-wgc-label">NDX-WGC</span>
            <span class="ptw-wgc-regime ${regimeCls}">${regime}</span>
            <span class="ptw-wgc-sign ${signCls}">${signStr}</span>
            <span class="ptw-wgc-pipe">|</span>
            <span class="ptw-wgc-stat"><b>${damp}</b> DAMP · <b>${ampl}</b> AMPL</span>
            <span class="ptw-wgc-pipe">|</span>
            <span class="ptw-wgc-stat">γ·w: <b>${netStr}</b></span>
            <span class="ptw-wgc-pipe">|</span>
            <span class="ptw-wgc-stat">cover <b>${covered}%</b> NDX</span>
            <span class="ptw-wgc-hint">${hint}</span>`;
    }

    function _render() {
        _renderWgc();
        if (!_tbody) return;
        if (!_latestWalls || _latestWalls.length === 0) {
            _tbody.innerHTML = `<tr><td colspan="7" class="ptw-empty">Waiting for single-name walls data…</td></tr>`;
            return;
        }
        const frag = document.createDocumentFragment();
        for (const w of _latestWalls) {
            const tr = document.createElement('tr');
            const above = (w.above_flip === true);
            const below = (w.above_flip === false);
            tr.className = above ? 'ptw-row-dampening' : below ? 'ptw-row-amplifying' : '';
            const flipStr = (w.gamma_flip && w.gamma_flip > 0) ? w.gamma_flip.toFixed(2) : '—';
            const aboveCell = above
                ? '<span class="ptw-above">▲ DAMP</span>'
                : below
                    ? '<span class="ptw-below">▼ AMPL</span>'
                    : '<span style="color:rgba(140,150,180,.4)">—</span>';
            const distFlip = (w.gamma_flip && w.gamma_flip > 0)
                ? ((w.spot - w.gamma_flip) / w.gamma_flip * 100).toFixed(2) + '%'
                : '—';
            tr.innerHTML = `
                <td class="ptw-ticker">${w.ticker}</td>
                <td class="ptw-spot">${w.spot.toFixed(2)}</td>
                <td class="ptw-pw">${w.put_wall.toFixed(2)}</td>
                <td class="ptw-cw">${w.call_wall.toFixed(2)}</td>
                <td class="ptw-flip">${flipStr}</td>
                <td>${aboveCell} <span style="color:rgba(140,150,180,.4)">${distFlip}</span></td>
                <td class="ptw-gex">$${w.total_gex.toFixed(2)}M</td>`;
            frag.appendChild(tr);
        }
        _tbody.innerHTML = '';
        _tbody.appendChild(frag);

        const ageEl = _container && _container.querySelector('#ptw-age');
        if (ageEl && _latestWalls[0]) {
            const ageMs = Date.now() - (_latestWalls[0].updated_ms || Date.now());
            ageEl.textContent = `${(ageMs / 1000).toFixed(1)}s ago · ${_latestWalls.length} names`;
        }
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _container.innerHTML = `
            <div class="ptw-wrap">
                <div class="ptw-hdr">
                    <span class="ptw-title">Per-Ticker Gamma Walls</span>
                    <span class="ptw-age" id="ptw-age">waiting…</span>
                </div>
                <div class="ptw-wgc" id="ptw-wgc"></div>
                <div class="ptw-scroll">
                    <table class="ptw-table">
                        <thead><tr>
                            <th>Ticker</th><th>Spot</th><th>Put Wall</th><th>Call Wall</th>
                            <th>γ Flip</th><th>Dealer Regime</th><th>Σ Dollar γ</th>
                        </tr></thead>
                        <tbody id="ptw-tbody"></tbody>
                    </table>
                </div>
            </div>`;
        _tbody = _container.querySelector('#ptw-tbody');
        _render();

        // Subscribe to walls + WGC composite events.
        if (window._sio) {
            _wallsHandler = (payload) => {
                if (payload && Array.isArray(payload.tickers)) {
                    _latestWalls = payload.tickers;
                    _render();
                }
            };
            window._sio.on('single_name_walls', _wallsHandler);

            _wgcHandler = (payload) => {
                if (payload && typeof payload.wgc_sign === 'number') {
                    _latestWgc = payload;
                    _renderWgc();
                }
            };
            window._sio.on('ndx_wgc', _wgcHandler);
        }
    }

    function destroy() {
        if (window._sio) {
            if (_wallsHandler) window._sio.off('single_name_walls', _wallsHandler);
            if (_wgcHandler)   window._sio.off('ndx_wgc', _wgcHandler);
        }
        _wallsHandler = null;
        _wgcHandler = null;
        _container = null;
        _tbody = null;
        _latestWalls = [];
        _latestWgc = null;
    }

    // Direct handler (in case AltarisEvents fan-out wants to route to us).
    function onWalls(data) {
        if (data && Array.isArray(data.tickers)) {
            _latestWalls = data.tickers;
            _render();
        }
    }

    return { init, destroy, onWalls };
})();
window.PerTickerWallsPane = PerTickerWallsPane;
