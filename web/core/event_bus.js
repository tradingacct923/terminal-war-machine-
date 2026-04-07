// ═══════════════════════════════════════════════════════════════════════════════
// Altaris Event Bus — Lightweight feature-to-feature communication
// ═══════════════════════════════════════════════════════════════════════════════
// Usage:
//   AltarisEvents.on('chart:ready', ({ chart, series }) => { ... });
//   AltarisEvents.emit('chart:ready', { chart, series });
//   AltarisEvents.off('chart:ready', myHandler);
//
// Event catalog:
//   chart:ready     → { chart, series, container }     ChartCore emits when LWC initialized
//   chart:resize    → { width, height }                ChartCore emits on resize
//   data:zones      → { put_wall, call_wall, ..., dex_profile }  DataFetch emits on zone_update
//   data:l2         → { dom, trades, signals, ... }    DataFetch emits on L2 poll
//   data:candle     → { symbol, tf, time, o, h, l, c } DataFetch emits on candle_update
//   symbol:change   → { symbol, timeframe }            Toolbar emits on switch
//   layout:mount    → { paneIdx, featureKey, slotEl }  LayoutIntegration emits on mount
//   layout:unmount  → { paneIdx, featureKey }          LayoutIntegration emits on unmount
// ═══════════════════════════════════════════════════════════════════════════════

(function() {
    'use strict';

    const _listeners = {};

    window.AltarisEvents = {
        /**
         * Subscribe to an event.
         * @param {string} event - Event name (e.g., 'chart:ready')
         * @param {Function} fn - Callback function
         */
        on(event, fn) {
            if (typeof fn !== 'function') return;
            (_listeners[event] ??= []).push(fn);
        },

        /**
         * Unsubscribe from an event.
         * @param {string} event - Event name
         * @param {Function} fn - The exact function reference passed to on()
         */
        off(event, fn) {
            if (!_listeners[event]) return;
            _listeners[event] = _listeners[event].filter(f => f !== fn);
        },

        /**
         * Emit an event to all subscribers.
         * @param {string} event - Event name
         * @param {*} data - Payload (any type)
         */
        emit(event, data) {
            const fns = _listeners[event];
            if (!fns || fns.length === 0) return;
            for (let i = 0; i < fns.length; i++) {
                try {
                    fns[i](data);
                } catch (err) {
                    console.error(`[AltarisEvents] Error in '${event}' handler:`, err);
                }
            }
        },

        /**
         * Subscribe to an event, but auto-unsubscribe after the first call.
         * @param {string} event - Event name
         * @param {Function} fn - Callback function
         */
        once(event, fn) {
            const wrapper = (data) => {
                window.AltarisEvents.off(event, wrapper);
                fn(data);
            };
            window.AltarisEvents.on(event, wrapper);
        },

        /** Debug: list all registered events and listener counts. */
        debug() {
            const summary = {};
            for (const [evt, fns] of Object.entries(_listeners)) {
                summary[evt] = fns.length;
            }
            console.table(summary);
            return summary;
        },
    };
})();
