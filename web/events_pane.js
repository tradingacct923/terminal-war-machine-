/**
 * EventsPane — earnings + macro event calendar with vol-regime impact.
 *
 * Backed by /api/intel/events + Socket.IO 'intel:events' (push every 60 min).
 *
 * Renders:
 *   1. HEADER          — countdown to next event + vol warning chip
 *   2. NEXT 24HR       — events within next 24 hours, sorted ascending
 *   3. NEXT 7D         — events within next week
 *
 * Data source: data/event_calendar.json (operator-maintained, 60 min reload).
 *
 * Anti-theater: every value traces to backend field documented in
 * docs/MEASURED_VALUES.md → "Phase 10B: Event Calendar".
 */
window.EventsPane = (() => {
    'use strict';

    let _slot = null;
    let _styleEl = null;
    let _destroyed = false;
    let _pollTimer = null;
    let _tickTimer = null;       // local 1s tick for countdown display
    let _pushHandler = null;
    let _state = null;

    const REST_POLL_MS = 60000;     // CONFIGURED — minute-level refresh

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('events-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'events-styles';
        _styleEl.textContent = `
            .events-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }

            .events-header { display:grid;
                grid-template-columns: auto 1fr auto auto;
                gap:10px; align-items:center; padding:0 2px; font-size:9px;
                border-bottom:1px solid rgba(255,255,255,.04); padding-bottom:5px; }
            .events-title { font-weight:700; letter-spacing:.7px;
                color:rgba(220,230,250,.9); font-size:10px; }
            .events-stat { display:flex; flex-direction:column; gap:1px; }
            .events-stat .lbl { color:rgba(140,150,180,.55); font-size:8px;
                text-transform:uppercase; letter-spacing:.5px; }
            .events-stat .val { color:rgba(220,230,250,.95); font-weight:700;
                font-size:11px; font-variant-numeric: tabular-nums; }
            .events-stat .sub { font-size:8px; color:rgba(140,150,180,.5); }

            /* ── Vol warning banner ──────────────────────────────── */
            .events-warn { background:rgba(255,255,255,.025); border-radius:4px;
                padding:7px 10px; border-left:3px solid rgba(140,150,180,.3);
                display:flex; flex-direction:column; gap:3px; }
            .events-warn.active {
                border-left-color:#cc6677;
                background:linear-gradient(90deg, rgba(204,102,119,.18), rgba(255,255,255,.025) 70%);
            }
            .events-warn-row { display:flex; align-items:baseline;
                justify-content:space-between; gap:10px; }
            .events-warn-tag { font-weight:700; font-size:13px; letter-spacing:.6px;
                padding:3px 9px; border-radius:3px; }
            .events-warn-tag.active   { background:rgba(204,102,119,.22); color:#ff9aa8; }
            .events-warn-tag.inactive { background:rgba(170,180,210,.10); color:rgba(180,190,210,.6); }
            .events-warn-detail { font-size:9px; color:rgba(170,180,210,.7); line-height:1.35; }

            /* ── Event list ──────────────────────────────────────── */
            .events-section { background:rgba(255,255,255,.02); border-radius:3px;
                padding:5px 7px; display:flex; flex-direction:column; gap:2px; }
            .events-section-hdr { font-size:8px; color:rgba(140,150,180,.55);
                text-transform:uppercase; letter-spacing:.5px;
                padding-bottom:2px;
                border-bottom:1px solid rgba(255,255,255,.04);
                display:flex; justify-content:space-between; }
            .events-row {
                display:grid;
                grid-template-columns: 70px 50px 100px 1fr 80px;
                gap:6px; align-items:center; font-size:9px;
                font-family:'JetBrains Mono',monospace;
                font-variant-numeric: tabular-nums;
                padding:3px 0;
                border-bottom:1px dashed rgba(255,255,255,.020); }
            .events-row:last-child { border-bottom:none; }
            .events-row .when { color:rgba(220,230,250,.85); font-weight:700; }
            .events-row .when.imminent { color:#ff9aa8; }
            .events-row .when.soon     { color:#ffd180; }
            .events-row .when.distant  { color:rgba(170,180,210,.7); }
            .events-row .ticker { color:#85b6e6; font-weight:700; font-size:10px; }
            .events-row .ticker.macro { color:#bcb3ff; }
            .events-row .ticker.mag8  { color:#85e0a3; }
            .events-row .type {
                font-size:8px; padding:1px 5px; border-radius:2px;
                background:rgba(170,180,210,.10); color:rgba(180,190,210,.7);
                text-align:center;
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
            .events-row .type.high   { background:rgba(204,102,119,.18); color:#e69aa5; }
            .events-row .type.medium { background:rgba(255,209,128,.18); color:#ffd180; }
            .events-row .type.low    { background:rgba(170,180,210,.10); color:rgba(180,190,210,.5); }
            .events-row .notes { color:rgba(160,170,200,.6);
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
            .events-row .ts { color:rgba(140,150,180,.55); font-size:8px; text-align:right; }

            .events-empty { padding:12px; text-align:center;
                color:rgba(140,150,180,.4); font-style:italic; font-size:10px; }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        if (!_slot) return;
        _slot.innerHTML = `
          <div class="events-wrap">
            <div class="events-header">
                <div class="events-title">📅 EVENT CALENDAR</div>
                <div></div>
                <div class="events-stat">
                    <span class="lbl">next event</span>
                    <span class="val" data-fld="hdr_next">—</span>
                    <span class="sub" data-fld="hdr_next_sub">—</span>
                </div>
                <div class="events-stat">
                    <span class="lbl">source</span>
                    <span class="val" data-fld="hdr_source">—</span>
                </div>
            </div>

            <div class="events-warn" data-fld="warn_host">
                <div class="events-empty">no high-impact event in 24hr window</div>
            </div>

            <div class="events-section">
                <div class="events-section-hdr">
                    <span>NEXT 24 HOURS</span>
                    <span data-fld="count_24h">0</span>
                </div>
                <div data-fld="list_24h"></div>
            </div>

            <div class="events-section" style="flex:1 1 auto; overflow:auto;">
                <div class="events-section-hdr">
                    <span>NEXT 7 DAYS</span>
                    <span data-fld="count_7d">0</span>
                </div>
                <div data-fld="list_7d"></div>
            </div>
          </div>
        `;
    }

    // ── Helpers ────────────────────────────────────────────────────────
    function _fmtCountdown(secUntil) {
        if (!Number.isFinite(secUntil)) return '—';
        if (secUntil < 0) return 'past';
        const m = Math.floor(secUntil / 60);
        const h = Math.floor(m / 60);
        const d = Math.floor(h / 24);
        if (d >= 1) {
            const remH = h % 24;
            return `${d}d ${remH}h`;
        }
        if (h >= 1) {
            const remM = m % 60;
            return `${h}h ${remM}m`;
        }
        if (m >= 1) {
            return `${m}m`;
        }
        return `${Math.max(0, Math.floor(secUntil))}s`;
    }
    function _fmtClockET(tsUnix) {
        if (!Number.isFinite(tsUnix)) return '—';
        const d = new Date(tsUnix * 1000);
        // toLocaleString in user's TZ — for ET display we'd need Intl format opts
        try {
            return d.toLocaleString('en-US', {
                month: 'short', day: 'numeric',
                hour: '2-digit', minute: '2-digit',
                hour12: false,
                timeZone: 'America/New_York',
            });
        } catch (_) {
            return d.toISOString().slice(5, 16);
        }
    }
    function _whenClass(secUntil) {
        if (!Number.isFinite(secUntil)) return 'distant';
        if (secUntil < 60 * 60 * 4)  return 'imminent';      // < 4h
        if (secUntil < 60 * 60 * 24) return 'soon';          // < 24h
        return 'distant';
    }
    function _tickerClass(ev) {
        if (ev.macro) return 'macro';
        if (ev.mag_8) return 'mag8';
        return '';
    }

    // ── Renderers ──────────────────────────────────────────────────────

    function _renderHeader() {
        if (!_slot || !_state) return;
        const ne = _state.next_event;
        const set = (sel, txt) => {
            const el = _slot.querySelector(`[data-fld="${sel}"]`);
            if (el) el.textContent = txt;
        };
        if (ne) {
            const sec = ne.time_until_sec;
            set('hdr_next', _fmtCountdown(sec));
            set('hdr_next_sub', `${ne.ticker} ${(ne.type || '').replace(/_/g, ' ')}`);
        } else {
            set('hdr_next', '—');
            set('hdr_next_sub', _state.reason || '—');
        }
        const src = _state.source || 'no_data';
        set('hdr_source', src === 'json_file' ? 'JSON' : (src === 'no_data' ? '—' : src));
    }

    function _renderWarning() {
        if (!_slot || !_state) return;
        const host = _slot.querySelector('[data-fld="warn_host"]');
        if (!host) return;
        const w = _state.vol_warning || {};
        if (w.active && w.event) {
            const ev = w.event;
            host.className = 'events-warn active';
            const hours = w.hours;
            const hStr = hours < 1 ? `${Math.round(hours*60)}m`
                         : `${hours.toFixed(1)}h`;
            host.innerHTML = `
                <div class="events-warn-row">
                    <span class="events-warn-tag active">⚠ VOL REGIME WARNING</span>
                    <span style="font-size:9px; color:rgba(170,180,210,.7);">in ${hStr}</span>
                </div>
                <div class="events-warn-detail">
                    <strong>${ev.ticker}</strong> ${(ev.type || '').replace(/_/g, ' ')}
                    · ${(ev.impact || 'high').toUpperCase()} impact
                    ${ev.notes ? ' · ' + ev.notes : ''}
                </div>
            `;
        } else {
            host.className = 'events-warn';
            host.innerHTML = `
                <div class="events-warn-row">
                    <span class="events-warn-tag inactive">· no warning</span>
                    <span style="font-size:9px; color:rgba(170,180,210,.5);">
                        ${(_state.in_24hr || []).length} in 24hr
                    </span>
                </div>
                <div class="events-warn-detail">
                    no high-impact event within 24-hour vol-warning window
                </div>
            `;
        }
    }

    function _renderEventList(host, events) {
        if (!host) return;
        if (!events || !events.length) {
            host.innerHTML = `<div class="events-empty">none</div>`;
            return;
        }
        host.innerHTML = events.map(ev => {
            const sec = ev.time_until_sec;
            const whenCls = _whenClass(sec);
            const whenTxt = _fmtCountdown(sec);
            const tickerCls = _tickerClass(ev);
            const impactCls = (ev.impact || 'medium').toLowerCase();
            const typeTxt = (ev.type || 'other').replace(/_/g, ' ').toUpperCase().slice(0, 14);
            const ts = _fmtClockET(ev.ts_unix);
            return `<div class="events-row">
                <span class="when ${whenCls}">${whenTxt}</span>
                <span class="ticker ${tickerCls}">${ev.ticker}</span>
                <span class="type ${impactCls}">${typeTxt}</span>
                <span class="notes">${ev.notes || ''}</span>
                <span class="ts">${ts}</span>
            </div>`;
        }).join('');
    }

    function _renderLists() {
        if (!_slot || !_state) return;
        const list24 = _slot.querySelector('[data-fld="list_24h"]');
        const list7d = _slot.querySelector('[data-fld="list_7d"]');
        const c24 = _slot.querySelector('[data-fld="count_24h"]');
        const c7d = _slot.querySelector('[data-fld="count_7d"]');
        if (c24) c24.textContent = String((_state.in_24hr || []).length);
        if (c7d) c7d.textContent = String((_state.in_7d || []).length);
        _renderEventList(list24, _state.in_24hr || []);
        // For the 7d list, exclude the 24h ones (already shown above)
        const onlyBeyond24 = (_state.in_7d || []).filter(e =>
            !(_state.in_24hr || []).some(x => x.ts_unix === e.ts_unix && x.ticker === e.ticker)
        );
        _renderEventList(list7d, onlyBeyond24);
    }

    function _renderAll() {
        _renderHeader();
        _renderWarning();
        _renderLists();
    }

    // ── Local 1s tick (refreshes countdown only — no network) ──────────
    function _tickCountdowns() {
        if (!_state) return;
        const now = Date.now() / 1000;
        // Update time_until on each event in place
        const update = (arr) => {
            (arr || []).forEach(e => {
                e.time_until_sec   = Math.max(0, e.ts_unix - now);
                e.time_until_hours = e.time_until_sec / 3600;
            });
        };
        update(_state.in_24hr);
        update(_state.in_7d);
        if (_state.next_event) {
            _state.next_event.time_until_sec   = Math.max(0, _state.next_event.ts_unix - now);
            _state.next_event.time_until_hours = _state.next_event.time_until_sec / 3600;
        }
        _renderHeader();
        _renderLists();
    }

    // ── Data flow ──────────────────────────────────────────────────────

    function _onPushUpdate(state) {
        if (!state) return;
        _state = state;
        _renderAll();
    }

    async function _refreshREST() {
        if (_destroyed) return;
        try {
            const r = await _authFetch('/api/intel/events');
            if (!r.ok) return;
            const j = await r.json();
            if (j && typeof j === 'object') {
                _state = j;
                _renderAll();
            }
        } catch (_) {}
    }

    // ── Lifecycle ──────────────────────────────────────────────────────

    function init(slotEl) {
        _slot = slotEl;
        _destroyed = false;
        _state = null;
        _injectStyles();
        _buildShell();

        if (window.AltarisEvents) {
            _pushHandler = (d) => _onPushUpdate(d);
            window.AltarisEvents.on('socket:intel:events', _pushHandler);
        }

        _refreshREST();
        _pollTimer = setInterval(_refreshREST, REST_POLL_MS);
        // Local 1s tick for countdown smoothness (no network)
        _tickTimer = setInterval(_tickCountdowns, 1000);
    }

    function destroy() {
        _destroyed = true;
        if (_pollTimer) clearInterval(_pollTimer);
        if (_tickTimer) clearInterval(_tickTimer);
        _pollTimer = null;
        _tickTimer = null;
        if (window.AltarisEvents && _pushHandler) {
            window.AltarisEvents.off('socket:intel:events', _pushHandler);
            _pushHandler = null;
        }
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
