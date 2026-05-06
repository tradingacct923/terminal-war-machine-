/* Signal Quality Dashboard pane (added 2026-05-01)
 *
 * Renders /api/intel/signal_quality output as a table:
 *   signal name | samples | hit rate | edge $ | verdict
 *
 * Verdict color-coded:
 *   PROMOTE  bright green  → high-edge signal worth doubling down on
 *   KEEP     muted green   → above-baseline, useful
 *   DEMOTE   yellow        → barely above baseline, deprioritize
 *   KILL     red           → ≤ 50% (worse than chance)
 *   TRACKING gray          → metric tracked but not yet hit-rate evaluable
 *   INSUFFICIENT gray-dim  → not enough samples to judge
 *
 * Polls /api/intel/signal_quality every 60s (matches backend cache TTL).
 * Hard-refresh button forces ?force=1 recompute.
 *
 * IIFE pattern matching other panes (init/destroy lifecycle).
 */
(function () {
    'use strict';

    const BG = '#0a0e14';
    const FG = '#e6edf3';
    const FG_DIM = 'rgba(180,190,220,0.65)';
    const FG_FAINT = 'rgba(180,190,220,0.35)';
    const BORDER = 'rgba(255,255,255,0.08)';
    const ROW_BG_HOVER = 'rgba(255,255,255,0.03)';

    // Verdict colors (background tint + foreground)
    const VERDICT_STYLE = {
        'PROMOTE':       { bg: 'rgba(34,221,85,0.20)',   fg: '#22dd55', icon: '🔥' },
        'KEEP':          { bg: 'rgba(122,184,255,0.15)', fg: '#7ab8ff', icon: '✓' },
        'DEMOTE':        { bg: 'rgba(255,215,0,0.15)',   fg: '#ffd700', icon: '↓' },
        'KILL':          { bg: 'rgba(255,68,68,0.18)',   fg: '#ff4444', icon: '✗' },
        'TRACKING':      { bg: 'rgba(180,180,180,0.10)', fg: '#aaaaaa', icon: '·' },
        'INSUFFICIENT':  { bg: 'rgba(120,120,120,0.10)', fg: '#888888', icon: '?' },
    };

    let _slotEl = null;
    let _root = null;
    let _bodyEl = null;
    let _statusEl = null;
    let _refreshBtn = null;
    let _destroyed = false;
    let _pollTimer = null;
    const POLL_MS = 60_000;  // matches backend cache TTL

    function _fmtNum(n) {
        if (n === undefined || n === null || isNaN(n)) return '—';
        if (typeof n !== 'number') return String(n);
        if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(1) + 'M';
        if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(1) + 'K';
        if (Math.abs(n) >= 100) return n.toFixed(0);
        if (Math.abs(n) >= 1)   return n.toFixed(2);
        if (n === 0)            return '0';
        return n.toFixed(3);
    }

    function _fmtPct(n) {
        if (n === undefined || n === null || isNaN(n)) return '—';
        return (n * 100).toFixed(1) + '%';
    }

    function _fetchAudit(force) {
        const url = force ? '/api/intel/signal_quality?force=1' : '/api/intel/signal_quality';
        return fetch(url, {headers: {'X-Auth-Token': sessionStorage.getItem('greeks-auth') || ''}})
            .then(r => r.json())
            .catch(e => ({error: String(e)}));
    }

    function _renderRow(s) {
        const tr = document.createElement('tr');
        tr.style.cssText = 'border-bottom:1px solid ' + BORDER;
        tr.onmouseenter = () => tr.style.background = ROW_BG_HOVER;
        tr.onmouseleave = () => tr.style.background = 'transparent';

        const verdict = s.verdict || 'INSUFFICIENT';
        const vs = VERDICT_STYLE[verdict] || VERDICT_STYLE.INSUFFICIENT;

        // Signal name (bold for promoted/killed signals)
        const nameTd = document.createElement('td');
        nameTd.style.cssText = `padding:8px 12px;font-family:"JetBrains Mono",monospace;font-size:11px;color:${FG};font-weight:${verdict === 'PROMOTE' || verdict === 'KILL' ? '600' : '400'}`;
        nameTd.textContent = s.signal || '?';
        tr.appendChild(nameTd);

        // Samples
        const nTd = document.createElement('td');
        nTd.style.cssText = `padding:8px 12px;font-family:"JetBrains Mono",monospace;font-size:11px;color:${FG_DIM};text-align:right`;
        nTd.textContent = _fmtNum(s.samples);
        tr.appendChild(nTd);

        // Hit rate at 3 horizons (decay curve) — 5m / 15m / 30m
        // Each cell color-coded by hit-rate threshold so you can SEE
        // whether a signal's edge dies fast or persists.
        const _hrColor = (hr) => {
            if (hr === null || hr === undefined) return FG_DIM;
            if (hr >= 0.65) return '#22dd55';
            if (hr >= 0.55) return '#7ab8ff';
            if (hr >= 0.50) return '#ffd700';
            return '#ff4444';
        };
        const _drawHrCell = (hr) => {
            const td = document.createElement('td');
            const c = _hrColor(hr);
            td.style.cssText = `padding:8px 8px;font-family:"JetBrains Mono",monospace;font-size:11px;color:${c};text-align:right;font-weight:600`;
            td.textContent = hr !== null && hr !== undefined ? _fmtPct(hr) : '—';
            return td;
        };
        // For aggressor signals: dealer_prints has explicit 5m/15m/30m hit rates.
        // For other signals: fall back to sign_match_rate / agreement (single value).
        const hr_5m  = s.hit_rate_5m  !== undefined ? s.hit_rate_5m  : null;
        const hr_15m = s.hit_rate_15m !== undefined ? s.hit_rate_15m : null;
        const hr_30m = s.hit_rate_30m !== undefined ? s.hit_rate_30m :
                       s.sign_match_rate !== undefined ? s.sign_match_rate :
                       s.agreement !== undefined ? s.agreement : null;
        tr.appendChild(_drawHrCell(hr_5m));
        tr.appendChild(_drawHrCell(hr_15m));
        tr.appendChild(_drawHrCell(hr_30m));

        // Decay direction indicator (5m → 30m): ↗ improving, ↘ decaying, → flat
        const decayTd = document.createElement('td');
        decayTd.style.cssText = `padding:8px 6px;font-family:"JetBrains Mono",monospace;font-size:13px;text-align:center`;
        if (hr_5m !== null && hr_30m !== null) {
            const delta = hr_30m - hr_5m;
            if (Math.abs(delta) < 0.02) { decayTd.textContent = '→'; decayTd.style.color = FG_FAINT; }
            else if (delta > 0)         { decayTd.textContent = '↗'; decayTd.style.color = '#22dd55'; decayTd.title = 'Edge GROWS over time — strong signal'; }
            else                        { decayTd.textContent = '↘'; decayTd.style.color = '#ff9830'; decayTd.title = 'Edge DECAYS — fade quickly'; }
        } else {
            decayTd.textContent = '—';
            decayTd.style.color = FG_FAINT;
        }
        tr.appendChild(decayTd);

        // Edge $ (where applicable)
        const edgeTd = document.createElement('td');
        edgeTd.style.cssText = `padding:8px 12px;font-family:"JetBrains Mono",monospace;font-size:11px;color:${FG_DIM};text-align:right`;
        const edge = s['edge_30m_$'];
        edgeTd.textContent = (edge !== undefined && edge > 0) ? '$' + _fmtNum(edge) : '—';
        tr.appendChild(edgeTd);

        // n_30m (eval-able sample count)
        const evalTd = document.createElement('td');
        evalTd.style.cssText = `padding:8px 12px;font-family:"JetBrains Mono",monospace;font-size:11px;color:${FG_FAINT};text-align:right`;
        const n_eval = s.n_30m !== undefined ? s.n_30m :
                       s.n_with_observed !== undefined ? s.n_with_observed :
                       s.n_warm !== undefined ? s.n_warm : '';
        evalTd.textContent = n_eval !== '' ? _fmtNum(n_eval) : '—';
        tr.appendChild(evalTd);

        // Verdict (colored badge)
        const vTd = document.createElement('td');
        vTd.style.cssText = `padding:8px 12px;text-align:center`;
        const badge = document.createElement('span');
        badge.style.cssText = `display:inline-block;padding:3px 10px;border-radius:11px;background:${vs.bg};color:${vs.fg};font-size:10px;font-weight:600;font-family:"Inter",system-ui,sans-serif;letter-spacing:0.5px`;
        badge.textContent = `${vs.icon} ${verdict}`;
        vTd.appendChild(badge);
        tr.appendChild(vTd);

        // Note column (truncated)
        const noteTd = document.createElement('td');
        noteTd.style.cssText = `padding:8px 12px;font-family:"Inter",system-ui,sans-serif;font-size:10px;color:${FG_FAINT};font-style:italic;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap`;
        noteTd.textContent = s.note || '';
        noteTd.title = s.note || '';
        tr.appendChild(noteTd);

        return tr;
    }

    function _renderTable(audit) {
        if (!_bodyEl) return;
        _bodyEl.innerHTML = '';

        const signals = (audit && audit.signals) || [];

        // Sort: PROMOTE first, then KEEP, DEMOTE, TRACKING, INSUFFICIENT, KILL last
        const order = {PROMOTE:0, KEEP:1, DEMOTE:2, TRACKING:3, INSUFFICIENT:4, KILL:5};
        signals.sort((a, b) => (order[a.verdict] ?? 99) - (order[b.verdict] ?? 99));

        if (signals.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 7;
            td.style.cssText = `padding:30px;text-align:center;color:${FG_DIM};font-family:"Inter",system-ui,sans-serif`;
            td.textContent = audit && audit.error ? `Error: ${audit.error}` : 'Loading signal audit data…';
            tr.appendChild(td);
            _bodyEl.appendChild(tr);
            return;
        }

        for (const s of signals) {
            _bodyEl.appendChild(_renderRow(s));
        }

        // Status bar
        if (_statusEl) {
            const t = audit.computed_at_ts || 0;
            const ago = Math.max(0, Math.round((Date.now() / 1000) - t));
            const ms = audit.compute_ms || 0;
            _statusEl.textContent = `${signals.length} signals · ${ms}ms compute · refreshed ${ago}s ago`;
        }
    }

    function _doRefresh(force) {
        return _fetchAudit(force).then(audit => {
            if (_destroyed) return;
            _renderTable(audit || {error: 'no data'});
        });
    }

    function _buildHeader(parent) {
        const header = document.createElement('div');
        header.style.cssText = `display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid ${BORDER};background:rgba(255,255,255,0.02)`;

        const left = document.createElement('div');
        left.style.cssText = 'display:flex;align-items:center;gap:12px';

        const title = document.createElement('div');
        title.style.cssText = `font-family:"Inter",system-ui,sans-serif;font-size:12px;font-weight:600;color:${FG};letter-spacing:0.5px`;
        title.textContent = '⚡ SIGNAL QUALITY';
        left.appendChild(title);

        _statusEl = document.createElement('div');
        _statusEl.style.cssText = `font-family:"JetBrains Mono",monospace;font-size:10px;color:${FG_FAINT}`;
        _statusEl.textContent = 'loading…';
        left.appendChild(_statusEl);

        header.appendChild(left);

        _refreshBtn = document.createElement('button');
        _refreshBtn.textContent = '↻ Force Refresh';
        _refreshBtn.title = 'Force recompute (bypass 60s cache)';
        _refreshBtn.style.cssText = `font-family:"Inter",system-ui,sans-serif;font-size:11px;font-weight:600;padding:5px 12px;border-radius:6px;cursor:pointer;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);color:${FG_DIM}`;
        _refreshBtn.onclick = () => {
            _refreshBtn.disabled = true;
            _refreshBtn.textContent = '… recomputing';
            _doRefresh(true).then(() => {
                if (_destroyed) return;
                _refreshBtn.disabled = false;
                _refreshBtn.textContent = '↻ Force Refresh';
            });
        };
        header.appendChild(_refreshBtn);

        parent.appendChild(header);
    }

    function _buildTable(parent) {
        const tableWrap = document.createElement('div');
        tableWrap.style.cssText = 'flex:1;overflow:auto';

        const table = document.createElement('table');
        table.style.cssText = 'width:100%;border-collapse:collapse';

        // Header row
        const thead = document.createElement('thead');
        thead.style.cssText = `position:sticky;top:0;background:${BG};z-index:1`;
        const headers = [
            ['signal',   'left',   'Signal name'],
            ['samples',  'right',  '# of ledger entries seen'],
            ['5m',       'right',  'Hit rate at 5min horizon (spot_300s)'],
            ['15m',      'right',  'Hit rate at 15min horizon (spot_900s)'],
            ['30m',      'right',  'Hit rate at 30min horizon (spot_1800s)'],
            ['decay',    'center', '↗ edge grows · → flat · ↘ edge dies fast'],
            ['edge $',   'right',  'Avg $-magnitude per correct call (30m)'],
            ['n eval',   'right',  '# evaluable for hit-rate (need fwd-look)'],
            ['verdict',  'center', 'PROMOTE / KEEP / DEMOTE / KILL / TRACKING'],
            ['note',     'left',   'Caveats / context'],
        ];
        const trh = document.createElement('tr');
        trh.style.cssText = `border-bottom:2px solid ${BORDER};background:rgba(255,255,255,0.02)`;
        for (const [label, align, title] of headers) {
            const th = document.createElement('th');
            th.style.cssText = `padding:8px 12px;font-family:"Inter",system-ui,sans-serif;font-size:9px;font-weight:600;color:${FG_DIM};text-align:${align};text-transform:uppercase;letter-spacing:0.8px`;
            th.textContent = label;
            th.title = title;
            trh.appendChild(th);
        }
        thead.appendChild(trh);
        table.appendChild(thead);

        _bodyEl = document.createElement('tbody');
        table.appendChild(_bodyEl);

        tableWrap.appendChild(table);
        parent.appendChild(tableWrap);
    }

    function _buildLegend(parent) {
        const legend = document.createElement('div');
        legend.style.cssText = `display:flex;align-items:center;gap:14px;padding:8px 14px;border-top:1px solid ${BORDER};background:rgba(255,255,255,0.02);font-family:"Inter",system-ui,sans-serif;font-size:9px;color:${FG_FAINT};letter-spacing:0.4px;flex-wrap:wrap`;
        const labelWrap = document.createElement('span');
        labelWrap.style.cssText = `color:${FG_DIM};text-transform:uppercase;font-weight:600`;
        labelWrap.textContent = 'verdict thresholds:';
        legend.appendChild(labelWrap);
        const thresholds = [
            ['PROMOTE',     '≥65% hit rate'],
            ['KEEP',        '55-65%'],
            ['DEMOTE',      '50-55%'],
            ['KILL',        '≤50% (random or worse)'],
            ['INSUFFICIENT', '<30 evaluable samples'],
            ['TRACKING',    'no hit-rate metric yet'],
        ];
        for (const [v, desc] of thresholds) {
            const vs = VERDICT_STYLE[v] || VERDICT_STYLE.INSUFFICIENT;
            const item = document.createElement('span');
            item.style.cssText = `display:inline-flex;align-items:center;gap:6px`;
            const dot = document.createElement('span');
            dot.style.cssText = `width:8px;height:8px;border-radius:50%;background:${vs.fg}`;
            item.appendChild(dot);
            const txt = document.createElement('span');
            txt.style.cssText = `color:${FG_FAINT}`;
            txt.innerHTML = `<b style="color:${vs.fg}">${v}</b> ${desc}`;
            item.appendChild(txt);
            legend.appendChild(item);
        }
        parent.appendChild(legend);
    }

    function _build(slotEl) {
        slotEl.innerHTML = '';
        slotEl.style.cssText = `display:flex;flex-direction:column;width:100%;height:100%;background:${BG};color:${FG};overflow:hidden;font-family:"Inter",system-ui,sans-serif`;

        _root = document.createElement('div');
        _root.style.cssText = 'display:flex;flex-direction:column;width:100%;height:100%;overflow:hidden';

        _buildHeader(_root);
        _buildTable(_root);
        _buildLegend(_root);

        slotEl.appendChild(_root);
    }

    window.SignalQualityPane = {
        init(slotEl) {
            _destroyed = false;
            _slotEl = slotEl;
            _build(slotEl);
            _doRefresh(false);
            // Poll every 60s (matches backend cache)
            _pollTimer = setInterval(() => {
                if (!_destroyed) _doRefresh(false);
            }, POLL_MS);
        },
        destroy() {
            _destroyed = true;
            if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
            if (_slotEl) _slotEl.innerHTML = '';
            _slotEl = null;
            _root = _bodyEl = _statusEl = _refreshBtn = null;
        },
    };
})();
