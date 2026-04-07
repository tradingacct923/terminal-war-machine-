(function() {
    'use strict';

    window._sio = null;
    let _sioConnected = false;
    let _currentSymbol = 'NQ';
    let _currentTf = '1m';

    window.authFetch = function(url, opts = {}) {
        const tok = sessionStorage.getItem('greeks-auth');
        if (tok) {
            opts.headers = { ...(opts.headers || {}), 'X-Auth-Token': tok };
        }
        return fetch(url, opts).then(res => {
            if (res.status === 401) {
                sessionStorage.removeItem('greeks-auth');
                window.location.href = '/login';
            }
            return res;
        });
    };

    window.DataFetch = {
        initSocket() {
            if (window._sio) return;
            if (typeof io === 'undefined') {
                console.warn('[Socket.IO] Client library not loaded, falling back to HTTP polling');
                return;
            }
            window._sio = io({ transports: ['websocket', 'polling'], reconnection: true, reconnectionDelay: 1000 });

            window._sio.on('connect', () => {
                _sioConnected = true;
                console.log('[Socket.IO] Connected — real-time data push active');
                if (typeof AltarisToast !== 'undefined') AltarisToast.success('Live data connected');
                this.subscribe(_currentSymbol, _currentTf);
            });

            window._sio.on('disconnect', () => {
                _sioConnected = false;
                console.warn('[Socket.IO] Disconnected — will reconnect');
                if (typeof AltarisToast !== 'undefined') AltarisToast.warn('Data stream disconnected — reconnecting…');
            });

            window._sio.on('candle_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:candles:update', data);
                }
            });

            window._sio.on('trade_tick', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:trades:update', data);
                }
            });

            window._sio.on('zone_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:zone:update', data);
                }
            });

            window._sio.on('tape_alert', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:tape:alert', data);
                }
            });

            window._sio.on('regime_update', (data) => {
                if (typeof SigmaEngine !== 'undefined' && data && data.regime) {
                    SigmaEngine.setRegime(data.regime);
                }
            });

            window._sio.on('eq_context', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:eq:context', data);
                }
            });

            // ── spot_update: Live NQ/QQQ/SPY/VIX spot price from Schwab streamer ──
            window._sio.on('spot_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:spot:update', data);
                }
            });

            // ── edge_signal: Cross-asset conviction signals from EdgeDetector ──
            window._sio.on('edge_signal', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:edge:signal', data);
                }
            });

            // ── eq_book_update: QQQ NASDAQ L2 book depth from Schwab ──
            window._sio.on('eq_book_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:eqbook:update', data);
                }
            });

            // ── screener_option_update: Unusual options activity from Schwab screener ──
            window._sio.on('screener_option_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:screener:update', data);
                }
            });

            // ── candle_history: Backfill candle data from l2_worker ──
            window._sio.on('candle_history', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:candles:history', data);
                }
            });

            // ── l2_update: Full L2 state push (replaces REST /api/l2 poll) ──
            window._sio.on('l2_update', (data) => {
                if (window.AltarisEvents) {
                    window.AltarisEvents.emit('data:l2:update', data);
                }
            });
        },

        subscribe(symbol, tf) {
            _currentSymbol = symbol;
            _currentTf = tf;
            if (window._sio && _sioConnected) {
                window._sio.emit('subscribe', { symbol, tf });
            }
        },

        isConnected() {
            return _sioConnected;
        },

        fetchCandles(symbol, tf, since = 0, signal = null) {
            let url = `/api/l2/candles?symbol=${symbol}&tf=${tf}`;
            if (since > 0) url += `&since=${since}`;
            
            const opts = signal ? { signal } : {};
            return window.authFetch(url, opts).then(r => r.json());
        }
    };
})();
