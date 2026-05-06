/**
 * DealerPrintsPane — Raw dealer-print capture display.
 *
 * Shows descriptive distributions over every option print we capture, joined
 * with the OPTIONS_BOOK snapshot that was live at print time. NO thresholds,
 * NO classification labels. See connectors/dealer_print_capture.py for the
 * capture pipeline. Purpose: let the operator see the population of prints
 * so that — once enough forward-sample data is collected (logs/dealer_prints_*)
 * — we can slice by (size, venue, level_hit, Δmm, post-spot-move) to learn
 * which combinations actually predict forward drift. Until then, descriptive.
 *
 * Pulls from /api/_debug/dealer_prints/summary and /recent at 2s cadence.
 */
const DealerPrintsPane = (() => {
    'use strict';

    let _container = null;
    let _styleEl = null;
    let _pollTimer = null;
    let _destroyed = false;

    function _injectStyles() {
        if (document.getElementById('dpp-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'dpp-styles';
        _styleEl.textContent = `
            .dpp-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px; overflow:hidden; }
            .dpp-header { display:flex; justify-content:space-between; align-items:center; padding:0 2px; font-size:9px; color:rgba(160,170,200,.6); letter-spacing:.5px; text-transform:uppercase; }
            .dpp-title { font-weight:600; color:rgba(200,210,230,.8); }
            .dpp-counters { display:flex; gap:10px; font-size:8px; }
            .dpp-counter-val { color:rgba(200,210,230,.75); font-weight:600; }

            .dpp-split { display:grid; grid-template-columns: minmax(0, 1fr) minmax(180px, 220px); gap:6px; flex:1; min-height:0; }
            .dpp-left  { display:flex; flex-direction:column; min-height:0; overflow:hidden; border:1px solid rgba(255,255,255,.04); border-radius:3px; }
            .dpp-right { display:flex; flex-direction:column; gap:6px; min-height:0; overflow-y:auto; }

            .dpp-table-head { display:grid; grid-template-columns: 48px 38px 54px 44px 44px 34px 34px 30px 54px; gap:2px; padding:4px 6px; font-size:8px; color:rgba(160,170,200,.5); letter-spacing:.4px; text-transform:uppercase; border-bottom:1px solid rgba(255,255,255,.05); }
            .dpp-table-body { flex:1; overflow-y:auto; }
            .dpp-row { display:grid; grid-template-columns: 48px 38px 54px 44px 44px 34px 34px 30px 54px; gap:2px; padding:2px 6px; font-size:9px; border-bottom:1px solid rgba(255,255,255,.02); }
            .dpp-row:nth-child(even) { background:rgba(255,255,255,.012); }
            .dpp-row span { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
            .dpp-side-C { color:#1fd17a; }
            .dpp-side-P { color:#e03060; }
            .dpp-aggr-BUY  { color:#1fd17a; }
            .dpp-aggr-SELL { color:#e03060; }
            .dpp-aggr-MID  { color:rgba(200,200,220,.45); }
            .dpp-aggr-     { color:rgba(180,180,200,.3); }

            .dpp-block { border:1px solid rgba(255,255,255,.04); border-radius:3px; padding:6px; }
            .dpp-block-title { font-size:8px; font-weight:600; color:rgba(160,170,200,.55); letter-spacing:.4px; text-transform:uppercase; margin-bottom:4px; }
            .dpp-kv { display:flex; justify-content:space-between; font-size:9px; padding:1px 0; }
            .dpp-kv-k { color:rgba(150,160,190,.55); }
            .dpp-kv-v { color:rgba(210,220,240,.85); font-weight:500; }

            .dpp-bar-row { display:grid; grid-template-columns: 50px 1fr 40px; gap:4px; align-items:center; font-size:8px; padding:1px 0; }
            .dpp-bar-label { color:rgba(150,160,190,.6); text-transform:uppercase; }
            .dpp-bar-track { height:6px; background:rgba(255,255,255,.03); border-radius:2px; overflow:hidden; }
            .dpp-bar-fill  { height:100%; background:rgba(100,160,220,.45); border-radius:2px; transition:width .4s ease; }
            .dpp-bar-val   { text-align:right; color:rgba(200,210,230,.7); }

            .dpp-empty { padding:20px; text-align:center; color:rgba(140,150,180,.4); font-size:10px; }
            .dpp-note { padding:4px 6px; font-size:8px; color:rgba(140,150,180,.38); font-style:italic; border-top:1px solid rgba(255,255,255,.03); }
        `;
        document.head.appendChild(_styleEl);
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _destroyed = false;
        _container.innerHTML = `
            <div class="dpp-wrap">
                <div class="dpp-header">
                    <span class="dpp-title">Dealer Prints · raw capture</span>
                    <span class="dpp-counters">
                        <span>N: <span class="dpp-counter-val" id="dpp-n">—</span></span>
                        <span>pending: <span class="dpp-counter-val" id="dpp-pending">—</span></span>
                        <span>logged: <span class="dpp-counter-val" id="dpp-logged">—</span></span>
                    </span>
                </div>
                <div class="dpp-split">
                    <div class="dpp-left">
                        <div class="dpp-table-head">
                            <span>TIME</span><span>SYM</span><span>STRK</span><span>PRICE</span>
                            <span>SIZE</span><span>VEN</span><span>AGG</span><span>LVL</span><span>BID/ASK</span>
                        </div>
                        <div class="dpp-table-body" id="dpp-body">
                            <div class="dpp-empty">waiting for prints…</div>
                        </div>
                    </div>
                    <div class="dpp-right" id="dpp-dists"></div>
                </div>
                <div class="dpp-note">
                    Population view. No thresholds, no labels. Once forward-sample rows accumulate, slice the disk log to find which combinations actually predict drift.
                </div>
            </div>
        `;
        _startPolling();
    }

    function destroy() {
        _destroyed = true;
        if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
        _container = null;
    }

    function _startPolling() {
        const tick = () => {
            if (_destroyed) return;
            Promise.all([
                fetch('/api/_debug/dealer_prints/summary?window_s=300').then(r => r.json()).catch(() => null),
                fetch('/api/_debug/dealer_prints/recent?n=40').then(r => r.json()).catch(() => null),
            ]).then(([summary, recent]) => {
                if (_destroyed) return;
                _renderSummary(summary || {});
                _renderRecent((recent && recent.prints) || []);
                _pollTimer = setTimeout(tick, 2000);
            });
        };
        tick();
    }

    function _fmtTime(ts) {
        try {
            const d = new Date(ts * 1000);
            const hh = String(d.getHours()).padStart(2, '0');
            const mm = String(d.getMinutes()).padStart(2, '0');
            const ss = String(d.getSeconds()).padStart(2, '0');
            return `${hh}:${mm}:${ss}`;
        } catch { return '—'; }
    }

    function _renderRecent(prints) {
        if (!_container) return;
        const body = _container.querySelector('#dpp-body');
        if (!body) return;
        if (!prints.length) {
            body.innerHTML = `<div class="dpp-empty">waiting for prints…</div>`;
            return;
        }
        const frag = document.createDocumentFragment();
        for (const p of prints) {
            const row = document.createElement('div');
            row.className = 'dpp-row';
            const sideCls = p.cp === 'C' ? 'dpp-side-C' : 'dpp-side-P';
            const aggCls = 'dpp-aggr-' + (p.aggressor || '');
            const strike = (p.strike || 0).toFixed(0);
            const price = (p.price || 0).toFixed(2);
            const size = p.size || 0;
            const ven = (p.exchange || '').slice(0, 4);
            const agg = (p.aggressor || '—').slice(0, 4);
            const lvl = (p.level_hit || '—').replace('+deep', '+d');
            const bid = p.bid1 ? p.bid1.p.toFixed(2) : '—';
            const ask = p.ask1 ? p.ask1.p.toFixed(2) : '—';
            row.innerHTML = `
                <span>${_fmtTime(p.ts)}</span>
                <span>${p.root || ''}</span>
                <span class="${sideCls}">${p.cp || ''} ${strike}</span>
                <span>${price}</span>
                <span>${size}</span>
                <span>${ven}</span>
                <span class="${aggCls}">${agg}</span>
                <span>${lvl}</span>
                <span>${bid}/${ask}</span>
            `;
            frag.appendChild(row);
        }
        body.innerHTML = '';
        body.appendChild(frag);
    }

    function _renderSummary(s) {
        if (!_container) return;
        const nEl = _container.querySelector('#dpp-n');
        const pendEl = _container.querySelector('#dpp-pending');
        const logEl = _container.querySelector('#dpp-logged');
        if (nEl) nEl.textContent = s.n != null ? s.n : '—';
        if (pendEl) pendEl.textContent = s.pending != null ? s.pending : '—';
        if (logEl) logEl.textContent = s.log_count != null ? s.log_count : '—';

        const dists = _container.querySelector('#dpp-dists');
        if (!dists) return;

        if (!s.n) {
            dists.innerHTML = `<div class="dpp-block"><div class="dpp-block-title">waiting</div><div class="dpp-kv"><span class="dpp-kv-k">no prints in window</span></div></div>`;
            return;
        }

        const sp = s.size_percentiles || {};
        const aggr = s.aggressor_mix || {};
        const aggrTotal = Math.max(1, (aggr.buy || 0) + (aggr.sell || 0) + (aggr.mid || 0) + (aggr.unknown || 0));
        const venues = s.venues || [];
        const levelMix = s.level_hit_mix || [];
        const venTotal = venues.reduce((a, [, v]) => a + v, 0) || 1;
        const lvlTotal = levelMix.reduce((a, [, v]) => a + v, 0) || 1;

        const sizeBlock = `
            <div class="dpp-block">
                <div class="dpp-block-title">Size distribution</div>
                <div class="dpp-kv"><span class="dpp-kv-k">p50</span><span class="dpp-kv-v">${sp.p50 || 0}</span></div>
                <div class="dpp-kv"><span class="dpp-kv-k">p75</span><span class="dpp-kv-v">${sp.p75 || 0}</span></div>
                <div class="dpp-kv"><span class="dpp-kv-k">p90</span><span class="dpp-kv-v">${sp.p90 || 0}</span></div>
                <div class="dpp-kv"><span class="dpp-kv-k">p95</span><span class="dpp-kv-v">${sp.p95 || 0}</span></div>
                <div class="dpp-kv"><span class="dpp-kv-k">p99</span><span class="dpp-kv-v">${sp.p99 || 0}</span></div>
                <div class="dpp-kv"><span class="dpp-kv-k">max</span><span class="dpp-kv-v">${sp.max || 0}</span></div>
            </div>`;

        const aggrRows = [['BUY', aggr.buy || 0], ['SELL', aggr.sell || 0], ['MID', aggr.mid || 0], ['UNK', aggr.unknown || 0]]
            .map(([k, v]) => {
                const pct = (v / aggrTotal * 100);
                return `<div class="dpp-bar-row">
                    <span class="dpp-bar-label">${k}</span>
                    <div class="dpp-bar-track"><div class="dpp-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
                    <span class="dpp-bar-val">${v}</span>
                </div>`;
            }).join('');
        const aggrBlock = `<div class="dpp-block"><div class="dpp-block-title">Aggressor mix</div>${aggrRows}</div>`;

        const venRows = venues.slice(0, 8).map(([k, v]) => {
            const pct = (v / venTotal * 100);
            return `<div class="dpp-bar-row">
                <span class="dpp-bar-label">${k || '?'}</span>
                <div class="dpp-bar-track"><div class="dpp-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
                <span class="dpp-bar-val">${v}</span>
            </div>`;
        }).join('');
        const venBlock = `<div class="dpp-block"><div class="dpp-block-title">Venue mix</div>${venRows || '<div class="dpp-kv"><span class="dpp-kv-k">—</span></div>'}</div>`;

        const lvlRows = levelMix.slice(0, 8).map(([k, v]) => {
            const pct = (v / lvlTotal * 100);
            return `<div class="dpp-bar-row">
                <span class="dpp-bar-label">${k || '?'}</span>
                <div class="dpp-bar-track"><div class="dpp-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
                <span class="dpp-bar-val">${v}</span>
            </div>`;
        }).join('');
        const lvlBlock = `<div class="dpp-block"><div class="dpp-block-title">Level hit</div>${lvlRows || '<div class="dpp-kv"><span class="dpp-kv-k">—</span></div>'}</div>`;

        dists.innerHTML = sizeBlock + aggrBlock + venBlock + lvlBlock;
    }

    return { init, destroy };
})();
window.DealerPrintsPane = DealerPrintsPane;
