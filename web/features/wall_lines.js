(function() {
    'use strict';

    // ═══ PRIVATE STATE — no globals leak ═══
    let _visible = true;
    // Per-instance: array of { series, container, wallLines[] }
    let _wlInstances = [];
    let _timer = null;
    let _currentSymbol = 'NQ';

    // ═══ PUBLIC API — exposed on window.WallLines ═══
    window.WallLines = {
        attachToSeries(series, container) {
            if (_wlInstances.find(i => i.container === container)) return;
            _wlInstances.push({ series, container, wallLines: [] });
        },
        detachInstance(container) {
            const idx = _wlInstances.findIndex(i => i.container === container);
            if (idx > -1) {
                const inst = _wlInstances[idx];
                for (const item of inst.wallLines) {
                    try { if (item.series && item.line) item.series.removePriceLine(item.line); } catch(e) {}
                }
                _wlInstances.splice(idx, 1);
            }
        },
        
        init() {
            // Re-fetch levels every 90s
            _timer = setInterval(() => this.update(), 90000);

            // Listen to AltarisEvents if/when available (Phase 5+)
            if (window.AltarisEvents) {
                window.AltarisEvents.on('symbol:change', ({ symbol }) => {
                    _currentSymbol = symbol;
                    this.update();
                });
            }
        },

        setCurrentSymbol(sym) {
            _currentSymbol = sym;
        },



        _mergeAndDrawLines(lines) {
            // Find average price to calculate a dynamic merge threshold (e.g. 0.015% ~ 3.5 NQ pts)
            const validLines = lines.filter(l => l.price && l.price > 0);
            if (validLines.length === 0) return;

            const avgPrice = validLines.reduce((sum, l) => sum + l.price, 0) / validLines.length;
            const MERGE_THRESHOLD = avgPrice * 0.00015;

            // Sort by price
            validLines.sort((a, b) => a.price - b.price);

            const merged = [];
            for (const line of validLines) {
                if (merged.length > 0) {
                    const last = merged[merged.length - 1];
                    if (Math.abs(line.price - last.price) <= MERGE_THRESHOLD) {
                        last.title += `  │  ${line.title}`;
                        last.price = (last.price + line.price) / 2;
                        continue;
                    }
                }
                merged.push({ ...line });
            }

            // Draw on all attached instances (per-pane config check)
            for (const inst of _wlInstances) {
                // Per-pane toggle check
                if (inst.container && inst.container._overlayConfig && !inst.container._overlayConfig.walls) continue;
                for (const cfg of merged) {
                    const pl = inst.series.createPriceLine({
                        price: cfg.price,
                        color: cfg.color,
                        lineWidth: 1,
                        lineStyle: LightweightCharts.LineStyle.Dashed,
                        axisLabelVisible: true,
                        title: cfg.title,
                    });
                    inst.wallLines.push({ series: inst.series, line: pl });
                }
            }
        },

        update() {
            if (!_visible) return;

            // Pick underlying: NQ → QQQ, GC → GLD
            const sym = _currentSymbol || 'NQ';
            const wallUrl = `/api/walls?symbol=${sym}`;

            authFetch(wallUrl)
                .then(r => r.json())
                .then(data => {
                    if (data.error) return;

                    // Remove old lines from all instances before rendering new ones
                    for (const inst of _wlInstances) {
                        for (const item of inst.wallLines) {
                            try { if (item.series && item.line) item.series.removePriceLine(item.line); } catch(e) {}
                        }
                        inst.wallLines = [];
                    }

                    const underlying = data.underlying_ticker || 'QQQ';
                    const lines = [
                        { price: data.put_wall, title: `PUT WALL (${underlying} ${data.underlying_put_wall || '?'})`, color: 'rgba(224, 48, 96, 0.8)' },
                        { price: data.call_wall, title: `CALL WALL (${underlying} ${data.underlying_call_wall || '?'})`, color: 'rgba(31, 209, 122, 0.8)' },
                        { price: data.max_pain, title: `MAX PAIN (${underlying} ${data.underlying_max_pain || '?'})`, color: 'rgba(255, 200, 50, 0.85)' },
                    ];

                    const freshTag = data.freshness || '⚡';

                    if (data.vanna_wall && data.vanna_wall > 0) {
                        lines.push({
                            price: data.vanna_wall,
                            color: 'rgba(0, 210, 190, 0.85)',
                            title: `${freshTag} 🌊 VANNA WALL (${underlying} ${data.underlying_vanna_wall || '?'})`
                        });
                    }

                    if (data.zero_dte_pin && data.zero_dte_pin > 0) {
                        const charmArrow = data.charm_direction === 'UP' ? '↑' : '↓';
                        lines.push({
                            price: data.zero_dte_pin,
                            color: 'rgba(255, 200, 50, 0.9)',
                            title: `${freshTag} 📌 0DTE PIN ${charmArrow} (${underlying} ${data.underlying_zero_dte_pin || '?'})`
                        });
                    }

                    // ⚡ Gamma Flip — cyan dashed line (where dealer GEX crosses zero)
                    if (data.gamma_flip && data.gamma_flip > 0) {
                        lines.push({
                            price: data.gamma_flip,
                            color: 'rgba(0, 200, 255, 0.85)',
                            title: `${freshTag} ⚡ GAMMA FLIP (${underlying} ${data.underlying_gamma_flip || '?'})`
                        });
                    }

                    this._mergeAndDrawLines(lines);

                    // Update toolbar metrics with underlying values
                    const setCW = document.getElementById('t-cw');
                    const setPW = document.getElementById('t-pw');
                    const setMP = document.getElementById('t-mp');
                    if (setCW) setCW.textContent = data.underlying_call_wall || '—';
                    if (setPW) setPW.textContent = data.underlying_put_wall || '—';
                    if (setMP) setMP.textContent = data.underlying_max_pain || '—';

                    console.log(`[Walls] ${sym}: PW=${data.put_wall} CW=${data.call_wall} MP=${data.max_pain} | ${underlying}: PW=${data.underlying_put_wall} CW=${data.underlying_call_wall} MP=${data.underlying_max_pain}`);
                    console.log(`[ELITE] 🌊 Vanna Wall: ${data.vanna_wall} (${underlying} ${data.underlying_vanna_wall}) | 📌 0DTE Pin: ${data.zero_dte_pin} (${underlying} ${data.underlying_zero_dte_pin}) | Charm: ${data.charm_direction} (${data.charm_magnitude})`);
                })
                .catch(() => {});
        },

        updateLive(data) {
            if (_wlInstances.length === 0 || !_visible) return;
            if (!data || data.error) return;

            // ── Sanity check: live WS levels must be plausible ──
            // The sigma-weighted mean can produce nonsense if only ATM strikes are subscribed.
            // Accept a live update only if put_wall < call_wall (structurally required).
            const pw = data.put_wall || 0;
            const cw = data.call_wall || 0;
            const gf = data.gamma_flip || 0;
            if (pw <= 0 || cw <= 0 || pw >= cw) {
                console.warn(`[Walls LIVE] Skipping structurally invalid zone: pw=${pw} cw=${cw} gf=${gf}`);
                return;
            }

            const underlying = data.underlying_ticker || 'QQQ';
            const ratio = data.ratio;
            if (!ratio || ratio <= 0) {
                console.warn(`[Walls LIVE] No live ratio from backend — refusing to draw guessed levels`);
                return;
            }
            const freshTag = '🔴';

            console.log(`[GEX-LIVE] zone_update: put=${pw} call=${cw} flip=${gf} src=${data.source} ratio=${ratio.toFixed(2)}`);

            for (const inst of _wlInstances) {
                for (const item of inst.wallLines) {
                    try { if (item.series && item.line) item.series.removePriceLine(item.line); } catch(e) {}
                }
                inst.wallLines = [];
            }

            // Show underlying QQQ strike in label for clarity
            const pwQQQ = data.underlying_put_wall ? data.underlying_put_wall.toFixed(0) : '?';
            const cwQQQ = data.underlying_call_wall ? data.underlying_call_wall.toFixed(0) : '?';
            const gfQQQ = data.underlying_gamma_flip ? data.underlying_gamma_flip.toFixed(0) : '?';
            const mpQQQ = data.underlying_max_pain ? data.underlying_max_pain.toFixed(0) : '?';

            const lines = [
                { price: pw, title: `🔴 PUT WALL (${underlying} ${pwQQQ})`, color: 'rgba(224, 48, 96, 0.85)' },
                { price: cw, title: `🟢 CALL WALL (${underlying} ${cwQQQ})`, color: 'rgba(31, 209, 122, 0.85)' },
                { price: data.max_pain, title: `💰 MAX PAIN (${underlying} ${mpQQQ})`, color: 'rgba(255, 200, 50, 0.85)' },
                { price: gf, title: `⚡ GAMMA FLIP (${underlying} ${gfQQQ})`, color: 'rgba(0, 220, 255, 0.85)' },
            ];

            // ── Higher-Order Greek Levels ──────────────────────────
            // Vanna Wall: where vol changes create maximum delta shift
            if (data.vanna_wall && data.vanna_wall > 0) {
                const vwQQQ = data.vanna_wall_qqq ? data.vanna_wall_qqq.toFixed(0) : '?';
                lines.push({
                    price: data.vanna_wall,
                    title: `🌊 VANNA WALL (${underlying} ${vwQQQ})`,
                    color: 'rgba(0, 210, 190, 0.85)',
                });
            }

            // Charm Gravity: where time decay pulls price (magnet into close)
            if (data.charm_gravity && data.charm_gravity > 0) {
                const cgQQQ = data.charm_gravity_qqq ? data.charm_gravity_qqq.toFixed(0) : '?';
                const charmArrow = data.charm_direction === 'UP' ? '↑' : '↓';
                lines.push({
                    price: data.charm_gravity,
                    title: `🧲 CHARM ${charmArrow} (${underlying} ${cgQQQ})`,
                    color: 'rgba(200, 50, 220, 0.85)',
                });
            }

            this._mergeAndDrawLines(lines);

            // ── Update toolbar with Greek surface metrics ─────────
            const tIvSkew = document.getElementById('t-iv-skew');
            const tTerm = document.getElementById('t-term');
            const tSpeed = document.getElementById('t-speed');
            const tConf = document.getElementById('t-confluence');
            if (tIvSkew) tIvSkew.textContent = data.iv_skew_label || '—';
            if (tTerm) tTerm.textContent = data.term_structure || '—';
            if (tSpeed) tSpeed.textContent = data.speed_sign || '—';
            if (tConf) tConf.textContent = data.confluence_count ? `${data.confluence_count}` : '0';

            // ── Phase 1+3: IV Spread, Mispricing, Flow metrics ────
            const tIvSpread = document.getElementById('t-iv-spread');
            const tMisprice = document.getElementById('t-misprice');
            const tFlow = document.getElementById('t-flow');

            if (tIvSpread) {
                const spread = data.iv_spread || 0;
                const label = data.iv_spread_label || '—';
                tIvSpread.textContent = spread > 0 ? `${(spread * 100).toFixed(1)}% ${label}` : '—';
                // Color by MM uncertainty level
                const unc = data.mm_uncertainty || 0;
                tIvSpread.style.color = unc >= 3 ? '#ff3060' : unc >= 2 ? '#ff9500' : unc >= 1 ? '#ffd700' : '#4cd964';
            }
            if (tMisprice) {
                const mp = data.avg_mispricing_pct || 0;
                tMisprice.textContent = mp > 0 ? `${mp.toFixed(1)}%` : '—';
                tMisprice.style.color = mp > 10 ? '#ff3060' : mp > 5 ? '#ff9500' : '#ff6b35';
            }
            if (tFlow) {
                const flow = data.mark_flow_direction || 'BALANCED';
                if (flow === 'CALL_ACCUMULATING') {
                    tFlow.textContent = '▲ CALLS';
                    tFlow.style.color = '#2ee88a';
                } else if (flow === 'PUT_ACCUMULATING') {
                    tFlow.textContent = '▼ PUTS';
                    tFlow.style.color = '#ff3060';
                } else {
                    tFlow.textContent = '◆ BAL';
                    tFlow.style.color = '#888';
                }
            }

            // ── Vol Surface regime + premium + IV rank ────────────
            const tVolRegime = document.getElementById('t-vol-regime');
            const tVolPrem = document.getElementById('t-vol-prem');
            const tIvRank = document.getElementById('t-iv-rank');

            if (tVolRegime && data.vol_regime) {
                const regime = data.vol_regime;
                const regimeColors = {
                    'STRESSED': '#ff3060',
                    'ELEVATED': '#ff9500',
                    'NORMAL': '#a78bfa',
                    'COMPLACENT': '#4cd964',
                    'COMPRESSED': '#00dcff',
                };
                tVolRegime.textContent = regime;
                tVolRegime.style.color = regimeColors[regime] || '#888';
            }
            if (tVolPrem && data.vol_premium !== undefined) {
                const vp = data.vol_premium;
                const sign = vp >= 0 ? '+' : '';
                tVolPrem.textContent = `${sign}${vp.toFixed(1)}%`;
                tVolPrem.style.color = vp > 15 ? '#ff3060' : vp > 8 ? '#ff9500' : vp > 0 ? '#38bdf8' : '#4cd964';
            }
            if (tIvRank && data.iv_rank !== undefined) {
                const rank = data.iv_rank;
                tIvRank.textContent = `${rank.toFixed(0)}`;
                tIvRank.style.color = rank > 80 ? '#ff3060' : rank > 60 ? '#ff9500' : rank < 20 ? '#4cd964' : '#888';
            }
        },

        toggle() {
            _visible = !_visible;
            const btn = document.getElementById('t-walls-toggle');
            if (btn) {
                btn.classList.toggle('active', _visible);
                btn.title = _visible ? 'Hide Options Levels' : 'Show Options Levels';
            }
            if (_visible) {
                this.update();
            } else {
                for (const inst of _wlInstances) {
                    for (const item of inst.wallLines) {
                        try { if (item.series && item.line) item.series.removePriceLine(item.line); } catch(e) {}
                    }
                    inst.wallLines = [];
                }
            }
        },

        destroy() {
            if (_timer) clearInterval(_timer);
            for (const inst of _wlInstances) {
                for (const item of inst.wallLines) {
                    try { if (item.series && item.line) item.series.removePriceLine(item.line); } catch(e) {}
                }
            }
            _wlInstances = [];
        }
    };
})();
