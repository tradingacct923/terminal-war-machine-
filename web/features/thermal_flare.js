// ═══════════════════════════════════════════════════════════════════════════════
// Thermal Flare — Options GEX/DEX heatmap overlay
// ═══════════════════════════════════════════════════════════════════════════════

(function() {
    'use strict';

    // ── Private State ──────────────────────────────────────────────────────────
    // Support multiple panes via an array of instances
    const _instances = []; // Array of { container, canvas, ctx, series }
    let _data = [];          // dex_profile array from zone_update
    let _visible = true;
    let _sensitivity = 1.0;  // Flare length multiplier
    let _threshold = 1.0;    // Minimum σ to render a flare

    // ── Canvas Initialization ──────────────────────────────────────────────────
    function _initCanvas(container, chartH) {
        if (!container) return;
        // Avoid duplicate initialization for the same container
        if (_instances.find(inst => inst.container === container)) return;

        const canvas = document.createElement('canvas');
        canvas.className = 'tf-canvas';
        canvas.style.position = 'absolute';
        canvas.style.top = '0';
        canvas.style.right = '0';
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.pointerEvents = 'none';
        canvas.style.zIndex = '5';

        const dpr = window.devicePixelRatio || 1;
        canvas.width = (container.clientWidth || 900) * dpr;
        canvas.height = (chartH || container.clientHeight || 700) * dpr;
        
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);

        if (container.firstChild) {
            container.insertBefore(canvas, container.firstChild.nextSibling);
        } else {
            container.appendChild(canvas);
        }

        _instances.push({ container, canvas, ctx, series: null });
    }

    // ── Core Render ────────────────────────────────────────────────────────────
    let _renderScheduled = false;
    function _scheduleRender() {
        if (_renderScheduled) return;
        _renderScheduled = true;
        requestAnimationFrame(() => { _renderScheduled = false; _render(); });
    }

    function _render() {
        if (!_visible || !_data.length) return;
        if (window._chartScrolling) return; // skip during scroll

        // Render flares on ALL active instances (per-pane toggleable)
        for (const inst of _instances) {
            if (!inst.canvas || !inst.ctx || !inst.series) continue;
            // Per-pane toggle check
            if (inst.container && inst.container._overlayConfig && !inst.container._overlayConfig.flare) {
                const dpr = window.devicePixelRatio || 1;
                inst.ctx.clearRect(0, 0, inst.canvas.width / dpr, inst.canvas.height / dpr);
                continue;
            }

            const dpr = window.devicePixelRatio || 1;
            const width = inst.canvas.width / dpr;
            const height = inst.canvas.height / dpr;

            inst.ctx.clearRect(0, 0, width, height);

            // Use cached price scale width (measured once, not per frame)
            // Re-measure every 2 seconds at most to handle resize
            const now = performance.now();
            if (!inst._psWidth || now - (inst._psWidthTs || 0) > 2000) {
                try {
                    if (inst.container) {
                        const tds = inst.container.querySelectorAll('tr:first-child > td');
                        inst._psWidth = tds.length > 1 ? (tds[tds.length - 1].offsetWidth || 55) : 55;
                    } else { inst._psWidth = 55; }
                } catch(e) { inst._psWidth = 55; }
                inst._psWidthTs = now;
            }
            const rightEdge = width - inst._psWidth;

            // Find z-score range for normalization
            const zValues = _data.map(d => d.z || 0).filter(z => z >= _threshold);
            if (zValues.length === 0) continue;
            const maxZ = Math.max(...zValues);
            const minZ = _threshold;
            const zRange = maxZ - minZ;

            let rendered = 0;
            for (let i = 0; i < _data.length; i++) {
                const item = _data[i];
                const z = item.z || 0;
                if (z < _threshold) continue;

                // CRITICAL: use this instance's series to translate price to Y coordinate
                const y = inst.series.priceToCoordinate(item.price);
                if (y === null || y < -30 || y > height + 30) continue;

                const isResistance = item.dex > 0;
                const t = zRange > 0 ? (z - minZ) / zRange : 1.0;
                const flareLength = t * rightEdge * _sensitivity;
                if (flareLength < 1) continue;

                const alpha = 0.1 + t * 0.9;
                const thickness = 2 + t * 18;

                const gradient = inst.ctx.createLinearGradient(rightEdge, y, rightEdge - flareLength, y);
                if (isResistance) {
                    gradient.addColorStop(0, `rgba(255, 60, 90, ${alpha})`);
                    gradient.addColorStop(1, 'rgba(230, 0, 30, 0.0)');
                } else {
                    gradient.addColorStop(0, `rgba(50, 255, 150, ${alpha})`);
                    gradient.addColorStop(1, 'rgba(0, 200, 80, 0.0)');
                }

                // Outer glow: thicker stroke with lower alpha (no GPU filter)
                inst.ctx.beginPath();
                inst.ctx.moveTo(rightEdge, y);
                inst.ctx.lineTo(rightEdge - flareLength, y);
                inst.ctx.lineWidth = thickness + t * 8;
                inst.ctx.lineCap = 'round';
                inst.ctx.strokeStyle = gradient;
                inst.ctx.globalAlpha = 0.4 + t * 0.3;
                inst.ctx.stroke();
                inst.ctx.globalAlpha = 1;

                // Core line: sharp bright center
                inst.ctx.beginPath();
                inst.ctx.moveTo(rightEdge, y);
                inst.ctx.lineTo(rightEdge - flareLength, y);
                inst.ctx.lineWidth = thickness * 0.5;
                inst.ctx.strokeStyle = gradient;
                inst.ctx.stroke();

                const coreR = isResistance ? 255 : (200 + Math.round(t * 55));
                const coreG = isResistance ? (200 + Math.round(t * 55)) : 255;
                const coreB = isResistance ? (220 + Math.round(t * 35)) : (230 + Math.round(t * 25));
                inst.ctx.beginPath();
                inst.ctx.moveTo(rightEdge, y);
                inst.ctx.lineTo(rightEdge - (flareLength * t), y);
                inst.ctx.lineWidth = 1 + t * 2;
                inst.ctx.strokeStyle = `rgba(${coreR}, ${coreG}, ${coreB}, ${alpha})`;
                inst.ctx.stroke();
                rendered++;
            }
            if (rendered === 0 && window.location.port !== '3000') {
               // Silence spam but keep it in mind
            }
        }
    }

    // ── Debug: Mock Data Injection ─────────────────────────────────────────────
    function _debugInject() {
        if (_instances.length === 0) { console.error('[ThermalFlare] No instances initialized'); return; }
        const inst = _instances[0]; // grab first instance to get a reference price
        if (!inst.series) { console.error('[ThermalFlare] No series — chart not initialized'); return; }
        
        const centerPrice = inst.series.coordinateToPrice(inst.canvas.clientHeight / 2) || 500;
        const mockData = [];
        for (let i = 0; i < 20; i++) {
            const price = centerPrice + (Math.random() - 0.5) * 50;
            const dex = (Math.random() - 0.5) * 1000000;
            const z = parseFloat((1 + Math.random() * 3).toFixed(2));
            mockData.push({ price, dex, gex: dex * 0.1, z });
        }
        _data = mockData;
        _scheduleRender();
        console.log('[ThermalFlare] Debug data injected:', mockData);
    }

    // ── Settings Panel ─────────────────────────────────────────────────────────
    let _settingsTimeout = null;
    function _openSettings() {
        let pnl = document.getElementById('tf-settings-panel');
        if (pnl) { pnl.remove(); return; }
        if (window.closeAllSettingsPanels) window.closeAllSettingsPanels('tf-settings-panel');

        pnl = document.createElement('div');
        pnl.id = 'tf-settings-panel';
        pnl.style.position = 'absolute';
        pnl.style.right = '20px';
        pnl.style.top = '45px';
        pnl.style.background = '#1a1f2c';
        pnl.style.border = '1px solid #334';
        pnl.style.borderRadius = '6px';
        pnl.style.padding = '14px 16px';
        pnl.style.zIndex = '9999';
        pnl.style.color = '#fff';
        pnl.style.fontFamily = 'system-ui, sans-serif';
        pnl.style.boxShadow = '0 8px 24px rgba(0,0,0,0.5)';
        pnl.style.minWidth = '220px';
        pnl.innerHTML = `
            <div style="font-weight:600;margin-bottom:12px;font-size:13px;color:#aab">Thermal Flare</div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;margin-bottom:10px;cursor:pointer">
                <input type="checkbox" id="tf-toggle-chk" ${_visible ? 'checked' : ''}> Enable Heatmap
            </label>
            <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;margin-bottom:10px;">
                <span>Flare Scale: <strong id="tf-sens-val">${_sensitivity.toFixed(1)}x</strong></span>
                <input type="range" id="tf-sens-range" min="0.1" max="5" step="0.1" value="${_sensitivity}" style="accent-color:#1fd17a">
            </label>
            <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;margin-bottom:6px;">
                <span>Min. Deviation: <strong id="tf-thresh-val">${_threshold.toFixed(1)}σ</strong></span>
                <input type="range" id="tf-thresh-range" min="0.5" max="4" step="0.1" value="${_threshold}" style="accent-color:#e03060">
                <span style="font-size:10px;color:#667;margin-top:2px">Low = show more levels · High = only monster walls</span>
            </label>
        `;
        document.body.appendChild(pnl);

        document.getElementById('tf-toggle-chk').addEventListener('change', (ev) => {
            _visible = ev.target.checked;
            if (!_visible) {
                const dpr = window.devicePixelRatio || 1;
                for (const inst of _instances) {
                    if (inst.ctx) inst.ctx.clearRect(0, 0, inst.canvas.width / dpr, inst.canvas.height / dpr);
                }
            }
            _scheduleRender();
        });
        document.getElementById('tf-sens-range').addEventListener('input', (ev) => {
            _sensitivity = parseFloat(ev.target.value);
            document.getElementById('tf-sens-val').textContent = _sensitivity.toFixed(1) + 'x';
            _scheduleRender();
        });
        document.getElementById('tf-thresh-range').addEventListener('input', (ev) => {
            _threshold = parseFloat(ev.target.value);
            document.getElementById('tf-thresh-val').textContent = _threshold.toFixed(1) + 'σ';
            _scheduleRender();
        });

        const settingsBtn = document.getElementById('tf-settings-btn');
        setTimeout(() => {
            const closeHandler = (ce) => {
                if (pnl && !pnl.contains(ce.target) && (!settingsBtn || !settingsBtn.contains(ce.target))) {
                    pnl.remove();
                    document.removeEventListener('click', closeHandler);
                }
            };
            document.addEventListener('click', closeHandler);
        }, 50);
    }

    function _destroy() {
        for (const inst of _instances) {
            if (inst.canvas && inst.canvas.parentElement) {
                inst.canvas.parentElement.removeChild(inst.canvas);
            }
        }
        _instances.length = 0;
        let pnl = document.getElementById('tf-settings-panel');
        if (pnl) pnl.remove();
    }

    // ── Event Bus Integration ──────────────────────────────────────────────────
    if (typeof AltarisEvents !== 'undefined') {
        AltarisEvents.on('chart:ready', ({ chart, series, container, chartH }) => {
            // Register container if not yet added
            _initCanvas(container, chartH);
            
            // Attach series to the instance that matches this container
            window.ThermalFlare.attachToSeries(series, container);
            
            // Subscribe so flares re-render on every scroll/zoom
            if (chart && chart.timeScale) {
                chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
                    _scheduleRender();
                });
            }
        });

        // Re-render on generic chart pan / zoom event
        AltarisEvents.on('chart:scroll', () => _scheduleRender());

        // CRITICAL FIX: Only resize the instance that matches the resized container!
        AltarisEvents.on('chart:resize', ({ width, height, container }) => {
            const inst = _instances.find(i => i.container === container);
            if (inst && inst.canvas) {
                const dpr = window.devicePixelRatio || 1;
                inst.canvas.width  = width  * dpr;
                inst.canvas.height = height * dpr;
                inst.ctx = inst.canvas.getContext('2d');
                inst.ctx.scale(dpr, dpr);
                _scheduleRender();
            }
        });
    }

    // ── Public API ─────────────────────────────────────────────────────────────
    window.ThermalFlare = {
        init(container, chartH) { _initCanvas(container, chartH); },
        
        attachToSeries(series, container) {
            // Find specific instance if container is provided, else use last created
            let inst;
            if (container) {
                inst = _instances.find(i => i.container === container);
            } else {
                inst = _instances.find(i => !i.series);
            }
            if (inst) {
                inst.series = series;
            } else {
                // Store globally just in case layout engine does something weird
                // but this shouldn't happen with the new multi-instance refactor
                console.warn('[ThermalFlare] attachToSeries called but no matching instance found for container', container);
            }
        },

        updateData(dexProfile) {
            _data = dexProfile;
            _scheduleRender();
        },

        render() { _render(); },
        openSettings() { _openSettings(); },
        destroy() { _destroy(); },
        debugInject() { _debugInject(); },
        isVisible() { return _visible; },
        getInstances() { return _instances; },
    };
})();
