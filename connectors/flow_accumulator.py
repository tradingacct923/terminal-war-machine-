"""
Flow Accumulator — signed delta notional per ticker.

Listens to every option trade (last_size > 0) and accumulates:
  - Cumulative signed Δ notional (0DTE vs all expirations)
  - Cumulative unsigned $ volume (0DTE vs all expirations)

Signed Δ notional convention (mirrors 0DT-Hero curves):
    dn = side * size * delta * underlying_spot * 100
  where:
    side  = +1 buyer-initiated (last >= ask), -1 seller-initiated (last <= bid)
    size  = last_size (contracts on the fill)
    delta = option delta at trade time (-1..+1)

  Buying calls → positive (dealer short → hedge buy → bullish)
  Buying puts  → negative (dealer short → hedge sell → bearish)

Side inference uses the Lee-Ready quote rule:
  last >= ask → +1,  last <= bid → -1,  else drop (ambiguous midmarket).

Ambiguous fills are counted into unsigned volume but not signed notional.

Emitted via socketio every ~1s as event 'flow_update':
    {ticker, t, cum_signed_0dte, cum_signed_all,
     cum_unsigned_0dte, cum_unsigned_all, trades_0dte, trades_all}
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class _TickerState:
    """Per-ticker running totals. All dollar amounts in raw units (not millions)."""
    cum_signed_0dte: float = 0.0
    cum_signed_all: float = 0.0
    cum_unsigned_0dte: float = 0.0
    cum_unsigned_all: float = 0.0
    trades_0dte: int = 0
    trades_all: int = 0
    ambiguous_trades: int = 0
    last_update_ts: float = 0.0


class FlowAccumulator:
    """Accumulate signed Δ notional per ticker from live option trades."""

    def __init__(self, socketio=None, emit_interval_sec: float = 1.0):
        self._socketio = socketio
        self._emit_interval = emit_interval_sec
        self._state: dict[str, _TickerState] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._emit_loop, daemon=True, name="flow-accumulator"
        )
        self._thread.start()
        log.info(f"[FLOW-ACC] Started (emit every {self._emit_interval}s)")

    def stop(self):
        self._running = False

    def on_option_update(self, data: dict) -> None:
        """Process a single Schwab LEVELONE_OPTIONS message.

        Only counts messages where last_size > 0 (a real trade, not a quote-only update).
        Must be thread-safe — called from the streamer thread.
        """
        size = data.get("last_size", 0) or 0
        if not size or size <= 0:
            return

        last = data.get("last") or 0.0
        bid = data.get("bid") or 0.0
        ask = data.get("ask") or 0.0
        delta = data.get("delta")
        dte = data.get("dte")
        spot = data.get("underlying_price") or 0.0
        symbol = data.get("symbol", "") or ""

        if not last or not spot or delta is None or dte is None or not symbol:
            return
        if len(symbol) < 6:
            return

        ticker = symbol[:6].strip()
        if not ticker:
            return

        # Side inference (Lee-Ready quote rule)
        if ask > 0 and last >= ask:
            side = 1
        elif bid > 0 and last <= bid:
            side = -1
        else:
            side = 0  # ambiguous — count in unsigned, skip signed

        unsigned = float(size) * float(last) * 100.0
        signed = 0.0
        if side != 0:
            signed = float(side) * float(size) * float(delta) * float(spot) * 100.0

        is_0dte = int(dte) == 0

        with self._lock:
            st = self._state.setdefault(ticker, _TickerState())
            st.cum_unsigned_all += unsigned
            st.trades_all += 1
            if is_0dte:
                st.cum_unsigned_0dte += unsigned
                st.trades_0dte += 1
            if side == 0:
                st.ambiguous_trades += 1
            else:
                st.cum_signed_all += signed
                if is_0dte:
                    st.cum_signed_0dte += signed
            st.last_update_ts = time.time()

    def get_state(self, ticker: str) -> Optional[dict]:
        """Snapshot for a ticker — used by /api/flow diagnostic endpoints."""
        with self._lock:
            st = self._state.get(ticker)
            if not st:
                return None
            return {
                "ticker": ticker,
                "cum_signed_0dte": st.cum_signed_0dte,
                "cum_signed_all": st.cum_signed_all,
                "cum_unsigned_0dte": st.cum_unsigned_0dte,
                "cum_unsigned_all": st.cum_unsigned_all,
                "trades_0dte": st.trades_0dte,
                "trades_all": st.trades_all,
                "ambiguous_trades": st.ambiguous_trades,
                "last_update_ts": st.last_update_ts,
            }

    def get_all_states(self) -> dict[str, dict]:
        with self._lock:
            return {t: self._ticker_dict(t, st) for t, st in self._state.items()}

    def _ticker_dict(self, ticker: str, st: _TickerState) -> dict:
        return {
            "ticker": ticker,
            "cum_signed_0dte": st.cum_signed_0dte,
            "cum_signed_all": st.cum_signed_all,
            "cum_unsigned_0dte": st.cum_unsigned_0dte,
            "cum_unsigned_all": st.cum_unsigned_all,
            "trades_0dte": st.trades_0dte,
            "trades_all": st.trades_all,
        }

    def _emit_loop(self) -> None:
        """Background loop that broadcasts per-ticker flow every emit_interval seconds."""
        while self._running:
            time.sleep(self._emit_interval)
            if not self._socketio:
                continue
            with self._lock:
                snapshot = [
                    self._ticker_dict(t, st)
                    for t, st in self._state.items()
                    if st.trades_all > 0
                ]
            if not snapshot:
                continue
            now_ms = int(time.time() * 1000)
            try:
                self._socketio.emit(
                    "flow_update",
                    {"t": now_ms, "tickers": snapshot},
                )
            except Exception as e:
                log.debug(f"[FLOW-ACC] emit failed: {e}")


# Global singleton, instantiated from schwab_bridge.start_schwab_bridge
_accumulator: Optional[FlowAccumulator] = None


def get_accumulator() -> Optional[FlowAccumulator]:
    return _accumulator


def init_accumulator(socketio) -> FlowAccumulator:
    """Create the global singleton (idempotent)."""
    global _accumulator
    if _accumulator is None:
        _accumulator = FlowAccumulator(socketio=socketio, emit_interval_sec=1.0)
        _accumulator.start()
    return _accumulator
