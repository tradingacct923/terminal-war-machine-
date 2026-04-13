/**
 * CrossDivergencePane — QQQ vs SPY institutional depth comparison
 * Shows QA-imbalance bars side by side with CONFIRMED/DIVERGENT/NEUTRAL badge.
 * Consumes book_microstructure events (emitted for both QQQ and SPY).
 */
const CrossDivergencePane = (() => {
    'use strict';

    const THRESHOLD = 0.05; // |qa_imb| above this = directional

    let _container = null;
    let _styleEl = null;
    let _qqq = { qa: 0, bidQ: 0, askQ: 0, hft: false, ts: 0 };
    let _spy = { qa: 0, bidQ: 0, askQ: 0, hft: false, ts: 0 };

    function _injectStyles() {
        if (document.getElementById('xdiv-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'xdiv-styles';
        _styleEl.textContent = `
            .xdiv-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; padding:8px; gap:6px; }
            .xdiv-title { font-size:9px; font-weight:600; color:rgba(180,190,220,.7); letter-spacing:.8px; text-transform:uppercase; text-align:center; }
            .xdiv-badge { text-align:center; padding:6px 0; }
            .xdiv-badge-label { font-size:14px; font-weight:700; letter-spacing:1.5px; padding:4px 14px; border-radius:4px; display:inline-block; }
            .xdiv-confirmed { background:rgba(31,209,122,.12); color:#1fd17a; border:1px solid rgba(31,209,122,.3); }
            .xdiv-divergent { background:rgba(224,48,96,.12); color:#e03060; border:1px solid rgba(224,48,96,.3); animation:xdiv-pulse 1.5s infinite; }
            .xdiv-neutral { background:rgba(140,150,180,.06); color:rgba(140,150,180,.5); border:1px solid rgba(140,150,180,.1); }
            @keyframes xdiv-pulse { 0%,100% { border-color:rgba(224,48,96,.3); } 50% { border-color:rgba(224,48,96,.7); } }

            .xdiv-pair { display:flex; gap:12px; flex:1; }
            .xdiv-col { flex:1; display:flex; flex-direction:column; gap:4px; }
            .xdiv-sym-label { font-size:10px; font-weight:600; color:rgba(200,210,230,.8); text-align:center; letter-spacing:.5px; }

            .xdiv-qa-wrap { position:relative; height:28px; background:rgba(255,255,255,.03); border-radius:3px; overflow:hidden; }
            .xdiv-qa-bar { position:absolute; top:0; height:100%; transition:width .3s ease, background .3s ease; border-radius:3px; }
            .xdiv-qa-bid { left:50%; background:rgba(31,209,122,.4); }
            .xdiv-qa-ask { right:50%; background:rgba(224,48,96,.4); }
            .xdiv-qa-mid { position:absolute; left:50%; top:0; width:1px; height:100%; background:rgba(255,255,255,.15); z-index:1; }
            .xdiv-qa-val { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:10px; font-weight:700; z-index:2; }

            .xdiv-stats { display:flex; justify-content:space-between; padding:0 2px; }
            .xdiv-stat { font-size:8px; color:rgba(140,150,180,.5); }
            .xdiv-stat-val { font-weight:600; color:rgba(180,190,220,.7); }

            .xdiv-hft-warn { text-align:center; font-size:8px; color:#ffb428; padding:2px; opacity:.8; }
            .xdiv-dir-arrow { font-size:16px; font-weight:700; text-align:center; padding:2px 0; }

            .xdiv-venue-section { border-top:1px solid rgba(255,255,255,.04); padding:6px 0 0; margin-top:4px; }
            .xdiv-venue-title { font-size:8px; font-weight:600; color:rgba(180,190,220,.5); letter-spacing:.5px; text-transform:uppercase; text-align:center; margin-bottom:4px; }
            .xdiv-venue-row { display:flex; gap:6px; }
            .xdiv-venue-col { flex:1; }
            .xdiv-venue-label { font-size:7px; color:rgba(140,150,180,.4); text-transform:uppercase; letter-spacing:.3px; margin-bottom:2px; text-align:center; }
            .xdiv-qa-imb-bar { position:relative; height:14px; background:rgba(255,255,255,.03); border-radius:2px; overflow:hidden; margin-bottom:2px; }
            .xdiv-qa-imb-fill { position:absolute; top:1px; bottom:1px; border-radius:2px; transition:all .3s ease; }
            .xdiv-qa-imb-mid { position:absolute; left:50%; top:0; width:1px; height:100%; background:rgba(255,255,255,.12); }
            .xdiv-qa-imb-val { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:8px; font-weight:600; z-index:1; }
            .xdiv-venue-chips { font-size:7px; line-height:1.6; text-align:center; min-height:14px; }
            .xdiv-v-slow { display:inline-block; padding:0 3px; border-radius:2px; background:rgba(31,209,122,.12); color:#1fd17a; border:1px solid rgba(31,209,122,.2); margin:1px; font-weight:600; }
            .xdiv-v-fast { display:inline-block; padding:0 3px; border-radius:2px; background:rgba(255,64,48,.08); color:#ff6050; border:1px solid rgba(255,64,48,.15); margin:1px; font-weight:600; }
            .xdiv-dq-row { display:flex; justify-content:space-between; font-size:7px; color:rgba(140,150,180,.4); padding:0 2px; }
            .xdiv-dq-val { font-weight:600; color:rgba(180,190,220,.7); }
        `;
        document.head.appendChild(_styleEl);
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _container.innerHTML = `
            <div class="xdiv-wrap">
                <div class="xdiv-title">Cross-Market Divergence</div>
                <div class="xdiv-badge"><span class="xdiv-badge-label xdiv-neutral" id="xdiv-badge">WAITING</span></div>
                <div class="xdiv-pair">
                    <div class="xdiv-col">
                        <div class="xdiv-sym-label">QQQ <span style="font-size:7px;opacity:.5">NASDAQ</span></div>
                        <div class="xdiv-qa-wrap">
                            <div class="xdiv-qa-mid"></div>
                            <div class="xdiv-qa-bar xdiv-qa-bid" id="xdiv-qqq-bid" style="width:0"></div>
                            <div class="xdiv-qa-bar xdiv-qa-ask" id="xdiv-qqq-ask" style="width:0"></div>
                            <div class="xdiv-qa-val" id="xdiv-qqq-val">0.000</div>
                        </div>
                        <div class="xdiv-dir-arrow" id="xdiv-qqq-dir" style="color:rgba(140,150,180,.3)">—</div>
                        <div class="xdiv-stats">
                            <span class="xdiv-stat">Bid Q: <span class="xdiv-stat-val" id="xdiv-qqq-bq">—</span></span>
                            <span class="xdiv-stat">Ask Q: <span class="xdiv-stat-val" id="xdiv-qqq-aq">—</span></span>
                        </div>
                        <div class="xdiv-hft-warn" id="xdiv-qqq-hft" style="display:none">HFT-ONLY BBO</div>
                    </div>
                    <div class="xdiv-col">
                        <div class="xdiv-sym-label">SPY <span style="font-size:7px;opacity:.5">NYSE</span></div>
                        <div class="xdiv-qa-wrap">
                            <div class="xdiv-qa-mid"></div>
                            <div class="xdiv-qa-bar xdiv-qa-bid" id="xdiv-spy-bid" style="width:0"></div>
                            <div class="xdiv-qa-bar xdiv-qa-ask" id="xdiv-spy-ask" style="width:0"></div>
                            <div class="xdiv-qa-val" id="xdiv-spy-val">0.000</div>
                        </div>
                        <div class="xdiv-dir-arrow" id="xdiv-spy-dir" style="color:rgba(140,150,180,.3)">—</div>
                        <div class="xdiv-stats">
                            <span class="xdiv-stat">Bid Q: <span class="xdiv-stat-val" id="xdiv-spy-bq">—</span></span>
                            <span class="xdiv-stat">Ask Q: <span class="xdiv-stat-val" id="xdiv-spy-aq">—</span></span>
                        </div>
                        <div class="xdiv-hft-warn" id="xdiv-spy-hft" style="display:none">HFT-ONLY BBO</div>
                    </div>
                </div>
                <div class="xdiv-venue-section">
                    <div class="xdiv-venue-title">BBO Venue Quality</div>
                    <div class="xdiv-venue-row">
                        <div class="xdiv-venue-col">
                            <div class="xdiv-venue-label">QQQ BID</div>
                            <div class="xdiv-dq-row"><span>Price: <span class="xdiv-dq-val" id="xdiv-qqq-bp">—</span></span><span>Sz: <span class="xdiv-dq-val" id="xdiv-qqq-bs">—</span></span></div>
                            <div class="xdiv-venue-chips" id="xdiv-qqq-bv"></div>
                        </div>
                        <div class="xdiv-venue-col">
                            <div class="xdiv-venue-label">QQQ ASK</div>
                            <div class="xdiv-dq-row"><span>Price: <span class="xdiv-dq-val" id="xdiv-qqq-ap">—</span></span><span>Sz: <span class="xdiv-dq-val" id="xdiv-qqq-as">—</span></span></div>
                            <div class="xdiv-venue-chips" id="xdiv-qqq-av"></div>
                        </div>
                    </div>
                </div>
            </div>`;
    }

    function destroy() {
        _container = null;
        _qqq = { qa: 0, bidQ: 0, askQ: 0, hft: false, ts: 0 };
        _spy = { qa: 0, bidQ: 0, askQ: 0, hft: false, ts: 0 };
    }

    function _updateBar(prefix, state) {
        if (!_container) return;
        const qa = state.qa;
        const pct = Math.min(Math.abs(qa) * 100, 50); // max 50% each side

        const bidBar = _container.querySelector(`#xdiv-${prefix}-bid`);
        const askBar = _container.querySelector(`#xdiv-${prefix}-ask`);
        const valEl = _container.querySelector(`#xdiv-${prefix}-val`);
        const dirEl = _container.querySelector(`#xdiv-${prefix}-dir`);
        const bqEl = _container.querySelector(`#xdiv-${prefix}-bq`);
        const aqEl = _container.querySelector(`#xdiv-${prefix}-aq`);
        const hftEl = _container.querySelector(`#xdiv-${prefix}-hft`);

        if (bidBar && askBar) {
            if (qa > 0) {
                bidBar.style.width = pct + '%';
                askBar.style.width = '0';
            } else {
                bidBar.style.width = '0';
                askBar.style.width = pct + '%';
            }
        }
        if (valEl) {
            valEl.textContent = qa.toFixed(3);
            valEl.style.color = qa > THRESHOLD ? '#1fd17a' : qa < -THRESHOLD ? '#e03060' : 'rgba(180,190,220,.7)';
        }
        if (dirEl) {
            if (qa > THRESHOLD) { dirEl.textContent = 'BID'; dirEl.style.color = '#1fd17a'; }
            else if (qa < -THRESHOLD) { dirEl.textContent = 'ASK'; dirEl.style.color = '#e03060'; }
            else { dirEl.textContent = '—'; dirEl.style.color = 'rgba(140,150,180,.3)'; }
        }
        if (bqEl) bqEl.textContent = (state.bidQ || 0).toFixed(2);
        if (aqEl) aqEl.textContent = (state.askQ || 0).toFixed(2);
        if (hftEl) hftEl.style.display = state.hft ? 'block' : 'none';
    }

    function _updateBadge() {
        if (!_container) return;
        const badge = _container.querySelector('#xdiv-badge');
        if (!badge) return;

        const qDir = _qqq.qa > THRESHOLD ? 1 : _qqq.qa < -THRESHOLD ? -1 : 0;
        const sDir = _spy.qa > THRESHOLD ? 1 : _spy.qa < -THRESHOLD ? -1 : 0;

        badge.className = 'xdiv-badge-label';
        if (qDir !== 0 && sDir !== 0 && qDir === sDir) {
            badge.textContent = 'CONFIRMED';
            badge.classList.add('xdiv-confirmed');
        } else if (qDir !== 0 && sDir !== 0 && qDir !== sDir) {
            badge.textContent = 'DIVERGENCE';
            badge.classList.add('xdiv-divergent');
        } else {
            badge.textContent = 'NEUTRAL';
            badge.classList.add('xdiv-neutral');
        }
    }

    const SLOW_VENUES = new Set(['NSDQ','arcx','phlx','cinn','cboe','bos','NYSE']);
    const FAST_VENUES = new Set(['edgx','batx','memx','edga','baty','iexg','drctedge']);

    function _venueChip(id) {
        const upper = (id||'').toUpperCase();
        const lower = (id||'').toLowerCase();
        const isSlow = SLOW_VENUES.has(id) || SLOW_VENUES.has(upper);
        const isFast = FAST_VENUES.has(id) || FAST_VENUES.has(lower);
        const cls = isSlow ? 'xdiv-v-slow' : isFast ? 'xdiv-v-fast' : '';
        return `<span class="${cls}">${id}</span>`;
    }

    function _updateVenueQuality(data) {
        if (!_container) return;
        const sym = (data.symbol || 'QQQ').toUpperCase();
        if (sym !== 'QQQ') return; // only show QQQ venue detail
        const bbo_bid = data.bbo_bid || {};
        const bbo_ask = data.bbo_ask || {};
        const _set = (id, v) => { const el = _container.querySelector('#' + id); if (el) el.textContent = v; };
        const _html = (id, v) => { const el = _container.querySelector('#' + id); if (el) el.innerHTML = v; };

        _set('xdiv-qqq-bp', bbo_bid.price ? bbo_bid.price.toFixed(2) : '—');
        _set('xdiv-qqq-bs', bbo_bid.size ? bbo_bid.size.toLocaleString() : '—');
        _html('xdiv-qqq-bv', (bbo_bid.venues||[]).map(_venueChip).join(' '));

        _set('xdiv-qqq-ap', bbo_ask.price ? bbo_ask.price.toFixed(2) : '—');
        _set('xdiv-qqq-as', bbo_ask.size ? bbo_ask.size.toLocaleString() : '—');
        _html('xdiv-qqq-av', (bbo_ask.venues||[]).map(_venueChip).join(' '));
    }

    function onBookMs(data) {
        if (!_container) return;
        const sym = data.symbol || 'QQQ';
        const state = {
            qa: data.qa_imbalance || 0,
            bidQ: data.bid_quality || 0,
            askQ: data.ask_quality || 0,
            hft: data.bbo_hft_only || false,
            ts: Date.now(),
        };

        if (sym === 'SPY') {
            _spy = state;
            _updateBar('spy', _spy);
        } else {
            _qqq = state;
            _updateBar('qqq', _qqq);
        }
        _updateBadge();
        _updateVenueQuality(data);
    }

    return { init, destroy, onBookMs };
})();
