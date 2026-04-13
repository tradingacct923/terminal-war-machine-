/**
 * BookMsHUD — Book Microstructure HUD Widget
 *
 * Displays real-time QQQ NASDAQ L2 book quality from the Schwab NASDAQ_BOOK feed:
 *   - Quality-Adjusted Imbalance (filters out phantom HFT depth)
 *   - BBO venue quality (institutional vs HFT-only)
 *   - Per-level depth breakdown with venue MPIDs
 *
 * Injected into the DOM when the equity book pane is visible.
 * Data comes from the `book_microstructure` WebSocket event (2Hz).
 *
 * The key insight: raw order book size is MISLEADING.
 * 2000 shares on EDGX alone will evaporate in <1ms on any sweep.
 * 2000 shares split across NSDQ + arcx + phlx will SURVIVE a sweep
 * and represent real institutional conviction.
 */

'use strict';

const BookMsHUD = (() => {

    // ── Venue display config ──────────────────────────────────────────────────
    const SLOW_VENUES = new Set(['NSDQ','arcx','phlx','cinn','cboe','bos','NYSE']);
    const FAST_VENUES = new Set(['edgx','batx','memx','edga','baty','iexg','drctedge']);

    function _venueTag(id) {
        const upper = (id||'').toUpperCase();
        const lower = (id||'').toLowerCase();
        const isSlow = SLOW_VENUES.has(id) || SLOW_VENUES.has(upper);
        const isFast = FAST_VENUES.has(id) || FAST_VENUES.has(lower);
        const cls = isSlow ? 'bms-venue-slow' : isFast ? 'bms-venue-fast' : 'bms-venue-neutral';
        return `<span class="bms-venue ${cls}">${id}</span>`;
    }

    // ── DOM creation ─────────────────────────────────────────────────────────
    let _panel = null;
    let _slotEl = null;  // non-null when mounted as a pane

    function init(slotEl) {
        _slotEl = slotEl;
        // Destroy any existing floating overlay
        if (_panel && _panel.parentNode) _panel.parentNode.removeChild(_panel);
        _panel = null;
        _ensurePanel();
    }

    function _ensurePanel() {
        if (_panel) return _panel;

        // Inject styles
        if (!document.getElementById('bms-hud-styles')) {
            const s = document.createElement('style');
            s.id = 'bms-hud-styles';
            s.textContent = `
                #bms-hud {
                    position: fixed;
                    bottom: 56px;
                    right: 14px;
                    width: 280px;
                    background: rgba(10, 14, 22, 0.92);
                    border: 1px solid rgba(255,255,255,0.08);
                    border-radius: 8px;
                    font-family: 'JetBrains Mono', 'Fira Code', monospace;
                    font-size: 10px;
                    color: #c9d1d9;
                    z-index: 2500;
                    backdrop-filter: blur(12px);
                    box-shadow: 0 4px 24px rgba(0,0,0,0.5);
                    overflow: hidden;
                    transition: opacity 0.2s;
                }
                #bms-hud-header {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    padding: 6px 10px;
                    background: rgba(255,255,255,0.04);
                    border-bottom: 1px solid rgba(255,255,255,0.06);
                    cursor: pointer;
                    user-select: none;
                }
                #bms-hud-header span { font-size: 9px; font-weight: 700; letter-spacing: 0.08em; color: #8b949e; }
                #bms-hud-header .bms-live-dot {
                    width: 6px; height: 6px; border-radius: 50%;
                    background: #1fd17a; margin-right: 5px;
                    animation: bms-pulse 1.5s infinite;
                }
                @keyframes bms-pulse {
                    0%,100% { opacity: 1; } 50% { opacity: 0.3; }
                }
                #bms-hud-body { padding: 8px 10px; }

                /* Quality-Adjusted Imbalance bar */
                .bms-qa-row { margin-bottom: 8px; }
                .bms-qa-label { color: #8b949e; font-size: 9px; margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.06em; }
                .bms-qa-bar-wrap {
                    position: relative; height: 16px;
                    background: linear-gradient(to right, rgba(255,64,48,0.15) 0%, rgba(255,64,48,0.15) 50%, rgba(31,209,122,0.15) 50%, rgba(31,209,122,0.15) 100%);
                    border-radius: 3px; overflow: hidden;
                }
                .bms-qa-center { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: rgba(255,255,255,0.2); }
                .bms-qa-fill {
                    position: absolute; top: 2px; bottom: 2px;
                    border-radius: 2px; transition: all 0.3s ease;
                }
                .bms-qa-val { position: absolute; right: 4px; top: 0; line-height: 16px; font-size: 9px; font-weight: 700; }

                /* BBO quality row */
                .bms-bbo-row {
                    display: flex; gap: 6px; margin-bottom: 8px;
                }
                .bms-bbo-side {
                    flex: 1; padding: 5px 7px;
                    border-radius: 5px;
                    border: 1px solid rgba(255,255,255,0.07);
                }
                .bms-bbo-side.bid { background: rgba(31,209,122,0.07); border-color: rgba(31,209,122,0.2); }
                .bms-bbo-side.ask { background: rgba(255,64,48,0.07); border-color: rgba(255,64,48,0.2); }
                .bms-bbo-price { font-size: 11px; font-weight: 700; line-height: 1.2; }
                .bms-bbo-price.bid { color: #1fd17a; }
                .bms-bbo-price.ask { color: #ff4030; }
                .bms-bbo-meta { font-size: 8.5px; color: #8b949e; margin-top: 2px; }
                .bms-bbo-meta .quality-bar {
                    display: inline-block; width: 40px; height: 3px;
                    background: rgba(255,255,255,0.1); border-radius: 2px;
                    vertical-align: middle; margin-left: 3px; overflow: hidden;
                }
                .bms-bbo-meta .quality-fill {
                    height: 100%; border-radius: 2px; transition: width 0.3s;
                }

                /* HFT warning badge */
                .bms-hft-warn {
                    background: rgba(255,184,0,0.12);
                    border: 1px solid rgba(255,184,0,0.3);
                    border-radius: 4px;
                    padding: 4px 7px;
                    color: #ffb800;
                    font-size: 9px;
                    margin-bottom: 8px;
                    display: none;
                }
                .bms-hft-warn.visible { display: block; }

                /* Venue chips */
                .bms-venue-row { margin-bottom: 6px; }
                .bms-venue-row-label { color: #8b949e; font-size: 8.5px; margin-bottom: 2px; text-transform: uppercase; }
                .bms-venue { display: inline-block; padding: 1px 4px; border-radius: 3px;
                             font-size: 8px; font-weight: 700; margin: 1px; line-height: 1.4; }
                .bms-venue-slow { background: rgba(31,209,122,0.15); color: #1fd17a;
                                  border: 1px solid rgba(31,209,122,0.25); }
                .bms-venue-fast { background: rgba(255,64,48,0.1); color: #ff6050;
                                  border: 1px solid rgba(255,64,48,0.2); }
                .bms-venue-neutral { background: rgba(255,255,255,0.07); color: #8b949e;
                                     border: 1px solid rgba(255,255,255,0.1); }

                /* Depth levels mini table */
                .bms-levels { width: 100%; border-collapse: collapse; font-size: 8.5px; }
                .bms-levels th { color: #4a5568; padding: 2px 3px; text-align: right; font-weight: 600; text-transform: uppercase; font-size: 8px; }
                .bms-levels td { padding: 2px 3px; text-align: right; color: #c9d1d9; }
                .bms-levels td:first-child { text-align: left; }
                .bms-levels tr.bid-row td { color: #1fd17a; }
                .bms-levels tr.ask-row td { color: #ff6050; }
                .bms-levels .dq-hi { color: #1fd17a; font-weight: 700; }
                .bms-levels .dq-lo { color: #ff6050; }
                .bms-divider { border: none; border-top: 1px solid rgba(255,255,255,0.05); margin: 6px 0; }
            `;
            document.head.appendChild(s);
        }

        _panel = document.createElement('div');
        _panel.id = 'bms-hud';
        _panel.innerHTML = `
            <div id="bms-hud-header">
                <div style="display:flex;align-items:center">
                    <div class="bms-live-dot"></div>
                    <span>QQQ L2 BOOK QUALITY</span>
                </div>
                <span id="bms-hud-toggle" style="cursor:pointer;color:#4a5568;">▼</span>
            </div>
            <div id="bms-hud-body">
                <div class="bms-qa-row">
                    <div class="bms-qa-label">Quality-Adjusted Imbalance <span style="color:#555">(filters HFT phantom depth)</span></div>
                    <div class="bms-qa-bar-wrap">
                        <div class="bms-qa-center"></div>
                        <div class="bms-qa-fill" id="bms-qa-fill"></div>
                        <div class="bms-qa-val" id="bms-qa-val">—</div>
                    </div>
                </div>
                <div class="bms-bbo-row">
                    <div class="bms-bbo-side bid">
                        <div class="bms-bbo-price bid" id="bms-bid-price">—</div>
                        <div class="bms-bbo-meta">
                            <span id="bms-bid-size">—</span>sh
                            <span id="bms-bid-mmc">—</span>venues
                            <span class="quality-bar"><span class="quality-fill" id="bms-bid-qfill" style="background:#1fd17a"></span></span>
                        </div>
                        <div id="bms-bid-venues" style="margin-top:3px"></div>
                    </div>
                    <div class="bms-bbo-side ask">
                        <div class="bms-bbo-price ask" id="bms-ask-price">—</div>
                        <div class="bms-bbo-meta">
                            <span id="bms-ask-size">—</span>sh
                            <span id="bms-ask-mmc">—</span>venues
                            <span class="quality-bar"><span class="quality-fill" id="bms-ask-qfill" style="background:#ff4030"></span></span>
                        </div>
                        <div id="bms-ask-venues" style="margin-top:3px"></div>
                    </div>
                </div>
                <div class="bms-hft-warn" id="bms-hft-warn">
                    ⚡ BBO HFT-ONLY — quotes will evaporate on sweep
                </div>
                <hr class="bms-divider">
                <table class="bms-levels">
                    <thead>
                        <tr>
                            <th>Price</th>
                            <th>Size</th>
                            <th>Venues</th>
                            <th>Avg Lot</th>
                            <th>DQ</th>
                        </tr>
                    </thead>
                    <tbody id="bms-levels-body"></tbody>
                </table>
            </div>
        `;

        if (_slotEl) {
            // Pane mode: fill slot, not floating overlay
            _panel.style.cssText = 'position:relative;width:100%;height:100%;overflow-y:auto;background:#070a14;';
            _slotEl.appendChild(_panel);
        } else {
            document.body.appendChild(_panel);
        }

        // Toggle collapse
        let collapsed = false;
        document.getElementById('bms-hud-toggle').addEventListener('click', () => {
            collapsed = !collapsed;
            document.getElementById('bms-hud-body').style.display = collapsed ? 'none' : '';
            document.getElementById('bms-hud-toggle').textContent = collapsed ? '▶' : '▼';
        });

        return _panel;
    }

    // ── Update function ───────────────────────────────────────────────────────
    function update(data) {
        // Only render if panel was explicitly created via init() (pane mode).
        // Don't auto-spawn a floating overlay — it overlaps other panes.
        if (!_panel) return;

        const qa   = data.qa_imbalance || 0;
        const bidQ = data.bid_quality  || 0;
        const askQ = data.ask_quality  || 0;
        const bbo_bid = data.bbo_bid || {};
        const bbo_ask = data.bbo_ask || {};
        const hft = !!data.bbo_hft_only;

        // ── QA Imbalance bar ──
        const fill = document.getElementById('bms-qa-fill');
        const val  = document.getElementById('bms-qa-val');
        if (fill && val) {
            // qa ∈ [-1, +1]. Center at 50%. Positive = bid heavy (green), neg = ask heavy (red).
            const pct = ((qa + 1) / 2) * 100;  // map to 0-100
            if (qa >= 0) {
                // Bid-heavy: fill from center rightward, green
                fill.style.left   = '50%';
                fill.style.right  = `${100 - pct}%`;
                fill.style.width  = `${pct - 50}%`;
                fill.style.background = 'rgba(31,209,122,0.6)';
            } else {
                // Ask-heavy: fill from center leftward, red
                fill.style.left   = `${pct}%`;
                fill.style.right  = '50%';
                fill.style.width  = `${50 - pct}%`;
                fill.style.background = 'rgba(255,64,48,0.6)';
            }
            val.textContent = (qa >= 0 ? '+' : '') + qa.toFixed(3);
            val.style.color = qa >= 0 ? '#1fd17a' : '#ff4030';
        }

        // ── BBO cards ──
        const _set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        const _html = (id, v) => { const el = document.getElementById(id); if (el) el.innerHTML = v; };

        _set('bms-bid-price', bbo_bid.price ? bbo_bid.price.toFixed(2) : '—');
        _set('bms-bid-size', bbo_bid.size ? bbo_bid.size.toLocaleString() : '—');
        _set('bms-bid-mmc', bbo_bid.mm_count || '—');
        _html('bms-bid-venues', (bbo_bid.venues||[]).map(_venueTag).join(' '));
        const bqf = document.getElementById('bms-bid-qfill');
        if (bqf) bqf.style.width = `${Math.round(bidQ * 100)}%`;

        _set('bms-ask-price', bbo_ask.price ? bbo_ask.price.toFixed(2) : '—');
        _set('bms-ask-size', bbo_ask.size ? bbo_ask.size.toLocaleString() : '—');
        _set('bms-ask-mmc', bbo_ask.mm_count || '—');
        _html('bms-ask-venues', (bbo_ask.venues||[]).map(_venueTag).join(' '));
        const aqf = document.getElementById('bms-ask-qfill');
        if (aqf) aqf.style.width = `${Math.round(askQ * 100)}%`;

        // ── HFT warning ──
        const warn = document.getElementById('bms-hft-warn');
        if (warn) warn.classList.toggle('visible', hft);

        // ── Depth table ──
        const tbody = document.getElementById('bms-levels-body');
        if (tbody) {
            const bidLevels = (data.bid_levels || []).slice(0, 4);
            const askLevels = (data.ask_levels || []).slice(0, 4);
            let html = '';

            // Asks above (price descending so best ask at bottom of asks section)
            for (const l of [...askLevels].reverse()) {
                const dqCls = l.depth_quality >= 0.5 ? 'dq-hi' : l.depth_quality < 0.25 ? 'dq-lo' : '';
                html += `<tr class="ask-row">
                    <td>${l.price ? l.price.toFixed(2) : '—'}</td>
                    <td>${l.size ? l.size.toLocaleString() : '—'}</td>
                    <td>${l.mm_count || '—'}</td>
                    <td>${l.avg_lot_size != null ? l.avg_lot_size : '—'}</td>
                    <td class="${dqCls}">${l.depth_quality != null ? (l.depth_quality * 100).toFixed(0)+'%' : '—'}</td>
                </tr>`;
            }
            // Separator
            html += `<tr><td colspan="5" style="padding:3px 0;border-top:1px solid rgba(255,255,255,0.08)"></td></tr>`;
            // Bids below (best bid at top of bids section)
            for (const l of bidLevels) {
                const dqCls = l.depth_quality >= 0.5 ? 'dq-hi' : l.depth_quality < 0.25 ? 'dq-lo' : '';
                html += `<tr class="bid-row">
                    <td>${l.price ? l.price.toFixed(2) : '—'}</td>
                    <td>${l.size ? l.size.toLocaleString() : '—'}</td>
                    <td>${l.mm_count || '—'}</td>
                    <td>${l.avg_lot_size != null ? l.avg_lot_size : '—'}</td>
                    <td class="${dqCls}">${l.depth_quality != null ? (l.depth_quality * 100).toFixed(0)+'%' : '—'}</td>
                </tr>`;
            }
            tbody.innerHTML = html;
        }
    }

    function destroy() {
        if (_panel && _panel.parentNode) {
            _panel.parentNode.removeChild(_panel);
        }
        _panel = null;
        _slotEl = null;
    }

    return { init, update, destroy };
})();
