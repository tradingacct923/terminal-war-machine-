/**
 * OptionsFlowPane — Live options flow feed filtered for significant moves
 * Shows mark changes, IV, delta, and GEX impact per contract.
 * Consumes option_mark_update events (500ms throttle per contract).
 */
const OptionsFlowPane = (() => {
    'use strict';

    const MAX_ROWS = 150;
    const MARK_THRESH = 0.05;  // min |mark_change| to show
    const GEX_THRESH = 0.5;    // min |dollar_gex| in $M to show

    let _container = null;
    let _tbody = null;
    let _styleEl = null;
    let _rowCount = 0;

    function _injectStyles() {
        if (document.getElementById('optflow-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'optflow-styles';
        _styleEl.textContent = `
            .of-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; }
            .of-hdr { display:flex; align-items:center; padding:4px 8px; border-bottom:1px solid rgba(255,255,255,.04); gap:6px; }
            .of-hdr-title { font-size:9px; font-weight:600; color:rgba(180,190,220,.7); letter-spacing:.8px; text-transform:uppercase; }
            .of-hdr-count { font-size:8px; color:rgba(140,150,180,.4); margin-left:auto; }
            .of-scroll { flex:1; overflow-y:auto; overflow-x:hidden; }
            .of-scroll::-webkit-scrollbar { width:3px; }
            .of-scroll::-webkit-scrollbar-thumb { background:rgba(124,90,247,.3); border-radius:2px; }
            .of-table { width:100%; border-collapse:collapse; font-size:9px; }
            .of-table thead { position:sticky; top:0; z-index:1; }
            .of-table th { padding:3px 5px; text-align:left; font-size:7.5px; font-weight:500; color:rgba(140,150,180,.5);
                           background:#0a0d18; border-bottom:1px solid rgba(255,255,255,.03); letter-spacing:.5px; text-transform:uppercase; }
            .of-table td { padding:2px 5px; border-bottom:1px solid rgba(255,255,255,.015); white-space:nowrap; }
            .of-call td { color:#1fd17a; }
            .of-put td { color:#e03060; }
            .of-gex-hi { border-left:2px solid rgba(124,90,247,.6) !important; }
            .of-mark-flash { animation: of-flash .5s ease-out; }
            @keyframes of-flash { from { background:rgba(255,200,0,.12); } to { background:transparent; } }
            .of-oi-badge { background:rgba(255,200,0,.12); color:#ffd700; padding:0 3px; border-radius:2px; font-size:8px; font-weight:600; }
            .of-chg-up { color:#1fd17a !important; }
            .of-chg-dn { color:#e03060 !important; }
            .of-iv { color:rgba(168,85,247,.8) !important; }
            .of-gex-val { color:rgba(124,90,247,.9) !important; font-weight:600; }
        `;
        document.head.appendChild(_styleEl);
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _rowCount = 0;
        _container.innerHTML = `
            <div class="of-wrap">
                <div class="of-hdr">
                    <span class="of-hdr-title">Options Flow</span>
                    <span class="of-hdr-count" id="of-count">0 prints</span>
                </div>
                <div class="of-scroll">
                    <table class="of-table">
                        <thead><tr>
                            <th>Time</th><th>Strike</th><th>C/P</th><th>Mark</th><th>Chg</th>
                            <th>IV</th><th>Delta</th><th>GEX$M</th>
                        </tr></thead>
                        <tbody id="of-tbody"></tbody>
                    </table>
                </div>
            </div>`;
        _tbody = _container.querySelector('#of-tbody');
    }

    function destroy() {
        _container = null;
        _tbody = null;
        _rowCount = 0;
    }

    function onOptionMark(data) {
        if (!_tbody) return;

        const markChg = data.mark_change || 0;
        const dollarGex = data.dollar_gex || 0;
        const gexM = dollarGex;  // backend already sends in $M

        // Filter noise
        if (Math.abs(markChg) < MARK_THRESH && Math.abs(gexM) < GEX_THRESH) return;

        const side = data.side || '';
        const isCall = side === 'C' || side === 'call';
        const isPut = side === 'P' || side === 'put';
        const strike = Number(data.strike) || 0;
        const mark = data.mark || 0;
        const iv = (data.iv || 0);
        const delta = data.delta || 0;
        const oi = data.oi || 0;
        const bigGex = Math.abs(gexM) > 1.0;
        const bigChg = Math.abs(markChg) > 0.20;
        const bigOi = oi > 10000;

        const tr = document.createElement('tr');
        let cls = '';
        if (isCall) cls += 'of-call';
        else if (isPut) cls += 'of-put';
        if (bigGex) cls += ' of-gex-hi';
        if (bigChg) cls += ' of-mark-flash';
        tr.className = cls;

        const now = new Date();
        const timeStr = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const chgCls = markChg > 0 ? 'of-chg-up' : markChg < 0 ? 'of-chg-dn' : '';
        const chgSign = markChg > 0 ? '+' : '';
        const oiBadge = bigOi ? `<span class="of-oi-badge">${(oi / 1000).toFixed(1)}K</span>` : '';

        tr.innerHTML = `
            <td>${timeStr}</td>
            <td>${strike.toFixed(0)} ${oiBadge}</td>
            <td>${isCall ? 'C' : isPut ? 'P' : '?'}</td>
            <td>$${mark.toFixed(2)}</td>
            <td class="${chgCls}">${chgSign}${markChg.toFixed(2)}</td>
            <td class="of-iv">${iv.toFixed(1)}%</td>
            <td>${delta.toFixed(3)}</td>
            <td class="of-gex-val">${gexM >= 0 ? '+' : ''}${gexM.toFixed(2)}</td>`;

        _tbody.prepend(tr);
        _rowCount++;

        while (_tbody.children.length > MAX_ROWS) {
            _tbody.removeChild(_tbody.lastChild);
        }

        const countEl = _container && _container.querySelector('#of-count');
        if (countEl) countEl.textContent = `${_rowCount} prints`;
    }

    return { init, destroy, onOptionMark };
})();
