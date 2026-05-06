/**
 * ConvictionPane — Composite Conviction Score (CCS) display.
 *
 * Surfaces the directional-bias framework engine in real time:
 *   - The MASTER VARIABLE: amplification_factor = |hp_γ_shares| / vol_5min
 *     Below 1.0 → regime is noise. Above 3.0 → regime is dominant.
 *   - CCS 0–100: composite of 6 weighted components
 *   - Direction: BULL / BEAR / NEUTRAL
 *   - Size: FULL / HALF / QUARTER / PASS / REVERSE
 *   - Anti-setup flags (red badges if any)
 *   - Per-component breakdown with progress bars
 *   - Regime transition watch chip
 *
 * Backed by /api/conviction/<ticker> + 'conviction_update' socket event.
 */
const ConvictionPane = (() => {
    'use strict';

    let _slotEl = null;
    let _ticker = 'QQQ';
    let _state = null;
    let _pollTimer = null;
    let _socketUnsub = null;

    // Style + DOM
    function _ensureStyle() {
        if (document.getElementById('conviction-pane-style')) return;
        const css = `
            .ccs-pane {
              padding: 8px 10px; color: #b9c0cc; font-family: var(--mono, monospace);
              font-size: 11px; height: 100%; overflow-y: auto; overflow-x: hidden;
              display: flex; flex-direction: column; gap: 8px;
            }
            .ccs-pane .ccs-header { display: flex; justify-content: space-between;
              align-items: baseline; padding-bottom: 4px;
              border-bottom: 1px solid #1f242c; }
            .ccs-pane .ccs-title { font-size: 11px; letter-spacing: 0.06em;
              color: #6f7785; text-transform: uppercase; }
            .ccs-pane .ccs-ticker { font-size: 13px; font-weight: 700; color: #e8ecf1; }
            .ccs-pane .ccs-status { font-size: 10px; color: #6f7785; }
            .ccs-pane .ccs-status.live::before {
              content: '●'; color: #5cb95c; margin-right: 4px;
            }

            .ccs-pane .ccs-master { display: grid;
              grid-template-columns: 1fr 1fr; gap: 8px;
              padding: 8px; background: #0f1419; border-radius: 4px;
              border: 1px solid #1f242c;
            }
            .ccs-pane .ccs-master-cell { display: flex; flex-direction: column;
              align-items: center; }
            .ccs-pane .ccs-master-label { font-size: 9px; color: #6f7785;
              letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 2px;
            }
            .ccs-pane .ccs-amp-value { font-size: 22px; font-weight: 700;
              line-height: 1; color: #f5d76e;
            }
            .ccs-pane .ccs-amp-value.dominant { color: #5cb95c; }
            .ccs-pane .ccs-amp-value.structural { color: #f5d76e; }
            .ccs-pane .ccs-amp-value.contributory { color: #d6b94e; }
            .ccs-pane .ccs-amp-value.background { color: #6f7785; }
            .ccs-pane .ccs-amp-class { font-size: 9px; color: #6f7785;
              text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px;
            }
            .ccs-pane .ccs-score-value { font-size: 22px; font-weight: 700;
              line-height: 1;
            }
            .ccs-pane .ccs-score-bar { width: 100%; height: 3px; margin-top: 4px;
              background: #1f242c; border-radius: 2px; overflow: hidden;
            }
            .ccs-pane .ccs-score-bar-fill { height: 100%; transition: width 0.3s; }

            .ccs-pane .ccs-action {
              padding: 8px; border-radius: 4px; text-align: center;
              border: 1px solid #1f242c;
            }
            .ccs-pane .ccs-action.bull { background: rgba(92, 185, 92, 0.08);
              border-color: rgba(92, 185, 92, 0.3); }
            .ccs-pane .ccs-action.bear { background: rgba(220, 80, 80, 0.08);
              border-color: rgba(220, 80, 80, 0.3); }
            .ccs-pane .ccs-action.neutral { background: rgba(150, 150, 150, 0.05); }
            .ccs-pane .ccs-action-direction { font-size: 16px; font-weight: 700;
              letter-spacing: 0.1em; }
            .ccs-pane .ccs-action.bull .ccs-action-direction { color: #5cb95c; }
            .ccs-pane .ccs-action.bear .ccs-action-direction { color: #dc5050; }
            .ccs-pane .ccs-action.neutral .ccs-action-direction { color: #6f7785; }
            .ccs-pane .ccs-action-size { font-size: 11px; color: #b9c0cc;
              margin-top: 2px;
            }
            .ccs-pane .ccs-action-size.pass { color: #6f7785; }
            .ccs-pane .ccs-action-size.full { color: #f5d76e; font-weight: 700; }
            .ccs-pane .ccs-action-size.half { color: #d6b94e; }
            .ccs-pane .ccs-action-size.quarter { color: #95a3b2; }
            .ccs-pane .ccs-action-size.reverse { color: #dc5050; font-weight: 700; }

            .ccs-pane .ccs-rationale { font-size: 10px; color: #95a3b2;
              padding: 4px 6px; background: #0f1419; border-radius: 3px;
              border-left: 2px solid #2a313a; line-height: 1.4;
              word-break: break-word;
            }

            .ccs-pane .ccs-anti { display: flex; flex-wrap: wrap; gap: 4px; }
            .ccs-pane .ccs-anti-flag {
              font-size: 9px; padding: 2px 6px; border-radius: 2px;
              background: rgba(220, 80, 80, 0.15); color: #dc5050;
              text-transform: lowercase; letter-spacing: 0.04em;
              border: 1px solid rgba(220, 80, 80, 0.3);
            }

            .ccs-pane .ccs-watch { padding: 4px 8px; border-radius: 3px;
              background: rgba(245, 215, 110, 0.1); color: #f5d76e;
              border: 1px solid rgba(245, 215, 110, 0.3); font-size: 10px;
              text-align: center; letter-spacing: 0.04em;
            }

            .ccs-pane .ccs-section-label {
              font-size: 9px; color: #6f7785;
              letter-spacing: 0.08em; text-transform: uppercase;
              margin-top: 2px; margin-bottom: 2px;
            }

            .ccs-pane .ccs-comp-row { display: grid;
              grid-template-columns: 110px 1fr 32px;
              align-items: center; gap: 6px;
              padding: 2px 0;
            }
            .ccs-pane .ccs-comp-row.ccs-comp-intel {
              border-left: 2px solid rgba(168,158,255,.45);
              padding-left: 4px;
              margin-left: -4px;
            }
            .ccs-pane .ccs-comp-label { font-size: 10px; color: #95a3b2; }
            .ccs-pane .ccs-comp-row.ccs-comp-intel .ccs-comp-label { color: #b3a9e0; }
            .ccs-pane .ccs-comp-bar { height: 6px; background: #1f242c;
              border-radius: 1px; overflow: hidden; position: relative;
            }
            .ccs-pane .ccs-comp-bar-fill { height: 100%; transition: width 0.3s; }
            .ccs-pane .ccs-comp-val { font-size: 10px; color: #95a3b2;
              text-align: right; font-variant-numeric: tabular-nums;
            }
            .ccs-pane .ccs-comp-divider {
              font-size: 8px; color: rgba(168,158,255,.65);
              text-transform: uppercase; letter-spacing: 0.5px;
              padding-top: 4px; margin-top: 2px;
              border-top: 1px dashed rgba(168,158,255,.20);
            }
            .ccs-pane .ccs-warehouse-tag {
              display: inline-block; font-size: 9px;
              padding: 1px 6px; border-radius: 2px;
              font-variant-numeric: tabular-nums;
              margin-left: 6px;
            }
            .ccs-pane .ccs-warehouse-tag.committed {
              background: rgba(133,224,163,.14); color: #85e0a3;
            }
            .ccs-pane .ccs-warehouse-tag.phantom {
              background: rgba(168,158,255,.14); color: #bcb3ff;
            }
            .ccs-pane .ccs-warehouse-tag.active {
              background: rgba(255,209,128,.14); color: #ffd180;
            }
            .ccs-pane .ccs-warehouse-tag.inactive {
              background: rgba(170,180,210,.10); color: rgba(180,190,210,.5);
            }

            .ccs-pane .ccs-meta { display: grid;
              grid-template-columns: 1fr 1fr; gap: 4px 12px;
              font-size: 10px; color: #95a3b2;
            }
            .ccs-pane .ccs-meta-cell { display: flex; justify-content: space-between; }
            .ccs-pane .ccs-meta-key { color: #6f7785; }
            .ccs-pane .ccs-meta-val { color: #b9c0cc;
              font-variant-numeric: tabular-nums;
            }
        `;
        const tag = document.createElement('style');
        tag.id = 'conviction-pane-style';
        tag.textContent = css;
        document.head.appendChild(tag);
    }

    function _scoreColor(score) {
        if (score >= 75) return '#5cb95c';
        if (score >= 60) return '#f5d76e';
        if (score >= 45) return '#d6b94e';
        return '#6f7785';
    }

    function _compColor(score) {
        if (score >= 80) return '#5cb95c';
        if (score >= 50) return '#f5d76e';
        if (score >= 30) return '#d6b94e';
        return '#6f7785';
    }

    function _fmtSigned(x) {
        if (!Number.isFinite(x)) return '—';
        const s = x > 0 ? '+' : '';
        return s + x.toFixed(2);
    }

    function _render() {
        if (!_slotEl) return;
        if (!_state || !_state.ticker) {
            _slotEl.innerHTML = `<div class="ccs-pane">
                <div class="ccs-header">
                  <span class="ccs-title">Conviction</span>
                  <span class="ccs-status">waiting for first compute…</span>
                </div>
            </div>`;
            return;
        }

        const s = _state;
        const score = +s.score || 0;
        const dir = s.direction || 'NEUTRAL';
        const size = s.size_recommendation || 'PASS';
        const amp = +s.amp_factor || 0;
        const ampClass = (s.regime_class || 'background').toLowerCase();
        const c = s.components || {};
        const anti = s.anti_setups || [];
        const watch = !!s.regime_transition_watch;

        const dirClass = dir === 'BULL' ? 'bull' : (dir === 'BEAR' ? 'bear' : 'neutral');
        const sizeClass = size.toLowerCase();

        const compDef = [
            { key: 'regime_alignment',  label: 'Regime align',  group: 'core' },
            { key: 'distance_to_flip',  label: 'Flip distance', group: 'core' },
            { key: 'flow_quality',      label: 'Flow quality',  group: 'core' },
            { key: 'mm_signature',      label: 'MM signature',  group: 'core' },
            { key: 'time_of_day',       label: 'Time of day',   group: 'core' },
            { key: 'cross_asset',       label: 'Cross-asset',   group: 'core' },
            { key: 'mispricing_signal', label: 'Theo·Mark',     group: 'core' },
            // Phase 9 — intel signal components (6 panels feed CCS)
            { key: 'intel_hedge_fc',    label: '🔮 Hedge FC',    group: 'intel' },
            { key: 'intel_pin',         label: '🎯 Pin pull',    group: 'intel' },
            { key: 'intel_vol_regime',  label: '📈 Vol regime',  group: 'intel' },
            { key: 'intel_spxqqq',      label: '⚖ SPX-QQQ Div', group: 'intel' },
            { key: 'intel_sweep',       label: '⚡ Sweep align', group: 'intel' },
            { key: 'intel_wing',        label: '🪶 Wing flow',   group: 'intel' },
        ];
        const compRowsParts = [];
        let lastGroup = null;
        for (const r of compDef) {
            if (r.group !== lastGroup) {
                if (r.group === 'intel') {
                    compRowsParts.push(
                        `<div class="ccs-comp-divider">Intel signals</div>`);
                }
                lastGroup = r.group;
            }
            const v = c[r.key] ?? 0;
            const groupCls = r.group === 'intel' ? ' ccs-comp-intel' : '';
            compRowsParts.push(`<div class="ccs-comp-row${groupCls}">
                <div class="ccs-comp-label">${r.label}</div>
                <div class="ccs-comp-bar">
                  <div class="ccs-comp-bar-fill" style="width:${v}%; background:${_compColor(v)};"></div>
                </div>
                <div class="ccs-comp-val">${v}</div>
            </div>`);
        }
        const compRows = compRowsParts.join('');

        // Warehouse multiplier indicator (shows if dist_score got boosted/penalized)
        const whMult = +c.warehouse_multiplier || 1.0;
        const whClass = c.warehouse_class_at_flip || null;
        const whTagCls = (whClass || '').toLowerCase();
        const whTagHtml = whClass
            ? `<span class="ccs-warehouse-tag ${whTagCls}">🛡 ${whClass} ×${whMult.toFixed(2)}</span>`
            : '';

        const antiHtml = anti.length
            ? `<div class="ccs-anti">${anti.map(a =>
                `<span class="ccs-anti-flag">${a}</span>`).join('')}</div>`
            : '';

        const watchHtml = watch
            ? `<div class="ccs-watch">⚠ regime transition imminent — flip within 0.5%</div>`
            : '';

        const distFlipPct = (c.distance_to_flip ?? null);
        const distPctRaw = s.distance_to_flip_pct;

        const ampClassLabel = (s.regime_class || 'background').toUpperCase();

        _slotEl.innerHTML = `<div class="ccs-pane">
            <div class="ccs-header">
              <span class="ccs-title">Conviction · ${s.ticker}</span>
              <span class="ccs-status live">${new Date(s.ts*1000).toLocaleTimeString()}</span>
            </div>

            <div class="ccs-master">
              <div class="ccs-master-cell">
                <div class="ccs-master-label">Amp factor</div>
                <div class="ccs-amp-value ${ampClass}">${amp.toFixed(2)}×</div>
                <div class="ccs-amp-class">${ampClassLabel}</div>
              </div>
              <div class="ccs-master-cell">
                <div class="ccs-master-label">CCS</div>
                <div class="ccs-score-value" style="color:${_scoreColor(score)};">${score.toFixed(0)}</div>
                <div class="ccs-score-bar">
                  <div class="ccs-score-bar-fill"
                       style="width:${Math.min(100, score)}%; background:${_scoreColor(score)};"></div>
                </div>
              </div>
            </div>

            <div class="ccs-action ${dirClass}">
              <div class="ccs-action-direction">${dir}</div>
              <div class="ccs-action-size ${sizeClass}">${size} SIZE</div>
            </div>

            ${watchHtml}
            ${antiHtml}

            <div class="ccs-rationale">${s.rationale || '—'}</div>

            <div class="ccs-section-label">Components</div>
            ${compRows}

            <div class="ccs-section-label">Inputs</div>
            <div class="ccs-meta">
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Spot</span>
                <span class="ccs-meta-val">${(s.spot || 0).toFixed(2)}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Regime</span>
                <span class="ccs-meta-val">${s.regime}${c.regime_disagrees ? ` <span style="color:#ff9aa8; font-size:9px;" title="wall_signals → ${c.regime_wall_signals}; using hp_gamma sign as authoritative">⚠ ws→${c.regime_wall_signals}</span>` : ''}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Flip dist</span>
                <span class="ccs-meta-val">${(distPctRaw*100).toFixed(2)}% ${whTagHtml}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Hp γ shares</span>
                <span class="ccs-meta-val">${(c.hp_gamma_shares_1pct || 0).toLocaleString()}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Vol 5m</span>
                <span class="ccs-meta-val">${(c.rolling_5min_volume || c.baseline_volume || 0).toLocaleString()}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Flow all</span>
                <span class="ccs-meta-val">${_fmtSigned(c.flow_signed_all_M || 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Flow 0DTE</span>
                <span class="ccs-meta-val">${_fmtSigned(c.flow_signed_0dte_M || 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Exch concen</span>
                <span class="ccs-meta-val">${((c.per_exch_concentration || 0)*100).toFixed(0)}%</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Mark−Theo</span>
                <span class="ccs-meta-val">${(c.mispricing_avg_pct ?? 0).toFixed(2)}%</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Misp strikes</span>
                <span class="ccs-meta-val">${c.mispricing_strikes_count ?? 0}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Inst share</span>
                <span class="ccs-meta-val">${((c.institutional_share ?? 0)*100).toFixed(0)}%</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Setup mode</span>
                <span class="ccs-meta-val">${c.setup_mode ?? '—'}${c.setup_confidence ? ' '+c.setup_confidence+'%':''}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Top venue</span>
                <span class="ccs-meta-val">${c.top_venue_mic ?? '—'}${c.top_venue_share_pct ? ' '+c.top_venue_share_pct.toFixed(0)+'%':''}</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Venue concen</span>
                <span class="ccs-meta-val">${((c.venue_concentration ?? 0)*100).toFixed(0)}%</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Opening flow</span>
                <span class="ccs-meta-val">${_fmtSigned(c.opening_signed_M ?? 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Closing flow</span>
                <span class="ccs-meta-val">${_fmtSigned(c.closing_signed_M ?? 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">OI classified</span>
                <span class="ccs-meta-val">${((c.oi_classified_share ?? 0)*100).toFixed(0)}%</span></div>
            </div>
            <div class="ccs-section-label">Cohort split (signed $M)</div>
            <div class="ccs-meta">
              <div class="ccs-meta-cell"><span class="ccs-meta-key">0DTE AM (inst)</span>
                <span class="ccs-meta-val">${_fmtSigned(c.cohort_0dte_am_M ?? 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">0DTE PM (retail)</span>
                <span class="ccs-meta-val">${_fmtSigned(c.cohort_0dte_pm_M ?? 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Weekly</span>
                <span class="ccs-meta-val">${_fmtSigned(c.cohort_weekly_M ?? 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Monthly</span>
                <span class="ccs-meta-val">${_fmtSigned(c.cohort_monthly_M ?? 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">Quarterly</span>
                <span class="ccs-meta-val">${_fmtSigned(c.cohort_quarterly_M ?? 0)}M</span></div>
              <div class="ccs-meta-cell"><span class="ccs-meta-key">LEAPS</span>
                <span class="ccs-meta-val">${_fmtSigned(c.cohort_leaps_M ?? 0)}M</span></div>
            </div>
        </div>`;
    }

    async function _fetchOnce() {
        try {
            const tok = sessionStorage.getItem('greeks-auth') || '';
            const r = await fetch(`/api/conviction/${_ticker}`, {
                headers: { 'X-Auth-Token': tok }
            });
            if (!r.ok) return;
            const d = await r.json();
            if (d && d.ticker) {
                _state = d;
                _render();
            }
        } catch (_) {}
    }

    function _onSocketUpdate(d) {
        if (!d || d.ticker !== _ticker) return;
        _state = d;
        _render();
    }

    function init(slotEl) {
        _slotEl = slotEl;
        _ensureStyle();
        _render();
        _fetchOnce();
        // Poll fallback every 5s
        _pollTimer = setInterval(_fetchOnce, 5000);
        // Live updates
        if (window._sio) {
            const handler = (d) => _onSocketUpdate(d);
            window._sio.on('conviction_update', handler);
            _socketUnsub = () => window._sio.off('conviction_update', handler);
        }
    }

    function destroy() {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
        if (_socketUnsub) { try { _socketUnsub(); } catch (_) {} _socketUnsub = null; }
        _slotEl = null; _state = null;
    }

    return { init, destroy };
})();

window.ConvictionPane = ConvictionPane;
