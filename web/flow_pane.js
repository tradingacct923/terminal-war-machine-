/**
 * Flow Pane — 0DT-Hero-style cumulative signed Δ notional chart.
 *
 * Layout matches 0DT Hero screenshot exactly:
 *   Top bar: [SPX] [SPY] [QQQ] | [DTO] [📅]
 *   Chart:   Green line = 0DTE, Orange line = All Expirations
 *   Y-axis:  Delta Notional ($M), right side
 *   X-axis:  Time HH:MM
 *   Header:  "{ticker} {weekday} {Month} {day}, {year}"
 *   Right edge: colored labels with current 0DTE + All-Exp values
 *
 * Data source: 'data:flow:update' bus event, backed by backend FlowAccumulator.
 *
 * Categories:
 *   SPX   — direct (we subscribe; accumulator normalizes SPXW→SPX)
 *   SPY   — direct
 *   QQQ   — direct
 *   DTO   — "0DTE Only" toggle: show green line only vs both lines
 */
(function () {
    'use strict';

    const CATEGORIES = ['SPX', 'QQQ'];

    // Colors match 0DT Hero for primary (A); secondary (B) uses a complementary set
    const COLOR_0DTE = '#00ff41';       // bright green  — A 0dte (retail-heavy)
    const COLOR_ALL = '#ff9830';        // orange        — A all-exp (super-set, incl 0dte)
    const COLOR_NON0 = '#7ab8ff';       // sky blue      — A non-0dte (institutional)
    const COLOR_0DTE_B = '#00d4ff';     // cyan          — B 0dte
    const COLOR_ALL_B = '#ff4d8d';      // hot pink      — B all-exp
    // Improvement #2 — Calls vs Puts decomposition colors
    const COLOR_CALL_BUY  = '#22dd55';  // bright green   — aggressive call buy
    const COLOR_CALL_SELL = '#5e8c5a';  // muted green    — call sell (vol/distribution)
    const COLOR_PUT_BUY   = '#ff4444';  // bright red     — aggressive put buy
    const COLOR_PUT_SELL  = '#aa6e5e';  // muted red      — put sell (premium harvest)
    const COLOR_GRID = 'rgba(255,255,255,0.05)';
    const COLOR_ZERO = 'rgba(255,255,255,0.15)';
    const COLOR_AXIS = 'rgba(180,190,220,0.55)';
    const BG = '#0a0d14';

    // Per-ticker rolling time-series. Each entry: {t, s0, sa}
    const _series = {};
    const _SERIES_MAX = 25000;  // ~6.9 hours @ 1Hz — full RTH session
                                 // 2026-05-05: bumped 14400→25000 so the chart
                                 // can hold an entire trading day's worth of
                                 // live ticks (open → close) without aging out
                                 // morning data. Memory cost ~3MB per ticker.
    let _selected = 'QQQ';      // primary ticker (A)
    let _selected_b = null;     // comparison ticker (B); null = single-ticker mode
    let _dto_only = false;      // false = show BOTH lines (default), true = 0DTE only
    // View modes (added cohort6 2026-05-01):
    //   'cohort' (default) → green/blue/orange (0dte / non-0dte / All)
    //   'decompose'        → 4 lines: call_buy / call_sell / put_buy / put_sell
    //   'cohort6'          → 6 lines: AM-0DTE / PM-0DTE / weekly / monthly / quarterly / LEAPS
    let _view_mode = 'cohort';
    // 6-cohort colors (cohort6 view) — chosen for hue separation:
    const COLOR_C0AM   = '#ffd700';  // gold       — AM-settled 0DTE (institutional vol-sell)
    const COLOR_C0PM   = '#22dd55';  // bright grn — PM-settled 0DTE (retail FOMO)
    const COLOR_CWK    = '#7ab8ff';  // sky blue   — weekly 1-7 DTE
    const COLOR_CMO    = '#c08fdb';  // lavender   — monthly 8-30 DTE
    const COLOR_CQT    = '#ff9830';  // orange     — quarterly 31-90 DTE
    const COLOR_CLP    = '#ff4488';  // pink       — LEAPS >90 DTE
    let _last_setup_mode = 'IDLE';
    let _last_setup_confidence = 0;

    // Fundamentals cache — keyed by ticker. Fetched on first mount + when ticker changes.
    const _fundamentals = {};   // {ticker: {peRatio, beta, divYield, marketCap, ...}}
    let _fundamentalsFetched = false;

    // Per-MIC venue concentration cache (added 2026-05-01).
    // Polled every 10s from /api/option_flow/by_exchange/<ticker>. Used to
    // render a slim "TOP: VENUE share% | CONC X.YZ" strip in the legend area.
    // Concentration score = top-1 venue's share. >= 0.60 = single-MM event
    // (institutional algo); < 0.30 = retail dispersed across many brokers.
    const _venue_data = {};   // {ticker: {top1_mic, concentration_score, venues: [...]}}
    let _venue_last_fetch_ts = 0;
    const _VENUE_REFRESH_MS = 10_000;

    let _slotEl = null;
    let _canvas = null;
    // Hover crosshair state (added 2026-05-04)
    let _cursorX = null;       // CSS-px x within canvas; null = no hover
    let _cursorY = null;       // CSS-px y within canvas
    let _cursorPinned = false; // true after click; false on mouseleave or click-empty
    let _lastLayout = null;    // {tMin,tMax,plotL,plotR,plotT,plotB,yMin,yMax}
    let _onMouseMove = null;
    let _onMouseLeave = null;
    let _onClick = null;
    let _onWheel = null;
    let _onDblClick = null;
    let _onMouseDown = null;
    let _onMouseUp = null;
    let _onMouseMovePan = null;
    // Zoom/pan state (added 2026-05-05)
    // _zoomTMin/_zoomTMax override the auto-fit range when set; null = auto.
    // Zoom: wheel up = zoom in (narrow range), wheel down = zoom out (widen range).
    // Pan: shift+drag horizontally to slide the visible window.
    // Reset: double-click anywhere on canvas → restores auto-fit.
    let _zoomTMin = null;
    let _zoomTMax = null;
    let _isPanning = false;
    let _panStartX = null;
    let _panStartTMin = null;
    let _panStartTMax = null;
    let _ctx = null;
    let _destroyed = false;
    let _raf = 0;
    let _dirty = true;
    let _unsubFlow = null;
    let _sessionStartHr = 9.5;  // 09:30 ET
    let _sessionEndHr = 16.0;   // 16:00 ET

    // ── Aggregation ───────────────────────────────────────────────────────
    function _computeAggregate(category) {
        return _series[category] || [];
    }

    // ── Data ingestion ────────────────────────────────────────────────────
    function _pushSample(t, tickerStates) {
        for (const s of tickerStates) {
            const tk = s.ticker;
            if (!_series[tk]) _series[tk] = [];
            const s0 = +s.cum_signed_0dte || 0;
            const sa = +s.cum_signed_all || 0;
            _series[tk].push({
                t,
                s0,                  // 0dte signed flow (retail-heavy)
                sa,                  // ALL signed flow = 0dte + non-0dte (super-set)
                snd: sa - s0,        // non-0dte signed flow (institutional-heavy)
                // Improvement #2 — atomic-action 4-bucket exposure
                cb:  +s.cum_call_buy  || 0,
                cs:  +s.cum_call_sell || 0,
                pb:  +s.cum_put_buy   || 0,
                ps:  +s.cum_put_sell  || 0,
                // 6-cohort drill-down (added 2026-05-01)
                c_0am: +s.cohort_0dte_am_signed   || 0,
                c_0pm: +s.cohort_0dte_pm_signed   || 0,
                c_wk:  +s.cohort_weekly_signed    || 0,
                c_mo:  +s.cohort_monthly_signed   || 0,
                c_qt:  +s.cohort_quarterly_signed || 0,
                c_lp:  +s.cohort_leaps_signed     || 0,
            });
            if (_series[tk].length > _SERIES_MAX) {
                _series[tk].splice(0, _series[tk].length - _SERIES_MAX);
            }
            // Track latest setup classification for primary ticker only
            if (tk === _selected) {
                _last_setup_mode       = s.setup_mode || 'IDLE';
                _last_setup_confidence = +s.setup_confidence || 0;
            }
        }
        _dirty = true;
    }

    function _hydrateFromREST() {
        // Live snapshot — single point of current cumulative state.
        // Used as a fallback when /history is empty (fresh server, <30s old).
        fetch('/api/option_flow', {headers: {'X-Auth-Token': sessionStorage.getItem('greeks-auth') || ''}})
            .then(r => r.json())
            .then(d => {
                if (!d || !d.tickers) return;
                _pushSample(Date.now(), d.tickers);
            })
            .catch(() => {});
    }

    // ── 2h history hydration (added 2026-05-01) ──────────────────────────
    // Pulls compact snapshots from /api/option_flow/history?ticker=X for
    // the selected ticker so the chart shows past 2 hours immediately on
    // page load. Each snapshot is the compact form
    //   {t, s0, sa, u0, ua, cb, cs, pb, ps, c_0am, c_0pm, c_wk, c_mo, c_qt, c_lp}
    // matching the keys flow_pane already renders.
    function _hydrateHistoryFor(ticker) {
        if (!ticker) return Promise.resolve();
        return fetch(`/api/option_flow/history?ticker=${ticker}`,
                     {headers: {'X-Auth-Token': sessionStorage.getItem('greeks-auth') || ''}})
            .then(r => r.json())
            .then(d => {
                if (!d || !Array.isArray(d.snapshots) || d.snapshots.length === 0) return;
                if (!_series[ticker]) _series[ticker] = [];
                // Each compact snapshot has fields s0, sa, u0, ua, cb, cs, pb, ps,
                // c_0am, c_0pm, c_wk, c_mo, c_qt, c_lp. Map to chart's render keys.
                // 2026-05-05 BUG FIX: hydrate is async and may complete AFTER live
                // ticks have already pushed into _series. Naively appending the
                // historical snapshots produces a time-disordered array — the
                // chart's drawing iterates in array-order, so disordered points
                // produce visual chaos (lines back-and-forth in time, etc.).
                // Solution: dedup-by-t (keep newer values for same timestamp)
                // and sort the merged array by `t` ascending after every hydrate.
                const existingByT = new Map();
                for (const e of _series[ticker]) {
                    if (e && e.t) existingByT.set(e.t, e);
                }
                for (const s of d.snapshots) {
                    const t = +s.t || 0;
                    if (!t) continue;
                    // Don't overwrite a live tick with an older history value at
                    // the same second; live values are more authoritative.
                    if (existingByT.has(t)) continue;
                    const entry = {
                        t,
                        s0:  +s.s0 || 0,
                        sa:  +s.sa || 0,
                        snd: (+s.sa || 0) - (+s.s0 || 0),
                        cb:  +s.cb || 0,
                        cs:  +s.cs || 0,
                        pb:  +s.pb || 0,
                        ps:  +s.ps || 0,
                        c_0am: +s.c_0am || 0,
                        c_0pm: +s.c_0pm || 0,
                        c_wk:  +s.c_wk  || 0,
                        c_mo:  +s.c_mo  || 0,
                        c_qt:  +s.c_qt  || 0,
                        c_lp:  +s.c_lp  || 0,
                    };
                    _series[ticker].push(entry);
                    existingByT.set(t, entry);
                }
                // Sort by t ascending so chart drawing produces a monotonic curve.
                _series[ticker].sort((a, b) => a.t - b.t);
                if (_series[ticker].length > _SERIES_MAX) {
                    _series[ticker].splice(0, _series[ticker].length - _SERIES_MAX);
                }
                // 2026-05-08: heal cb/cs/pb/ps discontinuities at render time.
                // Backend has occasional reset-to-near-zero in these atomic
                // counters (bridge restarts during the OCC-parse fix rollout
                // baked drops into the history buffer). Detect drops > $50M
                // between consecutive snapshots — those represent process
                // restarts, not real flow — and add a per-field offset so
                // subsequent samples connect smoothly to pre-drop. Pure
                // visual fix; raw fields stay readable for debugging.
                const HEAL_THRESHOLD = 50e6;  // $50M
                const HEAL_FIELDS = ['cb', 'cs', 'pb', 'ps'];
                const healOffsets = {cb: 0, cs: 0, pb: 0, ps: 0};
                const healLast = {cb: 0, cs: 0, pb: 0, ps: 0};
                for (let i = 0; i < _series[ticker].length; i++) {
                    const e = _series[ticker][i];
                    for (const f of HEAL_FIELDS) {
                        const cur = +e[f] || 0;
                        if (i > 0 && cur < healLast[f] - HEAL_THRESHOLD) {
                            healOffsets[f] += healLast[f];
                        }
                        healLast[f] = cur;
                        e[f] = cur + healOffsets[f];
                    }
                }
                _dirty = true;
                console.log(`[flow_pane] Hydrated ${d.snapshots.length} history snapshots for ${ticker}; series now ${_series[ticker].length}`);
            })
            .catch((e) => console.warn(`[flow_pane] history hydrate failed for ${ticker}:`, e));
    }

    function _fetchVenueData(ticker) {
        if (!ticker) return;
        return fetch(`/api/option_flow/by_exchange/${ticker}?top_n=5`,
                     {headers: {'X-Auth-Token': sessionStorage.getItem('greeks-auth') || ''}})
            .then(r => r.json())
            .then(d => {
                if (!d || d.error) return;
                _venue_data[ticker] = d;
                _dirty = true;
            })
            .catch(() => {});
    }

    function _fetchFundamentals() {
        if (_fundamentalsFetched) return;
        _fundamentalsFetched = true;
        const tickers = 'QQQ,SPY';
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

    function _fundamentalsForSelected() {
        // SPX has no fundamentals (it's an index, not an ETF) — returns null.
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

    function _labelFor(cat) {
        return cat;
    }

    function _fmtDateHeader() {
        const d = new Date();
        const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
        const months = ['January', 'February', 'March', 'April', 'May', 'June',
                        'July', 'August', 'September', 'October', 'November', 'December'];
        const fullName = (c) => c;
        const head = _selected_b
            ? `${fullName(_selected)} vs ${fullName(_selected_b)}`
            : fullName(_selected);
        return `${head} ${days[d.getDay()]}, ${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
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

        const dataA = _computeAggregate(_selected);
        const dataB = _selected_b ? _computeAggregate(_selected_b) : [];

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

        // Fundamentals badge (right-aligned on header row) — hidden in compare mode to save space
        if (!_selected_b) {
            const fund = _fundamentalsForSelected();
            if (fund) {
                const parts = [];
                if (fund.peRatio != null) parts.push(`P/E ${fund.peRatio.toFixed(1)}`);
                if (fund.beta != null)    parts.push(`β ${fund.beta.toFixed(2)}`);
                const dy = fund.dividendYield ?? fund.divYield;
                if (dy != null)           parts.push(`DivY ${dy.toFixed(2)}%`);
                if (fund.marketCap)       parts.push(`MCap ${_fmtMCap(fund.marketCap)}`);
                if (parts.length) {
                    _ctx.fillStyle = 'rgba(180,190,220,0.7)';
                    _ctx.font = '10px "JetBrains Mono", monospace';
                    _ctx.textAlign = 'right';
                    _ctx.fillText(parts.join('   '), plotR, 22);
                }
            }
        }

        if (!dataA.length && !dataB.length) {
            _ctx.fillStyle = 'rgba(180,190,220,0.35)';
            _ctx.font = '11px "JetBrains Mono", monospace';
            _ctx.textAlign = 'center';
            const msg = `waiting for ${_selected}${_selected_b ? '/' + _selected_b : ''} flow...`;
            _ctx.fillText(msg, w / 2, h / 2);
            return;
        }

        // Y range — include both A and B so both curves fit
        let yMin = Infinity, yMax = -Infinity;
        const _accRange = (arr) => {
            for (const d of arr) {
                if (_view_mode === 'decompose') {
                    // 4 unsigned magnitudes — all positive, range from 0 upward
                    yMin = Math.min(yMin, 0);
                    if (d.cb > yMax) yMax = d.cb;
                    if (d.cs > yMax) yMax = d.cs;
                    if (d.pb > yMax) yMax = d.pb;
                    if (d.ps > yMax) yMax = d.ps;
                } else if (_view_mode === 'cohort6') {
                    // 6 signed cohort lines — range across all 6
                    for (const k of ['c_0am','c_0pm','c_wk','c_mo','c_qt','c_lp']) {
                        const v = d[k] || 0;
                        if (v < yMin) yMin = v;
                        if (v > yMax) yMax = v;
                    }
                } else {
                    if (!_dto_only && d.sa < yMin) yMin = d.sa;
                    if (!_dto_only && d.sa > yMax) yMax = d.sa;
                    if (!_dto_only && d.snd < yMin) yMin = d.snd;
                    if (!_dto_only && d.snd > yMax) yMax = d.snd;
                    if (d.s0 < yMin) yMin = d.s0;
                    if (d.s0 > yMax) yMax = d.s0;
                }
            }
        };
        _accRange(dataA);
        _accRange(dataB);
        // Alias `data` to whichever series has samples so time-range logic below works unchanged
        const data = dataA.length ? dataA : dataB;
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

        // X range — fit to actual data range OR honor user zoom override.
        // 2026-05-05: added wheel-zoom + drag-pan. When _zoomTMin/_zoomTMax
        // are set (user manipulated), use those. Double-click resets to auto.
        const nowMs = Date.now();
        const _firstT = (arr) => arr.length ? arr[0].t : Infinity;
        const _lastT  = (arr) => arr.length ? arr[arr.length - 1].t : -Infinity;
        const dataTMin = Math.min(_firstT(dataA), _firstT(dataB));
        const dataTMax = Math.max(nowMs, _lastT(dataA), _lastT(dataB));
        const dataSpan = Math.max(1, dataTMax - dataTMin);
        // Right-pad 5% so the latest sample doesn't touch the right edge.
        // Left edge stays at first data point (no pre-session empty space).
        let tMin, tMax;
        if (_zoomTMin !== null && _zoomTMax !== null) {
            // Honor user's zoom/pan override — clamp to data bounds.
            tMin = Math.max(_zoomTMin, dataTMin - dataSpan * 0.5);
            tMax = Math.min(_zoomTMax, dataTMax + dataSpan * 0.5);
            if (tMax - tMin < 5000) {  // 5s minimum span
                const mid = (tMax + tMin) / 2;
                tMin = mid - 2500;
                tMax = mid + 2500;
            }
        } else {
            tMin = dataTMin;
            tMax = dataTMax + dataSpan * 0.05;
        }
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
        function _drawLine(arr, key, color, dashed) {
            if (!arr.length) return;
            _ctx.strokeStyle  = color;
            _ctx.lineWidth    = 1.6;
            _ctx.lineJoin     = 'round';   // smooth corners — kills staircase look
            _ctx.lineCap      = 'round';
            if (dashed) _ctx.setLineDash([4, 3]);
            _ctx.beginPath();

            // Pre-project to canvas coords so we can apply midpoint smoothing.
            // For cumulative-flow series (long flat runs + sharp jumps), drawing
            // straight `lineTo` between samples produces visible stair-steps.
            // Quadratic bezier through MIDPOINTS removes the blockiness without
            // distorting data magnitudes (curve always passes through real samples'
            // midpoints; controls are the actual sample points).
            const pts = new Array(arr.length);
            for (let i = 0; i < arr.length; i++) {
                const d = arr[i];
                pts[i] = [
                    plotL + plotW * ((d.t - tMin) / tSpan),
                    plotT + plotH * (yMax - d[key]) / (yMax - yMin),
                ];
            }

            if (pts.length === 1) {
                _ctx.moveTo(pts[0][0], pts[0][1]);
                _ctx.lineTo(pts[0][0] + 0.5, pts[0][1]);
            } else if (pts.length === 2) {
                _ctx.moveTo(pts[0][0], pts[0][1]);
                _ctx.lineTo(pts[1][0], pts[1][1]);
            } else {
                _ctx.moveTo(pts[0][0], pts[0][1]);
                // Use quadraticCurveTo with each sample as control point and
                // the midpoint to next sample as end-of-curve. Final tail uses
                // the last sample as endpoint.
                for (let i = 1; i < pts.length - 1; i++) {
                    const xc = (pts[i][0] + pts[i + 1][0]) / 2;
                    const yc = (pts[i][1] + pts[i + 1][1]) / 2;
                    _ctx.quadraticCurveTo(pts[i][0], pts[i][1], xc, yc);
                }
                // Last segment — straight to the final sample
                _ctx.quadraticCurveTo(
                    pts[pts.length - 1][0], pts[pts.length - 1][1],
                    pts[pts.length - 1][0], pts[pts.length - 1][1]
                );
            }
            _ctx.stroke();
            if (dashed) _ctx.setLineDash([]);
        }

        if (_view_mode === 'decompose') {
            // Improvement #2 — atomic-action breakdown
            _drawLine(dataA, 'cb', COLOR_CALL_BUY,  false);  // call_buy
            _drawLine(dataA, 'ps', COLOR_PUT_SELL,  false);  // put_sell (bullish too)
            _drawLine(dataA, 'cs', COLOR_CALL_SELL, false);  // call_sell
            _drawLine(dataA, 'pb', COLOR_PUT_BUY,   false);  // put_buy (bearish too)
        } else if (_view_mode === 'cohort6') {
            // Added 2026-05-01 — true 6-cohort drill-down:
            //   AM-settled 0DTE (gold)        — institutional vol-selling (SPX/NDX index)
            //   PM-settled 0DTE (green)       — retail FOMO (QQQ/SPY ETF)
            //   weekly 1-7 DTE (sky blue)     — speculative directional bets
            //   monthly 8-30 DTE (lavender)   — directional positioning
            //   quarterly 31-90 (orange)      — institutional hedges
            //   LEAPS >90 DTE (pink)          — long-term strategic
            _drawLine(dataA, 'c_lp',  COLOR_CLP,  false);   // LEAPS first (background)
            _drawLine(dataA, 'c_qt',  COLOR_CQT,  false);   // quarterly
            _drawLine(dataA, 'c_mo',  COLOR_CMO,  false);   // monthly
            _drawLine(dataA, 'c_wk',  COLOR_CWK,  false);   // weekly
            _drawLine(dataA, 'c_0am', COLOR_C0AM, false);   // AM 0DTE
            _drawLine(dataA, 'c_0pm', COLOR_C0PM, false);   // PM 0DTE (most-active, on top)
        } else {
            // Cohort mode (default) — 0dte / non-0dte / All
            // Render order: ALL first (orange, super-set), then non-0dte (blue,
            // institutional), then 0dte (green, retail) on top so it dominates
            // when retail flow is the visible signal.
            if (!_dto_only) _drawLine(dataA, 'sa',  COLOR_ALL,  false);  // orange  all-exp = 0dte + non-0dte
            if (!_dto_only) _drawLine(dataA, 'snd', COLOR_NON0, false);  // blue    non-0dte (institutional)
            _drawLine(dataA, 's0', COLOR_0DTE, false);                   // green   0dte (retail-heavy, on top)
            // Secondary (B) — dashed so overlaps are readable
            if (_selected_b) {
                if (!_dto_only) _drawLine(dataB, 'sa', COLOR_ALL_B, true);  // pink all-exp
                _drawLine(dataB, 's0', COLOR_0DTE_B, true);                 // cyan 0dte
            }
        }

        // Right-side current value labels (colored backgrounds, like 0DT Hero)
        // Also renders a faint dashed horizontal reference line at the current
        // value extending across the chart — matches 0DT Hero's reference lines.
        function _drawRightLabel(val, color) {
            const label = _fmtMoney(val);
            const y = plotT + plotH * (yMax - val) / (yMax - yMin);
            // Dashed reference line
            _ctx.save();
            _ctx.strokeStyle = color;
            _ctx.globalAlpha = 0.55;
            _ctx.lineWidth = 1;
            _ctx.setLineDash([3, 3]);
            _ctx.beginPath();
            _ctx.moveTo(plotL, y);
            _ctx.lineTo(plotR, y);
            _ctx.stroke();
            _ctx.restore();
            // Value pill on right edge
            const labelW = _ctx.measureText(label).width + 8;
            _ctx.fillStyle = color;
            _ctx.fillRect(plotR, y - 8, labelW + 2, 16);
            _ctx.fillStyle = '#0a0d14';
            _ctx.textAlign = 'left';
            _ctx.fillText(label, plotR + 4, y + 3);
        }
        const curA = dataA.length ? dataA[dataA.length - 1] : null;
        const curB = dataB.length ? dataB[dataB.length - 1] : null;
        if (curA) {
            if (_view_mode === 'decompose') {
                _drawRightLabel(curA.cb, COLOR_CALL_BUY);
                _drawRightLabel(curA.cs, COLOR_CALL_SELL);
                _drawRightLabel(curA.pb, COLOR_PUT_BUY);
                _drawRightLabel(curA.ps, COLOR_PUT_SELL);
            } else if (_view_mode === 'cohort6') {
                _drawRightLabel(curA.c_lp,  COLOR_CLP);
                _drawRightLabel(curA.c_qt,  COLOR_CQT);
                _drawRightLabel(curA.c_mo,  COLOR_CMO);
                _drawRightLabel(curA.c_wk,  COLOR_CWK);
                _drawRightLabel(curA.c_0am, COLOR_C0AM);
                _drawRightLabel(curA.c_0pm, COLOR_C0PM);
            } else {
                if (!_dto_only) _drawRightLabel(curA.sa,  COLOR_ALL);
                if (!_dto_only) _drawRightLabel(curA.snd, COLOR_NON0);
                _drawRightLabel(curA.s0, COLOR_0DTE);
            }
        }
        if (curB) {
            if (!_dto_only) _drawRightLabel(curB.sa, COLOR_ALL_B);
            _drawRightLabel(curB.s0, COLOR_0DTE_B);
        }

        // Legend bottom-left
        _ctx.fillStyle = COLOR_AXIS;
        _ctx.font = '10px "JetBrains Mono", monospace';
        _ctx.textAlign = 'left';
        const legendY = h - 10;
        let lx = 14;
        const _chip = (color, text, dashed) => {
            _ctx.fillStyle = color;
            _ctx.fillRect(lx, legendY - 8, 10, 10);
            if (dashed) {
                // Render dash indicator on the chip
                _ctx.fillStyle = BG;
                _ctx.fillRect(lx + 3, legendY - 7, 2, 8);
                _ctx.fillRect(lx + 7, legendY - 7, 2, 8);
            }
            _ctx.fillStyle = 'rgba(180,190,220,0.8)';
            _ctx.fillText(text, lx + 14, legendY);
            lx += 18 + _ctx.measureText(text).width + 10;
        };
        if (_selected_b) {
            // Compare mode — label each chip with the ticker to disambiguate
            const labA = _labelFor(_selected);
            const labB = _labelFor(_selected_b);
            _chip(COLOR_0DTE, `${labA} 0dte`, false);
            if (!_dto_only) _chip(COLOR_ALL, `${labA} all-exp`, false);
            _chip(COLOR_0DTE_B, `${labB} 0dte`, true);
            if (!_dto_only) _chip(COLOR_ALL_B, `${labB} all-exp`, true);
        } else if (_view_mode === 'decompose') {
            // Improvement #2 — atomic-action breakdown
            _chip(COLOR_CALL_BUY,  'call BUY (bullish)',     false);
            _chip(COLOR_PUT_SELL,  'put SELL (bullish)',     false);
            _chip(COLOR_CALL_SELL, 'call SELL (bearish/vol)',false);
            _chip(COLOR_PUT_BUY,   'put BUY (bearish/hedge)',false);
        } else if (_view_mode === 'cohort6') {
            // 6-cohort drill-down — true retail vs institutional split
            _chip(COLOR_C0PM, 'PM 0DTE (retail FOMO)',          false);
            _chip(COLOR_C0AM, 'AM 0DTE (inst. vol-sell)',       false);
            _chip(COLOR_CWK,  'weekly 1-7 DTE',                 false);
            _chip(COLOR_CMO,  'monthly 8-30 DTE',               false);
            _chip(COLOR_CQT,  'quarterly 31-90 DTE',            false);
            _chip(COLOR_CLP,  'LEAPS >90 DTE',                  false);
        } else {
            // Single ticker — three-line breakdown.
            // 2026-05-01: replaced misleading "retail/institutional" labels.
            // Truth is BOTH cohorts are mixed retail+institutional. The 0DTE
            // line is just "0DTE" (today's expiration, both retail FOMO and
            // institutional vol-selling/scalping). Non-0DTE is "1+DTE".
            // The accurate institutional vs retail split is in the 6-cohort
            // drill-down (toggle to cohort view for that).
            // Per-ticker cohort hint:
            //   SPX/NDX 0DTE = AM-settled (mostly institutional vol selling)
            //   QQQ/SPY 0DTE = PM-settled (retail FOMO heavy in last hour)
            const _ticker_hint = (_selected === 'SPX' || _selected === 'NDX') ? ' (AM-settled)' :
                                  (_selected === 'QQQ' || _selected === 'SPY') ? ' (PM-settled)' : '';
            _chip(COLOR_0DTE, `0DTE${_ticker_hint}`, false);
            if (!_dto_only) _chip(COLOR_NON0, '1+DTE (weekly/monthly/quarterly/LEAPS)', false);
            if (!_dto_only) _chip(COLOR_ALL,  'All (0DTE + 1+DTE)', false);
        }

        // ── VENUE concentration strip (added 2026-05-01) ─────────────────
        // Right-aligned in legend area. Format: "TOP: AMEX 15% PHLX 9% | CONC 0.15 (retail)"
        // CONC ≥ 0.60 = single-MM event (institutional algo) — colored RED
        // CONC < 0.30 = retail dispersed                       — colored GREEN
        // 0.30-0.60 = mixed                                    — colored YELLOW
        const vd = _venue_data[_selected];
        if (vd && Array.isArray(vd.venues) && vd.venues.length > 0) {
            const conc = +vd.concentration_score || 0;
            const concColor = conc >= 0.60 ? '#ff4444' :
                              conc <  0.30 ? '#22dd55' : '#ffd700';
            const concLabel = conc >= 0.60 ? '(single-MM)' :
                              conc <  0.30 ? '(dispersed)' : '(mixed)';
            const top3 = vd.venues.slice(0, 3)
                .map(v => `${v.mic} ${(v.share_signed_pct||0).toFixed(0)}%`)
                .join(' ');
            const venueText = `TOP: ${top3}`;
            const concText  = `CONC ${conc.toFixed(2)} ${concLabel}`;
            // Render right-aligned at end of legend strip
            _ctx.textAlign = 'right';
            const rx = w - 14;
            _ctx.fillStyle = concColor;
            _ctx.fillText(concText, rx, legendY);
            const concW = _ctx.measureText(concText).width;
            _ctx.fillStyle = 'rgba(180,190,220,0.65)';
            _ctx.fillText(venueText + '  |  ', rx - concW, legendY);
            _ctx.textAlign = 'left';  // restore
        }
        // Improvement #2 — setup_mode badge (top-right corner)
        if (_view_mode === 'decompose' && _last_setup_mode && _last_setup_mode !== 'IDLE') {
            const modeColors = {
                'AGG_LONG':     { bg: 'rgba(34,221,85,0.18)',  fg: '#22dd55' },
                'AGG_SHORT':    { bg: 'rgba(255,68,68,0.18)',  fg: '#ff4444' },
                'HEDGED_LONG':  { bg: 'rgba(122,184,255,0.15)', fg: '#7ab8ff' },
                'HEDGED_SHORT': { bg: 'rgba(255,152,48,0.18)', fg: '#ff9830' },
                'VOL_HARVEST':  { bg: 'rgba(180,130,200,0.15)', fg: '#c08fdb' },
                'VOL_LONG':     { bg: 'rgba(180,130,200,0.15)', fg: '#c08fdb' },
                'MIXED':        { bg: 'rgba(150,150,150,0.15)', fg: '#888' },
            };
            const mc = modeColors[_last_setup_mode] || modeColors.MIXED;
            const badgeText = `${_last_setup_mode} (${_last_setup_confidence}%)`;
            _ctx.font = '600 11px "Inter", monospace';
            const badgeW = _ctx.measureText(badgeText).width + 16;
            const badgeY = 8, badgeH = 20;
            const badgeX = w - badgeW - 12;
            _ctx.fillStyle = mc.bg;
            _ctx.fillRect(badgeX, badgeY, badgeW, badgeH);
            _ctx.strokeStyle = mc.fg;
            _ctx.lineWidth = 1;
            _ctx.strokeRect(badgeX, badgeY, badgeW, badgeH);
            _ctx.fillStyle = mc.fg;
            _ctx.textAlign = 'left';
            _ctx.textBaseline = 'middle';
            _ctx.fillText(badgeText, badgeX + 8, badgeY + badgeH / 2);
        }

        // Y-axis unit label top-right
        _ctx.fillStyle = COLOR_AXIS;
        _ctx.font = '9px "JetBrains Mono", monospace';
        _ctx.textAlign = 'left';
        _ctx.fillText('↑ Delta', plotR + 6, plotT - 16);
        _ctx.fillText('Notional', plotR + 6, plotT - 6);

        // ── Save layout for hover-crosshair (added 2026-05-04) ──
        _lastLayout = {
            tMin, tMax, plotL, plotR, plotT, plotB, plotW, plotH,
            yMin, yMax,
            seriesA: dataA, seriesB: dataB,
            mode: _view_mode,
        };
        // Draw crosshair LAST so it's on top of everything
        if (_cursorX != null && _cursorY != null) _drawCrosshair(w, h);
    }

    // ── Crosshair / hover-time tooltip (added 2026-05-04) ──
    function _drawCrosshair(w, h) {
        if (!_lastLayout || !_ctx) return;
        const L = _lastLayout;
        const x = _cursorX, y = _cursorY;
        // Only draw within plot area
        if (x < L.plotL || x > L.plotR || y < L.plotT || y > L.plotB) return;
        // Time at cursor
        const tCur = L.tMin + (x - L.plotL) / L.plotW * (L.tMax - L.tMin);
        const dt = new Date(tCur);
        const hh = String(dt.getHours()).padStart(2, '0');
        const mm = String(dt.getMinutes()).padStart(2, '0');
        const ss = String(dt.getSeconds()).padStart(2, '0');
        const timeStr = `${hh}:${mm}:${ss}`;
        // Y value at cursor (just for axis label)
        const vCur = L.yMax - (y - L.plotT) / L.plotH * (L.yMax - L.yMin);
        // Vertical line
        _ctx.save();
        _ctx.strokeStyle = _cursorPinned ? 'rgba(255,210,80,0.95)' : 'rgba(220,230,255,0.55)';
        _ctx.setLineDash([3, 3]);
        _ctx.lineWidth = 1;
        _ctx.beginPath();
        _ctx.moveTo(x, L.plotT);
        _ctx.lineTo(x, L.plotB);
        _ctx.stroke();
        _ctx.setLineDash([]);
        // Time bubble at bottom
        _ctx.fillStyle = _cursorPinned ? '#ffd200' : '#dde8ff';
        _ctx.fillRect(x - 32, L.plotB + 2, 64, 14);
        _ctx.fillStyle = '#0a0e1a';
        _ctx.font = '10px "JetBrains Mono", monospace';
        _ctx.textAlign = 'center';
        _ctx.textBaseline = 'middle';
        _ctx.fillText(timeStr, x, L.plotB + 9);
        // Find the nearest data point per visible series
        const nearestOf = (arr) => {
            if (!arr || !arr.length) return null;
            // Binary search by time
            let lo = 0, hi = arr.length - 1;
            while (lo < hi) {
                const mid = (lo + hi) >> 1;
                if (arr[mid].t < tCur) lo = mid + 1; else hi = mid;
            }
            const cand = [arr[lo]];
            if (lo > 0) cand.push(arr[lo - 1]);
            return cand.reduce((best, c) => Math.abs(c.t - tCur) < Math.abs(best.t - tCur) ? c : best);
        };
        const nA = nearestOf(L.seriesA);
        // Tooltip rows (per cohort, depending on view mode)
        const rows = [];
        if (nA) {
            rows.push({ label: '─time', value: timeStr, color: '#ffffff' });
            const dtData = new Date(nA.t);
            const dtStr = `${String(dtData.getHours()).padStart(2,'0')}:${String(dtData.getMinutes()).padStart(2,'0')}:${String(dtData.getSeconds()).padStart(2,'0')}`;
            if (dtStr !== timeStr) rows.push({ label: 'snap', value: dtStr, color: 'rgba(180,190,220,0.7)' });
            if (L.mode === 'cohort') {
                rows.push({ label: '0DTE',     value: _fmtMoney(nA.s0),         color: COLOR_0DTE });
                rows.push({ label: 'non-0DTE', value: _fmtMoney(nA.snd || (nA.sa - nA.s0)), color: '#3aa8ff' });
                rows.push({ label: 'All',      value: _fmtMoney(nA.sa),         color: COLOR_ALL });
            } else if (L.mode === 'cohort6') {
                rows.push({ label: 'PM-0DTE', value: _fmtMoney(nA.c_0pm || 0), color: '#22dd55' });
                rows.push({ label: 'AM-0DTE', value: _fmtMoney(nA.c_0am || 0), color: '#ffd700' });
                rows.push({ label: 'weekly',  value: _fmtMoney(nA.c_wk  || 0), color: '#3aa8ff' });
                rows.push({ label: 'monthly', value: _fmtMoney(nA.c_mo  || 0), color: '#ffa040' });
                rows.push({ label: 'qtrly',   value: _fmtMoney(nA.c_qt  || 0), color: '#cc66ff' });
                rows.push({ label: 'LEAPS',   value: _fmtMoney(nA.c_lp  || 0), color: '#ff5577' });
            } else { // decompose
                rows.push({ label: 'call_buy',  value: _fmtMoney(nA.cb || 0), color: '#22dd55' });
                rows.push({ label: 'call_sell', value: _fmtMoney(nA.cs || 0), color: '#88ff77' });
                rows.push({ label: 'put_buy',   value: _fmtMoney(nA.pb || 0), color: '#ff5577' });
                rows.push({ label: 'put_sell',  value: _fmtMoney(nA.ps || 0), color: '#ff9988' });
            }
        }
        if (rows.length) {
            const padX = 6, padY = 5, lineH = 13;
            // Measure widest label+value
            _ctx.font = '10px "JetBrains Mono", monospace';
            let wMax = 0;
            for (const r of rows) {
                const text = `${r.label}: ${r.value}`;
                wMax = Math.max(wMax, _ctx.measureText(text).width);
            }
            const tipW = wMax + padX * 2 + 8;
            const tipH = rows.length * lineH + padY * 2;
            // Position to right of cursor; flip to left if it would overflow
            let tipX = x + 12;
            if (tipX + tipW > L.plotR) tipX = x - tipW - 12;
            let tipY = y - tipH / 2;
            if (tipY < L.plotT + 4) tipY = L.plotT + 4;
            if (tipY + tipH > L.plotB - 4) tipY = L.plotB - 4 - tipH;
            // Background
            _ctx.fillStyle = 'rgba(15,18,28,0.92)';
            _ctx.strokeStyle = _cursorPinned ? '#ffd200' : 'rgba(120,140,180,0.5)';
            _ctx.lineWidth = 1;
            _ctx.fillRect(tipX, tipY, tipW, tipH);
            _ctx.strokeRect(tipX + 0.5, tipY + 0.5, tipW - 1, tipH - 1);
            // Rows
            _ctx.textAlign = 'left';
            _ctx.textBaseline = 'middle';
            for (let i = 0; i < rows.length; i++) {
                const r = rows[i];
                const ry = tipY + padY + i * lineH + lineH / 2;
                _ctx.fillStyle = r.color;
                _ctx.fillText(r.label, tipX + padX, ry);
                _ctx.fillStyle = '#ffffff';
                _ctx.fillText(r.value, tipX + padX + 60, ry);
            }
        }
        _ctx.restore();
    }

    function _loop() {
        if (_destroyed) return;
        _raf = requestAnimationFrame(_loop);
        if (!_canvas || !_ctx) return;
        if (_canvas.offsetParent === null) return;
        // Refresh venue data every 10s (only when pane is visible)
        const _now_for_venue = Date.now();
        if (_now_for_venue - _venue_last_fetch_ts >= _VENUE_REFRESH_MS) {
            _venue_last_fetch_ts = _now_for_venue;
            _fetchVenueData(_selected);
            if (_selected_b) _fetchVenueData(_selected_b);
        }
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

        const mkBtn = (label, onClick, isActive) => {
            const b = document.createElement('button');
            b.textContent = label;
            const base = `font-family:"Inter",system-ui,sans-serif;font-size:12px;font-weight:700;` +
                         `padding:5px 14px;border-radius:4px;cursor:pointer;letter-spacing:0.02em;`;
            if (isActive) {
                b.style.cssText = base + `background:#5cb85c;border:1px solid #5cb85c;color:#0a0d14`;
            } else {
                b.style.cssText = base + `background:rgba(120,180,90,0.15);border:1px solid rgba(120,180,90,0.4);color:#b6dd88`;
            }
            b.onclick = onClick;
            return b;
        };

        const allBtns = [];
        for (const c of CATEGORIES) {
            const b = document.createElement('button');
            b.textContent = c;
            b._cat = c;
            b.title = 'Click: set primary  |  Shift+Click: compare (secondary)';
            b.onclick = (evt) => {
                if (evt.shiftKey) {
                    // Toggle secondary: same ticker as A → no-op; same as B → clear; else set B
                    if (c === _selected) return;
                    _selected_b = (_selected_b === c) ? null : c;
                } else {
                    // Primary: if it equals current B, swap so we don't duplicate
                    if (c === _selected_b) _selected_b = _selected;
                    _selected = c;
                }
                // Hydrate 2h history for the newly-selected ticker (idempotent —
                // _hydrateHistoryFor pushes into _series, dedup happens via
                // pushSample's natural ordering).
                _hydrateHistoryFor(_selected);
                if (_selected_b) _hydrateHistoryFor(_selected_b);
                _dirty = true;
                _rebuild();
            };
            allBtns.push(b);
            bar.appendChild(b);
        }

        // Divider
        const sep = document.createElement('span');
        sep.style.cssText = 'width:1px;height:20px;background:rgba(255,255,255,0.12);margin:0 4px';
        bar.appendChild(sep);

        // Clear-compare chip (only visible when B is set)
        const clearVs = document.createElement('button');
        clearVs.textContent = '✕ VS';
        clearVs.title = 'Clear comparison';
        clearVs.onclick = () => { _selected_b = null; _dirty = true; _rebuild(); };
        bar.appendChild(clearVs);

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

        // View-mode cycle button: cohort → decompose → cohort6 → cohort
        // Single button cycles through all 3 modes. Label changes to reflect
        // the CURRENT mode (i.e. what's showing) for clarity.
        const decompBtn = document.createElement('button');
        const updateDecomp = () => {
            const base = `font-family:"Inter",system-ui,sans-serif;font-size:11px;font-weight:600;` +
                         `padding:5px 12px;border-radius:20px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;`;
            let label, dotColor, fgColor, title;
            if (_view_mode === 'decompose') {
                label = 'C/P';   dotColor = '#22dd55'; fgColor = '#22dd55';
                title = 'Atomic-action mode: 4 lines (call_buy/call_sell/put_buy/put_sell)';
            } else if (_view_mode === 'cohort6') {
                label = '6-DTE'; dotColor = '#ffd700'; fgColor = '#ffd700';
                title = '6-cohort mode: AM/PM 0DTE + weekly + monthly + quarterly + LEAPS';
            } else {
                label = '2-DTE'; dotColor = 'rgba(255,255,255,0.2)'; fgColor = 'rgba(200,210,230,0.6)';
                title = 'Click to cycle: 2-DTE → C/P → 6-DTE';
            }
            const dot = `<span style="width:10px;height:10px;border-radius:50%;background:${dotColor};${dotColor.startsWith('rgba') ? '' : 'box-shadow:0 0 6px ' + dotColor + '99'}"></span>`;
            decompBtn.innerHTML = `${dot}<span>${label}</span>`;
            decompBtn.style.cssText = base + `background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);color:${fgColor}`;
            decompBtn.title = title;
        };
        decompBtn.onclick = () => {
            // Cycle: cohort → decompose → cohort6 → cohort
            if (_view_mode === 'cohort')         _view_mode = 'decompose';
            else if (_view_mode === 'decompose') _view_mode = 'cohort6';
            else                                 _view_mode = 'cohort';
            _dirty = true;
            updateDecomp();
        };
        updateDecomp();
        bar.appendChild(decompBtn);

        // Calendar — opens a native date input and reloads the page in
        // historical-replay mode (AI Panel reads ?replay_date= param and
        // switches its data source to /api/alerts/history).
        const cal = document.createElement('input');
        cal.type = 'date';
        cal.style.cssText = 'background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);' +
                            'color:rgba(200,210,230,0.7);font-family:Inter,system-ui,sans-serif;' +
                            'font-size:11px;padding:4px 6px;border-radius:4px;cursor:pointer;';
        const urlParams = new URLSearchParams(window.location.search);
        const _replayDate = urlParams.get('replay_date') || '';
        if (_replayDate && _replayDate.length === 8) {
            cal.value = `${_replayDate.slice(0,4)}-${_replayDate.slice(4,6)}-${_replayDate.slice(6,8)}`;
        }
        cal.title = 'Load a past session\'s alert log';
        cal.onchange = () => {
            const v = cal.value; // YYYY-MM-DD
            if (!v) return;
            const compact = v.replace(/-/g, ''); // YYYYMMDD
            const u = new URL(window.location.href);
            if (compact === new Date().toISOString().slice(0,10).replace(/-/g,'')) {
                u.searchParams.delete('replay_date');
            } else {
                u.searchParams.set('replay_date', compact);
            }
            window.location.href = u.toString();
        };
        bar.appendChild(cal);

        function _rebuild() {
            const base = `font-family:"Inter",system-ui,sans-serif;font-size:12px;font-weight:700;` +
                         `padding:5px 14px;border-radius:4px;cursor:pointer;letter-spacing:0.02em;`;
            for (const b of allBtns) {
                if (_selected === b._cat) {
                    // Primary (A) — green
                    b.style.cssText = base + `background:#5cb85c;border:1px solid #5cb85c;color:#0a0d14`;
                } else if (_selected_b === b._cat) {
                    // Secondary (B) — cyan
                    b.style.cssText = base + `background:#00d4ff;border:1px solid #00d4ff;color:#0a0d14`;
                } else {
                    // Inactive — muted green outline
                    b.style.cssText = base + `background:rgba(120,180,90,0.15);border:1px solid rgba(120,180,90,0.4);color:#b6dd88`;
                }
            }
            const chipBase = `font-family:"Inter",system-ui,sans-serif;font-size:11px;font-weight:600;` +
                             `padding:5px 10px;border-radius:4px;cursor:pointer;letter-spacing:0.02em;`;
            if (_selected_b) {
                clearVs.style.cssText = chipBase + `background:rgba(0,212,255,0.15);border:1px solid rgba(0,212,255,0.5);color:#00d4ff;display:inline-block`;
            } else {
                clearVs.style.cssText = 'display:none';
            }
        }
        _rebuild();

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
            _canvas.style.cssText = 'width:100%;height:100%;display:block;cursor:crosshair';
            chartWrap.appendChild(_canvas);
            slotEl.appendChild(chartWrap);
            _ctx = _canvas.getContext('2d');

            // ── Hover-time crosshair handlers (added 2026-05-04) ──
            _onMouseMove = (e) => {
                if (_cursorPinned) return; // don't track while pinned
                const rect = _canvas.getBoundingClientRect();
                _cursorX = e.clientX - rect.left;
                _cursorY = e.clientY - rect.top;
                _dirty = true;
            };
            _onMouseLeave = () => {
                if (_cursorPinned) return;
                _cursorX = null; _cursorY = null;
                _dirty = true;
            };
            _onClick = (e) => {
                const rect = _canvas.getBoundingClientRect();
                const cx = e.clientX - rect.left;
                const cy = e.clientY - rect.top;
                // Click inside plot area = pin/unpin; outside = clear
                if (_lastLayout && cx >= _lastLayout.plotL && cx <= _lastLayout.plotR &&
                    cy >= _lastLayout.plotT && cy <= _lastLayout.plotB) {
                    _cursorPinned = !_cursorPinned;
                    _cursorX = cx; _cursorY = cy;
                } else {
                    _cursorPinned = false;
                    _cursorX = null; _cursorY = null;
                }
                _dirty = true;
            };
            _canvas.addEventListener('mousemove', _onMouseMove);
            _canvas.addEventListener('mouseleave', _onMouseLeave);
            _canvas.addEventListener('click', _onClick);

            // ── Zoom + pan handlers (added 2026-05-05) ─────────────────────
            // Wheel: zoom in/out around cursor X position.
            //   wheel UP   → zoom IN  (narrow time range, see more detail)
            //   wheel DOWN → zoom OUT (widen time range, see more context)
            // Drag: hold left-mouse and drag horizontally to pan the window.
            // Double-click: reset zoom/pan to auto-fit.
            _onWheel = (e) => {
                if (!_lastLayout) return;
                e.preventDefault();
                const rect = _canvas.getBoundingClientRect();
                const cx = e.clientX - rect.left;
                if (cx < _lastLayout.plotL || cx > _lastLayout.plotR) return;
                // Establish current zoom range (initialize from auto-fit if not set)
                if (_zoomTMin === null || _zoomTMax === null) {
                    _zoomTMin = _lastLayout.tMin;
                    _zoomTMax = _lastLayout.tMax;
                }
                // Cursor's t in current range
                const cursorFrac = (cx - _lastLayout.plotL) / (_lastLayout.plotR - _lastLayout.plotL);
                const cursorT = _zoomTMin + cursorFrac * (_zoomTMax - _zoomTMin);
                // Zoom factor: 0.85 per wheel step (wheel up = zoom in)
                const factor = e.deltaY < 0 ? 0.85 : (1 / 0.85);
                const newSpan = Math.max(5000, (_zoomTMax - _zoomTMin) * factor);
                _zoomTMin = cursorT - cursorFrac * newSpan;
                _zoomTMax = cursorT + (1 - cursorFrac) * newSpan;
                _dirty = true;
            };
            _onDblClick = () => {
                _zoomTMin = null;
                _zoomTMax = null;
                _isPanning = false;
                _dirty = true;
            };
            // Drag-to-pan (hold left mouse, drag horizontally)
            _onMouseDown = (e) => {
                if (e.button !== 0) return;
                if (!_lastLayout) return;
                const rect = _canvas.getBoundingClientRect();
                const cx = e.clientX - rect.left;
                const cy = e.clientY - rect.top;
                if (cx < _lastLayout.plotL || cx > _lastLayout.plotR ||
                    cy < _lastLayout.plotT || cy > _lastLayout.plotB) return;
                if (_zoomTMin === null || _zoomTMax === null) {
                    _zoomTMin = _lastLayout.tMin;
                    _zoomTMax = _lastLayout.tMax;
                }
                _isPanning = true;
                _panStartX = cx;
                _panStartTMin = _zoomTMin;
                _panStartTMax = _zoomTMax;
                _canvas.style.cursor = 'grabbing';
                e.preventDefault();
            };
            _onMouseUp = () => {
                if (_isPanning) {
                    _isPanning = false;
                    if (_canvas) _canvas.style.cursor = 'crosshair';
                }
            };
            _onMouseMovePan = (e) => {
                if (!_isPanning || !_lastLayout) return;
                const rect = _canvas.getBoundingClientRect();
                const cx = e.clientX - rect.left;
                const dxPx = cx - _panStartX;
                const plotW = _lastLayout.plotR - _lastLayout.plotL;
                const span = _panStartTMax - _panStartTMin;
                const dxT = -(dxPx / plotW) * span;   // drag right → see earlier (lower t)
                _zoomTMin = _panStartTMin + dxT;
                _zoomTMax = _panStartTMax + dxT;
                _dirty = true;
            };
            _canvas.addEventListener('wheel', _onWheel, {passive: false});
            _canvas.addEventListener('dblclick', _onDblClick);
            _canvas.addEventListener('mousedown', _onMouseDown);
            window.addEventListener('mouseup', _onMouseUp);
            _canvas.addEventListener('mousemove', _onMouseMovePan);

            // Hydrate 2h history FIRST (so chart shows past data immediately),
            // then fall back to current snapshot for live updates.
            _hydrateHistoryFor(_selected);
            if (_selected_b) _hydrateHistoryFor(_selected_b);
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
            // Crosshair + zoom listeners — remove before nulling canvas
            if (_canvas) {
                if (_onMouseMove)    _canvas.removeEventListener('mousemove', _onMouseMove);
                if (_onMouseLeave)   _canvas.removeEventListener('mouseleave', _onMouseLeave);
                if (_onClick)        _canvas.removeEventListener('click', _onClick);
                if (_onWheel)        _canvas.removeEventListener('wheel', _onWheel);
                if (_onDblClick)     _canvas.removeEventListener('dblclick', _onDblClick);
                if (_onMouseDown)    _canvas.removeEventListener('mousedown', _onMouseDown);
                if (_onMouseMovePan) _canvas.removeEventListener('mousemove', _onMouseMovePan);
            }
            if (_onMouseUp) window.removeEventListener('mouseup', _onMouseUp);
            _onMouseMove = _onMouseLeave = _onClick = null;
            _onWheel = _onDblClick = null;
            _onMouseDown = _onMouseUp = _onMouseMovePan = null;
            _zoomTMin = _zoomTMax = null;
            _isPanning = false;
            _cursorX = _cursorY = null;
            _cursorPinned = false;
            _lastLayout = null;
            _canvas = null;
            _ctx = null;
            _slotEl = null;
        },
    };
})();
