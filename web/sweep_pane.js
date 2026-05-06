/**
 * SweepPane — Multi-strike option sweep DETECTOR (descriptive, not predictive).
 *
 * Backed by /api/intel/sweeps + Socket.IO 'intel:sweep_alert' (push).
 *
 * A "sweep" is the institutional fingerprint: 3+ adjacent option strikes
 * traded within 500ms, all aggressor-side same direction. We detect and
 * report this OBSERVABLE event. We do NOT claim it predicts anything about
 * the underlying — see /tmp/sweep_zero_edge_deepdive.py for the audit that
 * established the dealer-hedging theory has zero edge over base rate
 * (n=15,902 cleaned sweeps, all six hypothesis tests confirmed null).
 *
 * Renders three sections:
 *   1. STATUS HEADER     — live counts, last sweep age, detector config chip
 *   2. ACTIVE EVENT      — most recent sweep with full leg breakdown,
 *                          Δ-notional, venue spray (descriptive only)
 *   3. HISTORY TABLE     — last N sweeps (newest first) with condensed rows
 *
 * What we DELIBERATELY don't show: directional predictions of any kind.
 * Sweep direction had zero edge over base rate (+0.27%, n=15,902 audit
 * 2026-05-04). 2026-05-05: backend stripped expected_hedge_side,
 * expected_hedge_shares, and the hf_alignment cross-validator entirely —
 * the disk ledger no longer carries them either. Pane is observation-only
 * by design AND by data availability.
 *
 * Anti-theater discipline: every rendered number traces to a backend field
 * sourced in connectors/sweep_detector.py.
 */
