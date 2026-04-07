(function() {
    'use strict';

    let _currentSymbol = 'NQ'; // default
    let _chartInstance = null;
    let _candleSeries = null;
    let _container = null;
    let _overlay = null;
    
    // Internal state of drawings: Array of objects
    // Horizontal Line: { id, type: 'hline', price, color }
    // Vertical Line:   { id, type: 'vline', time, color }
    // Square/Box:      { id, type: 'box', t1, p1, t2, p2, color }
    let _drawings = [];
    
    // UI State
    let _mode = 'idle'; // 'idle', 'draw_hline', 'draw_vline', 'draw_box_start', 'draw_box_end', 'delete'
    let _activeColor = '#E0A800'; // Default gold
    let _selectedId = null;
    
    // Drag & Drop State
    let _dragState = null; 

    // Temporary variables for 2-click tools (Box)
    let _tempPoint = null;

    // Track active DOM elements for overlay shapes
    let _overlayElements = new Map();
    // Track active LightweightCharts PriceLines
    let _priceLineObjects = new Map();

    function _saveDrawings() {
        if (!_currentSymbol) return;
        localStorage.setItem(`altaris-drawings-${_currentSymbol}`, JSON.stringify(_drawings));
    }

    function _loadDrawings() {
        if (!_currentSymbol) return;
        _clearAll(); 
        _drawings = [];
        const saved = localStorage.getItem(`altaris-drawings-${_currentSymbol}`);
        if (saved) {
            try {
                _drawings = JSON.parse(saved);
                _drawings.forEach(d => _addDrawingInternal(d));
            } catch (e) {
                console.error('[DrawingTools] Error parsing saved drawings', e);
                _drawings = [];
            }
        }
    }

    function _getChartCoordinatesRect() {
        if (!_container) return null;
        return _container.getBoundingClientRect();
    }

    // ─── Time/Coordinate Interpolation (Future Extrapolation Support) ───

    function _coordToTime(x) {
        if (!_chartInstance) return null;
        const ts = _chartInstance.timeScale();
        // Safe fallback for logicalToTime when not available
        function _logicalToTimeSafe(logical) {
            if (typeof ts.logicalToTime === 'function') {
                return ts.logicalToTime(logical);
            }
            const logicalRange = ts.getVisibleLogicalRange();
            const timeRange = ts.getVisibleTimeRange ? ts.getVisibleTimeRange() : null;
            if (!logicalRange || !timeRange) return null;
            const {from: lFrom, to: lTo} = logicalRange;
            const {from: tFrom, to: tTo} = timeRange;
            const proportion = (logical - lFrom) / (lTo - lFrom);
            return tFrom + proportion * (tTo - tFrom);
        }
        let t = ts.coordinateToTime(x);
        if (t !== null) return t; // exact match within bounds

        const log = ts.coordinateToLogical(x);
        if (log === null) return null;

        const r = ts.getVisibleLogicalRange();
        if (!r) return null;

        const maxL = Math.floor(r.to);
        const minL = Math.ceil(r.from);
        
        let tMax = _logicalToTimeSafe(maxL);
        let tMin = _logicalToTimeSafe(minL);

        if (typeof tMax !== 'number' || typeof tMin !== 'number') return null;
        if (maxL <= minL) return null;

        const interval = (tMax - tMin) / (maxL - minL);
        return Math.floor(tMax + (log - maxL) * interval);
    }

    function _timeToCoord(time) {
        if (!_chartInstance) return null;
        const ts = _chartInstance.timeScale(); // MUST be declared before inner helper uses it
        function _logicalToTimeSafe(logical) {
            if (typeof ts.logicalToTime === 'function') {
                return ts.logicalToTime(logical);
            }
            // Fallback: use visible logical and time ranges to approximate
            const logicalRange = ts.getVisibleLogicalRange();
            const timeRange = ts.getVisibleTimeRange ? ts.getVisibleTimeRange() : null;
            if (!logicalRange || !timeRange) return null;
            const {from: lFrom, to: lTo} = logicalRange;
            const {from: tFrom, to: tTo} = timeRange;
            const proportion = (logical - lFrom) / (lTo - lFrom);
            return tFrom + proportion * (tTo - tFrom);
        }
        let x = ts.timeToCoordinate(time);
        if (x !== null) return x; // exact match within bounds

        const r = ts.getVisibleLogicalRange();
        if (!r) return null;

        const maxL = Math.floor(r.to);
        const minL = Math.ceil(r.from);
        
        const tMax = _logicalToTimeSafe(maxL);
        const tMin = _logicalToTimeSafe(minL);
        if (tMax === null || tMin === null) return null;
        if (tMax <= tMin) return null;
        const logicalPerTime = (maxL - minL) / (tMax - tMin);
        const targetLog = maxL + (time - tMax) * logicalPerTime;
        return ts.logicalToCoordinate(targetLog);
    }


    function _createOverlayIfNeeded() {
        if (!_container) return;
        if (!_overlay) {
            _overlay = document.createElement('div');
            _overlay.className = 'dt-overlay';
            _overlay.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:9;overflow:hidden;';
            _container.appendChild(_overlay);

            // Subscribe to chart movements to redraw HTML overlays
            _chartInstance.timeScale().subscribeVisibleLogicalRangeChange(() => _updateOverlayCoordinates());
            _chartInstance.timeScale().subscribeVisibleTimeRangeChange(() => _updateOverlayCoordinates());
            _chartInstance.subscribeCrosshairMove(() => _updateOverlayCoordinates());

            // Global Mouse Events for Drag and Drop
            _overlay.addEventListener('mousedown', _onMouseDown);
            window.addEventListener('mousemove', _onMouseMove);
            window.addEventListener('mouseup', _onMouseUp);
        }
    }

    // ─── Interaction & Drag Engine ───

    function _onMouseDown(e) {
        if (!e.target) return;

        const isHandle = e.target.classList.contains('dt-handle');
        const isBody = e.target.classList.contains('dt-box') || e.target.classList.contains('dt-vline');
        
        if (!isHandle && !isBody) {
            _selectedId = null;
            _updateOverlayCoordinates();
            return;
        }

        e.stopPropagation();
        e.preventDefault();

        const id = e.target.dataset.id;
        const drawing = _drawings.find(d => d.id === id);
        if (!drawing) return;

        _selectedId = id;
        
        // Disable chart native panning during drag
        _chartInstance.applyOptions({ handleScroll: { mouseWheel: false, pressedMouseMove: false }});

        const rect = _getChartCoordinatesRect();
        const startX = e.clientX - rect.left;
        const startY = e.clientY - rect.top;

        _dragState = {
            id: id,
            targetHandle: isHandle ? e.target.dataset.dir : 'body',
            startX,
            startY,
            init: JSON.parse(JSON.stringify(drawing)) // deep copy
        };

        _updateOverlayCoordinates(); // show handles if not already
    }

    function _onMouseMove(e) {
        if (!_dragState) return;
        e.preventDefault(); // stop text selection

        const rect = _getChartCoordinatesRect();
        const rawX = e.clientX - rect.left;
        const rawY = e.clientY - rect.top;
        
        const deltaX = rawX - _dragState.startX;
        const deltaY = rawY - _dragState.startY;

        const d = _drawings.find(x => x.id === _dragState.id);
        if (!d) return;
        
        if (d.type === 'box') {
            if (_dragState.targetHandle === 'body') {
                const initL = _timeToCoord(_dragState.init.t1);
                const initR = _timeToCoord(_dragState.init.t2);
                const initT = _candleSeries.priceToCoordinate(_dragState.init.p1);
                const initB = _candleSeries.priceToCoordinate(_dragState.init.p2);

                if (initL !== null && initR !== null) {
                    const newT1 = _coordToTime(initL + deltaX);
                    const newT2 = _coordToTime(initR + deltaX);
                    if (newT1 && newT2) { d.t1 = newT1; d.t2 = newT2; }
                }
                if (initT !== null && initB !== null) {
                    const newP1 = _candleSeries.coordinateToPrice(initT + deltaY);
                    const newP2 = _candleSeries.coordinateToPrice(initB + deltaY);
                    if (newP1 && newP2) { d.p1 = newP1; d.p2 = newP2; }
                }

            } else {
                // Corner / Edge Resizing
                const dir = _dragState.targetHandle;
                
                const newTime = _coordToTime(rawX);
                const newPrice = _candleSeries.coordinateToPrice(rawY);

                const t1 = _timeToCoord(_dragState.init.t1);
                const t2 = _timeToCoord(_dragState.init.t2);
                const p1 = _candleSeries.priceToCoordinate(_dragState.init.p1);
                const p2 = _candleSeries.priceToCoordinate(_dragState.init.p2);
                if (t1===null || t2===null || p1===null || p2===null) return;

                const leftIsT1 = t1 < t2;
                const topIsP1 = p1 < p2;

                if (newTime !== null) {
                    if (dir.includes('w')) {
                        if (leftIsT1) d.t1 = newTime; else d.t2 = newTime;
                    }
                    if (dir.includes('e')) {
                        if (!leftIsT1) d.t1 = newTime; else d.t2 = newTime;
                    }
                }

                if (newPrice !== null) { // allow 0
                    if (dir.includes('n')) {
                        if (topIsP1) d.p1 = newPrice; else d.p2 = newPrice;
                    }
                    if (dir.includes('s')) {
                        if (!topIsP1) d.p1 = newPrice; else d.p2 = newPrice;
                    }
                }
            }
        } else if (d.type === 'vline') {
            if (_dragState.targetHandle === 'body') {
                const initX = _timeToCoord(_dragState.init.time);
                if (initX !== null) {
                    const newTime = _coordToTime(initX + deltaX);
                    if (newTime !== null) d.time = newTime;
                }
            }
        }

        _updateOverlayCoordinates();
    }

    function _onMouseUp(e) {
        if (!_dragState) return;
        _dragState = null;
        
        // Re-enable native chart scrolling
        _chartInstance.applyOptions({ handleScroll: { mouseWheel: true, pressedMouseMove: true }});
        
        _saveDrawings();
        _updateOverlayCoordinates();
    }

    // ─── Rendering ───

    function _addDrawingInternal(d) {
        if (!_candleSeries) return;

        if (d.type === 'hline') {
            const line = _candleSeries.createPriceLine({
                price: d.price,
                color: d.color,
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Dotted,
                axisLabelVisible: true,
                title: 'Level',
            });
            _priceLineObjects.set(d.id, line);
        } else if (d.type === 'vline') {
            _createOverlayIfNeeded();
            const el = document.createElement('div');
            el.className = 'dt-vline';
            el.style.cssText = `position:absolute;top:0;bottom:0;width:4px;background:transparent;border-left:2px dashed ${d.color};transform:translateX(-2px);pointer-events:auto;cursor:ew-resize;`;
            el.dataset.id = d.id;
            _overlay.appendChild(el);
            _overlayElements.set(d.id, el);
            _updateOverlayCoordinates();
        } else if (d.type === 'box') {
            _createOverlayIfNeeded();
            const el = document.createElement('div');
            el.className = 'dt-box';
            
            el.style.cssText = `
                position:absolute;
                background:${d.color}22; /* 22 hex = 13% opacity */
                border:1px solid ${d.color};
                pointer-events:auto;
                cursor:move;
                box-sizing:border-box;
            `;
            el.dataset.id = d.id;

            // Generate 8 handles
            const handles = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
            handles.forEach(dir => {
                const handle = document.createElement('div');
                handle.className = `dt-handle ${dir}`;
                handle.dataset.id = d.id;
                handle.dataset.dir = dir;
                handle.style.cssText = `
                    position:absolute; width:8px; height:8px; border-radius:50%;
                    border:2px solid #2962FF; background:#000; display:none; pointer-events:auto;
                    transform:translate(-50%, -50%);
                `;
                el.appendChild(handle);
            });

            _overlay.appendChild(el);
            _overlayElements.set(d.id, el);
            _updateOverlayCoordinates();
        }
    }

    function _updateOverlayCoordinates() {
        if (!_chartInstance || !_overlayElements.size) return;
        
        _overlayElements.forEach((el, id) => {
            const d = _drawings.find(x => x.id === id);
            if (!d) return;

            const isSelected = (_selectedId === id);

            if (d.type === 'vline') {
                const x = _timeToCoord(d.time);
                if (x !== null) {
                    el.style.left = x + 'px';
                    el.style.display = 'block';
                    el.style.borderLeftColor = isSelected ? '#2962FF' : d.color;
                } else {
                    el.style.display = 'none';
                }
            } else if (d.type === 'box') {
                const x1 = _timeToCoord(d.t1);
                const x2 = _timeToCoord(d.t2);
                const y1 = _candleSeries.priceToCoordinate(d.p1);
                const y2 = _candleSeries.priceToCoordinate(d.p2);

                if (x1 !== null && x2 !== null && y1 !== null && y2 !== null) {
                    const left = Math.min(x1, x2);
                    const width = Math.abs(x2 - x1);
                    const top = Math.min(y1, y2);
                    const height = Math.abs(y2 - y1);
                    
                    el.style.left = left + 'px';
                    el.style.width = width + 'px';
                    el.style.top = top + 'px';
                    el.style.height = height + 'px';
                    el.style.display = 'block';

                    if (isSelected) {
                        el.style.borderColor = '#2962FF';
                        el.style.zIndex = '10';
                        // Update handles
                        el.querySelectorAll('.dt-handle').forEach(h => {
                            h.style.display = 'block';
                            const dir = h.dataset.dir;
                            if (dir==='nw') { h.style.left='0'; h.style.top='0'; h.style.cursor='nwse-resize'; }
                            if (dir==='n')  { h.style.left='50%'; h.style.top='0'; h.style.cursor='ns-resize';}
                            if (dir==='ne') { h.style.left='100%'; h.style.top='0'; h.style.cursor='nesw-resize';}
                            if (dir==='e')  { h.style.left='100%'; h.style.top='50%'; h.style.cursor='ew-resize';}
                            if (dir==='se') { h.style.left='100%'; h.style.top='100%'; h.style.cursor='nwse-resize';}
                            if (dir==='s')  { h.style.left='50%'; h.style.top='100%'; h.style.cursor='ns-resize';}
                            if (dir==='sw') { h.style.left='0'; h.style.top='100%'; h.style.cursor='nesw-resize';}
                            if (dir==='w')  { h.style.left='0'; h.style.top='50%'; h.style.cursor='ew-resize';}
                        });
                    } else {
                        el.style.borderColor = d.color;
                        el.style.zIndex = '5';
                        el.querySelectorAll('.dt-handle').forEach(h => h.style.display = 'none');
                    }
                } else {
                    el.style.display = 'none';
                }
            }
        });
    }

    function _clearAll() {
        if (_candleSeries) {
            _priceLineObjects.forEach(line => {
                try { _candleSeries.removePriceLine(line); } catch (e) {}
            });
        }
        _priceLineObjects.clear();

        _overlayElements.forEach(el => {
            if (el.parentNode) el.parentNode.removeChild(el);
        });
        _overlayElements.clear();
    }

    function _addDrawing(data) {
        const id = 'draw_' + Date.now() + Math.floor(Math.random() * 1000);
        data.id = id;
        data.color = _activeColor;
        _drawings.push(data);
        _addDrawingInternal(data);
        _selectedId = id; // auto-select new draws
        _saveDrawings();
        _updateOverlayCoordinates();
    }

    function _removeSelectedOrNearPoint(price, timeParam) {
        if (_selectedId) {
            const idx = _drawings.findIndex(d => d.id === _selectedId);
            if (idx > -1) {
                const d = _drawings[idx];
                if (d.type === 'hline') {
                    const lineObj = _priceLineObjects.get(d.id);
                    if (lineObj) _candleSeries.removePriceLine(lineObj);
                    _priceLineObjects.delete(d.id);
                } else {
                    const el = _overlayElements.get(d.id);
                    if (el && el.parentNode) el.parentNode.removeChild(el);
                    _overlayElements.delete(d.id);
                }
                _drawings.splice(idx, 1);
                _selectedId = null;
                _saveDrawings();
                _updateOverlayCoordinates();
                return;
            }
        }
        
        let closestIdx = -1;
        let minDiff = Infinity;
        const clickY = _candleSeries.priceToCoordinate(price);
        // Use custom wrapper for checking near-point boundary
        const clickX = _timeToCoord(timeParam);
        
        if (clickY === null || clickX === null) return;

        for (let i = 0; i < _drawings.length; i++) {
            const d = _drawings[i];
            
            if (d.type === 'hline') {
                const lineY = _candleSeries.priceToCoordinate(d.price);
                if (lineY !== null) {
                    const dist = Math.abs(clickY - lineY);
                    if (dist < 15 && dist < minDiff) { minDiff = dist; closestIdx = i; }
                }
            } else if (d.type === 'vline') {
                const lineX = _timeToCoord(d.time);
                if (lineX !== null) {
                    const dist = Math.abs(clickX - lineX);
                    if (dist < 15 && dist < minDiff) { minDiff = dist; closestIdx = i; }
                }
            } else if (d.type === 'box') {
                const x1 = _timeToCoord(d.t1);
                const x2 = _timeToCoord(d.t2);
                const y1 = _candleSeries.priceToCoordinate(d.p1);
                const y2 = _candleSeries.priceToCoordinate(d.p2);
                
                if (x1 !== null && x2 !== null && y1 !== null && y2 !== null) {
                    const l = Math.min(x1, x2); const r = Math.max(x1, x2);
                    const t = Math.min(y1, y2); const b = Math.max(y1, y2);
                    if (clickX >= l && clickX <= r && clickY >= t && clickY <= b) {
                        minDiff = 0; closestIdx = i;
                    }
                }
            }
        }
        
        if (closestIdx > -1) {
            const d = _drawings[closestIdx];
            if (d.type === 'hline') {
                const lineObj = _priceLineObjects.get(d.id);
                if (lineObj) _candleSeries.removePriceLine(lineObj);
                _priceLineObjects.delete(d.id);
            } else {
                const el = _overlayElements.get(d.id);
                if (el && el.parentNode) el.parentNode.removeChild(el);
                _overlayElements.delete(d.id);
            }
            _drawings.splice(closestIdx, 1);
            _saveDrawings();
            _updateOverlayCoordinates();
        }
    }

    function _updateUIClasses() {
        document.querySelectorAll('.dt-btn').forEach(btn => {
            const isBaseMode = _mode.startsWith('draw_box') ? btn.dataset.mode === 'draw_box' : btn.dataset.mode === _mode;
            btn.classList.toggle('active', isBaseMode);
        });
        const container = document.getElementById('drawing-tools-bar');
        if (container) {
            if (_mode !== 'idle') {
                container.classList.add('dt-active');
            } else {
                container.classList.remove('dt-active');
            }
            
            document.querySelectorAll('.dt-color-btn').forEach(btn => {
                const isDraw = _mode.startsWith('draw') || _selectedId !== null;
                if (_selectedId && _mode === 'idle') {
                    // Update active color indicator based on selected item
                    const sel = _drawings.find(d => d.id === _selectedId);
                    if (sel) _activeColor = sel.color;
                }
                btn.style.borderColor = (isDraw && btn.dataset.color === _activeColor) ? '#fff' : 'transparent';
                btn.style.opacity = isDraw ? '1' : '0.4';
            });
        }
    }

    function _createToolbar() {
        if (document.getElementById('drawing-tools-bar')) return;
        const bar = document.createElement('div');
        bar.id = 'drawing-tools-bar';
        bar.className = 'drawing-tools-bar';
        bar.innerHTML = `
            <div class="dt-group">
                <button class="dt-btn" data-mode="draw_hline" title="Horizontal Support/Resistance">── H</button>
                <button class="dt-btn" data-mode="draw_vline" title="Vertical Timeline">│ V</button>
                <button class="dt-btn" data-mode="draw_box" title="Square Zone (Click twice)">■ Box</button>
                <div class="dt-colors">
                    <div class="dt-color-btn" data-color="#E0A800" style="background:#E0A800"></div>
                    <div class="dt-color-btn" data-color="#26A69A" style="background:#26A69A"></div>
                    <div class="dt-color-btn" data-color="#EF5350" style="background:#EF5350"></div>
                    <div class="dt-color-btn" data-color="#7C5AF7" style="background:#7C5AF7"></div>
                </div>
            </div>
            <div class="dt-separator"></div>
            <button class="dt-btn" data-mode="delete" title="Delete a Shape (Click shape to Delete)">✕ Del</button>
            <button class="dt-btn" data-mode="clear" title="Clear All Lines">🗑 Clear</button>
        `;
        document.body.appendChild(bar);

        bar.querySelectorAll('.dt-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const mode = e.target.closest('.dt-btn').dataset.mode;
                if (mode === 'clear') {
                    if (confirm('Clear all drawings for ' + _currentSymbol + '?')) {
                        _drawings = [];
                        _clearAll();
                        _saveDrawings();
                    }
                } else if (mode === _mode || (mode === 'draw_box' && _mode.startsWith('draw_box'))) {
                    _mode = 'idle';
                    _tempPoint = null;
                } else {
                    _mode = mode === 'draw_box' ? 'draw_box_start' : mode;
                    _tempPoint = null;
                }
                
                // Clicking DEl while something is selected = immediate delete
                if (mode === 'delete' && _selectedId) {
                    _removeSelectedOrNearPoint(null, null);
                    _mode = 'idle';
                }

                _updateUIClasses();
            });
        });

        bar.querySelectorAll('.dt-color-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                _activeColor = e.target.dataset.color;
                // If a shape is selected, change its color
                if (_selectedId) {
                    const sel = _drawings.find(d => d.id === _selectedId);
                    if (sel) {
                        sel.color = _activeColor;
                        if (sel.type === 'hline') {
                            const l = _priceLineObjects.get(sel.id);
                            l.applyOptions({color: _activeColor});
                        } else if (sel.type === 'box') {
                            const el = _overlayElements.get(sel.id);
                            el.style.backgroundColor = _activeColor + '22';
                        } else if (sel.type === 'vline') {
                            const el = _overlayElements.get(sel.id);
                            if (el) el.style.borderLeftColor = _activeColor;
                        }
                        _saveDrawings();
                    }
                } else if (!_mode.startsWith('draw')) {
                    _mode = 'idle'; // Let them select color without forcing a draw mode
                }
                _updateUIClasses();
            });
        });
    }

    window.DrawingTools = {
        init(chart, candleSeries, symbol, container) {
            _chartInstance = chart;
            _candleSeries = candleSeries;
            _container = container;
            _currentSymbol = symbol || 'NQ';

            _createToolbar();
            _createOverlayIfNeeded(); // Explicitly call to set up mouse listeners early
            _loadDrawings();
            _updateUIClasses();

            // Handle Global Canvas Clicks
            _chartInstance.subscribeClick((param) => {
                if (!param.point || !param.time || _dragState) {
                    _selectedId = null;
                    _updateOverlayCoordinates();
                    _updateUIClasses();
                    return;
                }
                
                const price = _candleSeries.coordinateToPrice(param.point.y);
                if (price === null) return;

                // For future-spaced clicks, map the extrapolated time safely
                let tTarget = param.time;
                if (!tTarget && param.point) {
                    tTarget = _coordToTime(param.point.x) || param.time;
                }

                if (_mode === 'draw_hline') {
                    _selectedId = null;
                    _addDrawing({ type: 'hline', price: price });
                    _mode = 'idle';
                } else if (_mode === 'draw_vline') {
                    _selectedId = null;
                    _addDrawing({ type: 'vline', time: tTarget });
                    _mode = 'idle';
                } else if (_mode === 'draw_box_start') {
                    _selectedId = null;
                    _tempPoint = { t: tTarget, p: price };
                    _mode = 'draw_box_end';
                } else if (_mode === 'draw_box_end') {
                    if (_tempPoint) {
                        _addDrawing({ type: 'box', t1: _tempPoint.t, p1: _tempPoint.p, t2: tTarget, p2: price });
                    }
                    _tempPoint = null;
                    _mode = 'idle';
                } else if (_mode === 'delete') {
                    _removeSelectedOrNearPoint(price, tTarget);
                    _mode = 'idle';
                } else {
                    // Clicked chart in Idle -> deselect
                    _selectedId = null;
                    _updateOverlayCoordinates();
                }
                _updateUIClasses();
            });
        },

        setCurrentSymbol(symbol) {
            if (_currentSymbol === symbol) return;
            _currentSymbol = symbol;
            _loadDrawings();
        }
    };

    document.addEventListener('DOMContentLoaded', () => {
        if (window.AltarisEvents) {
            window.AltarisEvents.on('chart:ready', (data) => {
                if (data.feature === 'chart') {
                    const sym = typeof _l2ChartSymbol !== 'undefined' ? _l2ChartSymbol : 'NQ';
                    DrawingTools.init(data.chart, data.candleSeries, sym, data.container);
                }
            });
        }
    });

})();
