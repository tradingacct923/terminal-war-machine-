/**
 * Layout Manager — TradingView-Style Multi-Pane System
 *
 * Injects maximize/restore controls into each chart zone and manages
 * the layout state machine:
 *   - `split`             → all 3 zones visible (60% / 25% / 15%)
 *   - `maximized:<zone>`  → one zone fills the entire chart area
 *
 * Relies on existing ResizeObserver instances in app.js for automatic
 * canvas and LWC chart resizing — no direct resize calls needed.
 */

(function LayoutManager() {
    'use strict';

    // ═══════════════════════════════════════════════════════════════════════
    // CONFIGURATION
    // ═══════════════════════════════════════════════════════════════════════

    const ZONES = [
        { id: 't-zone-chart',   label: 'CHART',   key: 'chart'   },
        { id: 't-zone-heatmap', label: 'HEATMAP', key: 'heatmap' },
        { id: 't-zone-ladder',  label: 'LADDER',  key: 'ladder'  },
    ];

    const LS_KEY = 'altaris_layout_state';

    // ═══════════════════════════════════════════════════════════════════════
    // STATE
    // ═══════════════════════════════════════════════════════════════════════

    let _currentMode = 'split';       // 'split' | 'maximized:chart' | 'maximized:heatmap' | 'maximized:ladder'
    let _initialized = false;

    // ═══════════════════════════════════════════════════════════════════════
    // INIT — called on DOMContentLoaded
    // ═══════════════════════════════════════════════════════════════════════

    function init() {
        if (_initialized) return;
        _initialized = true;

        const chartArea = document.getElementById('t-chart');
        if (!chartArea) return;

        // Inject pane headers into each zone
        for (const zone of ZONES) {
            const el = document.getElementById(zone.id);
            if (!el) continue;

            // Create the header bar
            const header = document.createElement('div');
            header.className = 'pane-header';
            header.setAttribute('data-zone', zone.key);

            // Zone label
            const label = document.createElement('span');
            label.className = 'pane-header-label';
            label.textContent = zone.label;

            // Maximize / Restore button
            const btn = document.createElement('button');
            btn.className = 'pane-maximize-btn';
            btn.setAttribute('data-zone', zone.key);
            btn.setAttribute('title', 'Maximize');
            btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="12" height="12" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M5 1V5H1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity="0"/></svg>';
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleMaximize(zone.key);
            });

            header.appendChild(label);
            header.appendChild(btn);

            // Insert header as first child of the zone
            el.insertBefore(header, el.firstChild);
        }

        // Keyboard shortcut: Escape to restore
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && _currentMode !== 'split') {
                restoreLayout();
            }
        });

        // Double-click on header to toggle maximize
        document.querySelectorAll('.pane-header').forEach(header => {
            header.addEventListener('dblclick', () => {
                const zoneKey = header.getAttribute('data-zone');
                if (zoneKey) toggleMaximize(zoneKey);
            });
        });

        // Restore saved layout state
        const saved = _loadState();
        if (saved && saved !== 'split') {
            const zoneKey = saved.replace('maximized:', '');
            if (ZONES.some(z => z.key === zoneKey)) {
                // Delay to let app.js initialize first
                requestAnimationFrame(() => {
                    maximizeZone(zoneKey);
                });
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // LAYOUT TRANSITIONS
    // ═══════════════════════════════════════════════════════════════════════

    function toggleMaximize(zoneKey) {
        if (_currentMode === `maximized:${zoneKey}`) {
            restoreLayout();
        } else {
            maximizeZone(zoneKey);
        }
    }

    function maximizeZone(zoneKey) {
        const chartArea = document.getElementById('t-chart');
        if (!chartArea) return;

        _currentMode = `maximized:${zoneKey}`;
        _saveState(_currentMode);

        // Add maximized class to the chart area
        chartArea.classList.add('pane-layout-maximized');
        chartArea.setAttribute('data-maximized', zoneKey);

        for (const zone of ZONES) {
            const el = document.getElementById(zone.id);
            if (!el) continue;

            if (zone.key === zoneKey) {
                // This is the maximized zone
                el.classList.add('pane-zone-maximized');
                el.classList.remove('pane-zone-hidden');
                // Update button to restore icon
                const btn = el.querySelector('.pane-maximize-btn');
                if (btn) {
                    btn.setAttribute('title', 'Restore');
                    btn.classList.add('is-maximized');
                }
            } else {
                // Hide this zone
                el.classList.add('pane-zone-hidden');
                el.classList.remove('pane-zone-maximized');
            }
        }

        // Fire resize so ResizeObservers pick up the new dimensions
        _triggerResize();
    }

    function restoreLayout() {
        const chartArea = document.getElementById('t-chart');
        if (!chartArea) return;

        _currentMode = 'split';
        _saveState(_currentMode);

        chartArea.classList.remove('pane-layout-maximized');
        chartArea.removeAttribute('data-maximized');

        for (const zone of ZONES) {
            const el = document.getElementById(zone.id);
            if (!el) continue;

            el.classList.remove('pane-zone-maximized', 'pane-zone-hidden');

            const btn = el.querySelector('.pane-maximize-btn');
            if (btn) {
                btn.setAttribute('title', 'Maximize');
                btn.classList.remove('is-maximized');
            }
        }

        // Fire resize
        _triggerResize();
    }

    // ═══════════════════════════════════════════════════════════════════════
    // HELPERS
    // ═══════════════════════════════════════════════════════════════════════

    function _triggerResize() {
        // Give the browser one frame to apply CSS changes, then fire resize
        requestAnimationFrame(() => {
            window.dispatchEvent(new Event('resize'));
            // Also directly poke ResizeObservers by forcing a reflow
            for (const zone of ZONES) {
                const el = document.getElementById(zone.id);
                if (el) {
                    // Force layout recalculation
                    void el.offsetHeight;
                }
            }
        });
    }

    function _saveState(mode) {
        try { localStorage.setItem(LS_KEY, mode); } catch (e) { /* ignore */ }
    }

    function _loadState() {
        try { return localStorage.getItem(LS_KEY) || 'split'; } catch (e) { return 'split'; }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // EXPOSE API
    // ═══════════════════════════════════════════════════════════════════════

    window.LayoutManager = {
        init,
        maximize: maximizeZone,
        restore: restoreLayout,
        toggle: toggleMaximize,
        getMode: () => _currentMode,
    };

    // Auto-init when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        // DOM already loaded (script loaded late)
        init();
    }

})();
