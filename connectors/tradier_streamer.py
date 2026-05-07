"""
Tradier WebSocket Streaming Client — options time & sales tape.

Fills the TIMESALE_OPTIONS gap that Schwab rejects (code=11) by sourcing
tick-level option trade prints from Tradier's consolidated WS feed. Works
with OCC symbols; Tradier omits the 6-char underlying padding Schwab uses
(so Schwab's "QQQ   260425C00520000" becomes Tradier's "QQQ260425C00520000").

Uses the synchronous `websocket-client` library (NOT `websockets`) so that
blocking socket I/O cooperates with gevent's monkeypatched hub — avoids the
"Cannot run the event loop while another loop is running" collision between
gevent and asyncio.

Usage:
    ts = TradierStreamer(token)
    ts.on('timesale', my_handler)         # handler(event_dict)
    ts.start()                             # spawns background thread
    ts.subscribe(['AAPL260425C00175000'])  # add OCC symbols

Each timesale event delivered to the handler:
    {
        'symbol': 'AAPL260425C00175000',   # OCC — no padding
        'price': 1.23,
        'size': 5,
        'timestamp_ms': 1745592000123,
        'bid': 1.22, 'ask': 1.24,          # BBO at trade time (Tradier-provided)
        'exchange': 'P',
        'seq': 12345,
        'cancel': False,
        'correction': False,
        'session': 'regular',
    }
"""
import json
import logging
import threading
import time
from collections import defaultdict
from typing import Callable, Iterable

import requests
import websocket  # `websocket-client` package

log = logging.getLogger(__name__)


