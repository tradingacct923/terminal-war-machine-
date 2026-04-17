/**
 * DealerFlowPane — Dealer session hedge flow visualization
 * Shows buy/sell session net, gamma regime, hedge wave count.
 * Consumes dealer_session_flow events (5s interval from zone cycle).
 */
const DealerFlowPane = (() => {
    'use strict';

    let _container = null;
    let _styleEl = null;

    function _injectStyles() {
        if (document.getElementById('dealer-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'dealer-styles';
        _styleEl.textContent = `
            .df-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; padding:8px; gap:6px; }
            .df-title { font-size:9px; font-weight:600; color:rgba(180,190,220,.7); letter-spacing:.8px; text-transform:uppercase; text-align:center; }

            .df-regime { text-align:center; padding:4px 0; }
            .df-regime-badge { font-size:13px; font-weight:700; padding:4px 14px; border-radius:4px; display:inline-block; letter-spacing:1px; }
            .df-regime-long { background:rgba(31,209,122,.1); color:#1fd17a; border:1px solid rgba(31,209,122,.25); }
            .df-regime-short { background:rgba(224,48,96,.1); color:#e03060; border:1px solid rgba(224,48,96,.25); }
            .df-regime-trans { background:rgba(255,180,40,.1); color:#ffb428; border:1px solid rgba(255,180,40,.25); }
            .df-regime-unknown { background:rgba(140,150,180,.06); color:rgba(140,150,180,.5); border:1px solid rgba(140,150,180,.1); }

            .df-bias-section { flex:1; display:flex; flex-direction:column; justify-content:center; gap:6px; }
            .df-bias-labels { display:flex; justify-content:space-between; font-size:8px; color:rgba(140,150,180,.5); }
            .df-bias-bar-wrap { height:32px; background:rgba(255,255,255,.03); border-radius:4px; overflow:hidden; display:flex; position:relative; }
            .df-bias-buy { background:rgba(31,209,122,.35); transition:width .5s ease; height:100%; display:flex; align-items:center; justify-content:center; }
            .df-bias-sell { background:rgba(224,48,96,.35); transition:width .5s ease; height:100%; display:flex; align-items:center; justify-content:center; }
            .df-bias-label { font-size:8px; font-weight:600; color:rgba(255,255,255,.7); white-space:nowrap; }

            .df-net { text-align:center; padding:4px 0; }
            .df-net-label { font-size:8px; color:rgba(140,150,180,.5); text-transform:uppercase; letter-spacing:.5px; }
            .df-net-val { font-size:22px; font-weight:700; transition:color .3s ease; }
            .df-net-long { color:#1fd17a; }
            .df-net-short { color:#e03060; }
            .df-net-flat { color:rgba(140,150,180,.5); }
            .df-net-unit { font-size:9px; color:rgba(140,150,180,.5); margin-left:3px; }

            .df-stats { display:flex; justify-content:space-around; padding:4px 0; border-top:1px solid rgba(255,255,255,.04); }
            .df-stat { text-align:center; }
            .df-stat-label { font-size:7px; color:rgba(140,150,180,.4); text-transform:uppercase; letter-spacing:.5px; }
            .df-stat-val { font-size:11px; font-weight:600; color:rgba(200,210,230,.8); }
            .df-stat-buy { color:#1fd17a; }
            .df-stat-sell { color:#e03060; }

            .df-gex-row { display:flex; justify-content:center; gap:14px; font-size:8px; color:rgba(140,150,180,.5); padding:2px 0; }
            .df-gex-val { font-weight:600; color:rgba(200,210,230,.7); }
        `;
        document.head.appendChild(_styleEl);
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _container.innerHTML = `
            <div class="df-wrap">
                <div class="df-title">Dealer Hedge Flow</div>
                <div class="df-regime"><span class="df-regime-badge df-regime-unknown" id="df-regime">WAITING</span></div>

                <div class="df-bias-section">
                    <div class="df-bias-labels">
                        <span>BUYS</span><span>SELLS</span>
                    </div>
                    <div class="df-bias-bar-wrap">
                        <div class="df-bias-buy" id="df-buy-bar" style="width:50%">
                            <span class="df-bias-label" id="df-buy-pct">—</span>
                        </div>
                        <div class="df-bias-sell" id="df-sell-bar" style="width:50%">
                            <span class="df-bias-label" id="df-sell-pct">—</span>
                        </div>
                    </div>
                </div>

                <div class="df-net">
                    <div class="df-net-label">Net Dealer Position</div>
                    <div><span class="df-net-val df-net-flat" id="df-net">0.0</span><span class="df-net-unit">NQ</span></div>
                </div>

                <div class="df-stats">
                    <div class="df-stat">
                        <div class="df-stat-label">Session Buys</div>
                        <div class="df-stat-val df-stat-buy" id="df-buys">0.0</div>
                    </div>
                    <div class="df-stat">
                        <div class="df-stat-label">Session Sells</div>
                        <div class="df-stat-val df-stat-sell" id="df-sells">0.0</div>
                    </div>
                    <div class="df-stat">
                        <div class="df-stat-label">Hedge Waves</div>
                        <div class="df-stat-val" id="df-waves">0</div>
                    </div>
                </div>

                <div class="df-gex-row">
                    <span>GEX: <span class="df-gex-val" id="df-gex">—</span></span>
                    <span>DEX: <span class="df-gex-val" id="df-dex">—</span></span>
                    <span>Flip: <span class="df-gex-val" id="df-flip">—</span></span>
                </div>
            </div>`;
    }

    function destroy() {
        _container = null;
    }

    function onDealerFlow(data) {
        if (!_container) return;

        const buys = data.session_buys || 0;
        const sells = data.session_sells || 0;
        const net = data.net_position || 0;
        const total = buys + sells;

        // Regime badge
        const regimeEl = _container.querySelector('#df-regime');
        if (regimeEl) {
            const regime = data.gamma_regime || 'UNKNOWN';
            const isLong = regime.includes('LONG') || regime.includes('long');
            const isShort = regime.includes('SHORT') || regime.includes('short');
            regimeEl.textContent = regime.replace(/_/g, ' ');
            regimeEl.className = 'df-regime-badge ' + (
                isLong ? 'df-regime-long' : isShort ? 'df-regime-short' :
                regime.includes('TRANSITION') ? 'df-regime-trans' : 'df-regime-unknown'
            );
        }

        // Bias bar
        const buyPct = total > 0 ? (buys / total * 100) : 50;
        const sellPct = total > 0 ? (sells / total * 100) : 50;
        const buyBar = _container.querySelector('#df-buy-bar');
        const sellBar = _container.querySelector('#df-sell-bar');
        const buyPctEl = _container.querySelector('#df-buy-pct');
        const sellPctEl = _container.querySelector('#df-sell-pct');
        if (buyBar) buyBar.style.width = buyPct + '%';
        if (sellBar) sellBar.style.width = sellPct + '%';
        if (buyPctEl) buyPctEl.textContent = buyPct.toFixed(0) + '%';
        if (sellPctEl) sellPctEl.textContent = sellPct.toFixed(0) + '%';

        // Net position
        const netEl = _container.querySelector('#df-net');
        if (netEl) {
            const sign = net > 0 ? '+' : '';
            netEl.textContent = sign + net.toFixed(1);
            netEl.className = 'df-net-val ' + (net > 0.5 ? 'df-net-long' : net < -0.5 ? 'df-net-short' : 'df-net-flat');
        }

        // Stats
        const buysEl = _container.querySelector('#df-buys');
        const sellsEl = _container.querySelector('#df-sells');
        const wavesEl = _container.querySelector('#df-waves');
        if (buysEl) buysEl.textContent = buys.toFixed(1);
        if (sellsEl) sellsEl.textContent = sells.toFixed(1);
        if (wavesEl) wavesEl.textContent = data.hedge_wave_count || 0;

        // GEX/DEX/Flip
        const gexEl = _container.querySelector('#df-gex');
        const dexEl = _container.querySelector('#df-dex');
        const flipEl = _container.querySelector('#df-flip');
        if (gexEl) {
            const gex = data.net_gex_m || 0;
            gexEl.textContent = (gex >= 0 ? '+' : '') + gex.toFixed(0) + 'M';
            gexEl.style.color = gex > 0 ? '#1fd17a' : '#e03060';
        }
        if (dexEl) {
            const dex = data.net_dex_m || 0;
            dexEl.textContent = '$' + (dex >= 0 ? '+' : '') + dex.toFixed(1) + 'M';
        }
        if (flipEl) {
            const flip = data.flip_strike || 0;
            flipEl.textContent = flip > 0 ? flip.toFixed(0) : '—';
        }
    }

    return { init, destroy, onDealerFlow };
})();
window.DealerFlowPane = DealerFlowPane;
