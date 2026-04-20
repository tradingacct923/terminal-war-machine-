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
class _BucketState:
    """Signed/unsigned + trade count for one (ticker, bucket) pair."""
    cum_signed: float = 0.0
    cum_unsigned: float = 0.0
    trades: int = 0


@dataclass
class _TickerState:
    """Per-ticker running totals.

    Legacy 0DTE/all fields are preserved for backwards-compat with the
    flow pane frontend. New bucket-level fields provide weekly/monthly/
    LEAPS splits for 0DT-Hero-style alert labelling.
    """
    # Legacy 2-way split (kept for frontend compat)
    cum_signed_0dte: float = 0.0
    cum_signed_all: float = 0.0
    cum_unsigned_0dte: float = 0.0
    cum_unsigned_all: float = 0.0
    trades_0dte: int = 0
    trades_all: int = 0
    ambiguous_trades: int = 0
    last_update_ts: float = 0.0

    # New: per-bucket state. Keys: '0dte','weekly','monthly','quarterly','leaps','unknown'
    buckets: dict = None

    def __post_init__(self):
        if self.buckets is None:
            self.buckets = {}


class FlowAccumulator:
    """Accumulate signed Δ notional per ticker from live option trades."""

    def __init__(self, socketio=None, emit_interval_sec: float = 1.0):
        self._socketio = socketio
        self._emit_interval = emit_interval_sec
        self._state: dict[str, _TickerState] = {}
        # Dedup: last `trade_time` seen per option symbol. Schwab repeats
        # last_size on delta updates that don't represent new trades; without
        # dedup we'd double-count ~10-20% of flow.
        self._last_trade_time: dict[str, int] = {}
        # Latest underlying spot price per ticker — updated on every option
        # message via the underlying_price field. Used by AlertEngine for
        # flow_divergence / flow_convergence detection.
        self._latest_spot: dict[str, float] = {}
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
        trade_time = int(data.get("trade_time") or 0)

        if not last or not spot or delta is None or dte is None or not symbol:
            return
        if len(symbol) < 6:
            return

        ticker = symbol[:6].strip()
        if not ticker:
            return

        # Dedup: if Schwab re-reports the same trade_time for this symbol,
        # skip. Only dedup when trade_time is present (>0); otherwise count
        # as a real trade. Check before expensive bucket classification.
        if trade_time > 0:
            with self._lock:
                if self._last_trade_time.get(symbol) == trade_time:
                    return  # already counted this trade
                self._last_trade_time[symbol] = trade_time

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

        # Classify expiration bucket (0dte, weekly, monthly, quarterly, leaps)
        bucket = '0dte' if is_0dte else 'unknown'
        classify_source = 'dte_field'
        try:
            from connectors.expiration_cache import get_cache
            _c = get_cache()
            if _c is not None:
                _t2, _b = _c.classify_symbol(symbol)
                if _b and _b != 'unknown':
                    bucket = _b
                    classify_source = 'cache'
        except Exception:
            pass

        # TEMP DIAGNOSTIC: track first 50 per ticker-bucket combo
        _diag = getattr(self, '_classify_diag', {})
        if len(_diag) < 50:
            key = (ticker, bucket, classify_source)
            if key not in _diag:
                _diag[key] = {'example_symbol': symbol, 'dte_field': dte, 'count': 0}
            _diag[key]['count'] += 1
            self._classify_diag = _diag

        # TEMP DIAGNOSTIC 2: count by (ticker, expiration YYMMDD) to see if ANY
        # 260420 symbols are reaching us
        _date_diag = getattr(self, '_date_diag', {})
        date_key = (ticker, symbol[6:12])
        _date_diag[date_key] = _date_diag.get(date_key, 0) + 1
        self._date_diag = _date_diag

        with self._lock:
            # Track most recent spot per ticker for AlertEngine divergence
            if spot > 0:
                self._latest_spot[ticker] = float(spot)

            st = self._state.setdefault(ticker, _TickerState())
            # Legacy 2-way split (frontend compat)
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

            # Per-bucket aggregation (new: weekly/monthly/quarterly/leaps split)
            bkt = st.buckets.setdefault(bucket, _BucketState())
            bkt.cum_unsigned += unsigned
            bkt.trades += 1
            if side != 0:
                bkt.cum_signed += signed

    def get_diag(self) -> dict:
        """TEMP diagnostic: show what we're classifying trades into."""
        diag = getattr(self, '_classify_diag', {})
        date_diag = getattr(self, '_date_diag', {})
        # Aggregate by (ticker, date) showing trade counts
        by_ticker_date = {}
        for (t, d), c in date_diag.items():
            by_ticker_date.setdefault(t, {})[d] = c
        return {
            'classify': {
                f"{t}__{b}__via_{src}": {'symbol': v['example_symbol'], 'dte_field': v['dte_field'], 'count': v['count']}
                for (t, b, src), v in diag.items()
            },
            'trades_by_expiration': by_ticker_date,
        }

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
        bucket_data = {
            name: {
                "cum_signed": b.cum_signed,
                "cum_unsigned": b.cum_unsigned,
                "trades": b.trades,
            }
            for name, b in (st.buckets or {}).items()
        }
        return {
            "ticker": ticker,
            # Legacy 2-way split (flow pane keeps rendering from these)
            "cum_signed_0dte": st.cum_signed_0dte,
            "cum_signed_all": st.cum_signed_all,
            "cum_unsigned_0dte": st.cum_unsigned_0dte,
            "cum_unsigned_all": st.cum_unsigned_all,
            "trades_0dte": st.trades_0dte,
            "trades_all": st.trades_all,
            # New: per-bucket breakdown for alert labels
            "buckets": bucket_data,
        }

    def _emit_loop(self) -> None:
        """Background loop: broadcasts per-ticker flow every emit_interval,
        and feeds AlertEngine with (state + spot) so divergence/cross/spike
        detectors can fire on live data."""
        while self._running:
            time.sleep(self._emit_interval)
            with self._lock:
                snapshot = [
                    self._ticker_dict(t, st)
                    for t, st in self._state.items()
                    if st.trades_all > 0
                ]
                spots = dict(self._latest_spot)
            if not snapshot:
                continue
            now_sec = time.time()
            now_ms = int(now_sec * 1000)

            # Feed the alert engine (one observe call per active ticker)
            try:
                from connectors.alert_engine import get_engine
                eng = get_engine()
                if eng is not None:
                    for st in snapshot:
                        eng.observe(
                            st['ticker'], now_sec,
                            st['cum_signed_0dte'],
                            st['cum_signed_all'],
                            st['cum_unsigned_0dte'],
                            st['cum_unsigned_all'],
                            spots.get(st['ticker'], 0.0),
                        )
            except Exception as e:
                log.debug(f"[FLOW-ACC] alert engine feed failed: {e}")

            # Broadcast flow_update socket event
            if self._socketio:
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
