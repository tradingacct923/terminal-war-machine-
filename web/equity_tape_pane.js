/**
 * EquityTapePane — Scrolling equity trade tape with MIC venue routing
 * Shows QQQ + SPY trades with execution venue, bid/ask venue attribution.
 * Dark pool (XADF/FINR) trades highlighted in purple.
 */
const EquityTapePane = (() => {
    'use strict';

    const MAX_ROWS = 200;
    const LARGE_SIZE = 500;
    const DARK_POOLS = new Set(['XADF', 'XDKP', 'FINR', 'FADF']);

    // Venue display names (shorter for table)
    const VENUE_SHORT = {
        'XNAS': 'NSDQ', 'ARCX': 'ARCA', 'BATS': 'BATS', 'EDGX': 'EDGX',
        'IEGX': 'IEX',  'XADF': 'DARK', 'MEMX': 'MEMX', 'XNYS': 'NYSE',
        'XBOS': 'BX',   'XPHL': 'PHLX', 'XCIS': 'NSX',  'FINR': 'DARK',
    };

    let _container = null;
    let _tbody = null;
    let _styleEl = null;
    let _rowCount = 0;

    function _injectStyles() {
        if (document.getElementById('eqtape-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'eqtape-styles';
        _styleEl.textContent = `
            .eqt-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; }
            .eqt-hdr { display:flex; align-items:center; padding:4px 8px; border-bottom:1px solid rgba(255,255,255,.04); gap:6px; }
            .eqt-hdr-title { font-size:9px; font-weight:600; color:rgba(180,190,220,.7); letter-spacing:.8px; text-transform:uppercase; }
            .eqt-hdr-count { font-size:8px; color:rgba(140,150,180,.4); margin-left:auto; }
            .eqt-scroll { flex:1; overflow-y:auto; overflow-x:hidden; }
            .eqt-scroll::-webkit-scrollbar { width:3px; }
            .eqt-scroll::-webkit-scrollbar-thumb { background:rgba(124,90,247,.3); border-radius:2px; }
            .eqt-table { width:100%; border-collapse:collapse; font-size:9px; }
            .eqt-table thead { position:sticky; top:0; z-index:1; }
            .eqt-table th { padding:3px 5px; text-align:left; font-size:7.5px; font-weight:500; color:rgba(140,150,180,.5);
                            background:#0a0d18; border-bottom:1px solid rgba(255,255,255,.03); letter-spacing:.5px; text-transform:uppercase; }
            .eqt-table td { padding:2px 5px; border-bottom:1px solid rgba(255,255,255,.015); white-space:nowrap; color:rgba(180,190,220,.7); }
            .eqt-row-buy td { color:#1fd17a; }
            .eqt-row-sell td { color:#e03060; }
            .eqt-row-dark td.eqt-exec { color:#7c5af7; font-weight:600; }
            .eqt-row-large { font-weight:700; font-size:10px; }
            .eqt-row-large td { border-left:2px solid rgba(255,200,0,.5); padding-left:4px; }
            .eqt-venue { font-size:8px; opacity:.7; }
            .eqt-size-lg { color:#ffd700 !important; }
            .eqt-flash { animation: eqt-flash-in .3s ease-out; }
            @keyframes eqt-flash-in { from { background:rgba(124,90,247,.15); } to { background:transparent; } }
        `;
        document.head.appendChild(_styleEl);
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _rowCount = 0;
        _container.innerHTML = `
            <div class="eqt-wrap">
                <div class="eqt-hdr">
                    <span class="eqt-hdr-title">Equity Tape &middot; Venue Routing</span>
                    <span class="eqt-hdr-count" id="eqt-count">0 prints</span>
                </div>
                <div class="eqt-scroll">
                    <table class="eqt-table">
                        <thead><tr>
                            <th>Time</th><th>Sym</th><th>Price</th><th>Size</th><th>Side</th>
                            <th>Exec</th><th>Bid@</th><th>Ask@</th>
                        </tr></thead>
                        <tbody id="eqt-tbody"></tbody>
                    </table>
                </div>
            </div>`;
        _tbody = _container.querySelector('#eqt-tbody');
    }

    function destroy() {
        _container = null;
        _tbody = null;
        _rowCount = 0;
    }

    function _venueLabel(mic) {
        return VENUE_SHORT[mic] || mic || '—';
    }

    function onTick(data) {
        if (!_tbody) return;
        const { symbol, size, side, exec_mic, bid_mic, ask_mic, ts } = data;
        const price = typeof data.price === 'number' ? data.price : parseFloat(data.price) || 0;
        const isDark = DARK_POOLS.has(exec_mic);
        const isLarge = (size || 0) >= LARGE_SIZE;
        const isBuy = side === 'b';
        const isSell = side === 's';

        const tr = document.createElement('tr');
        let cls = 'eqt-flash';
        if (isBuy) cls += ' eqt-row-buy';
        else if (isSell) cls += ' eqt-row-sell';
        if (isLarge) cls += ' eqt-row-large';
        if (isDark) cls += ' eqt-row-dark';
        tr.className = cls;

        const t = new Date(ts * 1000);
        const timeStr = t.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const sideStr = isBuy ? 'BUY' : isSell ? 'SELL' : '—';
        const sizeClass = isLarge ? ' class="eqt-size-lg"' : '';

        tr.innerHTML = `
            <td>${timeStr}</td>
            <td>${symbol}</td>
            <td>$${price.toFixed(2)}</td>
            <td${sizeClass}>${size.toLocaleString()}</td>
            <td>${sideStr}</td>
            <td class="eqt-exec">${_venueLabel(exec_mic)}${isDark ? ' <span style="color:#7c5af7;font-size:7px">DP</span>' : ''}</td>
            <td class="eqt-venue">${_venueLabel(bid_mic)}</td>
            <td class="eqt-venue">${_venueLabel(ask_mic)}</td>`;

        _tbody.prepend(tr);
        _rowCount++;

        // Prune
        while (_tbody.children.length > MAX_ROWS) {
            _tbody.removeChild(_tbody.lastChild);
        }

        // Update count
        const countEl = _container && _container.querySelector('#eqt-count');
        if (countEl) countEl.textContent = `${_rowCount} prints`;
    }

    return { init, destroy, onTick };
})();
