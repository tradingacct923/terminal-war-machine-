/**
 * OptionsFlowPane — Live options flow feed filtered for significant moves
 * Shows mark changes, IV, delta, and GEX impact per contract.
 * Consumes option_mark_update events (500ms throttle per contract).
 *
 * Enhancements:
 *   • Prints-only toggle   — firehose mode vs. real-tape mode
 *   • 60s scoreboard strip — per-ticker signed premium / C/P ratio / IV Δ
 */
const OptionsFlowPane = (() => {
    'use strict';

    const MAX_ROWS = 150;
    // Show a row if ANY of these is true:
    //   • $ premium paid on the print ≥ PREMIUM_THRESH (real money)
    //   • |mark %change| ≥ MARK_PCT_THRESH (wing-option percent move, scale-invariant)
    //   • |dollar_gex| ≥ GEX_THRESH (dealer impact)
    const PREMIUM_THRESH  = 10_000;
    const MARK_PCT_THRESH = 0.15;
    const GEX_THRESH      = 0.5;

    const SCOREBOARD_TICKERS = ['QQQ', 'SPX', 'SPY'];
    const WINDOW_MS = 60_000;

    let _container = null;
    let _tbody = null;
    let _styleEl = null;
    let _rowCount = 0;

    let _printsOnly = false;
    // _tickerStats[ticker] = { prints: [{ts, signedPrem, side, iv}], lastIvNow: n }
    let _tickerStats = {};
    let _scoreTimerId = null;
    let _unsubMark = null;
    let _unsubTrade = null;

    function _ensureStats(t) {
        if (!_tickerStats[t]) _tickerStats[t] = { prints: [], lastIvNow: 0 };
        return _tickerStats[t];
    }

    function _injectStyles() {
        if (document.getElementById('optflow-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'optflow-styles';
        _styleEl.textContent = `
            .of-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; }
            .of-hdr { display:flex; align-items:center; padding:4px 8px; border-bottom:1px solid rgba(255,255,255,.04); gap:8px; }
            .of-hdr-title { font-size:9px; font-weight:600; color:rgba(180,190,220,.7); letter-spacing:.8px; text-transform:uppercase; }
            .of-hdr-count { font-size:8px; color:rgba(140,150,180,.4); margin-left:auto; }
            .of-toggle { display:inline-flex; border:1px solid rgba(124,90,247,.35); border-radius:3px; overflow:hidden; font-size:8px; }
            .of-toggle button { background:transparent; color:rgba(180,190,220,.6); border:0; padding:2px 6px; cursor:pointer;
                                font-family:inherit; letter-spacing:.5px; text-transform:uppercase; transition:background .15s, color .15s; }
            .of-toggle button.active { background:rgba(124,90,247,.35); color:#fff; }
            .of-toggle button:hover:not(.active) { background:rgba(124,90,247,.12); color:rgba(220,225,235,.85); }

            .of-score { display:flex; gap:6px; padding:3px 6px; border-bottom:1px solid rgba(255,255,255,.05);
                        background:linear-gradient(90deg, rgba(124,90,247,.05), rgba(0,220,255,.02)); flex-wrap:nowrap; }
            .of-score-cell { flex:1 1 0; min-width:0; display:flex; flex-direction:column; gap:1px; padding:2px 5px;
                             background:rgba(10,13,24,.6); border-radius:3px; border:1px solid rgba(255,255,255,.03); }
            .of-score-tck { font-size:8px; font-weight:700; color:#d8c78c; letter-spacing:.5px; display:flex; align-items:center; gap:4px; }
            .of-score-tck-badge { font-size:7px; color:rgba(140,150,180,.5); font-weight:500; letter-spacing:.3px; text-transform:uppercase; }
            .of-score-row { display:flex; gap:6px; align-items:center; font-size:8.5px; }
            .of-score-row b { font-weight:600; }
            .of-score-prem { font-weight:700; }
            .of-score-prem.pos { color:#1fd17a; }
            .of-score-prem.neg { color:#e03060; }
            .of-score-prem.neutral { color:rgba(180,190,220,.6); }
            .of-score-cp { color:rgba(168,85,247,.85); }
            .of-score-iv { color:rgba(200,210,230,.7); }
            .of-score-iv.pos { color:#1fd17a; }
            .of-score-iv.neg { color:#e03060; }
            .of-score-label { font-size:7px; color:rgba(140,150,180,.45); letter-spacing:.3px; text-transform:uppercase; }

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
            .of-ticker { color:#d8c78c !important; font-weight:600; }
            .of-dte-0 { color:#ff6e6e !important; font-weight:700; }
            .of-dte-short { color:#ffd700 !important; }
            .of-dte-long  { color:rgba(140,150,180,.7) !important; }
            .of-prem-hi { color:#ffd700 !important; font-weight:600; }
            .of-prem-mid { color:rgba(220,225,235,.85) !important; }
            .of-aggr-buy  { background:rgba(31,209,122,.22); color:#1fd17a; padding:0 4px; border-radius:2px; font-weight:700; }
            .of-aggr-sell { background:rgba(224,48,96,.22); color:#e03060; padding:0 4px; border-radius:2px; font-weight:700; }
            .of-aggr-mid  { color:rgba(140,150,180,.6); }
            .of-voi-new   { background:rgba(31,209,122,.2); color:#1fd17a; padding:0 3px; border-radius:2px;
                            font-weight:700; font-size:7.5px; letter-spacing:.3px; margin-right:2px; }
            .of-voi-close { color:rgba(224,48,96,.7); }
            .of-voi-mid   { color:rgba(200,210,230,.7); }
            .of-voi-none  { color:rgba(140,150,180,.35); }
            .of-venue     { display:inline-block; margin-left:4px; padding:0 3px; border-radius:2px;
                            background:rgba(140,150,180,.14); color:rgba(200,210,230,.8);
                            font-size:7.5px; font-weight:600; letter-spacing:.3px; }
            .of-ah        { display:inline-block; margin-left:3px; padding:0 3px; border-radius:2px;
                            background:rgba(240,160,64,.18); color:#f0a040;
                            font-size:7px; font-weight:700; letter-spacing:.3px; }
            .of-trade td:first-child { box-shadow: inset 2px 0 0 rgba(0,220,255,.75); }
            .of-trade .of-trade-tag { color:#00dcff; font-weight:700; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildScoreboardHtml() {
        let html = '';
        for (const t of SCOREBOARD_TICKERS) {
            html += `
                <div class="of-score-cell" data-score-ticker="${t}">
                    <div class="of-score-tck">${t}<span class="of-score-tck-badge">60s</span></div>
                    <div class="of-score-row">
                        <span class="of-score-label">Δ$</span>
                        <b class="of-score-prem neutral" data-field="prem">—</b>
                    </div>
                    <div class="of-score-row">
                        <span class="of-score-label">C/P</span>
                        <b class="of-score-cp" data-field="cp">—</b>
                        <span class="of-score-label" style="margin-left:4px">IV</span>
                        <b class="of-score-iv" data-field="iv">—</b>
                    </div>
                </div>`;
        }
        return html;
    }

    function _renderScoreboard() {
        if (!_container) return;
        const scoreEl = _container.querySelector('.of-score');
        if (!scoreEl) return;
        const now = Date.now();
        for (const t of SCOREBOARD_TICKERS) {
            const st = _ensureStats(t);
            // Evict stale
            while (st.prints.length && now - st.prints[0].ts > WINDOW_MS) st.prints.shift();

            let signedPrem = 0, calls = 0, puts = 0, ivSum = 0, ivN = 0, ivOldSum = 0, ivOldN = 0;
            const cutoff = now - WINDOW_MS;
            const halfCut = now - WINDOW_MS / 2;
            for (const p of st.prints) {
                signedPrem += p.signedPrem;
                if (p.side === 'C') calls++;
                else if (p.side === 'P') puts++;
                if (p.iv > 0) {
                    if (p.ts >= halfCut) { ivSum += p.iv; ivN++; }
                    else { ivOldSum += p.iv; ivOldN++; }
                }
            }
            const ivNow = ivN ? ivSum / ivN : 0;
            const ivOld = ivOldN ? ivOldSum / ivOldN : 0;
            const ivDelta = (ivNow && ivOld) ? ivNow - ivOld : 0;

            const cell = scoreEl.querySelector(`[data-score-ticker="${t}"]`);
            if (!cell) continue;
            const premEl = cell.querySelector('[data-field="prem"]');
            const cpEl   = cell.querySelector('[data-field="cp"]');
            const ivEl   = cell.querySelector('[data-field="iv"]');

            let premStr = '—', premCls = 'neutral';
            if (st.prints.length) {
                const sign = signedPrem >= 0 ? '+' : '-';
                const abs = Math.abs(signedPrem);
                const val = abs >= 1_000_000 ? `$${(abs / 1_000_000).toFixed(2)}M`
                          : abs >= 1_000     ? `$${(abs / 1_000).toFixed(0)}k`
                          : `$${abs.toFixed(0)}`;
                premStr = `${sign}${val}`;
                premCls = signedPrem > 0 ? 'pos' : signedPrem < 0 ? 'neg' : 'neutral';
            }
            premEl.textContent = premStr;
            premEl.className = `of-score-prem ${premCls}`;

            const total = calls + puts;
            cpEl.textContent = total > 0 ? `${calls}/${puts}` : '—';

            let ivStr = '—', ivCls = '';
            if (ivDelta !== 0) {
                const sign = ivDelta > 0 ? '+' : '';
                ivStr = `${sign}${ivDelta.toFixed(2)}%`;
                ivCls = ivDelta > 0 ? 'pos' : 'neg';
            } else if (ivNow > 0) {
                ivStr = `${ivNow.toFixed(1)}%`;
            }
            ivEl.textContent = ivStr;
            ivEl.className = `of-score-iv ${ivCls}`;
        }
    }

    function _setPrintsOnly(flag) {
        _printsOnly = !!flag;
        const allBtn = _container && _container.querySelector('[data-of-mode="all"]');
        const prBtn  = _container && _container.querySelector('[data-of-mode="prints"]');
        if (allBtn) allBtn.classList.toggle('active', !_printsOnly);
        if (prBtn)  prBtn.classList.toggle('active',  _printsOnly);
        // Strip non-trade rows when flipping to prints-only
        if (_printsOnly && _tbody) {
            const kill = [];
            for (const tr of _tbody.children) {
                if (!tr.classList.contains('of-trade')) kill.push(tr);
            }
            for (const tr of kill) _tbody.removeChild(tr);
            _rowCount = _tbody.children.length;
            const countEl = _container.querySelector('#of-count');
            if (countEl) countEl.textContent = `${_rowCount} prints`;
        }
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _rowCount = 0;
        _tickerStats = {};
        _container.innerHTML = `
            <div class="of-wrap">
                <div class="of-hdr">
                    <span class="of-hdr-title">Options Flow</span>
                    <div class="of-toggle">
                        <button data-of-mode="all" class="active">All</button>
                        <button data-of-mode="prints">Prints</button>
                    </div>
                    <span class="of-hdr-count" id="of-count">0 prints</span>
                </div>
                <div class="of-score">${_buildScoreboardHtml()}</div>
                <div class="of-scroll">
                    <table class="of-table">
                        <thead><tr>
                            <th>Time</th><th>Ticker</th><th>Strike</th><th>C/P</th><th>DTE</th>
                            <th>Last</th><th>%Chg</th><th>Size</th><th>Prem$</th><th>Aggr</th>
                            <th>IV</th><th>&Delta;</th><th>GEX$M</th><th>V/OI</th>
                        </tr></thead>
                        <tbody id="of-tbody"></tbody>
                    </table>
                </div>
            </div>`;
        _tbody = _container.querySelector('#of-tbody');

        const allBtn = _container.querySelector('[data-of-mode="all"]');
        const prBtn  = _container.querySelector('[data-of-mode="prints"]');
        if (allBtn) allBtn.addEventListener('click', () => _setPrintsOnly(false));
        if (prBtn)  prBtn.addEventListener('click',  () => _setPrintsOnly(true));

        _renderScoreboard();
        if (_scoreTimerId) clearInterval(_scoreTimerId);
        _scoreTimerId = setInterval(_renderScoreboard, 1000);

        // Subscribe to AltarisEvents bus so the pane works regardless of
        // mount path (layout system in app.js or LIVE_TILES in index.html).
        // Backend emits option_mark_batch / option_trade_batch (50ms coalesced);
        // index.html fans them out per-update onto data:option:mark / :trade.
        if (window.AltarisEvents && typeof window.AltarisEvents.on === 'function') {
            const hMark  = (d) => onOptionMark(d);
            const hTrade = (d) => onOptionTrade(d);
            window.AltarisEvents.on('data:option:mark',  hMark);
            window.AltarisEvents.on('data:option:trade', hTrade);
            _unsubMark  = () => { try { window.AltarisEvents.off('data:option:mark',  hMark);  } catch (_) {} };
            _unsubTrade = () => { try { window.AltarisEvents.off('data:option:trade', hTrade); } catch (_) {} };
        }
    }

    function destroy() {
        if (_scoreTimerId) { clearInterval(_scoreTimerId); _scoreTimerId = null; }
        if (_unsubMark)  { _unsubMark();  _unsubMark  = null; }
        if (_unsubTrade) { _unsubTrade(); _unsubTrade = null; }
        _container = null;
        _tbody = null;
        _rowCount = 0;
        _tickerStats = {};
    }

    function _appendRow(data, opts) {
        if (!_tbody) return;
        const isRealTrade = !!(opts && opts.isRealTrade);

        const markChg = data.mark_change || 0;
        const dollarGex = data.dollar_gex || 0;
        const gexM = dollarGex;
        const mark = data.mark || 0;
        const premium = data.premium || 0;
        const markPct = (mark > 0 && markChg !== 0) ? Math.abs(markChg / mark) : 0;

        const ticker = String(data.ticker || '').trim();
        const side = data.side || '';
        const isCall = side === 'C' || side === 'call';
        const isPut = side === 'P' || side === 'put';

        // Feed scoreboard — real prints only, only for tracked tickers
        if (isRealTrade && SCOREBOARD_TICKERS.includes(ticker) && premium > 0) {
            const aggrUp = String(data.aggressor || '').toUpperCase();
            const sign = aggrUp === 'BUY' ? +1 : aggrUp === 'SELL' ? -1 : 0;
            const st = _ensureStats(ticker);
            st.prints.push({
                ts: Date.now(),
                signedPrem: sign * premium,
                side: isCall ? 'C' : isPut ? 'P' : '?',
                iv: Number(data.iv) || 0,
            });
            if (st.prints.length > 2000) st.prints.splice(0, st.prints.length - 2000);
        }

        // Mark-shift rows still gate on thresholds. Real trades always pass.
        if (!isRealTrade) {
            if (_printsOnly) return;
            const keep =
                premium  >= PREMIUM_THRESH ||
                markPct  >= MARK_PCT_THRESH ||
                Math.abs(gexM) >= GEX_THRESH;
            if (!keep) return;
        }

        const strike = Number(data.strike) || 0;
        const iv = (data.iv || 0);
        const delta = data.delta || 0;
        const oi = data.oi || 0;
        const vol = data.vol || 0;
        const voiRatio = oi > 0 ? vol / oi : 0;
        const dte = Number(data.dte || 0);
        const lastSize = data.last_size || 0;
        const aggr = String(data.aggressor || '').toUpperCase();
        const last = data.last || mark;
        const bigGex = Math.abs(gexM) > 1.0;
        const bigPrem = premium >= 100_000;
        const bigOi = oi > 10000;

        const tr = document.createElement('tr');
        let cls = '';
        if (isCall) cls += 'of-call';
        else if (isPut) cls += 'of-put';
        if (bigGex) cls += ' of-gex-hi';
        if (bigPrem) cls += ' of-mark-flash';
        if (isRealTrade) cls += ' of-trade';
        tr.className = cls;

        const tMs = Number(data.trade_time_ms) || Date.now();
        const timeStr = new Date(tMs).toLocaleTimeString('en-US', {
            hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
            fractionalSecondDigits: 3
        });
        const pctCls  = markChg > 0 ? 'of-chg-up' : markChg < 0 ? 'of-chg-dn' : '';
        const pctSign = markChg > 0 ? '+' : markChg < 0 ? '-' : '';
        const pctStr  = markPct > 0 ? `${pctSign}${(markPct * 100).toFixed(0)}%` : '—';
        const oiBadge = bigOi ? `<span class="of-oi-badge">${(oi / 1000).toFixed(1)}K</span>` : '';

        let dteCls = 'of-dte-long';
        if (dte === 0) dteCls = 'of-dte-0';
        else if (dte <= 7) dteCls = 'of-dte-short';
        const dteStr = dte === 0 ? '0DTE' : `${dte}d`;

        let premCls = 'of-prem-mid';
        if (premium >= 100_000) premCls = 'of-prem-hi';
        const premStr = premium >= 1_000_000
            ? `$${(premium / 1_000_000).toFixed(2)}M`
            : premium >= 1_000
                ? `$${(premium / 1_000).toFixed(0)}k`
                : premium > 0 ? `$${premium.toFixed(0)}` : '—';

        let aggrCell = '<span class="of-aggr-mid">—</span>';
        if (aggr === 'BUY')  aggrCell = '<span class="of-aggr-buy">BUY</span>';
        else if (aggr === 'SELL') aggrCell = '<span class="of-aggr-sell">SELL</span>';
        else if (aggr === 'MID')  aggrCell = '<span class="of-aggr-mid">MID</span>';

        const venue   = String(data.exchange || '').trim();
        const session = String(data.session  || '').trim().toLowerCase();
        const isAH    = session === 'pre' || session === 'post';
        const venueTag = venue ? `<span class="of-venue">${venue}</span>` : '';
        const ahTag    = isAH  ? `<span class="of-ah">AH</span>` : '';
        const tickerCell = isRealTrade
            ? `<span class="of-trade-tag">•</span> ${ticker || '?'}${venueTag}${ahTag}`
            : (ticker || '?');

        let voiCell = '<span class="of-voi-none">—</span>';
        if (oi > 0 && vol > 0) {
            const ratioStr = voiRatio >= 10 ? voiRatio.toFixed(0) : voiRatio.toFixed(2);
            if (voiRatio >= 1.0) {
                voiCell = `<span class="of-voi-new">NEW</span> ${ratioStr}x`;
            } else if (voiRatio < 0.1) {
                voiCell = `<span class="of-voi-close">${ratioStr}x</span>`;
            } else {
                voiCell = `<span class="of-voi-mid">${ratioStr}x</span>`;
            }
        }

        tr.innerHTML = `
            <td>${timeStr}</td>
            <td class="of-ticker">${tickerCell}</td>
            <td>${strike.toFixed(0)} ${oiBadge}</td>
            <td>${isCall ? 'C' : isPut ? 'P' : '?'}</td>
            <td class="${dteCls}">${dteStr}</td>
            <td>$${last.toFixed(2)}</td>
            <td class="${pctCls}">${pctStr}</td>
            <td>${lastSize || '—'}</td>
            <td class="${premCls}">${premStr}</td>
            <td>${aggrCell}</td>
            <td class="of-iv">${iv.toFixed(1)}%</td>
            <td>${delta.toFixed(3)}</td>
            <td class="of-gex-val">${gexM >= 0 ? '+' : ''}${gexM.toFixed(2)}</td>
            <td>${voiCell}</td>`;

        _tbody.prepend(tr);
        _rowCount++;

        while (_tbody.children.length > MAX_ROWS) {
            _tbody.removeChild(_tbody.lastChild);
        }

        const countEl = _container && _container.querySelector('#of-count');
        if (countEl) countEl.textContent = `${_rowCount} prints`;
    }

    function onOptionMark(data) {
        _appendRow(data, { isRealTrade: false });
    }

    function onOptionTrade(data) {
        _appendRow(data, { isRealTrade: true });
    }

    return { init, destroy, onOptionMark, onOptionTrade };
})();
window.OptionsFlowPane = OptionsFlowPane;
