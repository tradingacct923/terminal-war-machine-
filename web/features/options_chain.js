(function() {
    'use strict';

    // ═══ PRIVATE STATE ═══
    let _series = null;
    let _selectedStrike = null;
    let _gexLines = [];
    let _refreshTimer = null;

    // ═══ PUBLIC API ═══
    window.OptionsChain = {
        attachToSeries(series) {
            _series = series;
        },

        init() {
            this.populateChain();
            _refreshTimer = setInterval(() => this.populateChain(), 60000);

            // ── Wire symbol & expiry dropdown changes ──
            const ocSymSelect = document.getElementById('oc-symbol');
            const ocExpSelect = document.getElementById('oc-expiry');
            
            if (ocSymSelect) {
                ocSymSelect.addEventListener('change', () => this.populateChain());
            }
            if (ocExpSelect) {
                ocExpSelect.addEventListener('change', () => {
                    this.populateChain(ocExpSelect.value);
                });
            }
        },

        populateChain(selectedExp) {
            const tbody = document.getElementById('oc-tbody');
            if (!tbody) return;

            const ticker = document.getElementById('oc-symbol')?.value || 'QQQ';
            const expParam = selectedExp || document.getElementById('oc-expiry')?.value || '';
            const url = expParam
                ? `/api/chain?ticker=${ticker}&exp=${expParam}`
                : `/api/chain?ticker=${ticker}`;

            authFetch(url)
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        console.warn('[OptionsChain] API error:', data.error);
                        return;
                    }

                    const spot = data.spot || 0;
                    const chain = data.chain || [];

                    // Group by strike: {strike: {call: {...}, put: {...}}}
                    const byStrike = {};
                    for (const opt of chain) {
                        const s = opt.strike;
                        if (!byStrike[s]) byStrike[s] = {};
                        byStrike[s][opt.type] = opt;
                    }

                    const strikes = Object.keys(byStrike).map(Number).sort((a, b) => b - a);
                    const atm = strikes.reduce((best, s) =>
                        Math.abs(s - spot) < Math.abs(best - spot) ? s : best, strikes[0]);

                    const rows = strikes.map(strike => {
                        const c = byStrike[strike]?.call || {};
                        const p = byStrike[strike]?.put || {};
                        const isATM = strike === atm;
                        const isITMCall = strike < spot;

                        // Fusion fields from backend
                        const nqPrice = c.nq_price || p.nq_price || (strike * (data.ratio || 40));
                        const pcRatio = c.pc_ratio ?? p.pc_ratio ?? 0;
                        const cGex = c.gex || 0;
                        const pGex = p.gex || 0;
                        const netGex = cGex + pGex;

                        // P/C color: < 0.7 = bullish (green), > 1.3 = bearish (red)
                        const pcColor = pcRatio < 0.7 ? '#1fd17a' : pcRatio > 1.3 ? '#e03060' : 'rgba(140,160,200,.65)';

                        // GEX formatting: show as $M with sign
                        const gexAbs = Math.abs(netGex);
                        const gexStr = gexAbs >= 1e6 ? (netGex / 1e6).toFixed(1) + 'M'
                                     : gexAbs >= 1e3 ? (netGex / 1e3).toFixed(0) + 'K'
                                     : netGex.toFixed(0);
                        const gexColor = netGex > 0 ? '#1fd17a' : netGex < 0 ? '#e03060' : 'rgba(140,160,200,.45)';

                        const classes = [
                            isATM ? 'oc-atm' : '',
                            isITMCall ? 'oc-itm' : '',
                        ].filter(Boolean).join(' ');

                        return `<tr class="${classes}" data-strike="${strike}">
                            <td class="oc-call-cell">${(c.bid || 0).toFixed(2)}</td>
                            <td class="oc-call-cell">${(c.ask || 0).toFixed(2)}</td>
                            <td class="oc-call-cell">${(c.volume || 0).toLocaleString()}</td>
                            <td class="oc-call-cell">${c.iv != null ? c.iv + '%' : '—'}</td>
                            <td class="oc-fusion-cell" style="color:rgba(124,90,247,.85);font-size:.7rem">${nqPrice.toFixed(0)}</td>
                            <td class="oc-fusion-cell" style="color:${pcColor}">${pcRatio.toFixed(2)}</td>
                            <td class="oc-strike-cell">${strike}</td>
                            <td class="oc-fusion-cell" style="color:${gexColor};font-size:.7rem">${gexStr}</td>
                            <td class="oc-put-cell">${(p.bid || 0).toFixed(2)}</td>
                            <td class="oc-put-cell">${(p.ask || 0).toFixed(2)}</td>
                            <td class="oc-put-cell">${(p.volume || 0).toLocaleString()}</td>
                            <td class="oc-put-cell">${p.iv != null ? p.iv + '%' : '—'}</td>
                        </tr>`;
                    }).join('');

                    tbody.innerHTML = rows;

                    // ── Gamma Wall Lines on Chart ──
                    if (data.top_gex && _series) {
                        for (const gl of _gexLines) {
                            try { _series.removePriceLine(gl); } catch(e) {}
                        }
                        _gexLines = [];
                        for (const gex of data.top_gex) {
                            if (!gex.nq_price || gex.nq_price <= 0) continue;
                            const isCallSide = gex.type === 'call_wall';
                            const gexColor = isCallSide ? 'rgba(31,209,122,.45)' : 'rgba(224,48,96,.45)';
                            const gexTag = isCallSide ? 'γ RESIST' : 'γ SUPPORT';
                            const gexLabel = `${gexTag} ${(gex.gex/1e6).toFixed(1)}M`;
                            const pl = _series.createPriceLine({
                                price: gex.nq_price,
                                color: gexColor,
                                lineWidth: 1,
                                lineStyle: LightweightCharts.LineStyle.Dotted,
                                axisLabelVisible: false,
                                title: gexLabel,
                            });
                            _gexLines.push(pl);
                        }
                    }

                    const fusionBox = document.getElementById('oc-fusion-alert');
                    if (fusionBox) fusionBox.style.display = 'none';

                    // Update expiry label if present
                    const expLabel = document.getElementById('oc-exp-label');
                    if (expLabel) expLabel.textContent = `${data.expiry_label} (${data.dte}d)`;

                    // Dynamically populate expiry dropdown from API
                    const expSelect = document.getElementById('oc-expiry');
                    if (expSelect && data.expirations && data.expirations.length > 0) {
                        const currentVal = expSelect.value;
                        expSelect.innerHTML = data.expirations.map(e =>
                            `<option value="${e.date}" ${e.date === data.expiry ? 'selected' : ''}>${e.label} (${e.dte}d)</option>`
                        ).join('');
                        // Restore previous selection if still valid
                        if (currentVal && [...expSelect.options].some(o => o.value === currentVal)) {
                            expSelect.value = currentVal;
                        }
                    }

                    // Click-to-select wiring
                    tbody.querySelectorAll('.oc-strike-cell').forEach(cell => {
                        cell.addEventListener('click', () => {
                            const strike = parseFloat(cell.textContent);
                            tbody.querySelectorAll('.oc-selected').forEach(r => r.classList.remove('oc-selected'));
                            cell.parentElement.classList.add('oc-selected');
                            _selectedStrike = strike;
                            if (window.TerminalBus) {
                                window.TerminalBus.emit('strike-select', { strike, symbol: ticker });
                            }
                        });
                    });

                    // Auto-scroll to ATM
                    const atmRow = tbody.querySelector('.oc-atm');
                    if (atmRow) {
                        setTimeout(() => atmRow.scrollIntoView({ block: 'center', behavior: 'smooth' }), 100);
                    }
                })
                .catch(err => console.warn('[OptionsChain] fetch error:', err));
        },

        destroy() {
            if (_refreshTimer) clearInterval(_refreshTimer);
            if (_series) {
                for (const line of _gexLines) {
                    try { _series.removePriceLine(line); } catch(e) {}
                }
            }
            _gexLines = [];
        }
    };
})();
