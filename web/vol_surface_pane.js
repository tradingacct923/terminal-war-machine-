/**
 * VolSurfacePane — Real-time IV surface heatmap + vol regime + Greek surface summary
 * Consumes zone_update (vol regime, Greek surface aggregates) and option_mark_update (per-contract IV).
 * Canvas-based 2D heatmap: X=strike, Y=DTE, color=IV.
 */
const VolSurfacePane = (() => {
    'use strict';

    // DTE bucket boundaries and labels
    const DTE_BUCKETS = [
        { max: 0, label: '0DTE' },
        { max: 3, label: '1-3d' },
        { max: 7, label: '4-7d' },
        { max: 14, label: '8-14d' },
        { max: 30, label: '15-30d' },
        { max: Infinity, label: '30d+' },
    ];

    let _container = null;
    let _canvas = null;
    let _ctx = null;
    let _styleEl = null;
    let _ivMap = {};       // { dteBucket: { strike: { iv, oi, side } } }
    let _regime = {};
    let _greeks = {};
    let _dirty = false;
    let _rafId = null;
    let _lastRender = 0;
    let _view3d = false;
    let _3dTimer = null;

    function _injectStyles() {
        if (document.getElementById('volsurf-styles')) return;
        _styleEl = document.createElement('style');
        _styleEl.id = 'volsurf-styles';
        _styleEl.textContent = `
            .vs-wrap { height:100%; display:flex; flex-direction:column; background:#070a14; font-family:'JetBrains Mono','Share Tech Mono',monospace; }
            .vs-top { display:flex; align-items:center; padding:4px 8px; gap:8px; border-bottom:1px solid rgba(255,255,255,.04); flex-wrap:wrap; }
            .vs-title { font-size:9px; font-weight:600; color:rgba(180,190,220,.7); letter-spacing:.8px; text-transform:uppercase; }
            .vs-badge { font-size:8px; font-weight:700; padding:2px 6px; border-radius:3px; letter-spacing:.5px; }
            .vs-badge-normal { background:rgba(31,209,122,.1); color:#1fd17a; border:1px solid rgba(31,209,122,.2); }
            .vs-badge-elevated { background:rgba(255,180,40,.1); color:#ffb428; border:1px solid rgba(255,180,40,.2); }
            .vs-badge-extreme { background:rgba(224,48,96,.1); color:#e03060; border:1px solid rgba(224,48,96,.2); }
            .vs-metric { font-size:8px; color:rgba(140,150,180,.6); }
            .vs-metric-val { font-weight:600; color:rgba(200,210,230,.8); }
            .vs-metric-up { color:#1fd17a; }
            .vs-metric-dn { color:#e03060; }
            .vs-canvas-wrap { flex:1; position:relative; min-height:80px; }
            .vs-canvas { width:100%; height:100%; display:block; }
            .vs-bottom { display:flex; gap:10px; padding:4px 8px; border-top:1px solid rgba(255,255,255,.04); flex-wrap:wrap; }
            .vs-greek { font-size:8px; color:rgba(140,150,180,.5); }
            .vs-greek-label { font-weight:600; letter-spacing:.3px; }
            .vs-greek-val { font-weight:700; margin-left:2px; }
            .vs-vanna { color:rgba(168,85,247,.8); }
            .vs-charm { color:rgba(249,115,22,.8); }
            .vs-speed { color:rgba(255,149,0,.8); }
            .vs-zomma { color:rgba(6,182,212,.8); }
            .vs-confluence { color:rgba(255,48,96,.9); font-weight:700; }
            .vs-ivr-bar { width:60px; height:6px; background:rgba(255,255,255,.05); border-radius:3px; display:inline-block; vertical-align:middle; margin-left:4px; position:relative; }
            .vs-ivr-fill { height:100%; border-radius:3px; transition:width .5s ease; }
            .vs-term { font-size:7px; font-weight:600; padding:1px 4px; border-radius:2px; }
            .vs-term-back { background:rgba(224,48,96,.12); color:#e03060; }
            .vs-term-cont { background:rgba(31,209,122,.12); color:#1fd17a; }
            .vs-term-flat { background:rgba(140,150,180,.08); color:rgba(140,150,180,.5); }
        `;
        document.head.appendChild(_styleEl);
    }

    function init(slotEl) {
        _injectStyles();
        _container = slotEl;
        _ivMap = {};
        _regime = {};
        _greeks = {};
        _dirty = false;

        _container.innerHTML = `
            <div class="vs-wrap">
                <div class="vs-top">
                    <span class="vs-title">IV Surface</span>
                    <span class="vs-badge vs-badge-normal" id="vs-regime">—</span>
                    <span class="vs-metric">IVR: <span class="vs-metric-val" id="vs-ivr">—</span>
                        <span class="vs-ivr-bar"><span class="vs-ivr-fill" id="vs-ivr-fill" style="width:0;background:#7c5af7"></span></span>
                    </span>
                    <span class="vs-metric">V.Prm: <span class="vs-metric-val" id="vs-vprem">—</span></span>
                    <span class="vs-metric">Skew: <span class="vs-metric-val" id="vs-skew">—</span></span>
                    <span id="vs-term" class="vs-term vs-term-flat">—</span>
                    <span class="vs-metric" style="margin-left:auto">ATM IV: <span class="vs-metric-val" id="vs-atm-iv">—</span></span>
                    <button id="vs-3d-toggle" style="margin-left:6px;padding:1px 6px;font-size:8px;font-weight:700;font-family:inherit;background:rgba(100,165,250,.12);color:rgba(100,165,250,.8);border:1px solid rgba(100,165,250,.25);border-radius:3px;cursor:pointer;letter-spacing:.4px">3D</button>
                </div>
                <div class="vs-canvas-wrap">
                    <canvas class="vs-canvas" id="vs-canvas"></canvas>
                    <div id="vs-3d-container" style="display:none;width:100%;height:100%;position:absolute;top:0;left:0"></div>
                </div>
                <div class="vs-bottom" id="vs-greeks">
                    <span class="vs-greek vs-vanna"><span class="vs-greek-label">Vanna:</span> <span class="vs-greek-val" id="vs-vanna">—</span></span>
                    <span class="vs-greek vs-charm"><span class="vs-greek-label">Charm:</span> <span class="vs-greek-val" id="vs-charm">—</span></span>
                    <span class="vs-greek vs-speed"><span class="vs-greek-label">Speed:</span> <span class="vs-greek-val" id="vs-speed">—</span></span>
                    <span class="vs-greek vs-zomma"><span class="vs-greek-label">Zomma:</span> <span class="vs-greek-val" id="vs-zomma">—</span></span>
                    <span class="vs-greek vs-confluence" id="vs-confluence"></span>
                </div>
            </div>`;

        _canvas = _container.querySelector('#vs-canvas');
        _resizeCanvas();
        _ctx = _canvas.getContext('2d');
        _startRenderLoop();

        // 3D toggle button
        const toggleBtn = _container.querySelector('#vs-3d-toggle');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', () => {
                _view3d = !_view3d;
                toggleBtn.style.background = _view3d ? 'rgba(100,165,250,.3)' : 'rgba(100,165,250,.12)';
                toggleBtn.textContent = _view3d ? '2D' : '3D';
                const c = _container.querySelector('#vs-canvas');
                const d = _container.querySelector('#vs-3d-container');
                if (c) c.style.display = _view3d ? 'none' : 'block';
                if (d) d.style.display = _view3d ? 'block' : 'none';
                if (_view3d) _render3D();
            });
        }
    }

    function _render3D() {
        if (!_container) return;
        const el = _container.querySelector('#vs-3d-container');
        if (!el) return;
        const doRender = () => {
            if (typeof authFetch === 'undefined') return;
            authFetch('/api/volatility').then(r => r.json()).then(data => {
                if (!data || data.error || !data.surface?.length) {
                    el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:rgba(140,160,200,.4);font-size:.7rem">No vol surface data</div>';
                    return;
                }
                const spot = data.spot || 0;
                const strikes = data.strikes;
                const x = strikes.map(s => ((s / spot) * 100).toFixed(1));
                const y = data.expirations.map(e => e.dte);
                const z = data.surface.map(e => e.ivs.map(v => {
                    const pct = +(v * 100).toFixed(2);
                    return (pct > 1 && pct <= 75) ? pct : 20;
                }));
                Plotly.react(el, [{
                    type: 'surface', x, y, z,
                    colorscale: [[0,'rgb(10,10,60)'],[0.25,'rgb(30,70,200)'],[0.5,'rgb(60,180,220)'],[0.75,'rgb(255,200,60)'],[1,'rgb(255,60,60)']],
                    opacity: 0.92,
                    contours: { z: { show: true, usecolormap: true, project: { z: false } } },
                    showscale: false,
                }], {
                    paper_bgcolor: '#070a14', plot_bgcolor: '#070a14',
                    margin: { l: 0, r: 0, t: 10, b: 0 },
                    scene: {
                        bgcolor: '#070a14',
                        xaxis: { title: 'Moneyness %', gridcolor: 'rgba(50,70,120,.3)', tickfont: { size: 9, color: '#4a6a9a' } },
                        yaxis: { title: 'DTE', gridcolor: 'rgba(50,70,120,.3)', tickfont: { size: 9, color: '#4a6a9a' } },
                        zaxis: { title: 'IV %', gridcolor: 'rgba(50,70,120,.3)', tickfont: { size: 9, color: '#4a6a9a' } },
                        camera: { eye: { x: -1.5, y: -2.0, z: 0.8 } },
                    },
                    autosize: true,
                }, { displayModeBar: false, responsive: true });
            }).catch(() => {});
        };
        if (typeof window._ensurePlotly === 'function') {
            window._ensurePlotly(doRender);
        } else if (typeof Plotly !== 'undefined') {
            doRender();
        }
        if (_3dTimer) clearInterval(_3dTimer);
        _3dTimer = setInterval(() => { if (_view3d) doRender(); }, 60000);
    }

    function destroy() {
        if (_rafId) cancelAnimationFrame(_rafId);
        if (_3dTimer) clearInterval(_3dTimer);
        _rafId = null;
        _3dTimer = null;
        _view3d = false;
        _container = null;
        _canvas = null;
        _ctx = null;
        _ivMap = {};
        // Purge Plotly if mounted
        const el3d = document.getElementById('vs-3d-container');
        if (el3d && typeof Plotly !== 'undefined') { try { Plotly.purge(el3d); } catch(e) {} }
    }

    function _resizeCanvas() {
        if (!_canvas) return;
        const wrap = _canvas.parentElement;
        if (!wrap) return;
        const rect = wrap.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        const newW = Math.round(rect.width * dpr);
        const newH = Math.round(rect.height * dpr);
        // Only resize if dimensions actually changed (avoids infinite dirty loop)
        if (_canvas.width !== newW || _canvas.height !== newH) {
            _canvas.width = newW;
            _canvas.height = newH;
            _canvas.style.width = rect.width + 'px';
            _canvas.style.height = rect.height + 'px';
            _dirty = true;
        }
    }

    function _dteBucket(dte) {
        for (let i = 0; i < DTE_BUCKETS.length; i++) {
            if (dte <= DTE_BUCKETS[i].max) return i;
        }
        return DTE_BUCKETS.length - 1;
    }

    // IV -> color (cold blue to hot red)
    function _ivColor(iv) {
        // Clamp IV to 10-100 range for color mapping
        const t = Math.max(0, Math.min(1, (iv - 10) / 90));
        const r = Math.round(t < 0.5 ? 0 : (t - 0.5) * 2 * 255);
        const g = Math.round(t < 0.5 ? t * 2 * 180 : (1 - t) * 2 * 180);
        const b = Math.round(t < 0.5 ? 200 - t * 2 * 150 : 50);
        return `rgb(${r},${g},${b})`;
    }

    function _renderHeatmap() {
        if (!_ctx || !_canvas) return;
        const now = performance.now();
        if (now - _lastRender < 1000) return; // 1Hz max
        _lastRender = now;
        _dirty = false;

        const W = _canvas.width;
        const H = _canvas.height;
        const dpr = window.devicePixelRatio || 1;
        _ctx.clearRect(0, 0, W, H);

        // Collect all strikes across all DTE buckets
        const allStrikes = new Set();
        for (const bucket of Object.values(_ivMap)) {
            for (const k of Object.keys(bucket)) allStrikes.add(Number(k));
        }
        const strikes = Array.from(allStrikes).sort((a, b) => a - b);
        if (strikes.length < 2) {
            _ctx.fillStyle = 'rgba(140,150,180,.3)';
            _ctx.font = `${11 * dpr}px JetBrains Mono`;
            _ctx.textAlign = 'center';
            _ctx.fillText('Waiting for IV data...', W / 2, H / 2);
            return;
        }

        const numBuckets = DTE_BUCKETS.length;
        const padL = 45 * dpr, padR = 10 * dpr, padT = 5 * dpr, padB = 20 * dpr;
        const plotW = W - padL - padR;
        const plotH = H - padT - padB;
        const cellW = plotW / strikes.length;
        const cellH = plotH / numBuckets;

        // Draw cells
        for (let bi = 0; bi < numBuckets; bi++) {
            const bucketData = _ivMap[bi] || {};
            for (let si = 0; si < strikes.length; si++) {
                const entry = bucketData[strikes[si]];
                if (!entry) continue;
                const iv = entry.iv;
                if (iv <= 0) continue;

                const x = padL + si * cellW;
                const y = padT + bi * cellH;
                _ctx.fillStyle = _ivColor(iv);
                _ctx.fillRect(x, y, cellW - 1, cellH - 1);

                // Show IV value in cells if they're wide enough
                if (cellW > 28 * dpr && cellH > 12 * dpr) {
                    _ctx.fillStyle = 'rgba(255,255,255,.7)';
                    _ctx.font = `${7 * dpr}px JetBrains Mono`;
                    _ctx.textAlign = 'center';
                    _ctx.fillText(iv.toFixed(0), x + cellW / 2, y + cellH / 2 + 3 * dpr);
                }
            }
        }

        // Y-axis labels (DTE buckets)
        _ctx.fillStyle = 'rgba(140,150,180,.5)';
        _ctx.font = `${8 * dpr}px JetBrains Mono`;
        _ctx.textAlign = 'right';
        for (let bi = 0; bi < numBuckets; bi++) {
            const y = padT + bi * cellH + cellH / 2 + 3 * dpr;
            _ctx.fillText(DTE_BUCKETS[bi].label, padL - 4 * dpr, y);
        }

        // X-axis labels (strikes, every Nth)
        _ctx.textAlign = 'center';
        const step = Math.max(1, Math.floor(strikes.length / 10));
        for (let si = 0; si < strikes.length; si += step) {
            const x = padL + si * cellW + cellW / 2;
            _ctx.fillText(strikes[si].toFixed(0), x, H - 4 * dpr);
        }
    }

    function _startRenderLoop() {
        function loop() {
            if (!_container) return;
            if (_dirty) {
                _dirty = false; // clear before render to avoid re-trigger from _resizeCanvas
                _resizeCanvas();
                _renderHeatmap();
            }
            _rafId = requestAnimationFrame(loop);
        }
        _rafId = requestAnimationFrame(loop);
    }

    function onOptionMark(data) {
        if (!_container) return;
        const strike = data.strike || 0;
        const iv = data.iv || 0;
        const dte = data.dte || 0;
        if (strike <= 0 || iv <= 0) return;

        const bi = _dteBucket(dte);
        if (!_ivMap[bi]) _ivMap[bi] = {};
        _ivMap[bi][strike] = { iv, oi: data.oi || 0, side: data.side || '' };
        _dirty = true;
    }

    function onZoneUpdate(data) {
        if (!_container) return;

        // Vol regime
        const regime = data.vol_regime || 'NORMAL';
        const regimeEl = _container.querySelector('#vs-regime');
        if (regimeEl) {
            regimeEl.textContent = regime;
            regimeEl.className = 'vs-badge ' + (
                regime === 'EXTREME' || regime === 'CRISIS' ? 'vs-badge-extreme' :
                regime === 'ELEVATED' || regime === 'HIGH' ? 'vs-badge-elevated' : 'vs-badge-normal'
            );
        }

        // IV Rank
        const ivr = data.iv_rank || 0;
        const ivrEl = _container.querySelector('#vs-ivr');
        const ivrFill = _container.querySelector('#vs-ivr-fill');
        if (ivrEl) ivrEl.textContent = ivr.toFixed(0);
        if (ivrFill) {
            ivrFill.style.width = Math.min(100, Math.max(0, ivr)) + '%';
            ivrFill.style.background = ivr > 70 ? '#e03060' : ivr > 40 ? '#ffb428' : '#1fd17a';
        }

        // Vol Premium
        const vprem = data.vol_premium || 0;
        const vpEl = _container.querySelector('#vs-vprem');
        if (vpEl) {
            vpEl.textContent = (vprem >= 0 ? '+' : '') + vprem.toFixed(1) + '%';
            vpEl.className = 'vs-metric-val ' + (vprem > 0 ? 'vs-metric-up' : vprem < 0 ? 'vs-metric-dn' : '');
        }

        // IV Skew
        const skew = data.iv_skew || 0;
        const skewEl = _container.querySelector('#vs-skew');
        if (skewEl) skewEl.textContent = skew.toFixed(3);

        // Term Structure
        const term = data.term_structure || 'FLAT';
        const termEl = _container.querySelector('#vs-term');
        if (termEl) {
            termEl.textContent = term;
            termEl.className = 'vs-term ' + (
                term === 'BACKWARDATION' ? 'vs-term-back' :
                term === 'CONTANGO' ? 'vs-term-cont' : 'vs-term-flat'
            );
        }

        // ATM IV
        const atmIv = data.atm_iv || 0;
        const atmEl = _container.querySelector('#vs-atm-iv');
        if (atmEl) atmEl.textContent = atmIv > 0 ? atmIv.toFixed(1) + '%' : '—';

        // Greek surface
        const vannaEl = _container.querySelector('#vs-vanna');
        if (vannaEl) {
            const vw = data.vanna_wall || 0;
            vannaEl.textContent = vw > 0 ? `Wall: ${vw.toFixed(0)}` : '—';
        }

        const charmEl = _container.querySelector('#vs-charm');
        if (charmEl) {
            const cd = data.charm_direction || '';
            const cg = data.charm_gravity || 0;
            charmEl.textContent = cd ? `${cd === 'UP' ? '\u2191' : '\u2193'} ${cd} grav:${cg.toFixed(0)}` : '—';
        }

        const speedEl = _container.querySelector('#vs-speed');
        if (speedEl) {
            const sp = data.speed_at_spot || 0;
            const sign = data.speed_sign || '';
            speedEl.textContent = `${sp.toFixed(2)} ${sign === 'ACCELERATING' ? '\u2191' : '\u2193'}`;
        }

        const zommaEl = _container.querySelector('#vs-zomma');
        if (zommaEl) {
            const zm = data.zomma_at_spot || 0;
            const risk = data.zomma_risk || 'LOW';
            zommaEl.textContent = `${zm.toFixed(2)} ${risk}`;
        }

        // Confluence zones
        const confEl = _container.querySelector('#vs-confluence');
        if (confEl) {
            const zones = data.confluence_zones || [];
            if (zones.length > 0) {
                const top = zones[0];
                const topPrice = top.nq_price ? top.nq_price.toFixed(0) : '?';
                const topTypes = Array.isArray(top.types) ? top.types.join('+') : '—';
                confEl.textContent = `\u2622 ${zones.length} zones | top: ${topPrice} (${topTypes})`;
            } else {
                confEl.textContent = '';
            }
        }
    }

    return { init, destroy, onOptionMark, onZoneUpdate };
})();
window.VolSurfacePane = VolSurfacePane;
