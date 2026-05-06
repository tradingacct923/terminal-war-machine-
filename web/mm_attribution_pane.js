/**
 * MmAttributionPane — per-exchange L2 structural capture view.
 *
 * Backed by /api/mm_attribution/* (connectors/mm_attribution.py). Renders:
 *   1. NBBO ownership ribbon — event-driven segments per exchange per side
 *   2. Lead-follower table   — all level formations since 09:30 ET
 *   3. Capture-vs-post table — time-integrated posted presence vs. caught prints
 *   4. Impulse response     — mm_count curve captured until the next print
 *
 * No thresholds, no magnitude-based labels. Every rendered number traces to a
 * structural measurement, a session boundary (09:30 ET), or a user selection.
 */
window.MmAttributionPane = (() => {
    'use strict';

    // Fixed per-exchange color palette — kept identical across all four
    // panels so the eye can track one venue across views. Infrastructure
    // constant (not a signal/threshold).
    const EXCH_COLOR = {
        // NASDAQ family — cyan/blue
        NSDQ: '#00d4ff', PHLX: '#4da6ff', BX:   '#6699ff', BOSX: '#6699ff',
        ISEX: '#80b3ff', GMNI: '#3366ff', MRX:  '#0099cc',
        // CBOE family — orange/amber
        CBOE: '#ff9933', C2:   '#ff8c42', EDGX: '#ffb366',
        BATS: '#ffa64d', XBXO: '#e6994d', BYX:  '#d4884d',
        // NYSE family — purple
        AMEX: '#cc66cc', PACX: '#b366e6', NYSE: '#9933cc',
        // MIAX family — green
        XMIO: '#66cc66', PEARL:'#85e085', EMLD: '#40bf40', MIAX: '#66b366',
        // MEMX — yellow
        MEMX: '#ffd633',
        // BOX — gray
        BOX:  '#9aa0aa',
    };
    const EXCH_DEFAULT = '#8890a0';
    const colorFor = (e) => EXCH_COLOR[(e || '').toUpperCase()] || EXCH_DEFAULT;

    let _slot = null;
    let _styleEl = null;
    // Short interval poll for the contract RANKING list only (not per-contract
    // state — that's pushed via socket now). 5s is fine; rankings drift slowly.
    let _rankPollTimer = null;
    let _destroyed = false;

    // Current selection
    let _selectedSym = null;      // null = auto-rank-top-1
    let _watchedSym = null;       // what we've told the server to push
    let _rankMetric = 'events';   // events|prints|formations
    let _zoomMode = 'session';    // session|1h|15m|5m
    let _impulsePrintTs = null;   // null = last (live); otherwise specific

    // Cached responses
    let _lastContractsResp = null;
    let _lastStateResp = null;
    let _lastImpulseList = [];

    // Live event feed (optional, top ticker)
    let _liveFeed = [];
    const LIVE_FEED_CAP = 200;  // display cap (infra only; data is on disk)
    let _liveFeedHandler = null;
    let _stateHandler = null;
    let _wallHandler = null;

    // Wall state cache — last payload from _onWallSignals, used by the
    // state-machine line renderer inside _onLedger to enrich ARMED/QUIET
    // tags with live proximity readings.
    let _lastWallState = null;
    // Ledger poll — runs on a 30s cadence after mount. Outcome tracker
    // runs server-side at 30s; matching cadence prevents stale UI.
    // CONFIGURED infra value, documented in MEASURED_VALUES.md.
    let _ledgerTimer = null;
    const LEDGER_POLL_MS = 30000;

    // Hedge pressure poll — /api/hedge_pressure/QQQ. Matches the zone emit
    // cadence (5s) in schwab_bridge._maybe_emit_zones — no point asking faster
    // than the surface is recomputed. CONFIGURED infra value.
    let _hpTimer = null;
    const HP_POLL_MS = 5000;
    let _lastHpResp = null;
    let _lastHpxResp = null;  // venue rollup (Phase B)
    let _lastAlignResp = null; // alignment for currently-selected contract (Phase C)

    function _authFetch(url) {
        const tok = (typeof sessionStorage !== 'undefined')
            ? (sessionStorage.getItem('greeks-auth') || '') : '';
        return fetch(url, { headers: { 'X-Auth-Token': tok } });
    }

    function _injectStyles() {
        if (document.getElementById('mma-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'mma-styles';
        _styleEl.textContent = `
            .mma-wrap { height:100%; display:flex; flex-direction:column;
                background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace;
                padding:6px; gap:6px; color:rgba(210,220,240,.85); font-size:10px;
                overflow:hidden; }
            .mma-header { display:flex; justify-content:space-between; align-items:center;
                padding:0 2px; gap:12px; font-size:9px; color:rgba(160,170,200,.6);
                letter-spacing:.5px; text-transform:uppercase; }
            .mma-title { font-weight:600; color:rgba(200,210,230,.85); letter-spacing:.8px; }
            .mma-ctrls { display:flex; gap:8px; align-items:center; }
            .mma-ctrls label { font-size:8px; color:rgba(140,150,180,.5);
                margin-right:2px; }
            .mma-ctrls select { background:#0c1020; color:rgba(210,220,240,.85);
                border:1px solid rgba(255,255,255,.08); padding:2px 6px;
                font:inherit; font-size:9px; border-radius:2px; }
            .mma-counters { font-size:8px; color:rgba(160,170,200,.55); display:flex; gap:10px; }
            .mma-counters b { color:rgba(200,210,230,.8); font-weight:600; }

            .mma-body { flex:1; display:grid; grid-template-rows: 130px 1fr 170px;
                gap:6px; min-height:0; }
            .mma-panel { border:1px solid rgba(255,255,255,.05); border-radius:3px;
                padding:6px 8px; background:rgba(255,255,255,.012); display:flex;
                flex-direction:column; min-height:0; min-width:0; overflow:hidden; }
            .mma-panel-title { font-size:8px; font-weight:600;
                color:rgba(150,165,190,.55); letter-spacing:.5px; text-transform:uppercase;
                margin-bottom:4px; display:flex; justify-content:space-between; }
            .mma-panel-title em { font-style:normal; color:rgba(140,150,175,.4);
                font-weight:400; text-transform:none; letter-spacing:.2px; }

            /* NBBO ribbon */
            .mma-ribbon-wrap { flex:1; display:flex; flex-direction:column; min-height:0; gap:2px; }
            .mma-ribbon-side { display:flex; align-items:center; gap:4px;
                font-size:8px; color:rgba(160,170,200,.5); letter-spacing:.4px; }
            .mma-ribbon-side b { width:28px; color:rgba(200,210,230,.7);
                text-transform:uppercase; letter-spacing:.5px; }
            .mma-ribbon-canvas { flex:1; height:46px; display:block;
                background:rgba(255,255,255,.015); border-radius:2px; }
            .mma-ribbon-legend { display:flex; flex-wrap:wrap; gap:6px;
                font-size:7px; color:rgba(160,170,200,.4); padding:2px 0 0 32px; }
            .mma-legend-item { display:flex; align-items:center; gap:3px; }
            .mma-legend-swatch { width:7px; height:7px; border-radius:1px; }

            /* Middle split: lead-follower + capture + hedge-pressure.
               All minmax(0,1fr) so intrinsic content widths can't dominate;
               proportional ratios keep all visible at every pane width.
               Hedge-pressure column is wider so per-strike bars breathe. */
            .mma-mid { display:grid;
                grid-template-columns: minmax(0,1fr) minmax(0,1fr) minmax(0,1.2fr);
                gap:6px; min-height:0; min-width:0; }

            /* Hedge pressure subpanel — per-strike Γ/V/C bars + totals chip */
            .mma-hp-body { flex:1; overflow-y:auto; min-height:0; }
            .mma-hp-head { display:grid;
                grid-template-columns: 52px 1fr 1fr 1fr;
                gap:4px; font-size:7px; color:rgba(140,150,175,.5);
                letter-spacing:.4px; text-transform:uppercase;
                padding:0 0 3px; border-bottom:1px solid rgba(255,255,255,.05);
                margin-bottom:3px; }
            .mma-hp-row { display:grid;
                grid-template-columns: 52px 1fr 1fr 1fr;
                gap:4px; align-items:center; font-size:8px; padding:1px 0;
                border-bottom:1px solid rgba(255,255,255,.02); }
            .mma-hp-row.atm { background:rgba(133,182,230,.05); }
            .mma-hp-k { font-family:'JetBrains Mono',monospace; font-size:8px;
                color:rgba(210,220,240,.85); font-weight:600; text-align:right; }
            .mma-hp-cell { position:relative; height:9px;
                background:rgba(255,255,255,.02); border-radius:1px;
                overflow:hidden; }
            .mma-hp-bar { position:absolute; top:0; bottom:0;
                border-radius:1px; }
            .mma-hp-bar.pos { background:rgba(133,182,230,.65); }   /* dealers BUY — blue (matches dn-long) */
            .mma-hp-bar.neg { background:rgba(230,150,128,.65); }   /* dealers SELL — amber (matches dn-short) */
            .mma-hp-val { position:absolute; right:3px; top:0; bottom:0;
                display:flex; align-items:center; font-size:7px;
                color:rgba(210,220,240,.78); font-family:'JetBrains Mono',monospace;
                pointer-events:none; }
            .mma-hp-totals { display:flex; gap:6px; flex-wrap:wrap;
                padding:4px 4px 2px; margin-top:3px;
                border-top:1px solid rgba(255,255,255,.04);
                font-size:7px; }
            .mma-hp-tot { display:flex; gap:3px; align-items:baseline;
                padding:2px 6px; border-radius:2px;
                background:rgba(255,255,255,.02);
                color:rgba(180,190,215,.65);
                letter-spacing:.3px; }
            .mma-hp-tot .lbl { color:rgba(140,150,175,.55);
                font-size:7px; text-transform:uppercase;
                letter-spacing:.4px; }
            .mma-hp-tot .v { font-family:'JetBrains Mono',monospace;
                font-weight:700; }
            .mma-hp-tot .v.pos { color:#85b6e6; }
            .mma-hp-tot .v.neg { color:#e69580; }
            .mma-hp-tot .v.zero{ color:rgba(170,180,210,.4); }
            .mma-hp-zero { padding:2px 8px; font-size:7px;
                color:rgba(180,190,215,.55); letter-spacing:.4px;
                text-transform:uppercase; }
            .mma-hp-zero b { color:rgba(210,220,240,.85);
                font-family:'JetBrains Mono',monospace; font-weight:700; }

            /* Phase B — per-exchange HP_γ rollup table */
            .mma-hpx-head { display:grid;
                grid-template-columns: 50px 1fr 1fr 1fr;
                gap:4px; font-size:7px; color:rgba(140,150,175,.5);
                letter-spacing:.4px; text-transform:uppercase;
                padding:4px 0 3px; margin-top:4px;
                border-top:1px solid rgba(255,255,255,.05);
                border-bottom:1px solid rgba(255,255,255,.05); }
            .mma-hpx-body { max-height:110px; overflow-y:auto; }
            .mma-hpx-row { display:grid;
                grid-template-columns: 50px 1fr 1fr 1fr;
                gap:4px; align-items:center; font-size:8px; padding:1px 0;
                border-bottom:1px solid rgba(255,255,255,.02); }
            .mma-hpx-exch { font-weight:700;
                font-family:'JetBrains Mono',monospace;
                color:rgba(210,220,240,.85); }
            .mma-hpx-val  { text-align:right; font-family:'JetBrains Mono',monospace; }
            .mma-hpx-val.pos  { color:#85b6e6; }
            .mma-hpx-val.neg  { color:#e69580; }
            .mma-hpx-val.zero { color:rgba(170,180,210,.35); }

            /* Lead-follower list */
            .mma-formations { flex:1; overflow-y:auto; }
            .mma-form-item { padding:3px 0; border-bottom:1px solid rgba(255,255,255,.03); }
            .mma-form-head { font-size:9px; color:rgba(200,210,230,.7); }
            .mma-form-head span.ts { color:rgba(140,150,175,.55); margin-right:6px; }
            .mma-form-head span.side { text-transform:uppercase; font-weight:600; margin-right:4px; }
            .mma-form-head .side-bid { color:#66cc99; }
            .mma-form-head .side-ask { color:#e08040; }
            .mma-form-head .price { color:rgba(210,220,240,.9); font-weight:600; }
            .mma-arrivals { margin-left:10px; font-size:8px; color:rgba(160,170,200,.55); }
            .mma-arr { display:inline-block; padding:0 5px 0 0; white-space:nowrap; }
            .mma-arr .t { color:rgba(130,140,170,.45); }
            .mma-arr .x { font-weight:600; }
            .mma-arr .sz { color:rgba(140,150,175,.55); }

            /* Capture-vs-post table */
            .mma-cap-row { display:grid;
                grid-template-columns: 50px 70px 42px 44px 42px 70px;
                gap:4px; align-items:center; font-size:9px; padding:2px 0;
                border-bottom:1px solid rgba(255,255,255,.025); }
            .mma-cap-head { font-size:7px; color:rgba(140,150,175,.5);
                letter-spacing:.4px; text-transform:uppercase; border-bottom-color:rgba(255,255,255,.05); padding-bottom:4px; }
            .mma-cap-exch { font-weight:600; }
            .mma-cap-barwrap { height:6px; background:rgba(255,255,255,.04); border-radius:1px;
                position:relative; overflow:hidden; }
            .mma-cap-bar { position:absolute; left:0; top:0; bottom:0;
                border-radius:1px; }
            .mma-cap-pct  { text-align:right; color:rgba(210,220,240,.8); }
            .mma-cap-num  { text-align:right; color:rgba(210,220,240,.8); }
            .mma-cap-diff { text-align:right; font-weight:600; }
            .mma-cap-diff.up   { color:#66cc99; }
            .mma-cap-diff.down { color:#e06060; }
            .mma-cap-diff.zero { color:rgba(180,180,200,.35); }

            /* Impulse response */
            .mma-imp-head { display:flex; justify-content:space-between; align-items:center;
                font-size:9px; color:rgba(180,190,215,.7); gap:8px; margin-bottom:2px; }
            .mma-imp-nav button { background:#11162a; color:rgba(200,210,230,.75);
                border:1px solid rgba(255,255,255,.07); font:inherit; font-size:9px;
                padding:2px 8px; border-radius:2px; cursor:pointer; }
            .mma-imp-nav button:disabled { opacity:.3; cursor:default; }
            .mma-imp-body { flex:1; display:grid;
                grid-template-columns: minmax(0,1.5fr) minmax(0,1fr);
                gap:8px; min-height:0; min-width:0; }
            .mma-imp-canvas { width:100%; height:100%; display:block;
                background:rgba(255,255,255,.012); border-radius:2px; }
            .mma-imp-table { overflow-y:auto; font-size:8px;
                color:rgba(180,190,215,.7); }
            .mma-imp-table .row { padding:1px 0; border-bottom:1px solid rgba(255,255,255,.02); }
            .mma-imp-table .t { color:rgba(140,150,175,.55); }
            .mma-imp-table .w { color:#66cc99; }   /* signed equity shares positive */
            .mma-imp-table .a { color:#cc6677; }   /* signed equity shares negative */
            .mma-imp-table .n { color:rgba(170,180,210,.5); }
            .mma-empty { color:rgba(160,170,200,.35); font-size:9px;
                text-align:center; padding:12px; font-style:italic; }

            .mma-foot { font-size:7px; color:rgba(130,140,170,.38);
                text-align:center; padding:2px 0 0; letter-spacing:.4px;
                font-style:italic; }

            /* Wall signals ribbon (header row under the counters) */
            .mma-walls { display:flex; gap:6px; flex-wrap:wrap;
                font-size:9px; color:rgba(180,190,215,.7);
                padding:4px 2px; border-top:1px solid rgba(255,255,255,.04);
                border-bottom:1px solid rgba(255,255,255,.04); }
            .mma-wall-chip { display:flex; gap:4px; align-items:center;
                padding:2px 7px; border-radius:3px;
                background:rgba(255,255,255,.018);
                border:1px solid rgba(255,255,255,.05);
                font-size:8px; letter-spacing:.3px; text-transform:uppercase;
                color:rgba(170,180,210,.65); }
            .mma-wall-chip .name { font-weight:600;
                color:rgba(200,210,230,.85); }
            .mma-wall-chip .strike { color:rgba(210,220,240,.75);
                font-family:'JetBrains Mono',monospace; }
            .mma-wall-chip .scores { display:flex; gap:4px; align-items:center; }
            .mma-wall-chip .sc { padding:1px 4px; border-radius:2px;
                font-family:'JetBrains Mono',monospace; font-weight:600; }
            .mma-wall-chip .sc.cont { color:rgba(120,200,150,.85);
                background:rgba(100,220,140,.08); }
            .mma-wall-chip .sc.cont.hot { color:#66cc99;
                background:rgba(100,220,140,.22); }
            .mma-wall-chip .sc.fade { color:rgba(220,140,110,.85);
                background:rgba(220,130,90,.08); }
            .mma-wall-chip .sc.fade.hot { color:#e08040;
                background:rgba(220,130,90,.22); }
            .mma-wall-chip .tag { padding:1px 4px; border-radius:2px;
                font-size:7px; letter-spacing:.4px;
                background:rgba(255,255,255,.05);
                color:rgba(170,180,210,.55); }
            .mma-wall-chip .tag.crossed { background:rgba(100,220,140,.18);
                color:#66cc99; }
            .mma-wall-chip .tag.near    { background:rgba(220,180,100,.12);
                color:#d6b060; }
            .mma-wall-chip .dist { color:rgba(140,150,175,.5); }
            .mma-wall-chip .venues { color:rgba(140,160,200,.55);
                font-family:'JetBrains Mono',monospace; }
            .mma-walls .empty { color:rgba(140,150,175,.35);
                font-style:italic; padding:2px 0; font-size:8px; }

            /* Hit-rate ledger strip — "is this working?" ground truth */
            .mma-ledger { display:flex; gap:14px; align-items:center;
                padding:5px 8px; font-size:9px;
                background:rgba(20,25,40,.4);
                border-top:1px solid rgba(255,255,255,.04);
                border-bottom:1px solid rgba(255,255,255,.04); }
            .mma-ledger .group { display:flex; gap:4px; align-items:baseline; }
            .mma-ledger .lbl { color:rgba(130,140,170,.55);
                font-size:7px; letter-spacing:.4px; text-transform:uppercase; }
            .mma-ledger .val { color:rgba(210,220,240,.8);
                font-family:'JetBrains Mono',monospace; font-weight:600; }
            .mma-ledger .rate-hot  { color:#66cc99; }
            .mma-ledger .rate-warm { color:#d6b060; }
            .mma-ledger .rate-cold { color:#cc6677; }
            .mma-ledger .edge { padding:2px 6px; border-radius:3px;
                font-family:'JetBrains Mono',monospace; font-weight:700;
                letter-spacing:.3px; }
            .mma-ledger .edge.pos { color:#66cc99;
                background:rgba(100,220,140,.14); }
            .mma-ledger .edge.neg { color:#cc6677;
                background:rgba(220,100,120,.14); }
            .mma-ledger .edge.zero { color:rgba(170,180,210,.5);
                background:rgba(255,255,255,.03); }
            .mma-ledger .last-fire { margin-left:auto;
                color:rgba(180,190,215,.55); font-family:'JetBrains Mono',monospace;
                font-size:8px; }
            .mma-ledger .last-fire .ok  { color:#66cc99; }
            .mma-ledger .last-fire .bad { color:#cc6677; }
            .mma-ledger .last-fire .pend{ color:#d6b060; }

            /* State-machine line beneath the chip row */
            .mma-states { display:flex; gap:12px; flex-wrap:wrap;
                padding:3px 8px; font-size:8px;
                color:rgba(140,150,175,.55);
                font-family:'JetBrains Mono',monospace; }
            .mma-states .row { display:flex; gap:4px; align-items:center; }
            .mma-states .state { padding:1px 5px; border-radius:2px;
                font-size:7px; letter-spacing:.4px; font-weight:600; }
            .mma-states .state.ARMED    { color:#66cc99;
                background:rgba(100,220,140,.14); }
            .mma-states .state.COOLDOWN { color:#d6b060;
                background:rgba(220,180,100,.12); }
            .mma-states .state.QUIET    { color:rgba(140,150,175,.4);
                background:rgba(255,255,255,.03); }
            .mma-states .state.DISTANT  { color:rgba(140,150,175,.3);
                background:rgba(255,255,255,.02); }

            /* Signed-gamma pills on wall chips */
            .mma-wall-chip .dn { padding:1px 5px; border-radius:2px;
                font-size:8px; letter-spacing:.3px; font-weight:700;
                font-family:'JetBrains Mono',monospace; }
            .mma-wall-chip .dn.dn-short { color:#e69580;
                background:rgba(230,150,128,.18); }  /* dealers short gamma — amplify */
            .mma-wall-chip .dn.dn-long  { color:#85b6e6;
                background:rgba(133,182,230,.16); }  /* dealers long gamma — damp */
            .mma-wall-chip .dn.dn-flat  { color:rgba(170,180,210,.5);
                background:rgba(255,255,255,.03); }
            .mma-wall-chip .ed { padding:1px 5px; border-radius:2px;
                font-size:8px; letter-spacing:.3px; font-weight:700;
                color:#c4a8e6; background:rgba(196,168,230,.14);
                font-family:'JetBrains Mono',monospace; }

            /* Regime chip — overall gamma regime (spot vs flip) */
            .mma-wall-chip.regime-long  { border-left:2px solid #85b6e6; }
            .mma-wall-chip.regime-short { border-left:2px solid #e69580; }
            .mma-wall-chip.regime-unknown { border-left:2px solid rgba(170,180,210,.25); }

            /* Sign-convention edge pill in ledger strip */
            .mma-ledger .sig-edge { padding:2px 6px; border-radius:3px;
                font-family:'JetBrains Mono',monospace; font-weight:700;
                letter-spacing:.3px; font-size:8px; }
            .mma-ledger .sig-edge.pos { color:#85b6e6;
                background:rgba(133,182,230,.18); }  /* sign convention helps */
            .mma-ledger .sig-edge.neg { color:#e69580;
                background:rgba(230,150,128,.18); }  /* sign convention backwards */
            .mma-ledger .sig-edge.zero{ color:rgba(170,180,210,.5);
                background:rgba(255,255,255,.03); }
            .mma-ledger .regime-cell { display:inline-flex; gap:4px;
                align-items:baseline; font-family:'JetBrains Mono',monospace;
                font-size:8px; }

            /* Phase C — WITH/AGAINST alignment pill in the header row */
            .mma-align { padding:2px 8px; border-radius:3px;
                font-family:'JetBrains Mono',monospace; font-weight:700;
                font-size:8px; letter-spacing:.5px;
                border-left:3px solid rgba(255,255,255,.2);
                background:rgba(255,255,255,.03);
                color:rgba(210,220,240,.8);
                display:inline-flex; gap:6px; align-items:center;
                cursor:default; position:relative; }
            .mma-align.align-with    { border-left-color:#66cc99;
                color:#66cc99; background:rgba(100,220,140,.12); }
            .mma-align.align-against { border-left-color:#cc6677;
                color:#cc6677; background:rgba(220,100,120,.12); }
            .mma-align.align-neutral { border-left-color:rgba(170,180,210,.4);
                color:rgba(180,190,215,.65); background:rgba(255,255,255,.03); }
            .mma-align .reason { font-weight:400;
                color:rgba(170,180,210,.55); font-size:7px;
                text-transform:none; letter-spacing:.2px; }
            .mma-align-tip { display:none; position:absolute; top:100%;
                right:0; margin-top:4px;
                background:#0c1020; border:1px solid rgba(255,255,255,.08);
                border-radius:3px; padding:6px 9px;
                min-width:200px; z-index:10; font-weight:400;
                font-size:8px; letter-spacing:.2px; text-transform:none;
                color:rgba(200,210,230,.78); }
            .mma-align:hover .mma-align-tip { display:block; }
            .mma-align-tip .trow { display:grid;
                grid-template-columns: 80px 1fr; gap:4px; padding:1px 0; }
            .mma-align-tip .trow b { color:rgba(220,230,250,.9); font-weight:600; }
            .mma-align-tip .w { color:#66cc99; font-weight:700; }
            .mma-align-tip .a { color:#cc6677; font-weight:700; }
            .mma-align-tip .n { color:rgba(170,180,210,.5); }
        `;
        document.head.appendChild(_styleEl);
    }

    function _buildShell() {
        _slot.innerHTML = `
            <div class="mma-wrap">
              <div class="mma-header">
                <span class="mma-title">MM Attribution · raw structure</span>
                <div class="mma-ctrls">
                  <label>contract</label>
                  <select data-sel-contract><option value="">auto (rank top)</option></select>
                  <label>rank by</label>
                  <select data-sel-metric>
                    <option value="events">events</option>
                    <option value="prints">prints</option>
                    <option value="formations">formations</option>
                  </select>
                  <label>zoom</label>
                  <select data-sel-zoom>
                    <option value="session">session</option>
                    <option value="1h">1h</option>
                    <option value="15m">15m</option>
                    <option value="5m">5m</option>
                  </select>
                </div>
                <div class="mma-counters" data-counters>
                  <span>tracked <b data-c-tracked>–</b></span>
                  <span>events <b data-c-events>–</b></span>
                  <span>prints <b data-c-prints>–</b></span>
                  <span>logged <b data-c-log>–</b></span>
                </div>
                <div class="mma-align align-neutral" data-align>
                  · waiting
                  <div class="mma-align-tip" data-align-tip></div>
                </div>
              </div>

              <div class="mma-walls" data-walls>
                <span class="empty">wall signals — waiting for wall + spot feed…</span>
              </div>

              <div class="mma-ledger" data-ledger>
                <span class="group"><span class="lbl">collecting</span>
                  <span class="val">— signal ledger starting —</span></span>
              </div>

              <div class="mma-states" data-states></div>

              <div class="mma-body">
                <div class="mma-panel">
                  <div class="mma-panel-title">
                    <span>NBBO ownership ribbon</span>
                    <em data-ribbon-sub>—</em>
                  </div>
                  <div class="mma-ribbon-wrap">
                    <div class="mma-ribbon-side"><b>bid</b>
                      <canvas class="mma-ribbon-canvas" data-ribbon-bid></canvas></div>
                    <div class="mma-ribbon-side"><b>ask</b>
                      <canvas class="mma-ribbon-canvas" data-ribbon-ask></canvas></div>
                    <div class="mma-ribbon-legend" data-ribbon-legend></div>
                  </div>
                </div>

                <div class="mma-mid">
                  <div class="mma-panel">
                    <div class="mma-panel-title">
                      <span>Lead-follower · level formations</span>
                      <em data-form-sub>—</em>
                    </div>
                    <div class="mma-formations" data-formations></div>
                  </div>
                  <div class="mma-panel">
                    <div class="mma-panel-title">
                      <span>Capture vs post</span>
                      <em data-cap-sub>—</em>
                    </div>
                    <div class="mma-cap-row mma-cap-head">
                      <span>exch</span><span>posted</span><span>pct</span>
                      <span>caught</span><span>pct</span><span>diff</span>
                    </div>
                    <div data-capture style="flex:1;overflow-y:auto"></div>
                  </div>
                  <div class="mma-panel">
                    <div class="mma-panel-title">
                      <span>Hedge pressure · Γ·ΔS% / V·Δσpt / C·Δt_hr</span>
                      <em data-hp-sub>—</em>
                    </div>
                    <div class="mma-hp-head">
                      <span>K</span><span>Γ · 1%</span><span>V · 1σpt</span><span>C · 1hr</span>
                    </div>
                    <div class="mma-hp-body" data-hp-body></div>
                    <div class="mma-hp-zero" data-hp-zero></div>
                    <div class="mma-hp-totals" data-hp-totals></div>
                    <div class="mma-hpx-head">
                      <span>venue</span><span>γ posted</span><span>γ caught</span><span>diff</span>
                    </div>
                    <div class="mma-hpx-body" data-hpx-body>
                      <div class="mma-empty">venue rollup — waiting…</div>
                    </div>
                  </div>
                </div>

                <div class="mma-panel">
                  <div class="mma-imp-head">
                    <span>Impulse response · <em data-imp-sub>no print yet</em></span>
                    <div class="mma-imp-nav">
                      <button data-imp-prev>← prev</button>
                      <button data-imp-live>live</button>
                      <button data-imp-next>next →</button>
                    </div>
                  </div>
                  <div class="mma-imp-body">
                    <canvas class="mma-imp-canvas" data-imp-canvas></canvas>
                    <div class="mma-imp-table" data-imp-table></div>
                  </div>
                </div>
              </div>

              <div class="mma-foot">Measurements only · event-driven · session-to-date · zero hard-coded cutoffs</div>
            </div>
        `;
    }

    function _wireControls() {
        const selC = _slot.querySelector('[data-sel-contract]');
        const selM = _slot.querySelector('[data-sel-metric]');
        const selZ = _slot.querySelector('[data-sel-zoom]');
        selC.addEventListener('change', (e) => {
            _selectedSym = e.target.value || null;
            _impulsePrintTs = null;
            _refreshState();
            // Phase C — re-fetch alignment immediately on selection change so
            // the pill never lags the visual context.
            _pollAlignment();
        });
        selM.addEventListener('change', (e) => {
            _rankMetric = e.target.value;
            _refreshContracts();
        });
        selZ.addEventListener('change', (e) => {
            _zoomMode = e.target.value;
            _drawRibbon();
        });
        _slot.querySelector('[data-imp-prev]').addEventListener('click', () => _stepImpulse(-1));
        _slot.querySelector('[data-imp-next]').addEventListener('click', () => _stepImpulse(+1));
        _slot.querySelector('[data-imp-live]').addEventListener('click', () => {
            _impulsePrintTs = null;
            _refreshState();
        });
    }

    // ── REST calls ─────────────────────────────────────────────────────

    async function _refreshContracts() {
        try {
            const r = await _authFetch(`/api/mm_attribution/contracts?metric=${_rankMetric}&limit=50`);
            if (!r.ok) return;
            const j = await r.json();
            _lastContractsResp = j;
            _populateContractSelector(j.contracts || []);
            _renderSummary(j.summary || {});
            const targetSym = _selectedSym
                || (j.contracts && j.contracts.length > 0 ? j.contracts[0].sym : null);
            // Sync the socket subscription to whichever contract we're viewing.
            _ensureWatching(targetSym);
        } catch (e) { /* network hiccup, next tick */ }
    }

    function _ensureWatching(sym) {
        if (_destroyed) return;
        if (!sym) {
            if (_watchedSym && window._sio && window._sio.connected) {
                try { window._sio.emit('mm_attribution:unwatch', {}); } catch (_) {}
            }
            _watchedSym = null;
            return;
        }
        if (sym === _watchedSym) return;
        if (!window._sio || !window._sio.connected) {
            // Socket not ready yet — retry on next rank tick.
            return;
        }
        try {
            window._sio.emit('mm_attribution:watch', { sym });
            _watchedSym = sym;
        } catch (_) { /* best-effort */ }
    }

    function _populateContractSelector(rows) {
        const sel = _slot.querySelector('[data-sel-contract]');
        const cur = _selectedSym || '';
        // Rebuild options, preserve current
        const opts = ['<option value="">auto (rank top)</option>']
            .concat(rows.map(r => `<option value="${r.sym}">${_fmtSym(r.sym)} · ${r.events}ev · ${r.prints}pr</option>`));
        sel.innerHTML = opts.join('');
        sel.value = cur;
    }

    async function _refreshState(explicitSym) {
        // Base state now arrives via socket ('mm_contract_state'); this helper
        // only pulls the impulse-specific extras (prev/next navigation and the
        // full impulse list).
        const sym = explicitSym || _selectedSym
            || (_lastContractsResp && _lastContractsResp.contracts && _lastContractsResp.contracts[0]
                ? _lastContractsResp.contracts[0].sym : null);
        if (!sym) return;
        _ensureWatching(sym);
        try {
            if (_impulsePrintTs) {
                const ri = await _authFetch(`/api/mm_attribution/impulse/${encodeURIComponent(sym)}?print_ts=${_impulsePrintTs}`);
                if (ri.ok) {
                    const ij = await ri.json();
                    if (ij && ij.print_ts && _lastStateResp) {
                        _lastStateResp.last_impulse = ij;
                        _renderImpulse();
                    }
                }
            }
            const rl = await _authFetch(`/api/mm_attribution/impulse/${encodeURIComponent(sym)}?limit=200`);
            if (rl.ok) {
                const lj = await rl.json();
                _lastImpulseList = (lj && lj.impulses) || [];
            }
        } catch (e) { /* ignore */ }
    }

    function _onStatePush(state) {
        if (_destroyed || !state || !state.sym) return;
        // Filter: only accept the sym we're currently viewing (server emits to
        // our room, but this belt+braces keeps a stale emit from a race).
        const want = _watchedSym
            || _selectedSym
            || (_lastContractsResp && _lastContractsResp.contracts && _lastContractsResp.contracts[0]
                ? _lastContractsResp.contracts[0].sym : null);
        if (state.sym !== want) return;
        _lastStateResp = state;
        _renderAll();
    }

    // ── Render ────────────────────────────────────────────────────────

    function _renderSummary(s) {
        _slot.querySelector('[data-c-tracked]').textContent = s.contracts_tracked ?? '–';
        _slot.querySelector('[data-c-events]').textContent  = s.total_events ?? '–';
        _slot.querySelector('[data-c-prints]').textContent  = s.total_prints ?? '–';
        _slot.querySelector('[data-c-log]').textContent     = s.disk_log_count ?? '–';
    }

    function _renderAll() {
        if (!_lastStateResp) return;
        const s = _lastStateResp;
        _slot.querySelector('[data-ribbon-sub]').textContent =
            `${_fmtSym(s.sym)} · session open ${_fmtClock(s.session_open_ts)} · ribbon samples ${(s.ribbon||[]).length}`;
        _slot.querySelector('[data-form-sub]').textContent =
            `${(s.formations||[]).length} since 09:30`;
        _slot.querySelector('[data-cap-sub]').textContent =
            `${(s.total_posted_time_s || 0).toFixed(0)}s posted · ${s.total_caught || 0} prints`;
        _drawRibbon();
        _renderFormations();
        _renderCapture();
        _renderImpulse();
        _renderLegend();
    }

    function _renderLegend() {
        // Only exchanges that appear in this contract right now.
        const present = new Set();
        const s = _lastStateResp;
        for (const sm of (s.ribbon || [])) {
            (sm.bid_exchs || []).forEach(e => present.add((e||'').toUpperCase()));
            (sm.ask_exchs || []).forEach(e => present.add((e||'').toUpperCase()));
        }
        for (const row of (s.capture || [])) present.add((row.exch||'').toUpperCase());
        const items = Array.from(present).sort().map(e =>
            `<span class="mma-legend-item"><span class="mma-legend-swatch" style="background:${colorFor(e)}"></span>${e}</span>`
        ).join('');
        _slot.querySelector('[data-ribbon-legend]').innerHTML = items;
    }

    // NBBO ribbon renderer — one row per exchange per side.
    function _drawRibbon() {
        if (!_lastStateResp) return;
        _drawRibbonSide('bid', '[data-ribbon-bid]');
        _drawRibbonSide('ask', '[data-ribbon-ask]');
    }

    function _drawRibbonSide(side, selector) {
        const canvas = _slot.querySelector(selector);
        if (!canvas) return;
        const samples = (_lastStateResp.ribbon || [])
            .filter(sm => (side === 'bid' ? sm.bid_exchs : sm.ask_exchs));
        // Enumerate exchanges for this side
        const exchOrder = [];
        const exchSeen = new Set();
        for (const sm of samples) {
            const ex = side === 'bid' ? (sm.bid_exchs||[]) : (sm.ask_exchs||[]);
            for (const e of ex) {
                const u = (e||'').toUpperCase();
                if (!exchSeen.has(u)) { exchSeen.add(u); exchOrder.push(u); }
            }
        }

        const rect = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width  = Math.floor(rect.width * dpr);
        canvas.height = Math.floor(rect.height * dpr);
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        ctx.clearRect(0, 0, rect.width, rect.height);

        if (samples.length === 0 || exchOrder.length === 0) {
            ctx.fillStyle = 'rgba(160,170,200,.25)';
            ctx.font = '8px monospace';
            ctx.textAlign = 'center';
            ctx.fillText('no samples yet', rect.width / 2, rect.height / 2);
            return;
        }

        // Determine time window from zoom mode.
        // Session mode anchors to max(sessionOpen, firstSampleTs) so the canvas
        // doesn't render empty space before this contract's book subscription
        // began (e.g. mid-session server restart). Structural, not cosmetic —
        // without it, the ribbon compresses a 5-min data window into the last
        // 4% of pixels on a 5-hour session axis.
        const now = _lastStateResp.now_ts || (Date.now() / 1000);
        const sessionOpen = _lastStateResp.session_open_ts || (samples[0].ts);
        const firstSample = samples[0].ts;
        const dataStart = Math.max(sessionOpen, firstSample);
        let t0 = dataStart;
        if (_zoomMode === '1h')  t0 = now - 3600;
        else if (_zoomMode === '15m') t0 = now - 900;
        else if (_zoomMode === '5m')  t0 = now - 300;
        if (t0 < dataStart) t0 = dataStart;
        const t1 = now;
        const span = Math.max(1e-6, t1 - t0);

        // Per-exchange row height
        const rowH = rect.height / exchOrder.length;

        // Iterate samples pairwise; segment width = time between samples (event-driven).
        for (let i = 0; i < samples.length; i++) {
            const s  = samples[i];
            const ns = samples[i + 1];
            const segT0 = s.ts;
            const segT1 = ns ? ns.ts : now;
            if (segT1 < t0 || segT0 > t1) continue;
            const visT0 = Math.max(segT0, t0);
            const visT1 = Math.min(segT1, t1);
            const x0 = ((visT0 - t0) / span) * rect.width;
            const x1 = ((visT1 - t0) / span) * rect.width;
            const w = Math.max(1, x1 - x0);
            const ex = (side === 'bid' ? s.bid_exchs : s.ask_exchs) || [];
            const presentSet = new Set(ex.map(e => (e||'').toUpperCase()));
            for (let r = 0; r < exchOrder.length; r++) {
                const e = exchOrder[r];
                if (!presentSet.has(e)) continue;
                ctx.fillStyle = colorFor(e);
                ctx.globalAlpha = 0.85;
                ctx.fillRect(x0, r * rowH + 0.5, w, Math.max(1, rowH - 1));
            }
        }
        ctx.globalAlpha = 1;

        // Draw right-aligned exchange tags
        ctx.font = '8px monospace';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        for (let r = 0; r < exchOrder.length; r++) {
            ctx.fillStyle = 'rgba(210,220,240,.55)';
            ctx.fillText(exchOrder[r], rect.width - 3, r * rowH + rowH / 2);
        }
    }

    function _renderFormations() {
        const box = _slot.querySelector('[data-formations]');
        const forms = _lastStateResp.formations || [];
        if (forms.length === 0) {
            box.innerHTML = '<div class="mma-empty">no level formations yet</div>';
            return;
        }
        const parts = [];
        // Cap display at 200 items (infra cap; all are on disk); no data filtering.
        const MAX_DISPLAY = 200;
        for (let i = 0; i < Math.min(forms.length, MAX_DISPLAY); i++) {
            const f = forms[i];
            const sideCls = f.side === 'bid' ? 'side-bid' : 'side-ask';
            const ts = _fmtClockMs(f.ts_formed);
            const price = (f.price != null) ? f.price.toFixed(2) : '?';
            const arr = (f.arrivals || []).map(a =>
                `<span class="mma-arr"><span class="t">t+${a.t_ms}ms</span> <span class="x" style="color:${colorFor(a.exch)}">${(a.exch||'').toUpperCase()}</span> <span class="sz">sz=${a.size}</span></span>`
            ).join('');
            const liveBadge = f.ts_vanished == null ? ' · <em style="color:#66cc99">live</em>' : '';
            parts.push(`
                <div class="mma-form-item">
                  <div class="mma-form-head">
                    <span class="ts">${ts}</span>
                    <span class="side ${sideCls}">${f.side}</span>
                    <span class="price">${price}</span>
                    <span style="color:rgba(140,150,175,.45); font-size:8px">formed${liveBadge}</span>
                  </div>
                  <div class="mma-arrivals">${arr}</div>
                </div>
            `);
        }
        box.innerHTML = parts.join('');
    }

    function _renderCapture() {
        const box = _slot.querySelector('[data-capture]');
        const rows = _lastStateResp.capture || [];
        if (rows.length === 0) {
            box.innerHTML = '<div class="mma-empty">no posted-time yet</div>';
            return;
        }
        // POST%   = posted_share — MM's share of total posting time across all
        //           MMs. Σ = 100% by construction.
        // CAUGHT% = caught_at_top_pct — MM's share of prints that hit top-of-
        //           book while they were posting there. Σ = 100%.
        // DIFF    = caught_at_top_pct − posted_share. Σ = 0 by construction.
        //           +ve = MM catches more than its posting share (flow-attractor),
        //           -ve = MM posts more than it catches (defensive/over-quoting).
        // The secondary `caught_at_top / caught_count` ratio shows routed volume
        // vs attribution-relevant prints (off-top / off-book prints excluded
        // from DIFF math).
        const parts = rows.map(r => {
            const pShare = ((r.posted_share || 0) * 100).toFixed(1);
            const topPct = ((r.caught_at_top_pct || 0) * 100).toFixed(1);
            const dPct = (r.diff_pct * 100);
            const dCls = dPct > 0.05 ? 'up' : (dPct < -0.05 ? 'down' : 'zero');
            const dTxt = (dPct >= 0 ? '+' : '') + dPct.toFixed(1) + '%';
            const exch = (r.exch || '').toUpperCase();
            const col = colorFor(exch);
            const barW = Math.max(2, Math.min(100, (r.posted_share || 0) * 100)).toFixed(1);
            const caughtTop   = r.caught_at_top   || 0;
            const caughtTotal = r.caught_count    || 0;
            return `<div class="mma-cap-row">
                <span class="mma-cap-exch" style="color:${col}">${exch}</span>
                <span class="mma-cap-barwrap">
                  <span class="mma-cap-bar" style="width:${barW}%;background:${col};opacity:.55"></span>
                </span>
                <span class="mma-cap-pct">${pShare}%</span>
                <span class="mma-cap-num" title="caught_at_top / caught_total">${caughtTop}<span style="opacity:.45">/${caughtTotal}</span></span>
                <span class="mma-cap-pct">${topPct}%</span>
                <span class="mma-cap-diff ${dCls}">${dTxt}</span>
            </div>`;
        });
        box.innerHTML = parts.join('');
    }

    function _renderImpulse() {
        const canvas = _slot.querySelector('[data-imp-canvas]');
        const table = _slot.querySelector('[data-imp-table]');
        const sub = _slot.querySelector('[data-imp-sub]');
        const imp = _lastStateResp ? _lastStateResp.last_impulse : null;
        const prevBtn = _slot.querySelector('[data-imp-prev]');
        const nextBtn = _slot.querySelector('[data-imp-next]');

        if (!imp || !imp.print_ts) {
            sub.textContent = 'no print yet';
            table.innerHTML = '<div class="mma-empty">waiting for next print</div>';
            const ctx = canvas.getContext('2d');
            const r = canvas.getBoundingClientRect();
            canvas.width = r.width; canvas.height = r.height;
            ctx.clearRect(0, 0, r.width, r.height);
            ctx.fillStyle = 'rgba(160,170,200,.25)';
            ctx.font = '9px monospace';
            ctx.textAlign = 'center';
            ctx.fillText('no impulse data', r.width / 2, r.height / 2);
            prevBtn.disabled = nextBtn.disabled = !_lastImpulseList.length;
            return;
        }

        const liveLbl = imp.closed ? 'closed' : 'live';
        const boundary = imp.next_print_ts
            ? `next print ${_fmtClockMs(imp.next_print_ts)} (${Math.round((imp.next_print_ts - imp.print_ts) * 1000)}ms)`
            : 'awaiting next print';
        sub.innerHTML = `print ${_fmtClockMs(imp.print_ts)} · <span style="color:${colorFor(imp.print_exch)}">${(imp.print_exch||'').toUpperCase()}</span> ${imp.print_price} × ${imp.print_size} · ${liveLbl} · ${boundary}`;

        _drawImpulse(canvas, imp);
        _renderImpulseTable(table, imp);

        // Nav buttons
        const list = _lastImpulseList;
        if (!list.length) { prevBtn.disabled = nextBtn.disabled = true; return; }
        const idx = list.findIndex(r => Math.abs(r.print_ts - imp.print_ts) < 0.001);
        // list is newest-first
        prevBtn.disabled = idx < 0 || idx >= list.length - 1;
        nextBtn.disabled = idx <= 0;
    }

    function _stepImpulse(dir) {
        if (!_lastImpulseList.length) return;
        const cur = _lastStateResp && _lastStateResp.last_impulse;
        let idx = cur ? _lastImpulseList.findIndex(r => Math.abs(r.print_ts - cur.print_ts) < 0.001) : -1;
        // newest-first; "prev" = older in time = idx+1; "next" = newer = idx-1
        if (idx < 0) idx = 0;
        const target = dir < 0 ? idx + 1 : idx - 1;
        if (target < 0 || target >= _lastImpulseList.length) return;
        _impulsePrintTs = _lastImpulseList[target].print_ts;
        _refreshState();
    }

    function _drawImpulse(canvas, imp) {
        const rect = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.floor(rect.width * dpr);
        canvas.height = Math.floor(rect.height * dpr);
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        ctx.clearRect(0, 0, rect.width, rect.height);

        const ticks = imp.ticks || [];
        if (ticks.length === 0) {
            ctx.fillStyle = 'rgba(160,170,200,.3)';
            ctx.font = '9px monospace';
            ctx.textAlign = 'center';
            ctx.fillText('no ticks captured yet', rect.width / 2, rect.height / 2);
            return;
        }

        const padL = 30, padR = 8, padT = 10, padB = 18;
        const w = rect.width - padL - padR;
        const h = rect.height - padT - padB;

        const tMin = 0;
        const tMax = Math.max(1, ticks[ticks.length - 1].t_ms);
        const maxMm = Math.max(1, ...ticks.map(t => t.mm_count));

        // y gridlines for mm_count
        ctx.strokeStyle = 'rgba(255,255,255,.05)';
        ctx.lineWidth = 1;
        ctx.font = '7px monospace';
        ctx.fillStyle = 'rgba(160,170,200,.4)';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        for (let v = 0; v <= maxMm; v++) {
            const y = padT + h - (v / maxMm) * h;
            ctx.beginPath();
            ctx.moveTo(padL, y);
            ctx.lineTo(padL + w, y);
            ctx.stroke();
            ctx.fillText(v.toString(), padL - 3, y);
        }

        // mm_count polyline
        ctx.strokeStyle = '#66cc99';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (let i = 0; i < ticks.length; i++) {
            const t = ticks[i];
            const x = padL + (t.t_ms - tMin) / (tMax - tMin || 1) * w;
            const y = padT + h - (t.mm_count / maxMm) * h;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Dots colored by first exch at the level
        for (const t of ticks) {
            const x = padL + (t.t_ms - tMin) / (tMax - tMin || 1) * w;
            const y = padT + h - (t.mm_count / maxMm) * h;
            ctx.fillStyle = colorFor((t.exchs && t.exchs[0]) || '');
            ctx.beginPath();
            ctx.arc(x, y, 2.2, 0, Math.PI * 2);
            ctx.fill();
        }

        // x-axis label
        ctx.fillStyle = 'rgba(160,170,200,.5)';
        ctx.textAlign = 'left';
        ctx.fillText('t+0ms', padL, rect.height - 4);
        ctx.textAlign = 'right';
        ctx.fillText(`t+${tMax}ms`, padL + w, rect.height - 4);
    }

    function _renderImpulseTable(box, imp) {
        const ticks = imp.ticks || [];

        // Phase D — hp_context header row, if present (closed impulses only).
        // Shows equity-tape prints captured in [prev_option_print, next_option_print).
        let headHtml = '';
        const hp = imp.hp_context;
        if (hp && typeof hp === 'object') {
            const prints = Number(hp.equity_prints_in_window || 0);
            const signed = Number(hp.observed_signed_shares    || 0);
            const total  = Number(hp.observed_total_volume     || 0);
            const byEx   = hp.observed_by_exch || {};
            // Top 3 venues by abs volume (structural, not magnitude threshold).
            const venues = Object.entries(byEx)
                .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 3)
                .map(([mic, sz]) => `<span style="color:${colorFor(mic)}">${(mic||'').toUpperCase()}</span> ${sz}`)
                .join(' · ');
            const sign = signed > 0 ? '+' : '';
            const cls  = signed > 0 ? 'w' : (signed < 0 ? 'a' : 'n');
            headHtml = `<div class="row" style="border-bottom:1px solid rgba(255,255,255,.08);padding-bottom:3px;margin-bottom:3px">
                <span class="t">eq-tape window</span>
                prints=${prints}
                signed=<span class="${cls}" style="font-weight:700">${sign}${signed}</span>
                vol=${total}
                ${venues ? '[' + venues + ']' : ''}
            </div>`;
        }

        if (ticks.length === 0) {
            box.innerHTML = headHtml + '<div class="mma-empty">no ticks</div>';
            return;
        }
        const parts = ticks.map(t => {
            const ex = (t.exchs || []).map(e =>
                `<span style="color:${colorFor(e)}">${(e||'').toUpperCase()}</span>`
            ).join(' ');
            return `<div class="row"><span class="t">t+${t.t_ms}ms</span> mm=${t.mm_count} sz=${t.total_size} [${ex}]</div>`;
        });
        box.innerHTML = headHtml + parts.join('');
    }

    // ── Helpers ───────────────────────────────────────────────────────

    function _fmtClock(ts) {
        if (!ts) return '–';
        const d = new Date(ts * 1000);
        return d.toTimeString().slice(0, 8);
    }
    function _fmtClockMs(ts) {
        if (!ts) return '–';
        const d = new Date(ts * 1000);
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        const ss = String(d.getSeconds()).padStart(2, '0');
        const ms = String(d.getMilliseconds()).padStart(3, '0');
        return `${hh}:${mm}:${ss}.${ms}`;
    }
    function _fmtSym(s) {
        // Shorten OSI-style "QQQ   260501P00654000" to "QQQ 260501P654"
        if (!s) return '';
        const m = s.match(/^(\w+)\s+(\d{6})([CP])(\d+)$/);
        if (m) {
            const strike = parseInt(m[4], 10) / 1000;
            return `${m[1]} ${m[2]}${m[3]}${strike}`;
        }
        return s;
    }

    // ── Live event feed (optional; drives ribbon refresh pulse) ──────

    function _onLiveEvents(data) {
        if (!data || !Array.isArray(data.events)) return;
        for (const ev of data.events) _liveFeed.push(ev);
        // bounded feed for memory
        while (_liveFeed.length > LIVE_FEED_CAP) _liveFeed.shift();
    }

    // ── Wall signals (continuation + fade) ──────────────────────────

    function _wallNameLabel(n) {
        if (n === 'call_wall') return 'CALL';
        if (n === 'put_wall')  return 'PUT';
        if (n === 'gamma_flip') return 'FLIP';
        return (n || '').toUpperCase();
    }

    // Render the hit-rate ledger strip. Takes the payload shape from
    // GET /api/wall_signals/ledger: { summary: {...}, recent: [...], wall_state: {...} }
    function _onLedger(payload) {
        if (!_slot || _destroyed) return;
        const host = _slot.querySelector('[data-ledger]');
        if (!host) return;
        const s = payload && payload.summary;
        if (!s) {
            host.innerHTML =
              '<span class="group"><span class="lbl">collecting</span>'
              + '<span class="val">no ledger data yet</span></span>';
            return;
        }
        const act = s.actionable || {};
        const base = s.baseline || {};
        const n  = (s.total || 0);
        // Meaningful-stats threshold: MEASURED via statistical power — hit-rate
        // within ±10% at 95% CI needs ~100 observations. Until then show raw
        // counts only so the user doesn't chase noise.
        if (n < 10) {
            host.innerHTML =
              `<span class="group"><span class="lbl">collecting</span>`
              + `<span class="val">${n}/10 entries (need 10+ to show rate)</span></span>`
              + (s.last_fire
                  ? `<span class="last-fire">last fire ${_fmtAgo(s.last_fire.fired_ts)}</span>`
                  : '');
            return;
        }
        const rateCls = (r) => r >= 0.6 ? 'rate-hot' : r >= 0.4 ? 'rate-warm' : 'rate-cold';
        const edgeCls = s.edge_gap > 0.05 ? 'pos' : s.edge_gap < -0.05 ? 'neg' : 'zero';
        const edgePrefix = s.edge_gap > 0 ? '+' : '';

        // Last fire summary (with outcome if finalized)
        let lastTxt = '';
        if (s.last_fire) {
            const lf = s.last_fire;
            const wallTxt = (lf.wall || '').replace('_',' ').toUpperCase();
            const ago = _fmtAgo(lf.fired_ts);
            const dir = lf.direction === 'up' ? '↑' : '↓';
            let outcomeTxt = '';
            if (lf.outcome === 1) {
                outcomeTxt = `<span class="ok">✓ hit</span>`;
            } else if (lf.outcome === -1) {
                outcomeTxt = `<span class="bad">✗ opposed</span>`;
            } else if (lf.outcome === 0) {
                outcomeTxt = `<span class="pend">—  noise</span>`;
            } else {
                outcomeTxt = `<span class="pend">pending</span>`;
            }
            lastTxt = `<span class="last-fire">last: ${dir} ${wallTxt} ${lf.strike} ${ago} · ${outcomeTxt}</span>`;
        }

        // ── Sign-convention edge (signed-gamma vs naive direction) ──
        // sig_hit_rate: hit rate when classifying outcome against the signed-
        // gamma-predicted direction (expected_direction from dealer_net sign).
        // dir_hit_rate: hit rate when classifying against raw cross direction.
        // Positive gap = sign convention HELPS predict NQ moves.
        // Negative gap = sign convention is BACKWARDS — flip it.
        const sigEdge = Number(s.sign_convention_edge || 0);
        const sigEdgeCls = sigEdge > 0.05 ? 'pos' : sigEdge < -0.05 ? 'neg' : 'zero';
        const sigEdgePrefix = sigEdge > 0 ? '+' : '';
        const sigRate = Number(s.sig_hit_rate || 0);
        const dirRate = Number(s.dir_hit_rate || 0);

        // ── Per-regime split ──
        const regs = s.regimes || {};
        const lg = regs.long_gamma || {};
        const sg = regs.short_gamma || {};
        const regimeTxt =
            `<span class="regime-cell" title="long_gamma regime (spot > flip): n=${lg.n||0}, hit=${lg.hit||0}, opp=${lg.opp||0}"><span class="lbl">LG</span><span class="val ${rateCls(lg.hit_rate||0)}">${(lg.hit||0)}/${(lg.hit||0)+(lg.opp||0)+(lg.miss||0)}</span></span>`
          + ` <span class="regime-cell" title="short_gamma regime (spot < flip): n=${sg.n||0}, hit=${sg.hit||0}, opp=${sg.opp||0}"><span class="lbl">SG</span><span class="val ${rateCls(sg.hit_rate||0)}">${(sg.hit||0)}/${(sg.hit||0)+(sg.opp||0)+(sg.miss||0)}</span></span>`;

        host.innerHTML =
            `<span class="group"><span class="lbl">${s.hours||24}h</span>`
            + `<span class="val">${n}</span></span>`
            + `<span class="group"><span class="lbl">actionable (C/F≥${(s.actionable_threshold||0.3).toFixed(2)})</span>`
            + `<span class="val ${rateCls(act.hit_rate||0)}">${act.hit||0}/${(act.hit||0)+(act.opp||0)+(act.miss||0)} = ${((act.hit_rate||0)*100).toFixed(0)}%</span></span>`
            + `<span class="group"><span class="lbl">baseline</span>`
            + `<span class="val ${rateCls(base.hit_rate||0)}">${base.hit||0}/${(base.hit||0)+(base.opp||0)+(base.miss||0)} = ${((base.hit_rate||0)*100).toFixed(0)}%</span></span>`
            + `<span class="group"><span class="lbl">edge</span>`
            + `<span class="edge ${edgeCls}">${edgePrefix}${(s.edge_gap*100).toFixed(1)}%</span></span>`
            + `<span class="group"><span class="lbl">regime</span>${regimeTxt}</span>`
            + `<span class="group" title="sig_hit_rate (signed-gamma expected direction) vs dir_hit_rate (raw cross direction). Positive = sign convention helps; negative = flip convention."><span class="lbl">sig-edge</span>`
            + `<span class="sig-edge ${sigEdgeCls}">${sigEdgePrefix}${(sigEdge*100).toFixed(1)}% (${(sigRate*100).toFixed(0)}→${(dirRate*100).toFixed(0)})</span></span>`
            + `<span class="group"><span class="lbl">hit-δ</span>`
            + `<span class="val">${(s.hit_delta_nq||10).toFixed(0)}pt NQ</span></span>`
            + lastTxt;

        // Also update the state-machine line per wall. `wall_state` is keyed
        // by wall name (call_wall / put_wall / gamma_flip). Enrich using the
        // most recent wall_signals state we have (proximity/distance).
        const statesHost = _slot.querySelector('[data-states]');
        if (statesHost && _lastWallState) {
            const ws = payload.wall_state || {};
            const lines = (_lastWallState.walls || []).map(w => {
                const dist = (w.distance_pct || 0) * 100;
                const prox = (_lastWallState.proximity_pct || 0.0025) * 100;
                let stateName = 'DISTANT';
                if (dist > prox * 2.5)                       stateName = 'DISTANT';
                else if (ws[w.name] && ws[w.name].armed===false) stateName = 'COOLDOWN';
                else if (dist <= prox)                        stateName = 'ARMED';
                else                                          stateName = 'QUIET';
                const label = (w.name || '').replace('_',' ');
                const wouldFire = (stateName === 'ARMED')
                    ? `would fire on cross (C=${(w.continuation_score||0).toFixed(2)} F=${(w.fade_score||0).toFixed(2)})`
                    : stateName === 'COOLDOWN'
                        ? `cooldown — spot must exceed ${(prox*2).toFixed(2)}% from strike to re-arm`
                        : stateName === 'QUIET'
                            ? `${dist.toFixed(2)}% away`
                            : `${dist.toFixed(2)}% away (skipped)`;
                return `<div class="row"><span class="state ${stateName}">${stateName}</span>`
                     + `<span>${label} ${w.strike}</span>`
                     + `<span style="color:rgba(140,150,175,.4)">·</span>`
                     + `<span>${wouldFire}</span></div>`;
            });
            statesHost.innerHTML = lines.join('');
        }
    }

    // Format "Xm ago" / "Xs ago" from a unix ts.
    function _fmtAgo(ts) {
        if (!ts) return '—';
        const age = (Date.now() / 1000) - ts;
        if (age < 60)   return `${age.toFixed(0)}s ago`;
        if (age < 3600) return `${(age / 60).toFixed(0)}m ago`;
        return `${(age / 3600).toFixed(1)}h ago`;
    }

    // ── Hedge pressure renderer ──────────────────────────────────────
    // Consumes /api/hedge_pressure/QQQ. Per-strike signed shares bars + totals chip.
    // Structural visible-domain bounds:
    //   (1) any non-zero HP at strike (self-exclude zero-OI strikes)
    //   (2) K ∈ [put_wall − 1step, call_wall + 1step] when wall state known,
    //       otherwise ±5 strikes around spot (pure display cap, no threshold).
    function _renderHedgePressure() {
        if (!_slot || _destroyed) return;
        const body    = _slot.querySelector('[data-hp-body]');
        const totals  = _slot.querySelector('[data-hp-totals]');
        const zeroBox = _slot.querySelector('[data-hp-zero]');
        const subEm   = _slot.querySelector('[data-hp-sub]');
        if (!body || !totals) return;

        const r = _lastHpResp;
        if (!r || !Array.isArray(r.strikes) || r.strikes.length === 0) {
            body.innerHTML  = '<div class="mma-empty">hedge pressure — waiting for surface…</div>';
            totals.innerHTML = '';
            zeroBox.innerHTML = '';
            if (subEm) subEm.textContent = '—';
            return;
        }

        const spot = Number(r.spot || 0);
        const rows = r.strikes.slice();

        // Structural bounds from current wall state.
        let kLo = -Infinity, kHi = Infinity;
        if (_lastWallState && Array.isArray(_lastWallState.walls)) {
            let putK = 0, callK = 0;
            for (const w of _lastWallState.walls) {
                if (w.name === 'put_wall'  && w.strike > 0) putK  = Number(w.strike);
                if (w.name === 'call_wall' && w.strike > 0) callK = Number(w.strike);
            }
            if (putK > 0 && callK > 0 && callK > putK) {
                // one strike step = smallest adjacent gap across the surface.
                let step = Infinity;
                for (let i = 1; i < rows.length; i++) {
                    const d = Math.abs(rows[i].K - rows[i-1].K);
                    if (d > 0 && d < step) step = d;
                }
                if (!isFinite(step)) step = 1.0;
                kLo = putK  - step;
                kHi = callK + step;
            }
        }

        // Step 1: filter rows to [kLo, kHi] AND any non-zero HP.
        const visible = rows.filter(row => {
            if (row.K < kLo || row.K > kHi) return false;
            return (row.hp_gamma_shares_1pct !== 0) ||
                   (row.hp_vanna_shares_1volpt !== 0) ||
                   (row.hp_charm_shares_1hr !== 0);
        });

        if (visible.length === 0) {
            body.innerHTML = '<div class="mma-empty">hedge pressure — no non-zero strikes in wall window</div>';
        } else {
            // Peak |value| per-Greek across visible set — scales each column.
            let peakG = 0, peakV = 0, peakC = 0;
            for (const row of visible) {
                if (Math.abs(row.hp_gamma_shares_1pct)   > peakG) peakG = Math.abs(row.hp_gamma_shares_1pct);
                if (Math.abs(row.hp_vanna_shares_1volpt) > peakV) peakV = Math.abs(row.hp_vanna_shares_1volpt);
                if (Math.abs(row.hp_charm_shares_1hr)    > peakC) peakC = Math.abs(row.hp_charm_shares_1hr);
            }

            const fmt = (v) => {
                if (v === 0 || v === null || v === undefined) return '0';
                const a = Math.abs(v);
                if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
                if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
                if (a >= 1e3) return (v / 1e3).toFixed(2) + 'k';
                return v.toFixed(1);
            };
            const barCell = (val, peak) => {
                if (!peak || val === 0) return `<span class="mma-hp-cell"><span class="mma-hp-val">0</span></span>`;
                const w = Math.min(100, (Math.abs(val) / peak) * 100);
                const cls = val >= 0 ? 'pos' : 'neg';
                const sign = val > 0 ? '+' : '';
                return `<span class="mma-hp-cell"><span class="mma-hp-bar ${cls}" style="width:${w.toFixed(1)}%"></span><span class="mma-hp-val">${sign}${fmt(val)}</span></span>`;
            };

            // Sort ascending by strike for natural reading.
            visible.sort((a, b) => a.K - b.K);

            // Mark the row closest to spot as ATM (structural, no threshold).
            let atmIdx = -1, atmDist = Infinity;
            if (spot > 0) {
                for (let i = 0; i < visible.length; i++) {
                    const d = Math.abs(visible[i].K - spot);
                    if (d < atmDist) { atmDist = d; atmIdx = i; }
                }
            }

            body.innerHTML = visible.map((row, i) => {
                const atm = (i === atmIdx) ? ' atm' : '';
                return `<div class="mma-hp-row${atm}">
                    <span class="mma-hp-k">${row.K.toFixed(0)}</span>
                    ${barCell(row.hp_gamma_shares_1pct,   peakG)}
                    ${barCell(row.hp_vanna_shares_1volpt, peakV)}
                    ${barCell(row.hp_charm_shares_1hr,    peakC)}
                </div>`;
            }).join('');
        }

        // Totals chip — always visible. Values are SHARES (dealer rehedge flow,
        // signed with +BUY / −SELL by wall_signals convention).
        const t = r.totals || {};
        const totPart = (label, v) => {
            const cls = v > 0 ? 'pos' : (v < 0 ? 'neg' : 'zero');
            const sign = v > 0 ? '+' : '';
            const out  = v === 0 ? '0' :
                (Math.abs(v) >= 1e9 ? (v/1e9).toFixed(2)+'B' :
                 Math.abs(v) >= 1e6 ? (v/1e6).toFixed(2)+'M' :
                 Math.abs(v) >= 1e3 ? (v/1e3).toFixed(2)+'k' : v.toFixed(1));
            return `<span class="mma-hp-tot"><span class="lbl">${label}</span><span class="v ${cls}">${sign}${out}</span></span>`;
        };
        totals.innerHTML =
            totPart('Σ Γ·sh/1%',   Number(t.hp_gamma_shares_1pct   || 0)) +
            totPart('Σ V·sh/1σpt', Number(t.hp_vanna_shares_1volpt || 0)) +
            totPart('Σ C·sh/1hr',  Number(t.hp_charm_shares_1hr    || 0));

        // OI-balance strikes (structural — first sign flip per Greek across
        // strikes; this marks where per-strike dealer OI flips, NOT the
        // aggregate gamma_flip regime boundary).
        const zg = r.oi_balance_strike_gamma, zv = r.oi_balance_strike_vanna, zc = r.oi_balance_strike_charm;
        const zparts = [];
        if (zg !== null && zg !== undefined) zparts.push(`Γ@<b>${Number(zg).toFixed(2)}</b>`);
        if (zv !== null && zv !== undefined) zparts.push(`V@<b>${Number(zv).toFixed(2)}</b>`);
        if (zc !== null && zc !== undefined) zparts.push(`C@<b>${Number(zc).toFixed(2)}</b>`);
        zeroBox.innerHTML = zparts.length
            ? `OI balance · ${zparts.join(' · ')}`
            : 'OI balance · —';

        if (subEm) {
            subEm.textContent = `${visible.length}/${rows.length} strikes · spot ${spot.toFixed(2)}`;
        }
    }

    async function _pollHedgePressure() {
        if (_destroyed) return;
        try {
            const r = await _authFetch('/api/hedge_pressure/QQQ');
            if (r.ok) {
                _lastHpResp = await r.json();
                _renderHedgePressure();
            }
        } catch (_) { /* ignore transient */ }
        try {
            const rx = await _authFetch('/api/hedge_pressure/QQQ/by_exchange');
            if (rx.ok) {
                _lastHpxResp = await rx.json();
                _renderHpByExchange();
            }
        } catch (_) { /* ignore transient */ }
        // Alignment refresh piggy-backs the same 5s HP cadence.
        await _pollAlignment();
    }

    // Phase C — render the WITH/AGAINST alignment pill for the current contract.
    function _renderAlignment() {
        if (!_slot || _destroyed) return;
        const pill = _slot.querySelector('[data-align]');
        const tip  = _slot.querySelector('[data-align-tip]');
        if (!pill) return;
        const r = _lastAlignResp;
        if (!r || r.error) {
            pill.className = 'mma-align align-neutral';
            pill.innerHTML = '· waiting';
            if (tip) tip.innerHTML = '';
            return;
        }

        const rec  = (r.recommended_for_this_contract || 'neutral');
        const side = (r.option_side || '').toUpperCase();
        const dir  = r.expected_underlying_direction; // 'up'|'down'|null
        const reason = r.neutral_reason;

        let cls = 'align-neutral';
        let label = '· NEUTRAL';
        let reasonTxt = reason ? ` · ${reason}` : '';
        if (rec === 'long' || rec === 'short') {
            cls = 'align-with';
            const arrow = dir === 'up' ? '↑' : (dir === 'down' ? '↓' : '·');
            const side_word = rec === 'long' ? 'LONG' : 'SHORT';
            label = `${arrow} ${side_word} ${side} = WITH MM`;
        }
        pill.className = `mma-align ${cls}`;
        // Keep the tooltip child inside (hover UX).
        pill.innerHTML = `${label}<span class="reason">${reasonTxt}</span>
            <div class="mma-align-tip" data-align-tip></div>`;

        // Re-grab the tip (we just replaced it) and render the truth table.
        const t = pill.querySelector('[data-align-tip]');
        if (t) {
            const cell = (v) => `<span class="${v === 'with' ? 'w' : (v === 'against' ? 'a' : 'n')}">${v}</span>`;
            const crossTxt = r.last_cross
                ? `${r.last_cross.wall || '—'} · ${r.last_cross.direction || '—'} · ${(r.last_cross.age_sec || 0).toFixed(1)}s ago`
                : 'none';
            const dnTxt = (r.dn_gamma_normalized || 0).toFixed(2);
            const strikeTxt = (r.strike || 0).toFixed(2);
            t.innerHTML = `
              <div class="trow"><b>contract</b><span>${side || '—'} @ ${strikeTxt}</span></div>
              <div class="trow"><b>regime</b><span>${r.regime || '—'}</span></div>
              <div class="trow"><b>last cross</b><span>${crossTxt}</span></div>
              <div class="trow"><b>expected</b><span>${dir ? dir.toUpperCase() : '—'}</span></div>
              <div class="trow"><b>dn_γ (norm)</b><span>${dnTxt}</span></div>
              <div class="trow"><b>long call</b>${cell(r.alignment_long_call)}</div>
              <div class="trow"><b>short call</b>${cell(r.alignment_short_call)}</div>
              <div class="trow"><b>long put</b>${cell(r.alignment_long_put)}</div>
              <div class="trow"><b>short put</b>${cell(r.alignment_short_put)}</div>`;
        }
    }

    async function _pollAlignment() {
        if (_destroyed) return;
        const sym = _selectedSym
            || (_lastContractsResp && _lastContractsResp.contracts && _lastContractsResp.contracts[0]
                ? _lastContractsResp.contracts[0].sym : null);
        if (!sym) {
            _lastAlignResp = null;
            _renderAlignment();
            return;
        }
        try {
            const r = await _authFetch(`/api/hedge_pressure/QQQ/alignment/${encodeURIComponent(sym)}`);
            if (!r.ok) return;
            _lastAlignResp = await r.json();
            _renderAlignment();
        } catch (_) { /* ignore transient */ }
    }

    // Per-exchange HP_γ rollup table (Phase B). Each row signed & sign-colored.
    function _renderHpByExchange() {
        if (!_slot || _destroyed) return;
        const box = _slot.querySelector('[data-hpx-body]');
        if (!box) return;
        const r = _lastHpxResp;
        if (!r || !Array.isArray(r.exchanges) || r.exchanges.length === 0) {
            box.innerHTML = '<div class="mma-empty">venue rollup — no venues yet</div>';
            return;
        }
        const fmt = (v) => {
            if (v === 0 || !isFinite(v)) return '0';
            const a = Math.abs(v);
            if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
            if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
            if (a >= 1e3) return (v / 1e3).toFixed(2) + 'k';
            return v.toFixed(1);
        };
        const cls = (v) => v > 0 ? 'pos' : (v < 0 ? 'neg' : 'zero');
        const sign = (v) => v > 0 ? '+' : '';
        box.innerHTML = r.exchanges.map(e => {
            const vp = Number(e.hp_gamma_posted || 0);
            const vc = Number(e.hp_gamma_caught || 0);
            const vd = Number(e.diff || 0);
            return `<div class="mma-hpx-row">
                <span class="mma-hpx-exch" style="color:${colorFor(e.exch)}">${e.exch}</span>
                <span class="mma-hpx-val ${cls(vp)}">${sign(vp)}${fmt(vp)}</span>
                <span class="mma-hpx-val ${cls(vc)}">${sign(vc)}${fmt(vc)}</span>
                <span class="mma-hpx-val ${cls(vd)}">${sign(vd)}${fmt(vd)}</span>
            </div>`;
        }).join('');
    }

    function _onWallSignals(state) {
        if (!_slot || _destroyed) return;
        _lastWallState = state;   // cached so state-machine line can enrich
        const host = _slot.querySelector('[data-walls]');
        if (!host) return;
        if (!state || !Array.isArray(state.walls) || state.walls.length === 0) {
            host.innerHTML = '<span class="empty">wall signals — no walls from /api/data yet…</span>';
            return;
        }

        const spot = Number(state.spot || 0);
        const spotTxt = spot > 0 ? spot.toFixed(2) : '—';

        // Sort: continuation first (by score), then near-wall fades, then rest.
        const walls = state.walls.slice().sort((a, b) => {
            const sA = Math.max(a.continuation_score || 0, a.fade_score || 0);
            const sB = Math.max(b.continuation_score || 0, b.fade_score || 0);
            return sB - sA;
        });

        const parts = walls.map(w => {
            const nm  = _wallNameLabel(w.name);
            const strike = Number(w.strike || 0).toFixed(2);
            const cont = Number(w.continuation_score || 0);
            const fade = Number(w.fade_score || 0);
            const dist = Number(w.distance_pct || 0) * 100;
            const crossTag = w.just_crossed
                ? `<span class="tag crossed">crossed ${w.cross_direction||''} ${w.cross_age_sec ? w.cross_age_sec.toFixed(0)+'s':'now'}</span>`
                : (dist * 100 <= 100 && dist <= (state.proximity_pct || 0.0025) * 10000
                    ? '<span class="tag near">near</span>' : '');
            const venues = (w.venues_pulled_list || []).slice(0, 4).join(',');
            const venueTxt = w.venues_pulled > 0
                ? `<span class="venues">pull ${w.venues_pulled}${venues ? ' ['+venues+']' : ''}</span>` : '';
            const contCls = 'sc cont' + (cont >= 0.4 ? ' hot' : '');
            const fadeCls = 'sc fade' + (fade >= 0.5 ? ' hot' : '');
            const prorataTxt = w.prints_at_strike > 0
                ? `<span class="dist">prints ${w.prorata_prints}/${w.prints_at_strike} prorata</span>`
                : '';
            // ── Signed-gamma DN (dealer net) pill ──
            // Derived from Schwab LEVELONE_OPTIONS gamma × OI with the dealer-sign
            // convention (calls short, puts long). Shows -1..+1 normalized signed
            // gamma AT this wall's strike. Negative = dealers SHORT gamma here
            // (hedge WITH cross); Positive = LONG gamma (hedge AGAINST cross).
            const dn = Number(w.dealer_net_normalized || 0);
            let dnTxt = '';
            if (Math.abs(dn) >= 0.02 || w.dealer_net_at_strike) {
                const dnCls = dn < 0 ? 'dn-short' : (dn > 0 ? 'dn-long' : 'dn-flat');
                const dnSign = dn > 0 ? '+' : '';
                dnTxt = `<span class="dn ${dnCls}" title="dealer signed gamma at ${strike}: ${dn.toFixed(2)} normalized · ${dn<0?'SHORT-gamma (hedge WITH move)':'LONG-gamma (hedge AGAINST move)'}">DN ${dnSign}${dn.toFixed(2)}</span>`;
            }
            // ── Expected direction pill ──
            // Only shows when there's a cross event active (otherwise no direction
            // has been observed to predict from).
            let edTxt = '';
            if (w.expected_direction) {
                const edArrow = w.expected_direction === 'up' ? '↑' : '↓';
                edTxt = `<span class="ed" title="Signed-gamma predicts NQ ${w.expected_direction}">pred ${edArrow}</span>`;
            }
            return `
              <div class="mma-wall-chip" title="distance=${dist.toFixed(3)}% · prints=${w.prints_at_strike} · pro-rata=${w.prorata_prints} · pulls=${w.venues_pulled} · verdict=${w.verdict||'—'} · dn=${(w.dealer_net_at_strike||0).toExponential(2)}">
                <span class="name">${nm}</span>
                <span class="strike">${strike}</span>
                <span class="dist">${dist.toFixed(2)}%</span>
                ${crossTag}
                <span class="scores">
                  <span class="${contCls}">C ${cont.toFixed(2)}</span>
                  <span class="${fadeCls}">F ${fade.toFixed(2)}</span>
                </span>
                ${dnTxt}
                ${edTxt}
                ${prorataTxt}
                ${venueTxt}
              </div>`;
        });

        // ── Regime chip ──
        // Derived from spot vs gamma_flip (Schwab dealer signed-gamma zero-crossing).
        //   long_gamma  → dealers stabilizing; mean-reverting regime
        //   short_gamma → dealers destabilizing; trending regime
        const regime = state.regime || 'unknown';
        const regimeCls = regime === 'long_gamma' ? 'regime-long'
                        : regime === 'short_gamma' ? 'regime-short' : 'regime-unknown';
        const regimeLabel = regime === 'long_gamma' ? 'LONG γ (mean-revert)'
                          : regime === 'short_gamma' ? 'SHORT γ (trend)' : 'γ regime —';
        const flipTxt = state.gamma_flip ? ` · flip ${Number(state.gamma_flip).toFixed(2)}` : '';

        host.innerHTML =
            `<div class="mma-wall-chip" title="spot / ticker"><span class="name">${state.ticker||'QQQ'}</span><span class="strike">${spotTxt}</span></div>`
            + `<div class="mma-wall-chip ${regimeCls}" title="dealer gamma regime: ${regime}${flipTxt}"><span class="name">γ</span><span class="strike">${regimeLabel}</span></div>`
            + parts.join('');
    }

    // ── Lifecycle ─────────────────────────────────────────────────────

    function init(slotEl) {
        _slot = slotEl;
        _destroyed = false;
        _injectStyles();
        _buildShell();
        _wireControls();

        if (window.AltarisEvents) {
            _liveFeedHandler = (d) => _onLiveEvents(d);
            window.AltarisEvents.on('data:mm:event', _liveFeedHandler);
            // Primary per-contract data source — server pushes via socket at
            // ~4Hz for the watched symbol (see flush_contract_states_to_socket).
            _stateHandler = (d) => _onStatePush(d);
            window.AltarisEvents.on('data:mm:state', _stateHandler);
            // Wall signals — push at ~1Hz from connectors/wall_signals.py.
            _wallHandler = (d) => _onWallSignals(d);
            window.AltarisEvents.on('data:wall:signals', _wallHandler);
        }

        // Fetch initial wall-signal snapshot (socket push is 1Hz; avoid a
        // 1-second "empty" flash on pane mount).
        _authFetch('/api/wall_signals/state?ticker=QQQ')
            .then(r => r.ok ? r.json() : null)
            .then(s => { if (s) _onWallSignals(s); })
            .catch(() => {});

        // Fetch ledger hit-rate on mount + poll. Outcome tracker runs
        // server-side every 30s; matching cadence keeps the UI fresh
        // without hammering REST.
        const pollLedger = () => {
            if (_destroyed) return;
            _authFetch('/api/wall_signals/ledger?hours=24&ticker=QQQ')
                .then(r => r.ok ? r.json() : null)
                .then(p => { if (p) _onLedger(p); })
                .catch(() => {});
        };
        pollLedger();
        _ledgerTimer = setInterval(pollLedger, LEDGER_POLL_MS);

        // Hedge-pressure poll — 5s cadence matches zone emit. First call
        // seeds the panel instantly instead of waiting HP_POLL_MS on mount.
        _pollHedgePressure();
        _hpTimer = setInterval(_pollHedgePressure, HP_POLL_MS);

        _refreshContracts();
        // Poll for the ranking list every 5s — this is the ONLY REST loop now.
        // Per-contract state arrives via socket push.
        _rankPollTimer = setInterval(() => {
            if (_destroyed) return;
            _refreshContracts();
        }, 5000);
    }

    function destroy() {
        _destroyed = true;
        if (_rankPollTimer) clearInterval(_rankPollTimer);
        _rankPollTimer = null;
        if (_ledgerTimer) clearInterval(_ledgerTimer);
        _ledgerTimer = null;
        if (_hpTimer) clearInterval(_hpTimer);
        _hpTimer = null;
        if (window.AltarisEvents) {
            if (_liveFeedHandler) {
                window.AltarisEvents.off('data:mm:event', _liveFeedHandler);
                _liveFeedHandler = null;
            }
            if (_stateHandler) {
                window.AltarisEvents.off('data:mm:state', _stateHandler);
                _stateHandler = null;
            }
            if (_wallHandler) {
                window.AltarisEvents.off('data:wall:signals', _wallHandler);
                _wallHandler = null;
            }
        }
        // Release our server-side watch so the flush loop stops pushing.
        if (_watchedSym && window._sio && window._sio.connected) {
            try { window._sio.emit('mm_attribution:unwatch', {}); } catch (_) {}
        }
        _watchedSym = null;
        if (_slot) _slot.innerHTML = '';
        _slot = null;
    }

    return { init, destroy };
})();
