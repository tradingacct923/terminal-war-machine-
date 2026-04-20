/**
 * Flow Pane — 0DT-Hero-style cumulative signed Δ notional chart.
 *
 * Layout matches 0DT Hero screenshot exactly:
 *   Top bar: [SPX] [SPY] [QQQ] [Mag7] [S&PE] | [DTO] [📅]
 *   Chart:   Green line = 0DTE, Orange line = All Expirations
 *   Y-axis:  Delta Notional ($M), right side
 *   X-axis:  Time HH:MM
 *   Header:  "{ticker} {weekday} {Month} {day}, {year}"
 *   Right edge: colored labels with current 0DTE + All-Exp values
 *
 * Data source: 'data:flow:update' bus event, backed by backend FlowAccumulator.
 *
 * Categories:
 *   SPX   — tier-blocked, button disabled
 *   SPY   — direct (we subscribe)
 *   QQQ   — direct
 *   Mag7  — client-side aggregate of 7 Mag7 names
 *   S&PE  — pseudo-aggregate: weighted sum of SPY + QQQ + Mag7 (70-85% of true signal)
 *   DTO   — "0DTE Only" toggle: show green line only vs both lines
 */
(function () {
    'use strict';

    const MAG7 = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA'];
    const CATEGORIES = ['SPX', 'SPY', 'QQQ', 'MAG7', 'S&PE'];

    // Colors match 0DT Hero
    const COLOR_0DTE = '#00ff41';       // bright green
    const COLOR_ALL = '#ff9830';        // orange
    const COLOR_GRID = 'rgba(255,255,255,0.05)';
    const COLOR_ZERO = 'rgba(255,255,255,0.15)';
    const COLOR_AXIS = 'rgba(180,190,220,0.55)';
    const BG = '#0a0d14';

    // Per-ticker rolling time-series. Each entry: {t, s0, sa}
    const _series = {};
    const _SERIES_MAX = 14400;  // 4 hours @ 1Hz
    let _selected = 'QQQ';
    let _dto_only = false;      // false = show BOTH lines (default), true = 0DTE only

    // Fundamentals cache — keyed by ticker. Fetched on first mount + when ticker changes.
    const _fundamentals = {};   // {ticker: {peRatio, beta, divYield, marketCap, ...}}
    let _fundamentalsFetched = false;

    let _slotEl = null;
    let _canvas = null;
    let _ctx = null;
    let _destroyed = false;
    let _raf = 0;
    let _dirty = true;
    let _unsubFlow = null;
    let _sessionStartHr = 9.5;  // 09:30 ET
    let _sessionEndHr = 16.0;   // 16:00 ET

    // ── Aggregation ───────────────────────────────────────────────────────
    function _computeAggregate(category) {
        if (category === 'SPX') {
            // Tier-blocked: return empty series but proxy with SPY ×10
            const spy = _series['SPY'] || [];
            return spy.map(d => ({t: d.t, s0: d.s0 * 10, sa: d.sa * 10}));
        }
        if (category === 'MAG7') {
            return _aggregateByTime(MAG7);
        }
        if (category === 'S&PE') {
            // Pseudo: SPY + QQQ + Mag7 weighted sum
            const names = ['SPY', 'QQQ', ...MAG7];
            return _aggregateByTime(names);
        }
        return _series[category] || [];
    }

    function _aggregateByTime(names) {
        // Use SPY's timeline as the spine (most active ticker)
        const spine = _series['SPY'] || _series['QQQ'] || [];
        if (!spine.length) return [];
        const out = [];
        for (const sample of spine) {
            let s0 = 0, sa = 0;
            for (const n of names) {
                const lst = _series[n];
                if (!lst || !lst.length) continue;
                // Find the closest sample ≤ sample.t
                const last = lst[lst.length - 1];
                if (last.t <= sample.t) {
                    s0 += last.s0;
                    sa += last.sa;
                }
            }
            out.push({t: sample.t, s0, sa});
        }
        return out;
    }

    // ── Data ingestion ────────────────────────────────────────────────────
    function _pushSample(t, tickerStates) {
        for (const s of tickerStates) {
            const tk = s.ticker;
            if (!_series[tk]) _series[tk] = [];
            _series[tk].push({
                t,
                s0: +s.cum_signed_0dte || 0,
                sa: +s.cum_signed_all || 0,
            });
            if (_series[tk].length > _SERIES_MAX) {
                _series[tk].splice(0, _series[tk].length - _SERIES_MAX);
            }
        }
        _dirty = true;
    }

    function _hydrateFromREST() {
        fetch('/api/option_flow', {headers: {'X-Auth-Token': sessionStorage.getItem('greeks-auth') || ''}})
            .then(r => r.json())
            .then(d => {
                if (!d || !d.tickers) return;
                _pushSample(Date.now(), d.tickers);
            })
            .catch(() => {});
    }

    function _fetchFundamentals() {
        if (_fundamentalsFetched) return;
        _fundamentalsFetched = true;
        const tickers = ['QQQ', 'SPY', ...MAG7].join(',');
        fetch(`/api/fundamentals?symbols=${tickers}`,
              {headers: {'X-Auth-Token': sessionStorage.getItem('greeks-auth') || ''}})
            .then(r => r.json())
            .then(d => {
                if (!d || !d.fundamentals) return;
                Object.assign(_fundamentals, d.fundamentals);
                _dirty = true;
            })
            .catch(() => { _fundamentalsFetched = false; });  // retry on next cycle if failed
    }

    function _aggregateFundamentals(tickers) {
        // Average P/E, β, DivYield; sum market cap. Skip tickers without data.
        let pe_sum = 0, pe_n = 0, b_sum = 0, b_n = 0, dy_sum = 0, dy_n = 0, mc_sum = 0;
        for (const t of tickers) {
            const f = _fundamentals[t];
            if (!f) continue;
            if (f.peRatio && isFinite(f.peRatio)) { pe_sum += f.peRatio; pe_n++; }
            if (f.beta && isFinite(f.beta))       { b_sum  += f.beta;    b_n++;  }
            if (f.divYield != null && isFinite(f.divYield)) { dy_sum += f.divYield; dy_n++; }
            if (f.marketCap && isFinite(f.marketCap)) mc_sum += f.marketCap;
        }
        return {
            peRatio: pe_n ? pe_sum / pe_n : null,
            beta:    b_n  ? b_sum / b_n  : null,
            divYield:dy_n ? dy_sum / dy_n: null,
            marketCap: mc_sum || null,
        };
    }

    function _fundamentalsForSelected() {
        if (_selected === 'MAG7') return _aggregateFundamentals(MAG7);
        if (_selected === 'S&PE') return _aggregateFundamentals(['SPY', 'QQQ', ...MAG7]);
        if (_selected === 'SPX')  return _fundamentals['SPY'] || null;  // SPY proxy
        return _fundamentals[_selected] || null;
    }

    function _fmtMCap(mc) {
        if (!mc || !isFinite(mc)) return '—';
        if (mc >= 1e12) return `$${(mc / 1e12).toFixed(2)}T`;
        if (mc >= 1e9)  return `$${(mc / 1e9).toFixed(1)}B`;
        if (mc >= 1e6)  return `$${(mc / 1e6).toFixed(0)}M`;
        return `$${mc.toFixed(0)}`;
    }

    // ── Formatting ────────────────────────────────────────────────────────
    function _fmtMoney(v) {
        const a = Math.abs(v);
        const sign = v < 0 ? '-' : '';
        if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(2)}B`;
        if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(1)}M`;
        if (a >= 1e3) return `${sign}${Math.round(a / 1e3)}K`;
        return `${sign}${Math.round(a)}`;
    }

    function _fmtYAxis(v) {
        const a = Math.abs(v);
        const sign = v < 0 ? '-' : '';
        if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(1)}B`;
        if (a >= 1e6) return `${sign}${Math.round(a / 1e6)}M`;
        if (a >= 1e3) return `${sign}${Math.round(a / 1e3)}K`;
        return `${sign}0`;
    }

    function _fmtTime(ms) {
        const d = new Date(ms);
        return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }

    function _fmtDateHeader() {
        const d = new Date();
        const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
        const months = ['January', 'February', 'March', 'April', 'May', 'June',
                        'July', 'August', 'September', 'October', 'November', 'December'];
        const ticker = _selected === 'MAG7' ? 'Magnificent 7'
                     : _selected === 'S&PE' ? 'S&P500 Equities'
                     : _selected;
        return `${ticker} ${days[d.getDay()]}, ${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
    }

    // ── Rendering ─────────────────────────────────────────────────────────
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
        _ctx.fillStyle = BG;
        _ctx.fillRect(0, 0, w, h);

        const data = _computeAggregate(_selected);

        // Plot area — leave room for right-side labels + bottom time axis
        const plotL = 10;
        const plotR = w - 64;
        const plotT = 38;
        const plotB = h - 28;
        const plotW = plotR - plotL;
        const plotH = plotB - plotT;

        // Header (ticker + date)
        _ctx.fillStyle = '#ffffff';
        _ctx.font = '13px "Inter", system-ui, sans-serif';
        _ctx.textAlign = 'left';
        _ctx.fillText(_fmtDateHeader(), 12, 22);

        // Fundamentals badge (right-aligned on header row)
        const fund = _fundamentalsForSelected();
        if (fund) {
            const parts = [];
            if (fund.peRatio != null) parts.push(`P/E ${fund.peRatio.toFixed(1)}`);
            if (fund.beta != null)    parts.push(`β ${fund.beta.toFixed(2)}`);
            if (fund.divYield != null) parts.push(`DivY ${fund.divYield.toFixed(2)}%`);
            if (fund.marketCap)        parts.push(`MCap ${_fmtMCap(fund.marketCap)}`);
            if (parts.length) {
                _ctx.fillStyle = 'rgba(180,190,220,0.7)';
                _ctx.font = '10px "JetBrains Mono", monospace';
                _ctx.textAlign = 'right';
                _ctx.fillText(parts.join('   '), plotR, 22);
            }
        }

        if (!data.length) {
            _ctx.fillStyle = 'rgba(180,190,220,0.35)';
            _ctx.font = '11px "JetBrains Mono", monospace';
            _ctx.textAlign = 'center';
            const msg = _selected === 'SPX'
                ? 'SPX tier blocked — showing SPY × 10 proxy when SPY data arrives'
                : `waiting for ${_selected} flow...`;
            _ctx.fillText(msg, w / 2, h / 2);
            return;
        }

        // Y range
        let yMin = Infinity, yMax = -Infinity;
        for (const d of data) {
            if (!_dto_only && d.sa < yMin) yMin = d.sa;
            if (!_dto_only && d.sa > yMax) yMax = d.sa;
            if (d.s0 < yMin) yMin = d.s0;
            if (d.s0 > yMax) yMax = d.s0;
        }
        if (!isFinite(yMin) || !isFinite(yMax)) { yMin = -1e6; yMax = 1e6; }
        if (yMin === yMax) { yMin -= 1e6; yMax += 1e6; }
        // Symmetric-ish around zero if data straddles it
        if (yMin < 0 && yMax > 0) {
            const absMax = Math.max(Math.abs(yMin), Math.abs(yMax));
            yMin = -absMax * 1.1;
            yMax =  absMax * 1.1;
        } else {
            const pad = (yMax - yMin) * 0.1;
            yMin -= pad; yMax += pad;
        }

        // X range — market open to 4pm or data range, whichever wider
        const nowMs = Date.now();
        const todayMidnight = new Date(); todayMidnight.setHours(0, 0, 0, 0);
        const openMs = todayMidnight.getTime() + _sessionStartHr * 3600e3;
        const closeMs = todayMidnight.getTime() + _sessionEndHr * 3600e3;
        const tMin = Math.min(data[0].t, openMs);
        const tMax = Math.max(nowMs, closeMs, data[data.length - 1].t);
        const tSpan = Math.max(1, tMax - tMin);

        // Horizontal grid lines (8 divisions)
        _ctx.strokeStyle = COLOR_GRID;
        _ctx.lineWidth = 1;
        _ctx.fillStyle = COLOR_AXIS;
        _ctx.font = '10px "JetBrains Mono", monospace';
        _ctx.textAlign = 'left';
        for (let i = 0; i <= 8; i++) {
            const y = plotT + (plotH * i) / 8;
            const v = yMax - (yMax - yMin) * (i / 8);
            _ctx.beginPath();
            _ctx.moveTo(plotL, y);
            _ctx.lineTo(plotR, y);
            _ctx.stroke();
            _ctx.fillText(_fmtYAxis(v), plotR + 6, y + 3);
        }

        // Zero line (brighter)
        if (yMin < 0 && yMax > 0) {
            const yZero = plotT + plotH * (yMax - 0) / (yMax - yMin);
            _ctx.strokeStyle = COLOR_ZERO;
            _ctx.lineWidth = 1;
            _ctx.beginPath();
            _ctx.moveTo(plotL, yZero);
            _ctx.lineTo(plotR, yZero);
            _ctx.stroke();
        }

        // Vertical grid lines every 15 min
        const QUARTER = 15 * 60 * 1000;
        const firstMark = Math.ceil(tMin / QUARTER) * QUARTER;
        _ctx.strokeStyle = COLOR_GRID;
        _ctx.fillStyle = COLOR_AXIS;
        _ctx.textAlign = 'center';
        _ctx.font = '10px "JetBrains Mono", monospace';
        for (let t = firstMark; t <= tMax; t += QUARTER) {
            const x = plotL + plotW * ((t - tMin) / tSpan);
            if (x < plotL || x > plotR) continue;
            _ctx.beginPath();
            _ctx.moveTo(x, plotT);
            _ctx.lineTo(x, plotB);
            _ctx.stroke();
            const d = new Date(t);
            const isHour = d.getMinutes() === 0;
            if (isHour) {
                _ctx.fillText(_fmtTime(t), x, plotB + 14);
            } else {
                _ctx.globalAlpha = 0.5;
                _ctx.fillText(_fmtTime(t), x, plotB + 14);
                _ctx.globalAlpha = 1;
            }
        }

        // Current time marker (dashed vertical)
        const xNow = plotL + plotW * ((nowMs - tMin) / tSpan);
        if (xNow >= plotL && xNow <= plotR) {
            _ctx.strokeStyle = 'rgba(255,255,255,0.08)';
            _ctx.setLineDash([3, 3]);
            _ctx.beginPath();
            _ctx.moveTo(xNow, plotT);
            _ctx.lineTo(xNow, plotB);
            _ctx.stroke();
            _ctx.setLineDash([]);
        }

        // Curves
        function _drawLine(key, color) {
            _ctx.strokeStyle = color;
            _ctx.lineWidth = 1.8;
            _ctx.beginPath();
            for (let i = 0; i < data.length; i++) {
                const d = data[i];
                const x = plotL + plotW * ((d.t - tMin) / tSpan);
                const y = plotT + plotH * (yMax - d[key]) / (yMax - yMin);
                if (i === 0) _ctx.moveTo(x, y);
                else _ctx.lineTo(x, y);
            }
            _ctx.stroke();
        }

        if (!_dto_only) _drawLine('sa', COLOR_ALL);    // orange first (underneath)
        _drawLine('s0', COLOR_0DTE);                    // green on top

        // Right-side current value labels (colored backgrounds, like 0DT Hero)
        const cur = data[data.length - 1];
        function _drawRightLabel(val, color) {
            const label = _fmtMoney(val);
            const y = plotT + plotH * (yMax - val) / (yMax - yMin);
            const labelW = _ctx.measureText(label).width + 8;
            _ctx.fillStyle = color;
            _ctx.fillRect(plotR, y - 8, labelW + 2, 16);
            _ctx.fillStyle = '#0a0d14';
            _ctx.textAlign = 'left';
            _ctx.fillText(label, plotR + 4, y + 3);
        }
        if (!_dto_only) _drawRightLabel(cur.sa, COLOR_ALL);
        _drawRightLabel(cur.s0, COLOR_0DTE);

        // Legend bottom-left ("● 0dte  ■ All expirations")
        _ctx.fillStyle = COLOR_AXIS;
        _ctx.font = '10px "JetBrains Mono", monospace';
        _ctx.textAlign = 'left';
        const legendY = h - 10;
        _ctx.fillStyle = COLOR_0DTE;
        _ctx.fillRect(14, legendY - 8, 10, 10);
        _ctx.fillStyle = 'rgba(180,190,220,0.8)';
        _ctx.fillText('0dte', 28, legendY);
        if (!_dto_only) {
            _ctx.fillStyle = COLOR_ALL;
            _ctx.fillRect(62, legendY - 8, 10, 10);
            _ctx.fillStyle = 'rgba(180,190,220,0.8)';
            _ctx.fillText('All expirations', 76, legendY);
        }

        // Y-axis unit label top-right
        _ctx.fillStyle = COLOR_AXIS;
        _ctx.font = '9px "JetBrains Mono", monospace';
        _ctx.textAlign = 'left';
        _ctx.fillText('↑ Delta', plotR + 6, plotT - 16);
        _ctx.fillText('Notional', plotR + 6, plotT - 6);
    }

    function _loop() {
        if (_destroyed) return;
        _raf = requestAnimationFrame(_loop);
        if (!_canvas || !_ctx) return;
        if (_canvas.offsetParent === null) return;
        if (!_dirty) {
            // Redraw every 10s even without new data (for time-axis updates)
            const now = Date.now();
            if (now - (_loop._lastRender || 0) < 10_000) return;
        }
        _dirty = false;
        _loop._lastRender = Date.now();
        _render();
    }

    // ── UI controls ───────────────────────────────────────────────────────
    function _buildControls(slot) {
        const bar = document.createElement('div');
        bar.style.cssText = 'display:flex;gap:8px;padding:8px 14px;background:' + BG +
                            ';border-bottom:1px solid rgba(255,255,255,0.04);align-items:center;justify-content:center;flex-wrap:wrap';

        const mkBtn = (label, onClick, isActive, isDisabled) => {
            const b = document.createElement('button');
            b.textContent = label;
            const base = `font-family:"Inter",system-ui,sans-serif;font-size:12px;font-weight:700;` +
                         `padding:5px 14px;border-radius:4px;cursor:pointer;letter-spacing:0.02em;`;
            if (isDisabled) {
                b.style.cssText = base + `background:rgba(120,140,100,0.12);border:1px solid rgba(120,140,100,0.25);color:rgba(150,180,120,0.35);cursor:not-allowed`;
                b.title = label === 'SPX' ? 'SPX requires Schwab index-options tier upgrade' : '';
            } else if (isActive) {
                b.style.cssText = base + `background:#5cb85c;border:1px solid #5cb85c;color:#0a0d14`;
            } else {
                b.style.cssText = base + `background:rgba(120,180,90,0.15);border:1px solid rgba(120,180,90,0.4);color:#b6dd88`;
            }
            if (!isDisabled) b.onclick = onClick;
            return b;
        };

        const allBtns = [];
        for (const c of CATEGORIES) {
            const label = c === 'MAG7' ? 'Mag7' : c;
            const disabled = c === 'SPX';
            const b = mkBtn(label, () => { _selected = c; _dirty = true; _rebuild(); }, _selected === c, disabled);
            b._cat = c;
            allBtns.push(b);
            bar.appendChild(b);
        }

        // Divider
        const sep = document.createElement('span');
        sep.style.cssText = 'width:1px;height:20px;background:rgba(255,255,255,0.12);margin:0 4px';
        bar.appendChild(sep);

        // DTO toggle
        const dto = document.createElement('button');
        const updateDto = () => {
            const base = `font-family:"Inter",system-ui,sans-serif;font-size:11px;font-weight:600;` +
                         `padding:5px 12px;border-radius:20px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;`;
            const dot = _dto_only
                ? `<span style="width:10px;height:10px;border-radius:50%;background:#5cb85c;box-shadow:0 0 6px rgba(92,184,92,0.7)"></span>`
                : `<span style="width:10px;height:10px;border-radius:50%;background:rgba(255,255,255,0.2)"></span>`;
            dto.innerHTML = `${dot}<span>DTO</span>`;
            dto.style.cssText = base + `background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);color:${_dto_only ? '#5cb85c' : 'rgba(200,210,230,0.6)'}`;
        };
        dto.onclick = () => { _dto_only = !_dto_only; _dirty = true; updateDto(); };
        updateDto();
        bar.appendChild(dto);

        // Calendar placeholder
        const cal = document.createElement('span');
        cal.textContent = '📅';
        cal.style.cssText = 'font-size:14px;opacity:0.4;cursor:default;padding:0 4px';
        cal.title = 'Historical date picker — coming soon';
        bar.appendChild(cal);

        function _rebuild() {
            for (const b of allBtns) {
                const active = _selected === b._cat;
                const disabled = b._cat === 'SPX';
                const base = `font-family:"Inter",system-ui,sans-serif;font-size:12px;font-weight:700;` +
                             `padding:5px 14px;border-radius:4px;cursor:${disabled ? 'not-allowed' : 'pointer'};letter-spacing:0.02em;`;
                if (disabled) {
                    b.style.cssText = base + `background:rgba(120,140,100,0.12);border:1px solid rgba(120,140,100,0.25);color:rgba(150,180,120,0.35)`;
                } else if (active) {
                    b.style.cssText = base + `background:#5cb85c;border:1px solid #5cb85c;color:#0a0d14`;
                } else {
                    b.style.cssText = base + `background:rgba(120,180,90,0.15);border:1px solid rgba(120,180,90,0.4);color:#b6dd88`;
                }
            }
        }

        slot.appendChild(bar);
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────
    window.FlowPane = {
        init(slotEl) {
            _destroyed = false;
            _slotEl = slotEl;
            slotEl.innerHTML = '';
            slotEl.style.cssText = 'display:flex;flex-direction:column;width:100%;height:100%;background:' + BG + ';overflow:hidden;font-family:"Inter",system-ui,sans-serif';

            _buildControls(slotEl);

            const chartWrap = document.createElement('div');
            chartWrap.style.cssText = 'flex:1;width:100%;position:relative;overflow:hidden;background:' + BG;
            _canvas = document.createElement('canvas');
            _canvas.style.cssText = 'width:100%;height:100%;display:block';
            chartWrap.appendChild(_canvas);
            slotEl.appendChild(chartWrap);
            _ctx = _canvas.getContext('2d');

            _hydrateFromREST();
            _fetchFundamentals();
            if (window.AltarisEvents) {
                const handler = (data) => {
                    if (data && Array.isArray(data.tickers)) {
                        _pushSample(data.t || Date.now(), data.tickers);
                    }
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
