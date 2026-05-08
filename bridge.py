"""bridge.py — Standalone process running the Schwab+Tradier+Intel pipeline.

Runs as its OWN OS process (its own Python interpreter) so it gets its own
P-core, isolated from server.py's gevent loop. Connects to server.py via
Socket.IO localhost client to forward events to browsers.

ARCHITECTURE (Phase 1 of multiprocess split, 2026-05-07):

    ┌──────────────────────────┐         ┌──────────────────────────┐
    │ server.py                │         │ bridge.py (this file)    │
    │ (P-core 1)               │         │ (P-core 2)               │
    │                          │         │                          │
    │ Flask + Socket.IO 3001   │ ◄────── │ Schwab WS streamer       │
    │ L2 worker (NQ ticks)     │  events │ 5× Tradier WS            │
    │ Browser fan-out          │  via    │ Greek surface, walls     │
    │ REST endpoints           │  SIO    │ Intel modules            │
    │ Relay handlers           │  client │ Persistence daemons      │
    └──────────────────────────┘         └──────────────────────────┘

KEY DECISIONS:

1. Gevent monkey-patch FIRST. The schwab_bridge module uses threading.Thread
   for its WS reader threads, but gevent makes those greenlets so we get
   cooperative concurrency on this process's single Python thread.

2. The Socket.IO CLIENT is wired into schwab_bridge via set_socketio() —
   same injection point server.py used. Bridge module doesn't know it's
   running in a separate process.

3. Events emitted by bridge are received by server.py's relay handler
   (added in Phase 2) and re-broadcast to all browser clients.

4. Bridge ONLY handles Schwab+Tradier+Intel. The L2 worker (TopStepX NQ)
   stays in server.py because it directly drives the chart and shares
   _CANDLES state with REST endpoints.

USAGE:

    # 1. Start server.py in one terminal
    python server.py

    # 2. Start bridge.py in another terminal
    python bridge.py

    # Or via supervisor / launchd (Phase 5)

ENV:
    BRIDGE_TARGET   default http://localhost:3001 — the server.py URL
"""
# ── Gevent monkey-patch FIRST, before any other imports ──────────────
# Required because schwab_bridge uses threading.Thread for WS readers;
# gevent monkey-patches threading to greenlets for cooperative scheduling
# under one Python thread.
import gevent.monkey
gevent.monkey.patch_all()

import os
import sys
import time
import signal
import logging

# Add repo root to path so we can import sibling modules
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ── Logging setup ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [BRIDGE] %(levelname)s [%(name)s] %(message)s',
)
log = logging.getLogger('bridge')

# Quiet noisy WebSocket library
logging.getLogger('websocket').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)

# ── Socket.IO client to server.py ────────────────────────────────────
import socketio

BRIDGE_TARGET = os.environ.get('BRIDGE_TARGET', 'http://localhost:3001')
RECONNECT_DELAY_SEC = 2.0
RECONNECT_MAX_DELAY = 30.0

# This is the Socket.IO client. We'll wrap it in a small adapter so its
# .emit() signature matches Flask-SocketIO's, which schwab_bridge uses.
_sio_client = socketio.Client(
    reconnection=True,
    reconnection_attempts=0,            # infinite
    reconnection_delay=RECONNECT_DELAY_SEC,
    reconnection_delay_max=RECONNECT_MAX_DELAY,
    logger=False,
    engineio_logger=False,
)


@_sio_client.event
def connect():
    log.info(f"✅ Connected to server at {BRIDGE_TARGET}")


@_sio_client.event
def connect_error(data):
    log.warning(f"⚠ connection error: {data}")


@_sio_client.event
def disconnect():
    log.warning(f"⚠ Disconnected from server (auto-reconnect armed)")


