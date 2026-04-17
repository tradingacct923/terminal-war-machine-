/**
 * Flow Pane — 0DT-Hero-style cumulative signed Δ notional chart.
 *
 * Renders two curves per selected ticker:
 *   - 0DTE cumulative signed Δ notional (green)
 *   - All-expirations cumulative signed Δ notional (orange)
 *
 * Data source: 'data:flow:update' bus event, backed by backend FlowAccumulator
 * which processes every LEVELONE_OPTIONS message (Schwab stream).
 *
 * Ticker selector: QQQ · SPY · AAPL · MSFT · GOOGL · AMZN · NVDA · META · TSLA · MAG7 (aggregate)
 * Mode toggle: 0DTE / All-exp / Both
 */
(function () {
    'use strict';

    const MAG7 = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA'];
    const PRIMARY_TICKERS = ['QQQ', 'SPY', 'MAG7'];
    const INDIVIDUAL = ['AAPL', 'NVDA', 'TSLA', 'MSFT', 'GOOGL', 'META', 'AMZN'];

    // Rolling time-series per ticker. Each entry: {t: ms, s0: signed_0dte, sa: signed_all, u0: unsigned_0dte, ua: unsigned_all}
    const _series = {};
    const _SERIES_MAX = 7200; // 2 hours at 1Hz
    let _selected = 'QQQ';
    let _mode = 'both'; // 'both' | '0dte' | 'all'

    let _slotEl = null;
    let _canvas = null;
    let _ctx = null;
    let _destroyed = false;
    let _raf = 0;
    let _dirty = true;
    let _unsubFlow = null;

    // ── Data plumbing ─────────────────────────────────────────────────────
    function _pushSample(t, tickerStates) {
        // tickerStates: array of { ticker, cum_signed_0dte, cum_signed_all, cum_unsigned_0dte, cum_unsigned_all }
        for (const s of tickerStates) {
            const t2 = s.ticker;
            if (!_series[t2]) _series[t2] = [];
            _series[t2].push({
                t,
                s0: +s.cum_signed_0dte || 0,
                sa: +s.cum_signed_all || 0,
                u0: +s.cum_unsigned_0dte || 0,
                ua: +s.cum_unsigned_all || 0,
            });
            if (_series[t2].length > _SERIES_MAX) _series[t2].splice(0, _series[t2].length - _SERIES_MAX);
        }
        // Compute Mag7 aggregate as sum across the 7 names
        const agg = [];
        let hasAny = false;
        for (const name of MAG7) {
            const lst = _series[name];
            if (lst && lst.length) hasAny = true;
        }
        if (hasAny) {
            // Align on this tick only (simple: take latest value from each ticker)
            let s0 = 0, sa = 0, u0 = 0, ua = 0;
            for (const name of MAG7) {
                const lst = _series[name];
                if (!lst || !lst.length) continue;
                const last = lst[lst.length - 1];
                s0 += last.s0; sa += last.sa; u0 += last.u0; ua += last.ua;
            }
            if (!_series.MAG7) _series.MAG7 = [];
            _series.MAG7.push({ t, s0, sa, u0, ua });
            if (_series.MAG7.length > _SERIES_MAX) _series.MAG7.splice(0, _series.MAG7.length - _SERIES_MAX);
        }
        _dirty = true;
    }

    function _hydrateFromREST() {
        fetch('/api/option_flow', { headers: { 'X-Auth-Token': sessionStorage.getItem('greeks-auth') } })
            .then(r => r.json())
            .then(d => {
                if (!d || !d.tickers) return;
                _pushSample(Date.now(), d.tickers);
            })
            .catch(() => {});
    }

    // ── Rendering ─────────────────────────────────────────────────────────
    function _fmtMoney(v) {
        const a = Math.abs(v);
        const sign = v < 0 ? '-' : (v > 0 ? '+' : '');
        if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(2)}B`;
        if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(1)}M`;
        if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(0)}K`;
        return `${sign}${a.toFixed(0)}`;
    }

    function _render() {
        if (!_canvas || !_ctx) return;
        const dpr = window.devicePixelRatio || 1;
        const rect = _canvas.getBoundingClientRect();
        const w = rect.width, h = rect.height;
        if (w <= 0 || h <= 0) return;

        if (_canvas.width !== Math.round(w * dpr) || _canvas.height !== Math.round(h * dpr)) {
            _canvas.width = Math.round(w * dpr);
            _canvas.height = Math.round(h * dpr);
        }
        _ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        _ctx.clearRect(0, 0, w, h);
        _ctx.fillStyle = '#06090e';
        _ctx.fillRect(0, 0, w, h);

        const data = _series[_selected] || [];
        const headerH = 28;
        const footerH = 22;
        const plotY = headerH + 8;
        const plotH = h - headerH - footerH - 8;
        const plotX = 58;
        const plotW = w - plotX - 8;

        // Header
        _ctx.fillStyle = 'rgba(180,190,220,0.9)';
        _ctx.font = '11px "JetBrains Mono", monospace';
        _ctx.textAlign = 'left';
        _ctx.fillText(`FLOW · ${_selected} · ${_mode.toUpperCase()}`, 8, 18);

        if (!data.length) {
            _ctx.fillStyle = 'rgba(140,160,200,0.4)';
            _ctx.textAlign = 'center';
            _ctx.font = '10px "JetBrains Mono", monospace';
            _ctx.fillText(`waiting for ${_selected} option trades...`, w / 2, h / 2);
            return;
        }

        // Y range across both series we draw
        let yMin = Infinity, yMax = -Infinity;
        for (const d of data) {
            if (_mode === 'both' || _mode === '0dte') {
                if (d.s0 < yMin) yMin = d.s0;
                if (d.s0 > yMax) yMax = d.s0;
            }
            if (_mode === 'both' || _mode === 'all') {
                if (d.sa < yMin) yMin = d.sa;
                if (d.sa > yMax) yMax = d.sa;
            }
        }
        // Symmetric around zero if flow spans both signs; otherwise snug
        if (yMin === Infinity) { yMin = -1; yMax = 1; }
        if (yMin === yMax) { yMin -= 1; yMax += 1; }
        const pad = (yMax - yMin) * 0.08;
        yMin -= pad; yMax += pad;

        const tMin = data[0].t;
        const tMax = data[data.length - 1].t;
        const tSpan = Math.max(1, tMax - tMin);

        // Gridlines (horizontal, 4)
        _ctx.strokeStyle = 'rgba(255,255,255,0.04)';
        _ctx.fillStyle = 'rgba(140,160,200,0.4)';
        _ctx.font = '9px "JetBrains Mono", monospace';
        _ctx.textAlign = 'right';
        _ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {
            const y = plotY + (plotH * i) / 4;
            const v = yMax - (yMax - yMin) * (i / 4);
            _ctx.beginPath();
            _ctx.moveTo(plotX, y);
            _ctx.lineTo(plotX + plotW, y);
            _ctx.stroke();
            _ctx.fillText(_fmtMoney(v), plotX - 4, y + 3);
        }
        // Zero line (brighter)
        if (yMin < 0 && yMax > 0) {
            const yZero = plotY + plotH * (yMax - 0) / (yMax - yMin);
            _ctx.strokeStyle = 'rgba(255,255,255,0.18)';
            _ctx.beginPath();
            _ctx.moveTo(plotX, yZero);
            _ctx.lineTo(plotX + plotW, yZero);
            _ctx.stroke();
        }

        // Curves
        function drawLine(key, color) {
            _ctx.strokeStyle = color;
            _ctx.lineWidth = 1.5;
            _ctx.beginPath();
            for (let i = 0; i < data.length; i++) {
                const d = data[i];
                const x = plotX + plotW * ((d.t - tMin) / tSpan);
                const y = plotY + plotH * (yMax - d[key]) / (yMax - yMin);
                if (i === 0) _ctx.moveTo(x, y); else _ctx.lineTo(x, y);
            }
            _ctx.stroke();
        }
        if (_mode === 'both' || _mode === 'all') drawLine('sa', '#ff9830'); // all-exp: orange
        if (_mode === 'both' || _mode === '0dte') drawLine('s0', '#4cd964'); // 0dte: green

        // Current values (rightmost)
        const cur = data[data.length - 1];
        _ctx.font = '10px "JetBrains Mono", monospace';
        _ctx.textAlign = 'right';
        if (_mode === 'both' || _mode === 'all') {
            _ctx.fillStyle = '#ff9830';
            _ctx.fillText(_fmtMoney(cur.sa), plotX + plotW - 4, plotY + 12);
        }
        if (_mode === 'both' || _mode === '0dte') {
            _ctx.fillStyle = '#4cd964';
            _ctx.fillText(_fmtMoney(cur.s0), plotX + plotW - 4, plotY + plotH - 4);
        }

        // Footer: current unsigned totals + sample count
        _ctx.fillStyle = 'rgba(180,190,220,0.55)';
        _ctx.font = '9px "JetBrains Mono", monospace';
        _ctx.textAlign = 'left';
        const footerText = `signed 0DTE=${_fmtMoney(cur.s0)}  signed all=${_fmtMoney(cur.sa)}  ` +
                           `traded(unsigned) 0DTE=${_fmtMoney(cur.u0)}  all=${_fmtMoney(cur.ua)}  ` +
                           `samples=${data.length}`;
        _ctx.fillText(footerText, 8, h - 6);
    }

    function _loop() {
        if (_destroyed) return;
        _raf = requestAnimationFrame(_loop);
        if (!_canvas || !_ctx) return;
        if (_canvas.offsetParent === null) return;
        if (!_dirty) return;
        _dirty = false;
        _render();
    }

    // ── UI controls ───────────────────────────────────────────────────────
    function _buildControls(slot) {
        const bar = document.createElement('div');
        bar.style.cssText = 'display:flex;gap:4px;padding:4px 6px;background:rgba(255,255,255,0.02);border-bottom:1px solid rgba(255,255,255,0.05);flex-wrap:wrap;align-items:center';

        const btnStyle = 'font-family:"JetBrains Mono",monospace;font-size:9px;padding:2px 8px;border-radius:3px;cursor:pointer;border:1px solid rgba(255,255,255,0.08);background:rgba(255,255,255,0.03);color:rgba(180,190,220,0.7)';
        const activeStyle = 'font-family:"JetBrains Mono",monospace;font-size:9px;padding:2px 8px;border-radius:3px;cursor:pointer;border:1px solid rgba(76,217,100,0.5);background:rgba(76,217,100,0.15);color:#4cd964;font-weight:600';

        const makeBtn = (label, onClick, activeFn) => {
            const b = document.createElement('button');
            b.textContent = label;
            b.style.cssText = activeFn() ? activeStyle : btnStyle;
            b.onclick = () => { onClick(); _rebuildActiveStates(); _dirty = true; };
            b._activeFn = activeFn;
            return b;
        };

        const allButtons = [];
        const primaryGroup = document.createElement('span');
        primaryGroup.style.cssText = 'display:inline-flex;gap:3px;margin-right:6px';
        for (const t of PRIMARY_TICKERS) {
            const b = makeBtn(t, () => { _selected = t; _dirty = true; }, () => _selected === t);
            allButtons.push(b);
            primaryGroup.appendChild(b);
        }
        bar.appendChild(primaryGroup);

        const sep = document.createElement('span');
        sep.style.cssText = 'color:rgba(255,255,255,0.15);margin:0 2px';
        sep.textContent = '|';
        bar.appendChild(sep);

        const individualGroup = document.createElement('span');
        individualGroup.style.cssText = 'display:inline-flex;gap:3px;margin-right:6px';
        for (const t of INDIVIDUAL) {
            const b = makeBtn(t, () => { _selected = t; _dirty = true; }, () => _selected === t);
            allButtons.push(b);
            individualGroup.appendChild(b);
        }
        bar.appendChild(individualGroup);

        const sep2 = document.createElement('span');
        sep2.style.cssText = 'color:rgba(255,255,255,0.15);margin:0 4px';
        sep2.textContent = '|';
        bar.appendChild(sep2);

        for (const m of [['0DTE', '0dte'], ['ALL', 'all'], ['BOTH', 'both']]) {
            const [label, val] = m;
            const b = makeBtn(label, () => { _mode = val; _dirty = true; }, () => _mode === val);
            allButtons.push(b);
            bar.appendChild(b);
        }

        function _rebuildActiveStates() {
            for (const b of allButtons) b.style.cssText = b._activeFn() ? activeStyle : btnStyle;
        }

        slot.appendChild(bar);
        return bar;
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────
    window.FlowPane = {
        init(slotEl) {
            _destroyed = false;
            _slotEl = slotEl;
            slotEl.innerHTML = '';
            slotEl.style.cssText = 'display:flex;flex-direction:column;width:100%;height:100%;background:#06090e;overflow:hidden';

            _buildControls(slotEl);

            _canvas = document.createElement('canvas');
            _canvas.style.cssText = 'flex:1;width:100%;display:block';
            slotEl.appendChild(_canvas);
            _ctx = _canvas.getContext('2d');

            // Hydrate + subscribe
            _hydrateFromREST();
            if (window.AltarisEvents) {
                const handler = (data) => {
                    if (data && Array.isArray(data.tickers)) _pushSample(data.t || Date.now(), data.tickers);
                };
                window.AltarisEvents.on('data:flow:update', handler);
                _unsubFlow = () => window.AltarisEvents.off('data:flow:update', handler);
            }
            _raf = requestAnimationFrame(_loop);
        },
        destroy() {
            _destroyed = true;
            if (_raf) cancelAnimationFrame(_raf);
            _raf = 0;
            if (_unsubFlow) { try { _unsubFlow(); } catch (_) {} _unsubFlow = null; }
            _canvas = null;
            _ctx = null;
            _slotEl = null;
        },
    };
})();
