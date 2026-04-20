/**
 * MoversPane — Index-scoped top gainers/losers from Schwab /movers.
 * Consumes /api/movers REST (5-min cache backend). Polls on interval.
 * Index selectors: $SPX, $DJI, $COMPX, $IUXX, INDEX_ALL.
 * Sort: PERCENT_CHANGE_UP (gainers) / PERCENT_CHANGE_DOWN (losers) / VOLUME.
 */
const MoversPane = (() => {
    'use strict';

    const POLL_MS = 60000;           // 1 min (server caches 5 min)
    const INDEXES = [
        ['$SPX',      'SPX'],
        ['$DJI',      'DJI'],
        ['$COMPX',    'NDX'],
        ['$IUXX',     'RUT'],
        ['INDEX_ALL', 'ALL'],
    ];

    let _container = null;
    let _tbody = null;
    let _styleEl = null;
    let _indexSel = '$SPX';
    let _sortSel = 'PERCENT_CHANGE_UP';
    let _pollTimer = 0;
    let _lastFetchMs = 0;
    let _lastMovers = [];

    function _injectStyles() {
        if (document.getElementById('movers-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'movers-styles';
        _styleEl.textContent = `
            .mv-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; color:rgba(220,225,235,.9); }
            .mv-hdr { display:flex; align-items:center; padding:6px 10px; border-bottom:1px solid rgba(255,255,255,.04); gap:6px; flex-wrap:wrap; }
            .mv-title { font-size:10px; font-weight:600; color:rgba(180,190,220,.75); letter-spacing:.8px; text-transform:uppercase; }
            .mv-btns { display:flex; gap:3px; margin-left:8px; }
            .mv-btn { background:rgba(30,35,55,.7); color:rgba(180,190,220,.75); border:1px solid rgba(255,255,255,.05); padding:2px 8px; font:inherit; font-size:9px; letter-spacing:.5px; cursor:pointer; border-radius:3px; }
            .mv-btn:hover { background:rgba(60,70,100,.85); color:#fff; }
            .mv-btn.active { background:rgba(124,90,247,.25); border-color:rgba(124,90,247,.6); color:#c9b5ff; }
            .mv-sort-btns { display:flex; gap:3px; margin-left:auto; }
            .mv-ts { font-size:8.5px; color:rgba(140,150,180,.5); margin-left:auto; }
            .mv-scroll { flex:1; overflow-y:auto; overflow-x:hidden; }
            .mv-scroll::-webkit-scrollbar { width:3px; }
            .mv-scroll::-webkit-scrollbar-thumb { background:rgba(124,90,247,.3); border-radius:2px; }
            .mv-table { width:100%; border-collapse:collapse; font-size:10px; }
            .mv-table thead { position:sticky; top:0; z-index:1; }
            .mv-table th { padding:4px 6px; text-align:left; font-size:8px; font-weight:500; color:rgba(140,150,180,.5);
                           background:#0a0d18; border-bottom:1px solid rgba(255,255,255,.03); letter-spacing:.5px; text-transform:uppercase; }
            .mv-table td { padding:3px 6px; border-bottom:1px solid rgba(255,255,255,.02); white-space:nowrap; }
            .mv-sym { color:#c9b5ff; font-weight:600; }
            .mv-desc { color:rgba(180,190,220,.5); font-size:9px; max-width:140px; overflow:hidden; text-overflow:ellipsis; }
            .mv-up { color:#1fd17a; }
            .mv-dn { color:#e03060; }
            .mv-num { text-align:right; font-variant-numeric:tabular-nums; }
            .mv-empty { padding:16px; text-align:center; color:rgba(140,150,180,.45); font-size:10px; }
            .mv-err { padding:12px; text-align:center; color:#e03060; font-size:9px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _fmtVol(v) {
        if (v == null) return '—';
        if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
        if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
        if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
        return String(v);
    }

    function _fmtPct(p) {
        if (p == null) return '—';
        const v = p * 100;
        const sign = v >= 0 ? '+' : '';
        return `${sign}${v.toFixed(2)}%`;
    }

    function _fmtNum(n, d = 2) {
        if (n == null) return '—';
        return n.toFixed(d);
    }

    function _buildHeader() {
        const idxBtns = INDEXES.map(([val, label]) =>
            `<button class="mv-btn${val === _indexSel ? ' active' : ''}" data-idx="${val}">${label}</button>`
        ).join('');
        const sortBtns = [
            ['PERCENT_CHANGE_UP',   '↑ Gain'],
            ['PERCENT_CHANGE_DOWN', '↓ Loss'],
            ['VOLUME',              'Vol'],
        ].map(([val, label]) =>
            `<button class="mv-btn${val === _sortSel ? ' active' : ''}" data-sort="${val}">${label}</button>`
        ).join('');
        return `
            <div class="mv-hdr">
                <span class="mv-title">Movers</span>
                <div class="mv-btns">${idxBtns}</div>
                <div class="mv-sort-btns">${sortBtns}</div>
                <span class="mv-ts" id="mv-ts">loading…</span>
            </div>`;
    }

    function _renderRows() {
        if (!_tbody) return;
        if (!_lastMovers.length) {
            _tbody.innerHTML = `<tr><td colspan="5" class="mv-empty">No movers data</td></tr>`;
            return;
        }
        const rows = _lastMovers.map(m => {
            const pct = m.netPercentChange;
            const up = (pct != null && pct >= 0);
            const cls = up ? 'mv-up' : 'mv-dn';
            return `
                <tr>
                    <td><span class="mv-sym">${m.symbol || '—'}</span></td>
                    <td class="mv-desc" title="${m.description || ''}">${m.description || ''}</td>
                    <td class="mv-num">${_fmtNum(m.lastPrice)}</td>
                    <td class="mv-num ${cls}">${_fmtNum(m.netChange)}</td>
                    <td class="mv-num ${cls}">${_fmtPct(pct)}</td>
                    <td class="mv-num">${_fmtVol(m.volume != null ? m.volume : m.totalVolume)}</td>
                </tr>`;
        }).join('');
        _tbody.innerHTML = rows;
    }

    function _updateTs(cachedAge) {
        const ts = document.getElementById('mv-ts');
        if (!ts) return;
        if (cachedAge == null) {
            ts.textContent = '—';
        } else if (cachedAge === 0) {
            ts.textContent = 'fresh';
        } else {
            ts.textContent = `cached ${cachedAge}s`;
        }
    }

    function _fetchMovers() {
        const tok = sessionStorage.getItem('greeks-auth') || '';
        const url = `/api/movers?index=${encodeURIComponent(_indexSel)}&sort=${encodeURIComponent(_sortSel)}`;
        fetch(url, { headers: tok ? { 'X-Auth-Token': tok } : {} })
            .then(r => r.json())
            .then(d => {
                if (!_container) return;  // unmounted mid-flight
                if (d.error) {
                    _tbody.innerHTML = `<tr><td colspan="5" class="mv-err">${d.error}</td></tr>`;
                    _updateTs(null);
                    return;
                }
                let movers = Array.isArray(d.movers) ? d.movers : [];
                // Schwab's PERCENT_CHANGE_UP/DOWN returns "top movers" by magnitude,
                // including wrong-sign names. Post-filter to match requested direction.
                if (_sortSel === 'PERCENT_CHANGE_UP') {
                    movers = movers.filter(m => (m.netPercentChange ?? 0) >= 0);
                } else if (_sortSel === 'PERCENT_CHANGE_DOWN') {
                    movers = movers.filter(m => (m.netPercentChange ?? 0) < 0);
                }
                _lastMovers = movers;
                _lastFetchMs = performance.now();
                _renderRows();
                _updateTs(d.cached_age);
            })
            .catch(e => {
                if (!_tbody) return;
                _tbody.innerHTML = `<tr><td colspan="5" class="mv-err">${e.message || 'fetch failed'}</td></tr>`;
                _updateTs(null);
            });
    }

    function _onHeaderClick(ev) {
        const idxBtn = ev.target.closest('button[data-idx]');
        if (idxBtn) {
            const val = idxBtn.dataset.idx;
            if (val !== _indexSel) {
                _indexSel = val;
                _container.querySelector('.mv-hdr').outerHTML = _buildHeader();
                _container.querySelector('.mv-hdr').addEventListener('click', _onHeaderClick);
                _fetchMovers();
            }
            return;
        }
        const sortBtn = ev.target.closest('button[data-sort]');
        if (sortBtn) {
            const val = sortBtn.dataset.sort;
            if (val !== _sortSel) {
                _sortSel = val;
                _container.querySelector('.mv-hdr').outerHTML = _buildHeader();
                _container.querySelector('.mv-hdr').addEventListener('click', _onHeaderClick);
                _fetchMovers();
            }
        }
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _container.innerHTML = `
            <div class="mv-wrap">
                ${_buildHeader()}
                <div class="mv-scroll">
                    <table class="mv-table">
                        <thead><tr>
                            <th>Sym</th><th>Name</th><th class="mv-num">Last</th>
                            <th class="mv-num">Chg</th><th class="mv-num">%Chg</th><th class="mv-num">Vol</th>
                        </tr></thead>
                        <tbody id="mv-tbody"></tbody>
                    </table>
                </div>
            </div>`;
        _tbody = _container.querySelector('#mv-tbody');
        _container.querySelector('.mv-hdr').addEventListener('click', _onHeaderClick);
        _fetchMovers();
        _pollTimer = setInterval(_fetchMovers, POLL_MS);
    }

    function destroy() {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = 0; }
        _container = null;
        _tbody = null;
        _lastMovers = [];
    }

    return { init, destroy };
})();