# ── Adapter: make _sio_client.emit() match Flask-SocketIO's signature ─
# Flask-SocketIO server's .emit() takes optional `room=`, `to=`, `namespace=`
# kwargs. The python-socketio Client only takes `namespace=`. We need to
# strip kwargs that schwab_bridge passes but the client doesn't support.
class _ServerSocketIOAdapter:
    """Wraps the SIO client to look like a Flask-SocketIO server instance.

    schwab_bridge calls _socketio.emit('event_name', data, namespace='/'),
    which on a server broadcasts to all clients. On the client, we wrap
    every emit so it goes via 'relay:event_name' to the server, where a
    relay handler re-broadcasts to all browsers.
    """

    def __init__(self, client: socketio.Client):
        self._client = client

    def emit(self, event, data=None, **kwargs):
        # Strip server-only kwargs the client doesn't support
        # (room, to, broadcast, include_self, callback, skip_sid)
        kwargs.pop('room', None)
        kwargs.pop('to', None)
        kwargs.pop('broadcast', None)
        kwargs.pop('include_self', None)
        kwargs.pop('callback', None)
        kwargs.pop('skip_sid', None)
        # Wrap in 'relay:' prefix so server.py's relay handler picks it up
        # and re-broadcasts to all browsers as the original event name.
        relay_event = f'relay:{event}'
        try:
            if self._client.connected:
                self._client.emit(relay_event, data, **kwargs)
            # If not connected, drop silently — auto-reconnect will resume
        except Exception as e:
            log.debug(f"emit '{relay_event}' failed: {e}")

    # ── Flask-SocketIO compatibility shims ──────────────────────────
    # schwab_bridge spawns background work via _socketio.start_background_task
    # (a Flask-SocketIO method that's gevent-aware). Under gevent monkey-patch
    # we can use threading.Thread directly — it becomes a greenlet, same effect.
    def start_background_task(self, target, *args, **kwargs):
        import threading
        t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
        t.start()
        return t

    def sleep(self, seconds):
        # Flask-SocketIO has a gevent-aware sleep helper. Under monkey-patch
        # time.sleep is already gevent-aware.
        import time as _t
        _t.sleep(seconds)


_sio_adapter = _ServerSocketIOAdapter(_sio_client)