class TradierStreamer:
    """Real-time streaming client for Tradier options/equity time-and-sales.

    Uses blocking `websocket-client` WebSocketApp + threads — under gevent
    monkeypatch the blocking I/O becomes cooperative automatically.
    Callbacks fire from the WS reader thread — keep them fast.
    """

    STREAM_SESSION_URL = "https://api.tradier.com/v1/markets/events/session"
    WS_URL = "wss://ws.tradier.com/v1/markets/events"
    SESSION_REFRESH_SEC = 240  # sessions expire ~5 min; refresh at 4

    def __init__(self, token: str):
        if not token:
            raise ValueError("TradierStreamer: token required")
        self._token = token
        self._session_id = None
        self._session_created_at = 0.0

        self._symbols: set[str] = set()
        self._symbols_lock = threading.Lock()
        self._symbols_dirty = threading.Event()

        self._callbacks: dict[str, list[Callable]] = defaultdict(list)

        self._ws = None
        self._ws_connected = threading.Event()
        self._main_thread = None
        self._refresher_thread = None
        self._watcher_thread = None
        self._watchdog_thread = None
        self._running = False
        self._reconnect_attempts = 0
        self._max_reconnect_delay = 30
        # Data-activity watchdog. Tradier's WS server doesn't respond to
        # WS-level ping frames reliably (observed: disconnects every ~60s
        # with `ping/pong timed out`). Instead, we disable WS-level ping
        # and watch raw message arrival. If no messages for DATA_SILENCE_SEC,
        # we force a reconnect.
        self._last_msg_ts = 0.0
        self.DATA_SILENCE_SEC = 90.0

        # Per-conn instrumentation (added 2026-05-05) — exposed via stats()
        # so /api/_debug/capture_rate can show disconnects without grepping
        # logs. Real example caught 14:24:42: 3 conns dropped within 3s.
        self._total_reconnects = 0
        self._connected_at = 0.0          # current uptime start (0 if down)
        self._last_disconnect_ts = 0.0
        self._last_disconnect_reason = ''
        self._cumulative_uptime_sec = 0.0  # rolling sum across reconnects
        self._msg_count = 0                # total messages this lifetime

    # ─── PUBLIC API ─────────────────────────────────────

    def on(self, event_type: str, callback: Callable[[dict], None]):
        """Register callback for event type: 'timesale', 'trade', 'quote', 'summary', 'tradex'."""
        self._callbacks[event_type].append(callback)

    def subscribe(self, symbols: Iterable[str]):
        """Add OCC symbols to the active stream. Safe to call multiple times.

        Uses Tradier's un-padded OCC format (e.g. 'AAPL260425C00175000').
        Schwab OSI symbols with underlying padding will be stripped of spaces.
        """
        added = 0
        with self._symbols_lock:
            before = len(self._symbols)
            for s in symbols:
                if not s:
                    continue
                self._symbols.add(s.replace(' ', ''))
            added = len(self._symbols) - before
        if added > 0:
            log.info(f"[TRADIER] +{added} symbols (total: {len(self._symbols)})")
            self._symbols_dirty.set()

    def unsubscribe(self, symbols: Iterable[str]):
        removed = 0
        with self._symbols_lock:
            for s in symbols:
                key = s.replace(' ', '') if s else ''
                if key in self._symbols:
                    self._symbols.discard(key)
                    removed += 1
        if removed > 0:
            log.info(f"[TRADIER] -{removed} symbols (total: {len(self._symbols)})")
            self._symbols_dirty.set()

    def start(self):
        if self._running:
            log.info("[TRADIER] Already running")
            return
        self._running = True
        self._main_thread = threading.Thread(target=self._run_loop, daemon=True, name="TradierStream")
        self._main_thread.start()
        self._refresher_thread = threading.Thread(target=self._session_refresher, daemon=True, name="TradierSessionRefresh")
        self._refresher_thread.start()
        self._watcher_thread = threading.Thread(target=self._subscribe_watcher, daemon=True, name="TradierSubWatcher")
        self._watcher_thread.start()
        self._watchdog_thread = threading.Thread(target=self._data_watchdog, daemon=True, name="TradierWatchdog")
        self._watchdog_thread.start()

    def stop(self):
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        for t in (self._main_thread, self._refresher_thread, self._watcher_thread, self._watchdog_thread):
            if t:
                try:
                    t.join(timeout=5)
                except Exception:
                    pass
        log.info("[TRADIER] Stopped")

    def stats(self) -> dict:
        now = time.time()
        cur_uptime = (now - self._connected_at) if self._connected_at else 0.0
        return {
            'running': self._running,
            'connected': self._ws_connected.is_set(),
            'symbols': len(self._symbols),
            'has_session': bool(self._session_id),
            'session_age_sec': int(now - self._session_created_at) if self._session_created_at else -1,
            'msg_count': self._msg_count,
            'last_msg_age_sec': round(now - self._last_msg_ts, 2) if self._last_msg_ts else -1,
            # Disconnect history
            'total_reconnects': self._total_reconnects,
            'current_uptime_sec': int(cur_uptime),
            'cumulative_uptime_sec': int(self._cumulative_uptime_sec + cur_uptime),
            'last_disconnect_ts': self._last_disconnect_ts,
            'last_disconnect_age_sec': round(now - self._last_disconnect_ts, 1) if self._last_disconnect_ts else -1,
            'last_disconnect_reason': self._last_disconnect_reason,
        }

    # ─── INTERNAL: SESSION ─────────────────────────────

    def _create_session(self) -> str:
        headers = {'Authorization': f'Bearer {self._token}', 'Accept': 'application/json'}
        r = requests.post(self.STREAM_SESSION_URL, headers=headers, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"Tradier session failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        sid = data.get('stream', {}).get('sessionid')
        if not sid:
            raise RuntimeError(f"Tradier session missing sessionid: {data}")
        self._session_id = sid
        self._session_created_at = time.time()
        return sid

    # ─── INTERNAL: MAIN LOOP ──────────────────────────

    def _run_loop(self):
        """Outer reconnect loop. Each iteration runs a full WebSocketApp session."""
        while self._running:
            connect_start = 0.0
            try:
                # 2026-05-06 BUG FIX: explicitly close the OLD WS + socket
                # before creating a new one (same fix as topstepx_connector).
                # websocket-client doesn't close the underlying TCP socket on
                # __del__ promptly under gevent, so old WS objects accumulate
                # as zombie ESTABLISHED conns each holding hundreds of KB of
                # unread TCP buffer. When buffers fill, server RSTs (Errno 54)
                # — same disconnect storm we observed across all 5 conns.
                if self._ws is not None:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    try:
                        _sock = getattr(self._ws, 'sock', None)
                        if _sock is not None:
                            _sock.close()
                    except Exception:
                        pass
                    self._ws = None

                self._create_session()
                log.info(f"[TRADIER] Connecting → {self.WS_URL}")

                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                connect_start = time.time()
                self._last_msg_ts = time.time()
                # Disable WS-level ping/pong. Tradier's streaming server does
                # not reliably respond to control-frame pings, so ping_timeout
                # fires spuriously every ~60s. Instead, we monitor data
                # arrival in the watchdog thread and force reconnect on
                # silence. skip_utf8_validation shaves ~5% CPU on high-volume
                # streams (we never ingest binary frames).
                self._ws.run_forever(
                    ping_interval=0,
                    skip_utf8_validation=True,
                )
            except Exception as e:
                log.warning(f"[TRADIER] Session/connect error: {e}")
            finally:
                self._ws_connected.clear()
                # Don't null self._ws here — the next loop iter does the
                # explicit close+null at the top. Nulling here would skip
                # the close path on the FIRST iter after this finally.

            if not self._running:
                break

            # Only reset retry counter if the last connection lasted ≥10s —
            # otherwise we're in a flap loop (e.g. subscribe rejected) and we
            # must back off exponentially instead of hammering the server.
            uptime = time.time() - connect_start
            if uptime >= 10.0:
                self._reconnect_attempts = 0
            self._reconnect_attempts += 1
            delay = min(2 ** self._reconnect_attempts, self._max_reconnect_delay)
            log.warning(f"[TRADIER] 🔁 Reconnect in {delay}s (attempt {self._reconnect_attempts}, "
                        f"last uptime {uptime:.1f}s)")
            time.sleep(delay)

    def _on_open(self, ws):
        self._ws_connected.set()
        self._connected_at = time.time()
        log.info("[TRADIER] ✅ Connected")
        # Send initial subscribe if symbols already registered.
        self._send_subscribe()

    def _on_message(self, ws, message):
        # Tradier sends one JSON object per line when linebreak=True, or
        # multiple newline-joined objects per frame. Split defensively.
        if not message:
            return
        self._last_msg_ts = time.time()
        self._msg_count += 1
        for line in message.splitlines():
            line = line.strip()
            if not line:
                continue
            self._dispatch(line)

    def _on_error(self, ws, err):
        log.warning(f"[TRADIER] WS error: {err}")
        # Record reason for stats(); _on_close will fire next and
        # increment the reconnect counter.
        self._last_disconnect_reason = f'error: {str(err)[:120]}'

    def _on_close(self, ws, code, reason):
        # Capture uptime BEFORE clearing _connected_at so stats() still
        # reflects the just-ended session for the next ~ms.
        if self._connected_at:
            self._cumulative_uptime_sec += (time.time() - self._connected_at)
        self._connected_at = 0.0
        self._ws_connected.clear()
        self._total_reconnects += 1
        self._last_disconnect_ts = time.time()
        if not self._last_disconnect_reason:
            self._last_disconnect_reason = f'closed: code={code} reason={reason!r}'
        log.warning(f"[TRADIER] WS closed: code={code} reason={reason!r} "
                    f"(reconnects-this-process={self._total_reconnects})")

    def _session_refresher(self):
        """Tradier sessions die after ~5 minutes. Recreate before that happens
        so we never hit the cliff. Cheap: one REST call per 4 minutes.
        """
        while self._running:
            time.sleep(self.SESSION_REFRESH_SEC)
            if not self._running:
                break
            if not self._ws_connected.is_set():
                continue
            try:
                self._create_session()
                # Re-send subscribe with the new sessionid (Tradier binds sub to sessionid).
                self._send_subscribe()
                log.debug("[TRADIER] Session refreshed")
            except Exception as e:
                log.warning(f"[TRADIER] Session refresh failed: {e}")

    def _subscribe_watcher(self):
        """Re-send the subscribe payload whenever the symbol set changes.

        2026-05-07 FIX: previously fired SUBS within 1s of any subscribe()/
        unsubscribe() call. With chain rotation adding/removing contracts
        every 30s, this hit Tradier with 50KB SUBS payloads multiple times
        per minute → Tradier rate-limited us → forced disconnect → reconnect
        → re-SUBS the full list → rinse/repeat. 83 SUBS in 26min observed,
        causing 33 reconnects + 200% sustained CPU on the gevent loop.

        Now: debounce dirty signals over a window so bursts of subscribe()
        calls coalesce into one SUBS. Plus enforce a minimum interval between
        consecutive SUBS sends.
        """
        DEBOUNCE_SEC = 3.0          # wait this long after a dirty signal
        MIN_SUBS_INTERVAL_SEC = 8.0 # at least this long between SUBS sends
        last_subs_ts = 0.0
        while self._running:
            if self._symbols_dirty.wait(timeout=1.0):
                # Debounce: hold dirty for a window to coalesce a burst of
                # subscribe()/unsubscribe() calls into a single SUBS.
                time.sleep(DEBOUNCE_SEC)
                self._symbols_dirty.clear()
                # Rate-limit: don't hammer Tradier
                now = time.time()
                wait = MIN_SUBS_INTERVAL_SEC - (now - last_subs_ts)
                if wait > 0:
                    time.sleep(wait)
                if self._ws_connected.is_set():
                    self._send_subscribe()
                    last_subs_ts = time.time()

    def _data_watchdog(self):
        """Force reconnect if no messages have arrived for DATA_SILENCE_SEC.
        Replaces WS-level ping/pong (which Tradier's server doesn't honor).
        """
        while self._running:
            time.sleep(10.0)
            if not self._ws_connected.is_set():
                continue
            if self._last_msg_ts <= 0:
                continue
            silence = time.time() - self._last_msg_ts
            if silence > self.DATA_SILENCE_SEC:
                log.warning(f"[TRADIER] no data for {silence:.0f}s — forcing reconnect")
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:
                    pass

    def _send_subscribe(self):
        if not self._ws_connected.is_set() or self._ws is None or self._session_id is None:
            return
        with self._symbols_lock:
            syms = sorted(self._symbols)
        if not syms:
            return
        # Subscribe to every event type we care about. timesale is the primary
        # signal; trade is a compact confirmation; quote gives us BBO deltas.
        # tradex covers extended-hours prints.
        payload = {
            'symbols': syms,
            'sessionid': self._session_id,
            'linebreak': True,
            'filter': ['timesale', 'trade', 'quote', 'tradex'],
        }
        try:
            self._ws.send(json.dumps(payload))
            log.info(f"[TRADIER] 📡 SUBS → {len(syms)} symbols")
        except Exception as e:
            log.warning(f"[TRADIER] SUBS failed: {e}")

    # ─── INTERNAL: DISPATCH ────────────────────────────

    def _dispatch(self, raw: str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        event_type = data.get('type')
        if not event_type:
            return

        if event_type == 'timesale':
            evt = {
                'symbol': data.get('symbol') or '',
                'price': _safe_float(data.get('last') or data.get('price')),
                'size': _safe_int(data.get('size')),
                'timestamp_ms': _safe_int(data.get('date')),
                'bid': _safe_float(data.get('bid')),
                'ask': _safe_float(data.get('ask')),
                'exchange': data.get('exch') or '',
                'seq': _safe_int(data.get('seq')),
                'cancel': bool(data.get('cancel')),
                'correction': bool(data.get('correction')),
                'session': data.get('session') or 'regular',
                'flag': data.get('flag') or '',
            }
        elif event_type == 'trade':
            evt = {
                'symbol': data.get('symbol') or '',
                'price': _safe_float(data.get('price')),
                'size': _safe_int(data.get('size')),
                'timestamp_ms': _safe_int(data.get('date')),
                'exchange': data.get('exch') or '',
            }
        elif event_type == 'tradex':
            evt = {
                'symbol': data.get('symbol') or '',
                'price': _safe_float(data.get('price')),
                'size': _safe_int(data.get('size')),
                'timestamp_ms': _safe_int(data.get('date')),
                'exchange': data.get('exch') or '',
                'session': data.get('session') or '',
            }
        elif event_type == 'quote':
            evt = {
                'symbol': data.get('symbol') or '',
                'bid': _safe_float(data.get('bid')),
                'ask': _safe_float(data.get('ask')),
                'bidsize': _safe_int(data.get('bidsize')),
                'asksize': _safe_int(data.get('asksize')),
                'timestamp_ms': _safe_int(data.get('biddate') or data.get('askdate')),
                'exchange_bid': data.get('bidexch') or '',
                'exchange_ask': data.get('askexch') or '',
            }
        elif event_type == 'summary':
            evt = {
                'symbol': data.get('symbol') or '',
                'open': _safe_float(data.get('open')),
                'high': _safe_float(data.get('high')),
                'low': _safe_float(data.get('low')),
                'close': _safe_float(data.get('prevClose')),
            }
        else:
            return

        for cb in self._callbacks.get(event_type, []):
            try:
                cb(evt)
            except Exception as e:
                log.debug(f"[TRADIER] callback error ({event_type}): {e}")


def _safe_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ─── OCC symbol converter helper (Schwab OSI ↔ Tradier OCC) ────────────────

def schwab_osi_to_tradier(osi_symbol: str) -> str:
    """Convert Schwab's padded OSI symbol to Tradier's un-padded OCC.

    Schwab  : "QQQ   260425C00520000" (21 chars, 6-char root pad)
    Tradier : "QQQ260425C00520000"    (strip spaces)
    """
    return (osi_symbol or '').replace(' ', '')


def tradier_occ_to_schwab_osi(occ: str) -> str:
    """Convert Tradier's un-padded OCC to Schwab's padded OSI.

    Splits at the first digit — everything before is the root; pad to 6 chars.
    'AAPL260425C00175000' → 'AAPL  260425C00175000'
    """
    if not occ:
        return ''
    for i, ch in enumerate(occ):
        if ch.isdigit():
            root, tail = occ[:i], occ[i:]
            return f"{root:<6}{tail}"
    return occ