window.SweepPane = (() => {
    'use strict';

    // ── Per-exchange color palette (mirrors mm_attribution_pane convention)
    const EXCH_COLOR = {
        // NASDAQ family — cyan/blue
        NSDQ: '#00d4ff', PHLX: '#4da6ff', BX:   '#6699ff', BOSX: '#6699ff',
        ISEX: '#80b3ff', GMNI: '#3366ff', MRX:  '#0099cc', MERC: '#0099cc',
        // CBOE family — orange/amber
        CBOE: '#ff9933', C2:   '#ff8c42', EDGX: '#ffb366',
        BATS: '#ffa64d', XBXO: '#e6994d', BYX:  '#d4884d', BATY: '#d4884d',
        // NYSE family — purple
        AMEX: '#cc66cc', PACX: '#b366e6', NYSE: '#9933cc',
        // MIAX family — green
        XMIO: '#66cc66', PEARL:'#85e085', EMLD: '#40bf40', MIAX: '#66b366',
        // MEMX — yellow
        MEMX: '#ffd633',
        // BOX (Boston) — gray
        BOX:  '#9aa0aa',
        // Generic single-letter venue codes from Tradier OPRA stream
        'A': '#cc66cc', 'B': '#6699ff', 'C': '#ff9933', 'I': '#80b3ff',
        'M': '#ffd633', 'N': '#9933cc', 'P': '#4da6ff', 'Q': '#00d4ff',
        'T': '#9aa0aa', 'W': '#66b366', 'X': '#ff8c42', 'Z': '#ffa64d',
        'S': '#ffb366',
    };
    const colorFor = (e) => EXCH_COLOR[(e || '').toUpperCase()] || '#8890a0';

    // ── Module state
    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _historyPollTimer = null;
    let _ageTickTimer = null;       // re-renders age field every second
    let _alertHandler = null;        // Socket.IO push handler

    let _sweeps = [];                // history (newest last → reversed for display)
    let _activeAlertId = -1;         // most-recent sweep id we're highlighting
    let _activePulseUntilMs = 0;      // pulse-anim end timestamp

    // CONFIGURED — UI cadence; not signal thresholds
    const HISTORY_POLL_MS  = 30000;   // 30s — initial-load refresh + drift correction
    const ACTIVE_PULSE_MS  = 4000;    // banner pulse-animation duration on new alert
    const HISTORY_LIMIT    = 30;      // rows shown in table

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('sweep-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'sweep-styles';
        _styleEl.textContent = `
            .sweep-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            /* ── Header ────────────────────────────────────── */
            .sweep-header { display:flex; justify-content:space-between; align-items:center;
                padding:0 2px; gap:12px; font-size:9px; color:rgba(160,170,200,.6);
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:4px; }
            .sweep-title { font-weight:700; letter-spacing:.7px; color:rgba(220,230,250,.9);
                font-size:10px; }
            .sweep-stat { display:flex; gap:4px; align-items:baseline; }
            .sweep-stat .lbl { color:rgba(140,150,180,.5); font-size:8px; text-transform:uppercase;
                letter-spacing:.6px; }
            .sweep-stat .val { color:rgba(210,220,240,.95); font-weight:600; }
            .sweep-stat .val.warn { color:#ffb366; }
            .sweep-stat .val.ok { color:#66cc99; }
            .sweep-cfg { font-size:8px; color:rgba(140,150,180,.5);
                display:flex; gap:6px; }

            /* ── Active alert banner ───────────────────────── */
            .sweep-active { background:rgba(255,255,255,.025); border-radius:4px;
                padding:8px; border-left:3px solid rgba(140,150,180,.3);
                display:flex; flex-direction:column; gap:6px; }
            .sweep-active.dir-buy {
                border-left-color:#66cc99;
                background:linear-gradient(90deg, rgba(102,204,153,.06), rgba(255,255,255,.025) 60%); }
            .sweep-active.dir-sell {
                border-left-color:#cc6677;
                background:linear-gradient(90deg, rgba(204,102,119,.06), rgba(255,255,255,.025) 60%); }
            .sweep-active.pulse { animation:sweep-pulse 0.8s 4 ease-in-out; }
            @keyframes sweep-pulse {
                0%,100% { box-shadow:0 0 0 0 rgba(255,255,255,0); }
                50%     { box-shadow:0 0 0 4px rgba(255,179,102,.18); }
            }
            .sweep-active-empty { padding:12px; text-align:center;
                color:rgba(140,150,180,.4); font-size:10px; font-style:italic; }

            .sweep-active-row1 { display:flex; justify-content:space-between;
                align-items:center; gap:8px; }
            .sweep-active-tag { font-weight:700; font-size:13px; letter-spacing:.8px;
                padding:2px 8px; border-radius:3px; }
            .sweep-active-tag.dir-buy { background:rgba(102,204,153,.18); color:#85e0a3; }
            .sweep-active-tag.dir-sell { background:rgba(204,102,119,.18); color:#e69aa5; }
            .sweep-active-meta { font-size:9px; color:rgba(160,170,200,.7); display:flex; gap:10px; }
            /* Phase 10A — hedge_forecaster alignment badge */
            .sweep-hf-badge {
                display:inline-block; font-size:9px; font-weight:700;
                padding:1px 6px; border-radius:2px; margin-left:6px;
                letter-spacing:.4px;
                font-variant-numeric: tabular-nums;
            }
            .sweep-hf-badge.aligned {
                background:rgba(255,180,80,.20); color:#ffd180;
                border:1px solid rgba(255,180,80,.4);
            }
            .sweep-hf-badge.misaligned {
                background:rgba(204,102,119,.20); color:#e69aa5;
            }
            .sweep-hf-badge.flat {
                background:rgba(170,180,210,.10); color:rgba(180,190,210,.6);
            }
            .sweep-hf-badge.nodata {
                background:rgba(170,180,210,.06); color:rgba(140,150,180,.5);
            }

            .sweep-active-row2 { display:grid; grid-template-columns: 1fr 1fr 1fr;
                gap:8px; padding:6px 0; border-top:1px dashed rgba(255,255,255,.06);
                border-bottom:1px dashed rgba(255,255,255,.06); }
            .sweep-active-card { display:flex; flex-direction:column; gap:4px; }
            .sweep-active-card .k { font-size:8px; color:rgba(140,150,180,.6);
                text-transform:uppercase; letter-spacing:.5px; }
            .sweep-active-card .v { font-size:13px; font-weight:700;
                color:rgba(220,230,250,.95); }
            .sweep-active-card .v.dir-buy { color:#85e0a3; }
            .sweep-active-card .v.dir-sell { color:#e69aa5; }
            .sweep-active-card .sub { font-size:8px; color:rgba(140,150,180,.5); }

            .sweep-active-legs { display:flex; flex-direction:column; gap:2px; }
            .sweep-active-legs-title { font-size:8px; color:rgba(140,150,180,.5);
                text-transform:uppercase; margin-bottom:2px; }
            .sweep-active-leg { display:grid;
                grid-template-columns: 60px 60px 50px 70px 60px 1fr;
                gap:6px; font-size:9px; padding:1px 0; align-items:center; }
            .sweep-active-leg .ts { color:rgba(140,150,180,.5); font-size:8px; }
            .sweep-active-leg .strike { color:rgba(210,220,240,.9); font-weight:600; }
            .sweep-active-leg .size { color:#85b6e6; text-align:right; font-weight:700; }
            .sweep-active-leg .price { color:rgba(180,190,220,.7); text-align:right; }
            .sweep-active-leg .exch { padding:1px 4px; border-radius:2px;
                font-weight:700; font-size:8px; text-align:center; }

            /* ── History table ─────────────────────────────── */
            .sweep-history { flex:1; display:flex; flex-direction:column;
                gap:2px; overflow-y:auto; min-height:0; }
            .sweep-history-title { font-size:9px; font-weight:700;
                color:rgba(180,190,220,.6); text-transform:uppercase; letter-spacing:.7px;
                padding:4px 2px; position:sticky; top:0; background:#070a14;
                border-bottom:1px solid rgba(255,255,255,.05); display:flex;
                justify-content:space-between; }
            .sweep-history-empty { padding:14px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
            .sweep-row { display:grid;
                grid-template-columns: 38px 26px 28px 26px 60px 90px 50px 1fr;
                gap:6px; align-items:center; padding:3px 4px;
                border-bottom:1px solid rgba(255,255,255,.03); font-size:9px; }
            .sweep-row:hover { background:rgba(255,255,255,.025); }
            .sweep-row.is-active { background:rgba(255,179,102,.06); }
            .sweep-row .age { color:rgba(140,150,180,.55); font-size:8px; text-align:right; }
            .sweep-row .dirtag { font-weight:700; padding:1px 3px; border-radius:2px;
                font-size:8px; text-align:center; }
            .sweep-row .dirtag.buy { background:rgba(102,204,153,.18); color:#85e0a3; }
            .sweep-row .dirtag.sell { background:rgba(204,102,119,.18); color:#e69aa5; }
            .sweep-row .opt { color:rgba(210,220,240,.8); text-align:center;
                font-weight:600; }
            .sweep-row .opt.call { color:#85b6e6; }
            .sweep-row .opt.put { color:#e69580; }
            .sweep-row .legs { color:rgba(170,180,210,.7); text-align:right; }
            .sweep-row .strikes { color:rgba(200,210,235,.85); font-size:8px; }
            .sweep-row .size { color:#ffd6a3; text-align:right; font-weight:700; }
            .sweep-row .hedge-shares { color:rgba(180,190,220,.65); font-size:8px;
                text-align:right; }
            .sweep-row .hedge-shares .hf-mark { margin-left:2px; }
            .sweep-row .hedge-shares .hf-mark.hf-aligned { color:#ffd180; }
            .sweep-row .hedge-shares .hf-mark.hf-mis { color:#e69aa5; }
            .sweep-row .venues { color:rgba(140,150,180,.6); font-size:8px;
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="sweep-wrap">
            <div class="sweep-header">
                <div class="sweep-title">⚡ MULTI-STRIKE SWEEP DETECTOR</div>
                <div class="sweep-stat">
                    <span class="lbl">today</span><span class="val" data-fld="count_today">0</span>
                    <span class="lbl">last</span><span class="val" data-fld="last_age">—</span>
                </div>
                <div class="sweep-cfg" data-fld="cfg">window=500ms · adj=$3.0 · min legs=3 · min size=50</div>
            </div>
            <div class="sweep-active-host" data-fld="active_host">
                <div class="sweep-active-empty">Waiting for the first sweep…</div>
            </div>
            <div class="sweep-history" data-fld="history">
                <div class="sweep-history-title">
                    <span>SWEEP HISTORY (newest first)</span>
                    <span data-fld="hist_count">0</span>
                </div>
                <div class="sweep-history-empty">No sweeps yet today.</div>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────

    function _ageStr(ms_ago) {
        if (!Number.isFinite(ms_ago)) return '—';
        if (ms_ago < 0) ms_ago = 0;
        const s = Math.floor(ms_ago / 1000);
        if (s < 60) return `${s}s ago`;
        if (s < 3600) return `${Math.floor(s/60)}m ${s%60}s ago`;
        return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ago`;
    }

    function _fmtSize(n) {
        if (!Number.isFinite(n)) return '—';
        if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(2) + 'M';
        if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(1) + 'K';
        return String(n);
    }

    function _fmtSigned(n) {
        if (!Number.isFinite(n)) return '—';
        const sign = n > 0 ? '+' : (n < 0 ? '−' : '');
        const abs = Math.abs(n);
        return sign + _fmtSize(abs);
    }

    // ── Renderers ──────────────────────────────────────────────────────

    function _renderActive(sweep, opts) {
        if (!_slot) return;
        const host = _slot.querySelector('[data-fld="active_host"]');
        if (!host) return;
        if (!sweep) {
            host.innerHTML = `<div class="sweep-active-empty">Waiting for the first sweep…</div>`;
            return;
        }
        // 2026-05-04: directional prediction stripped from this pane.
        // The dealer-hedging hypothesis was empirically falsified on n=15,902
        // sweeps (zero edge over base rate, see /tmp/sweep_zero_edge_deepdive.py).
        // We render the OBSERVABLE FACT only — what happened, not what it means.
        const dirCls = sweep.direction === 'BUY' ? 'dir-buy' : 'dir-sell';
        const optTxt = sweep.option_side === 'C' ? 'CALL' : 'PUT';
        const arrowTxt = sweep.direction === 'BUY' ? '⬆ BUY' : '⬇ SELL';

        const t0 = sweep.first_print_ts;
        const legsRows = (sweep.legs || []).map(L => {
            const dt = L.ts_ms - t0;
            const exchClr = colorFor(L.exch);
            return `
                <div class="sweep-active-leg">
                    <span class="ts">+${dt}ms</span>
                    <span class="strike">${L.strike.toFixed(2)}</span>
                    <span class="size">${_fmtSize(L.size)}</span>
                    <span class="price">$${L.price.toFixed(2)}</span>
                    <span class="exch" style="background:${exchClr}33; color:${exchClr};">${L.exch || '?'}</span>
                    <span></span>
                </div>
            `;
        }).join('');

        const deltaResolvedTxt = sweep.delta_resolved < sweep.delta_total_legs
            ? `(Δ resolved ${sweep.delta_resolved}/${sweep.delta_total_legs})`
            : '';

        const pulseCls = (opts && opts.pulse) ? ' pulse' : '';
        host.innerHTML = `
            <div class="sweep-active ${dirCls}${pulseCls}">
                <div class="sweep-active-row1">
                    <div>
                        <span class="sweep-active-tag ${dirCls}">${arrowTxt} ${optTxt} SWEEP</span>
                        <span style="margin-left:8px; font-size:11px; color:rgba(220,230,250,.9); font-weight:600;">
                            ${sweep.underlying} • exp ${sweep.expiration}
                        </span>
                    </div>
                    <div class="sweep-active-meta">
                        <span>id #${sweep.id}</span>
                        <span>${sweep.leg_count} legs</span>
                        <span>${sweep.time_span_ms}ms span</span>
                        <span>${sweep.venue_count} venues</span>
                    </div>
                </div>
                <div class="sweep-active-row2">
                    <div class="sweep-active-card">
                        <div class="k">Total contracts swept</div>
                        <div class="v">${_fmtSize(sweep.total_size)}</div>
                        <div class="sub">across strikes $${sweep.strike_range[0].toFixed(2)} – $${sweep.strike_range[1].toFixed(2)}</div>
                    </div>
                    <div class="sweep-active-card">
                        <div class="k">Δ-notional</div>
                        <div class="v">${_fmtSigned(Math.round(sweep.notional_delta))}</div>
                        <div class="sub">Σ size × Δ × 100 ${deltaResolvedTxt}</div>
                    </div>
                    <div class="sweep-active-card">
                        <div class="k">Venue spray</div>
                        <div class="v">${sweep.venue_count} unique</div>
                        <div class="sub">${(sweep.venue_sequence || []).join(' ')}</div>
                    </div>
                </div>
                <div class="sweep-active-legs">
                    <div class="sweep-active-legs-title">leg-by-leg sequence (observation, not prediction)</div>
                    ${legsRows}
                </div>
            </div>
        `;
    }

    function _renderHistory() {
        if (!_slot) return;
        const host = _slot.querySelector('[data-fld="history"]');
        if (!host) return;

        // Order: newest first
        const ordered = _sweeps.slice().reverse().slice(0, HISTORY_LIMIT);
        const titleHtml = `
            <div class="sweep-history-title">
                <span>SWEEP HISTORY (newest first)</span>
                <span>${_sweeps.length}</span>
            </div>
        `;
        if (!ordered.length) {
            host.innerHTML = titleHtml + `<div class="sweep-history-empty">No sweeps yet today.</div>`;
            return;
        }
        const now = Date.now();
        const rows = ordered.map(s => {
            const age = _ageStr(now - s.last_print_ts);
            const dirCls = s.direction === 'BUY' ? 'buy' : 'sell';
            const dirTxt = s.direction === 'BUY' ? '⬆ B' : '⬇ S';
            const optCls = s.option_side === 'C' ? 'call' : 'put';
            const optTxt = s.option_side === 'C' ? 'C' : 'P';
            const strikeRange = `$${s.strike_range[0].toFixed(0)}–${s.strike_range[1].toFixed(0)}`;
            const venueDisplay = (s.venue_sequence || []).slice(0, 5)
                .map(v => `<span style="color:${colorFor(v)};">${v}</span>`)
                .join(' → ');
            const isActive = (s.id === _activeAlertId) ? ' is-active' : '';
            // 2026-05-04: replaced predicted-hedge column with Δ-notional fact.
            // hedge_side prediction had zero edge over base rate.
            const dnotional = _fmtSigned(Math.round(s.notional_delta || 0));
            return `
                <div class="sweep-row${isActive}" data-sweep-id="${s.id}">
                    <span class="age">${age}</span>
                    <span class="dirtag ${dirCls}">${dirTxt}</span>
                    <span class="opt ${optCls}">${optTxt}</span>
                    <span class="legs">${s.leg_count}L</span>
                    <span class="strikes">${strikeRange}</span>
                    <span class="size">${_fmtSize(s.total_size)}</span>
                    <span class="hedge-shares">Δ ${dnotional}</span>
                    <span class="venues">${venueDisplay}</span>
                </div>
            `;
        }).join('');
        host.innerHTML = titleHtml + rows;
    }

    function _renderHeader() {
        if (!_slot) return;
        const today = _slot.querySelector('[data-fld="count_today"]');
        const lastAge = _slot.querySelector('[data-fld="last_age"]');
        if (today) today.textContent = String(_sweeps.length);
        if (lastAge) {
            if (_sweeps.length === 0) {
                lastAge.textContent = '—';
                lastAge.className = 'val';
            } else {
                const last = _sweeps[_sweeps.length - 1];
                const ageMs = Date.now() - last.last_print_ts;
                lastAge.textContent = _ageStr(ageMs);
                lastAge.className = ageMs < 60000 ? 'val ok' : (ageMs < 600000 ? 'val' : 'val warn');
            }
        }
    }

    function _renderAll(opts) {
        _renderHeader();
        const last = _sweeps.length ? _sweeps[_sweeps.length - 1] : null;
        _renderActive(last, opts);
        _renderHistory();
    }

    // ── Data flow ──────────────────────────────────────────────────────

    function _onSweepAlert(data) {
        if (!data || !data.id) return;
        // Replace if same id (re-emit on leg-count growth), else append
        const existing = _sweeps.findIndex(s => s.id === data.id);
        if (existing >= 0) {
            _sweeps[existing] = data;
        } else {
            _sweeps.push(data);
            // Cap to HISTORY_LIMIT × 4 to avoid unbounded growth
            const cap = HISTORY_LIMIT * 4;
            if (_sweeps.length > cap) _sweeps.splice(0, _sweeps.length - cap);
        }
        _activeAlertId = data.id;
        _activePulseUntilMs = Date.now() + ACTIVE_PULSE_MS;
        _renderAll({ pulse: true });
    }

    async function _refreshHistory() {
        if (_destroyed) return;
        try {
            const r = await _authFetch(`/api/intel/sweeps?limit=${HISTORY_LIMIT * 2}`);
            if (!r.ok) return;
            const j = await r.json();
            if (!j || !Array.isArray(j.sweeps)) return;
            // server returns newest LAST in array (per get_recent_sweeps slice)
            _sweeps = j.sweeps.slice();
            _renderAll();
        } catch (_) {}
    }

    function _tickAge() {
        if (_destroyed) return;
        // Re-render ages every 1s so "5s ago" → "6s ago" updates
        _renderHeader();
        // Don't re-render history table every second (DOM thrash); just header
    }

    // ── Lifecycle ─────────────────────────────────────────────────────

    function init(slotEl) {
        _slot = slotEl;
        _destroyed = false;
        _sweeps = [];
        _activeAlertId = -1;
        _injectStyles();
        _buildShell();

        // Subscribe to live alert push
        if (window.AltarisEvents) {
            _alertHandler = (d) => _onSweepAlert(d);
            window.AltarisEvents.on('socket:intel:sweep_alert', _alertHandler);
        }

        // Initial REST load + drift-correction poll every 30s
        _refreshHistory();
        _historyPollTimer = setInterval(_refreshHistory, HISTORY_POLL_MS);

        // Age tick — 1s cadence to keep "Xs ago" fresh
        _ageTickTimer = setInterval(_tickAge, 1000);
    }

    function destroy() {
        _destroyed = true;
        if (_historyPollTimer) clearInterval(_historyPollTimer);
        _historyPollTimer = null;
        if (_ageTickTimer) clearInterval(_ageTickTimer);
        _ageTickTimer = null;
        if (window.AltarisEvents && _alertHandler) {
            window.AltarisEvents.off('socket:intel:sweep_alert', _alertHandler);
            _alertHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