# ── Connection manager: connect with retry ───────────────────────────
def _connect_to_server():
    """Connect to server.py with exponential backoff. Blocking."""
    delay = RECONNECT_DELAY_SEC
    while True:
        try:
            log.info(f"Connecting to {BRIDGE_TARGET}...")
            _sio_client.connect(BRIDGE_TARGET, wait_timeout=10)
            return  # success
        except Exception as e:
            log.warning(f"connect failed: {e} — retry in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 1.5, RECONNECT_MAX_DELAY)


# ── Main ─────────────────────────────────────────────────────────────
def main():
    log.info("═" * 60)
    log.info(" BRIDGE.PY starting (Schwab + Tradier + Intel)")
    log.info(f" Target server: {BRIDGE_TARGET}")
    log.info(f" PID: {os.getpid()}")
    log.info("═" * 60)

    # 1. Connect to server.py first (so events emitted during bridge boot
    #    arrive at the server). If server isn't up yet, retry forever.
    _connect_to_server()

    # 2. Inject the Socket.IO adapter into schwab_bridge as if it were
    #    a Flask-SocketIO server. The schwab_bridge module doesn't know
    #    the difference.
    log.info("Importing schwab_bridge module...")
    from background_engine.schwab_bridge import (
        set_socketio as sb_set_socketio,
        start_schwab_bridge,
    )
    sb_set_socketio(_sio_adapter)
    log.info("schwab_bridge socketio injected")

    # 3. Start the bridge (spawns Schwab WS, Tradier WS, intel loops, etc.)
    log.info("Starting schwab_bridge...")
    start_schwab_bridge()
    log.info("✅ schwab_bridge.start_schwab_bridge() returned — bridge running")

    # 3a. Periodic publishers — make REST endpoints in server.py work in
    #     multiproc mode by writing module state to disk every few seconds.
    #     Server reads via _bridge_state.fetch when its own in-process copy
    #     is empty (BRIDGE_PROCESS=1 case).
    import threading
    def _capture_rate_publisher():
        from connectors import dealer_print_capture as _dpc
        from connectors._bridge_state import publish as _bs_publish
        from background_engine import schwab_bridge as _sb
        import time as _t
        while True:
            try:
                _t.sleep(5.0)
                # capture_rate() self-publishes the dealer-pipeline stats
                base = _dpc.capture_rate()
                # Now augment with Tradier per-conn stats and publish the combined view
                try:
                    base['tradier_conns'] = _sb.get_tradier_conn_stats()
                except Exception as _e:
                    base['tradier_conns'] = [{'error': str(_e)[:120]}]
                _bs_publish('capture_rate', '_snapshot', base)
            except Exception as _e:
                log.debug(f"capture_rate publisher err: {_e}")
    threading.Thread(target=_capture_rate_publisher, daemon=True,
                     name='CaptureRatePublisher').start()
    log.info("capture_rate publisher thread started (5s cadence)")

    # 3b. Flow accumulator publisher — same disk-state pattern. The flow
    #     pane hits /api/option_flow + /api/option_flow/history at hydration
    #     time; without this the pane shows empty.
    def _flow_accumulator_publisher():
        from connectors._bridge_state import publish as _bs_publish
        from connectors.flow_accumulator import get_accumulator
        import time as _t
        while True:
            try:
                _t.sleep(2.0)   # 2s cadence — flow pane polls every ~5s
                acc = get_accumulator()
                if acc is None:
                    continue
                states = acc.get_all_states()
                _bs_publish('flow_accumulator', '_all_tickers', {
                    'tickers': list(states.values()),
                    'ready':   True,
                })
                # Per-ticker by_exchange snapshots — powers the
                # /api/option_flow/by_exchange/<ticker> endpoint.
                for tk in states.keys():
                    try:
                        _bs_publish('flow_by_exchange', tk,
                                    acc.get_by_exchange(tk, top_n=10))
                    except Exception as _be:
                        log.debug(f"by_exchange publish {tk} err: {_be}")
            except Exception as _e:
                log.debug(f"flow_accumulator publisher err: {_e}")
    threading.Thread(target=_flow_accumulator_publisher, daemon=True,
                     name='FlowAccumulatorPublisher').start()
    log.info("flow_accumulator publisher thread started (2s cadence)")

    # 3c. AI panel publishers — alert engine state-matrix + ndx_wgc.
    #     /api/alerts/state and /api/ndx_wgc hydrate the AI panel on mount.
    def _ai_panel_publisher():
        from connectors._bridge_state import publish as _bs_publish
        from connectors.alert_engine import get_engine
        from background_engine import schwab_bridge as _sb
        import time as _t
        while True:
            try:
                _t.sleep(2.0)
                # Alerts state matrix
                eng = get_engine()
                if eng is not None:
                    try:
                        _bs_publish('alerts', 'state_matrix', {
                            'ready':       True,
                            'server_time': _t.time(),
                            'tickers':     eng.get_state_matrix(),
                        })
                    except Exception as _ae:
                        log.debug(f"alerts state_matrix publish err: {_ae}")
                # NDX WGC composite
                try:
                    wgc = _sb.get_latest_wgc()
                    if wgc:
                        _bs_publish('ndx_wgc', 'latest', wgc)
                except Exception as _we:
                    log.debug(f"ndx_wgc publish err: {_we}")
            except Exception as _e:
                log.debug(f"ai_panel publisher err: {_e}")
    threading.Thread(target=_ai_panel_publisher, daemon=True,
                     name='AIPanelPublisher').start()
    log.info("ai_panel publisher thread started (2s cadence)")

    # 4. Keep the main thread alive. start_schwab_bridge() spawns daemon
    #    threads, so without this the process would exit immediately.
    log.info("Bridge main loop entering wait state — Ctrl-C to stop")

    def _shutdown(signum, frame):
        log.info(f"Received signal {signum} — shutting down")
        try:
            _sio_client.disconnect()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # 2026-05-08: daily self-exit at BRIDGE_RESTART_HOUR:BRIDGE_RESTART_MINUTE
    # to mitigate slow memory growth (~50 MB/hr observed). launchd's
    # KeepAlive=true auto-restarts within ThrottleInterval (20s). Disk-
    # persisted state survives the restart so we lose nothing.
    # Disabled if either env is unset/invalid (single-process or manual mode).
    try:
        _restart_h = int(os.environ.get('BRIDGE_RESTART_HOUR', ''))
        _restart_m = int(os.environ.get('BRIDGE_RESTART_MINUTE', '0'))
        _restart_enabled = 0 <= _restart_h < 24 and 0 <= _restart_m < 60
    except (ValueError, TypeError):
        _restart_enabled = False
        _restart_h, _restart_m = -1, -1
    if _restart_enabled:
        log.info(f"Daily self-exit armed for {_restart_h:02d}:{_restart_m:02d} (memory-leak mitigation)")
    _last_restart_check_day = -1   # which date we last triggered on (don't double-fire)

    # gevent-friendly main loop. We just sleep forever; the daemon threads
    # (running as gevent greenlets) keep the bridge alive.
    while True:
        gevent.sleep(60)
        # Periodic heartbeat log so we can confirm bridge is alive
        if _sio_client.connected:
            log.debug("bridge heartbeat — sio connected")
        else:
            log.warning("bridge heartbeat — sio NOT connected (auto-reconnecting)")
        # Daily-restart check
        if _restart_enabled:
            from datetime import datetime as _dt
            now = _dt.now()
            today_int = now.year * 10000 + now.month * 100 + now.day
            if (now.hour == _restart_h
                    and now.minute >= _restart_m
                    and today_int != _last_restart_check_day):
                _last_restart_check_day = today_int
                log.info(f"Daily restart triggered at {now:%H:%M} — exiting (launchd will restart)")
                try:
                    _sio_client.disconnect()
                except Exception:
                    pass
                # Exit code 0 so launchd treats this as a clean exit (vs crash)
                sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — exiting")
        sys.exit(0)
    except Exception as e:
        log.error(f"Fatal error in bridge main: {e}", exc_info=True)
        sys.exit(1)
