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
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── Phase 19 (2026-05-01) — Volatility-Delta adjustment feature flag ────
# Enables Kobayashi (2025) Δ_vol = Δ - Vega × (k_v / S_ref) on each print.
# Default ON. Disable via env: FLOW_ACC_KV_ADJUST_ENABLED=0
# Falls back to classical Δ if estimator returns k_v=0, vega missing, or
# any error occurs. Safe-by-default: errors NEVER break flow.
FLOW_ACC_KV_ADJUST_ENABLED = (
    os.getenv('FLOW_ACC_KV_ADJUST_ENABLED', '1').strip() not in ('0', 'false', 'False', '')
)


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

    # ── Phase 19 (Kobayashi 2025) Volatility-Delta diagnostic counters ──
    # Track raw-Δ flow IN PARALLEL to k_v-adjusted flow for validation.
    # The cum_signed_* fields above use Δ_vol when adjustment is applied.
    # These cum_signed_*_raw track what they would have been without adjustment.
    # Difference = flow attributable to spot-IV co-movement.
    cum_signed_0dte_raw: float = 0.0
    cum_signed_all_raw: float = 0.0
    kv_adjusted_trades: int = 0   # count of trades where k_v adjustment fired

    # ── Improvement #2 — Calls vs Puts decomposition ──
    # Magnitudes (always ≥0) of $-delta-notional flow per atomic action.
    # Unit: same as cum_signed_* (size × delta × spot × 100), but unsigned.
    # Sign convention is encoded in WHICH bucket the trade lands in:
    #   call_buy  → bullish directional (lifts offer on calls)
    #   call_sell → bearish OR vol-harvest (hits bid on calls)
    #   put_buy   → bearish directional OR hedge (lifts offer on puts)
    #   put_sell  → bullish OR vol-harvest (hits bid on puts)
    # Composite bullish_directional = call_buy + put_sell
    # Composite bearish_directional = call_sell + put_buy
    # Composite vol_long             = call_buy + put_buy   (long premium)
    # Composite vol_short            = call_sell + put_sell (short premium)
    cum_call_buy:  float = 0.0
    cum_call_sell: float = 0.0
    cum_put_buy:   float = 0.0
    cum_put_sell:  float = 0.0

    # ── Improvement #3 — exp_type / DTE 6-cohort signed flow ──
    # Splits the binary 0dte/non-0dte view into the actual trader cohorts:
    #   0dte_am    DTE=0 AND AM-settled (SPX/NDX) — institutional vol-selling
    #   0dte_pm    DTE=0 AND PM-settled (QQQ/SPY) — retail FOMO
    #   weekly     DTE 1-7
    #   monthly    DTE 8-30
    #   quarterly  DTE 31-90
    #   leaps      DTE > 90
    # The "institutional vs retail" framing now uses these as primary inputs
    # instead of the (oversimplified) DTE binary split.
    cohort_0dte_am_signed:    float = 0.0
    cohort_0dte_am_unsigned:  float = 0.0
    cohort_0dte_am_trades:    int   = 0
    cohort_0dte_pm_signed:    float = 0.0
    cohort_0dte_pm_unsigned:  float = 0.0
    cohort_0dte_pm_trades:    int   = 0
    cohort_weekly_signed:     float = 0.0
    cohort_weekly_unsigned:   float = 0.0
    cohort_weekly_trades:     int   = 0
    cohort_monthly_signed:    float = 0.0
    cohort_monthly_unsigned:  float = 0.0
    cohort_monthly_trades:    int   = 0
    cohort_quarterly_signed:  float = 0.0
    cohort_quarterly_unsigned:float = 0.0
    cohort_quarterly_trades:  int   = 0
    cohort_leaps_signed:      float = 0.0
    cohort_leaps_unsigned:    float = 0.0
    cohort_leaps_trades:      int   = 0

    # ── Improvement #5 — Streaming OI delta opening/closing classifier ──
    # Per-strike OI velocity classifies each trade into:
    #   opening_long  = aggressor BUY  + OI rising → fresh long position
    #   closing_short = aggressor BUY  + OI falling → cover (short close)
    #   opening_short = aggressor SELL + OI rising → fresh short position
    #   closing_long  = aggressor SELL + OI falling → profit-take (long close)
    #   unknown       = OI velocity below classification threshold
    # Empirical edge: Pan & Poteshman (2006) — opening flow has next-day
    # return alpha, closing flow does not. Splitting these is the single
    # highest-edge improvement in the framework.
    cohort_opening_long_signed:    float = 0.0
    cohort_opening_long_trades:    int   = 0
    cohort_closing_short_signed:   float = 0.0
    cohort_closing_short_trades:   int   = 0
    cohort_opening_short_signed:   float = 0.0
    cohort_opening_short_trades:   int   = 0
    cohort_closing_long_signed:    float = 0.0
    cohort_closing_long_trades:    int   = 0
    cohort_unknown_signed:         float = 0.0
    cohort_unknown_trades:         int   = 0

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
        # ── Streaming OI delta classifier state (Improvement #5) ──
        # `_last_trade_oi[symbol]` = OI value at the time of the most recent
        # trade we classified on this strike. When the next trade arrives,
        # we compute oi_delta = current_oi − last_trade_oi to capture ALL
        # OI changes that happened between the prior trade and this one.
        # This handles Schwab's sparse OI update cadence robustly: even if
        # OI didn't change at the exact moment of trade, the cumulative
        # change since the last trade reflects the net opening/closing
        # pressure on this strike.
        # `_strike_first_oi[symbol]` = first OI we ever saw for the strike
        # (used as baseline when no prior trade exists yet).
        self._last_trade_oi:  dict[str, int] = {}
        self._strike_first_oi: dict[str, int] = {}
        # ── Per-exchange flow attribution (Improvement #4) ──
        # Nested dict tracking signed/unsigned flow per venue MIC:
        #   _by_exchange[ticker][mic] = {
        #     'signed':   float (cum $-delta-notional, signed),
        #     'unsigned': float (cum $-notional, always ≥0),
        #     'trades':   int   (count),
        #     'last_ts':  float (most recent print epoch),
        #   }
        # Used to compute concentration_score = max|signed| / sum|signed|
        # which reveals "single-MM events" (concentration ≥ 0.60 = one venue
        # owns the flow, typically institutional algo routing) vs distributed
        # (retail clicking buy across multiple broker primary venues).
        # Common option MICs:
        #   CBOE/CBOEW — Chicago Board Options Exchange
        #   ARCA       — NYSE Arca Options (institutional skew)
        #   BATS/EDGX  — CBOE EDGX/BZX (HFT skew)
        #   NSDQ/PHLX  — Nasdaq Options Markets
        #   BOSX/MERC  — BOX / MIAX Pearl
        #   AMEX       — NYSE American
        self._by_exchange: dict[str, dict] = {}

        # ── Theoretical-vs-Mark mispricing tracker (Improvement #1) ──
        # Per-strike rolling mispricing readings:
        #   _mispricing[ticker][symbol] = {
        #     'mispricing_pct': float,   last computed (mark − theo) / theo
        #     'volume_5min':    int,     trade volume in last 5min
        #     'last_ts':        float,   last update timestamp
        #     'strike':         float,
        #     'side':           'C'|'P',
        #     'dte':            int,
        #   }
        # Two filters keep this clean:
        #   - theo > $0.50 AND mark > $0.50  (avoid div-by-near-zero on far-OTM)
        #   - last_size > 0                  (only real trades, no quote-only ticks)
        # The signal interpretation:
        #   mispricing > +3% with heavy volume → institutional paying premium
        #     (high-conviction accumulation, often precedes directional move)
        #   mispricing < -3% with heavy volume → forced/distressed selling
        #     (margin calls, inventory dumps, contrarian opportunity)
        self._mispricing: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # ── Rolling 2h history buffer (added 2026-05-01) ──────────────────
        # Survives server restarts via disk persistence. Frontend hydrates
        # from /api/option_flow/history on page-load so users see the last
        # ~6.5 hours of flow without needing to wait for live ticks to populate.
        # Keyed by ticker → deque of compact snapshot dicts.
        # Cadence: snapshot every 30s, maxlen=780 = 6.5h coverage = whole RTH.
        # Each snapshot is ~14 fields × 8 bytes = ~112 bytes; full buffer
        # for 17 active tickers ≈ 1.5MB resident, ~15MB JSON-serialized.
        # 2026-05-05: bumped 240→780 so the chart shows the FULL trading day,
        # not just the last 2 hours. User feedback: "we don't see anything
        # from earlier in the day, only live forward."
        from collections import deque as _deque
        self._HISTORY_MAXLEN = 780          # 780 × 30s = 6.5h (full RTH)
        self._HISTORY_INTERVAL_S = 30.0     # snapshot cadence
        self._history_buffer: dict[str, "_deque"] = {}
        self._history_last_snapshot_ts = 0.0
        self._history_last_persist_ts = 0.0
        self._HISTORY_PERSIST_INTERVAL_S = 60.0  # disk write cadence
        # Try restoring from disk (within today's date)
        self._restore_history_from_disk()

        # ── Diagnostic counters (added 2026-05-01 for SPY over-count debug) ──
        # Per-ticker call/drop telemetry — reveals whether dedup is firing,
        # how many calls are gated, and what the actual signed-flow accept
        # rate is. Exposed via /api/_debug/flow_diag (separate dict from
        # _on_tradier_timesale's _flow_diag which only sees the Tradier path).
        # All Schwab `_on_options_quote` feeds bypass the Tradier diag — these
        # counters are the ONLY visibility into the SPY/IWM/sector-ETF path.
        self._diag: dict[str, dict] = {}  # ticker → counter dict

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
        # ── Improvement #5 — capture first-seen OI on every tick ──
        # `_strike_first_oi` records the earliest OI we observed per strike.
        # Used as baseline for the very first trade we classify on a strike
        # (subsequent trades use _last_trade_oi instead). This way every
        # strike starts with a valid baseline the moment we first see it.
        # Lock-protected: Schwab and Tradier streamer threads concurrently
        # call this entry point; the check-then-set without lock allowed
        # races where two threads observed `not in` and both wrote, and
        # opens a window for read-vs-write torn state from the OI
        # classifier (line ~452) that reads _strike_first_oi.
        try:
            _stream_sym = (data.get("symbol") or "").strip()
            _stream_oi = data.get("open_interest")
            if _stream_sym and _stream_oi is not None and int(_stream_oi or 0) > 0:
                with self._lock:
                    if _stream_sym not in self._strike_first_oi:
                        self._strike_first_oi[_stream_sym] = int(_stream_oi)
        except Exception as _e:
            log.debug(f"[FLOW-ACC] strike_first_oi capture err: {_e}")

        # ── PRE-FILTER DIAGNOSTIC (hard-debug why 0DTE SPX shows 0 trades) ──
        # Record every message we see for any SPX/SPXW symbol BEFORE filtering,
        # so we can tell whether trades are arriving with last_size=0 or not
        # arriving at all.
        # Lock-protected: same race pattern as _strike_first_oi above.
        _sym = data.get("symbol", "") or ""
        if _sym and (_sym.startswith('SPX') or _sym.startswith('SPXW')):
            with self._lock:
                _raw = getattr(self, '_raw_spx_diag', None)
                if _raw is None:
                    _raw = {'totals': {}, 'samples': []}
                    self._raw_spx_diag = _raw
                _exp = _sym[6:12] if len(_sym) >= 12 else '?'
                _sz = data.get("last_size", 0) or 0
                _bucket = 'with_size' if _sz > 0 else 'no_size'
                _k = (_exp, _bucket)
                _raw['totals'][_k] = _raw['totals'].get(_k, 0) + 1
                if len(_raw['samples']) < 5 and _exp == '260420':
                    _raw['samples'].append({
                        'symbol': _sym, 'last_size': _sz,
                        'last': data.get('last'), 'bid': data.get('bid'),
                        'ask': data.get('ask'), 'delta': data.get('delta'),
                        'dte': data.get('dte'), 'trade_time': data.get('trade_time'),
                        'underlying_price': data.get('underlying_price'),
                        'all_keys': list(data.keys()),
                    })

        # Diagnostic: per-ticker counters for SPY over-count debug
        # Determine ticker EARLY (before gate drops) so we count those too.
        _diag_sym = (data.get("symbol", "") or "")
        _diag_ticker = _diag_sym[:6].strip() if len(_diag_sym) >= 6 else ''
        if _diag_ticker == 'SPXW': _diag_ticker = 'SPX'
        elif _diag_ticker == 'NDXP': _diag_ticker = 'NDX'
        elif _diag_ticker == 'RUTW': _diag_ticker = 'RUT'
        elif _diag_ticker == 'VIXW': _diag_ticker = 'VIX'
        if _diag_ticker:
            with self._lock:
                _td = self._diag.setdefault(_diag_ticker, {
                    'calls': 0, 'gate_no_size': 0, 'gate_other': 0,
                    'dedup_hits': 0, 'no_trade_time': 0,
                    'side_buy': 0, 'side_sell': 0, 'side_mid': 0,
                    'signed_added': 0, 'sample_sizes': []})
                _td['calls'] += 1

        size = data.get("last_size", 0) or 0
        if not size or size <= 0:
            if _diag_ticker:
                with self._lock:
                    self._diag[_diag_ticker]['gate_no_size'] += 1
            return

        # ── Defense-in-depth oversize guard (added 2026-05-01) ──
        # Reject any print with >100,000 contracts as size — empirically
        # observed Schwab quirk for SPY where field 18 (last_size) occasionally
        # carries 100K-600K values that look like cumulative volume. Even
        # multi-leg institutional blocks rarely exceed 50K contracts. Guarding
        # at the FlowAccumulator level ensures any path (Schwab options_quote,
        # Schwab options_timesale, Tradier timesale) is protected.
        if size > 100_000:
            if _diag_ticker:
                with self._lock:
                    _d = self._diag[_diag_ticker]
                    _d['gate_oversize'] = _d.get('gate_oversize', 0) + 1
                    if _d.get('gate_oversize', 0) <= 3:
                        log.warning(
                            f"[FLOW-ACC OVERSIZE] DROPPED size={size} "
                            f"sym={data.get('symbol')} last={data.get('last')} "
                            f"trade_time={data.get('trade_time')}"
                        )
            return

        last = data.get("last") or 0.0
        bid = data.get("bid") or 0.0
        ask = data.get("ask") or 0.0
        delta = data.get("delta")
        dte = data.get("dte")
        spot = data.get("underlying_price") or 0.0
        symbol = data.get("symbol", "") or ""
        trade_time = int(data.get("trade_time") or 0)
        # Mispricing inputs (Improvement #1) — Schwab fields 34 + 37
        theo  = float(data.get("theoretical_value") or 0.0)
        mark  = float(data.get("mark") or last or 0.0)

        # Schwab omits `dte` for $SPX / $NDX / $VIX / $RUT index options.
        # Fall back to computing DTE from the OSI symbol's YYMMDD field so
        # index-option trades don't get silently dropped. Without this fix,
        # we lose ~100% of 0DTE SPX trade prints even though Schwab sends them.
        if dte is None and len(symbol) >= 12:
            try:
                from datetime import date as _d
                yymmdd = symbol[6:12]
                if yymmdd.isdigit():
                    y, m, dd = 2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
                    dte = max(0, (_d(y, m, dd) - _d.today()).days)
            except Exception as _e:
                log.debug(f"[FLOW-ACC] DTE parse err for {symbol!r}: {_e}")
                dte = None

        if not last or not spot or delta is None or dte is None or not symbol:
            if _diag_ticker:
                with self._lock:
                    _d = self._diag[_diag_ticker]
                    _d['gate_other'] += 1
                    # Track WHICH gate triggered to figure out where SPY drops
                    if not last: _d.setdefault('drop_no_last', 0); _d['drop_no_last'] += 1
                    elif not spot: _d.setdefault('drop_no_spot', 0); _d['drop_no_spot'] += 1
                    elif delta is None: _d.setdefault('drop_no_delta', 0); _d['drop_no_delta'] += 1
                    elif dte is None: _d.setdefault('drop_no_dte', 0); _d['drop_no_dte'] += 1
            return
        if len(symbol) < 6:
            return

        ticker = symbol[:6].strip()
        if not ticker:
            return
        # Normalize index-option roots to their underlying symbol so SPX+SPXW
        # aggregate as "SPX", NDX+NDXP as "NDX", RUT+RUTW as "RUT", VIX+VIXW
        # as "VIX". Otherwise flow splits across two buckets the UI never joins.
        if ticker == 'SPXW': ticker = 'SPX'
        elif ticker == 'NDXP': ticker = 'NDX'
        elif ticker == 'RUTW': ticker = 'RUT'
        elif ticker == 'VIXW': ticker = 'VIX'

        # Dedup: if Schwab re-reports the same trade_time for this symbol,
        # skip. Only dedup when trade_time is present (>0); otherwise count
        # as a real trade. Check before expensive bucket classification.
        if trade_time > 0:
            with self._lock:
                if self._last_trade_time.get(symbol) == trade_time:
                    if _diag_ticker:
                        self._diag[_diag_ticker]['dedup_hits'] += 1
                    return  # already counted this trade
                self._last_trade_time[symbol] = trade_time
        else:
            if _diag_ticker:
                with self._lock:
                    self._diag[_diag_ticker]['no_trade_time'] += 1

        # Side inference (Lee-Ready quote rule)
        if ask > 0 and last >= ask:
            side = 1
        elif bid > 0 and last <= bid:
            side = -1
        else:
            side = 0  # ambiguous — count in unsigned, skip signed
        # Diagnostic: count side inference + sample sizes (first 10) per ticker
        if _diag_ticker:
            with self._lock:
                _d = self._diag[_diag_ticker]
                if side == 1:    _d['side_buy']  += 1
                elif side == -1: _d['side_sell'] += 1
                else:            _d['side_mid']  += 1
                if side != 0:    _d['signed_added'] += 1
                # Track top-50 LARGEST prints by absolute signed (not first-50)
                _signed_now = side * size * float(delta) * spot * 100
                _smap = _d.setdefault('top_signed', [])
                _smap.append({
                    'sym': symbol, 'size': size, 'last': last,
                    'delta': round(float(delta), 4),
                    'spot': spot, 'side': side,
                    'tt':   trade_time, 'dte': int(dte),
                    'signed_$M': round(_signed_now / 1e6, 3),
                })
                # Keep only top 50 by abs(signed_$)
                if len(_smap) > 100:
                    _smap.sort(key=lambda x: abs(x['signed_$M']), reverse=True)
                    del _smap[50:]
                if len(_d['sample_sizes']) < 10:
                    _d['sample_sizes'].append({
                        'sym': symbol, 'size': size, 'last': last,
                        'delta': round(float(delta), 4),
                        'spot': spot, 'side': side,
                        'tt':   trade_time, 'dte': int(dte),
                        'signed_$': round(_signed_now / 1e6, 3),
                    })

        # ── Phase 19 (Kobayashi 2025) Volatility-Delta adjustment ────────
        # Δ_vol = Δ - Vega × (k_v / S_ref) — corrects for spot-IV co-movement.
        # Falls back to classical Δ when k_v=0, vega missing, or estimator off.
        # Feature-flagged via FLOW_ACC_KV_ADJUST_ENABLED (default: True).
        delta_used = float(delta)
        kv_diag = {'applied': False, 'kv': 0.0, 'delta_raw': float(delta), 'delta_adj': float(delta)}
        if FLOW_ACC_KV_ADJUST_ENABLED:
            try:
                from connectors.kv_estimator import get_kv_estimator, adjust_delta_for_volatility
                vega = data.get('vega') or 0.0  # per 1% IV move (Schwab convention)
                if vega > 0 and spot > 0:
                    _est = get_kv_estimator()
                    k_v = _est.get_kv(ticker)
                    if k_v != 0.0:
                        delta_adj = adjust_delta_for_volatility(
                            delta=float(delta),
                            vega_per_pp=float(vega),
                            k_v=float(k_v),
                            spot=float(spot),
                        )
                        # Sanity guard: adjustment should be small (typically <2% of delta)
                        # If runaway result, fall back to raw delta
                        if abs(delta_adj - delta) <= max(0.10, 0.5 * abs(delta)):
                            delta_used = delta_adj
                            kv_diag['applied'] = True
                            kv_diag['kv'] = k_v
                            kv_diag['delta_adj'] = delta_adj
            except Exception as _kv_err:
                # Never break flow on k_v error — silent fallback to classical Δ
                kv_diag['error'] = str(_kv_err)

        unsigned = float(size) * float(last) * 100.0
        signed = 0.0
        if side != 0:
            signed = float(side) * float(size) * float(delta_used) * float(spot) * 100.0
        # Track raw-Δ signed flow as a parallel sum (for validation / comparison)
        signed_raw = 0.0
        if side != 0 and kv_diag['applied']:
            signed_raw = float(side) * float(size) * float(delta) * float(spot) * 100.0
        else:
            signed_raw = signed  # same when no adjustment

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
        except Exception as _e:
            log.debug(f"[FLOW-ACC] expiration_cache classify err for {symbol!r}: {_e}")

        # TEMP DIAGNOSTIC: track first 50 per ticker-bucket combo
        # Lock-protected — multiple streamer threads call this entry.
        with self._lock:
            _diag = getattr(self, '_classify_diag', None)
            if _diag is None:
                _diag = {}
                self._classify_diag = _diag
            if len(_diag) < 50:
                key = (ticker, bucket, classify_source)
                if key not in _diag:
                    _diag[key] = {'example_symbol': symbol, 'dte_field': dte, 'count': 0}
                _diag[key]['count'] += 1

            # TEMP DIAGNOSTIC 2: count by (ticker, expiration YYMMDD) to see
            # if ANY 260420 symbols are reaching us
            _date_diag = getattr(self, '_date_diag', None)
            if _date_diag is None:
                _date_diag = {}
                self._date_diag = _date_diag
            date_key = (ticker, symbol[6:12])
            _date_diag[date_key] = _date_diag.get(date_key, 0) + 1

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
                st.cum_signed_all_raw += signed_raw   # Phase 19 — parallel raw-Δ track
                if is_0dte:
                    st.cum_signed_0dte += signed
                    st.cum_signed_0dte_raw += signed_raw
                if kv_diag.get('applied'):
                    st.kv_adjusted_trades += 1
            st.last_update_ts = time.time()

            # Per-bucket aggregation (new: weekly/monthly/quarterly/leaps split)
            bkt = st.buckets.setdefault(bucket, _BucketState())
            bkt.cum_unsigned += unsigned
            bkt.trades += 1
            if side != 0:
                bkt.cum_signed += signed

            # ── AM vs PM SETTLEMENT (DEFINITIVE 2026-05-04) ──
            # Schwab streaming field 43 = `settlement_type` returns the
            # AUTHORITATIVE AM/PM indicator (verified via live cache probe at
            # /api/_debug/sym_cache_sample on 2026-05-04):
            #   'A' = AM-settled (cash-settled index monthly, SOQ at open)
            #   'P' = PM-settled (4 PM close — ETFs, weeklies, dailies)
            # Examples observed in production cache:
            #   SPX   260515P07205000  exp_type='S' settlement_type='A' (3rd-Fri OPEX, AM)
            #   SPXW  260513P07230000  exp_type='W' settlement_type='P' (weekly, PM)
            #   QQQ   260504C00673000  exp_type='W' settlement_type='P' (daily 0DTE, PM)
            # The previous logic checked `exp_type=='AM'` which Schwab NEVER sends —
            # it sends 'W'/'S'/'Q' for exp_type. The right field is settlement_type.
            _settle = (data.get('settlement_type') or '').upper()
            _settles_am = (_settle == 'A') and is_0dte

            if is_0dte:
                if _settles_am:
                    st.cohort_0dte_am_unsigned += unsigned
                    st.cohort_0dte_am_trades   += 1
                    if side != 0: st.cohort_0dte_am_signed += signed
                else:
                    st.cohort_0dte_pm_unsigned += unsigned
                    st.cohort_0dte_pm_trades   += 1
                    if side != 0: st.cohort_0dte_pm_signed += signed
            elif dte <= 7:
                st.cohort_weekly_unsigned += unsigned
                st.cohort_weekly_trades   += 1
                if side != 0: st.cohort_weekly_signed += signed
            elif dte <= 30:
                st.cohort_monthly_unsigned += unsigned
                st.cohort_monthly_trades   += 1
                if side != 0: st.cohort_monthly_signed += signed
            elif dte <= 90:
                st.cohort_quarterly_unsigned += unsigned
                st.cohort_quarterly_trades   += 1
                if side != 0: st.cohort_quarterly_signed += signed
            else:
                st.cohort_leaps_unsigned += unsigned
                st.cohort_leaps_trades   += 1
                if side != 0: st.cohort_leaps_signed += signed

            # ── Improvement #5 — Streaming OI delta classifier ──
            # Compare current OI to OI value at the LAST classified trade
            # on this strike (or first-seen baseline for the very first one).
            # The cumulative OI delta SINCE LAST TRADE captures all OI
            # changes that happened between trades, so we don't depend on
            # OI updating exactly at the moment of trade.
            #
            # Threshold: |oi_delta| ≥ max(2, 0.10 × size). Below threshold,
            # we can't attribute direction → 'unknown' rather than guess.
            if side != 0:
                _oi_class = 'unknown'
                _current_oi = data.get('open_interest')
                if _current_oi is not None and int(_current_oi or 0) > 0:
                    _current_oi = int(_current_oi)
                    # Reference OI: prior trade's OI, or first-seen if none
                    _ref_oi = self._last_trade_oi.get(
                        symbol, self._strike_first_oi.get(symbol, _current_oi)
                    )
                    oi_delta = _current_oi - _ref_oi
                    threshold = max(2, int(0.10 * size))
                    if abs(oi_delta) >= threshold:
                        if oi_delta > 0:
                            if side == 1:  _oi_class = 'opening_long'
                            else:          _oi_class = 'opening_short'
                        else:
                            if side == 1:  _oi_class = 'closing_short'
                            else:          _oi_class = 'closing_long'
                    # Update reference for next trade on this strike
                    self._last_trade_oi[symbol] = _current_oi
                # Otherwise → 'unknown' (no OI in this update)

                if _oi_class == 'opening_long':
                    st.cohort_opening_long_signed  += signed
                    st.cohort_opening_long_trades  += 1
                elif _oi_class == 'closing_short':
                    st.cohort_closing_short_signed += signed
                    st.cohort_closing_short_trades += 1
                elif _oi_class == 'opening_short':
                    st.cohort_opening_short_signed += signed
                    st.cohort_opening_short_trades += 1
                elif _oi_class == 'closing_long':
                    st.cohort_closing_long_signed  += signed
                    st.cohort_closing_long_trades  += 1
                else:
                    st.cohort_unknown_signed += signed
                    st.cohort_unknown_trades += 1

            # ── Improvement #4 — per-exchange flow attribution ──
            # Tradier timesale provides exchange in `exchange` key (single
            # letter codes). Schwab options stream provides field 40 also
            # as `exchange` (often longer MIC). Both arrive in the data
            # dict here. Skip if missing.
            _mic = (data.get('exchange') or '').strip().upper()
            if _mic:
                # Tradier single-letter exchange codes → canonical names
                # (per Tradier API docs):
                _TRADIER_MIC = {
                    'A': 'AMEX',  'B': 'NASDAQ_BX', 'C': 'CBOE',
                    'D': 'BOSX',  'E': 'EDGX',      'H': 'ISE_MRX',
                    'I': 'ISE',   'J': 'MIAX',      'M': 'MERC',
                    'N': 'NYSE',  'O': 'NOM',       'P': 'ARCA',
                    'Q': 'NSDQ',  'T': 'NASDAQ_BX', 'U': 'MEMX',
                    'W': 'C2',    'X': 'PHLX',      'Y': 'BATS_Y',
                    'Z': 'BATS_Z',
                    'S': 'SAPPHIRE',  # MIAX Sapphire
                }
                if len(_mic) == 1 and _mic in _TRADIER_MIC:
                    _mic = _TRADIER_MIC[_mic]
                # Normalize common Schwab/Tradier longer-form variants
                elif _mic in ('CBOE_C', 'XCBO'): _mic = 'CBOE'
                elif _mic in ('XNDQ', 'NDAQ'):    _mic = 'NSDQ'
                elif _mic in ('XPHL',):           _mic = 'PHLX'
                elif _mic in ('ARCX',):           _mic = 'ARCA'
                elif _mic in ('XBOX',):           _mic = 'BOSX'
                tk_be = self._by_exchange.setdefault(ticker, {})
                ent = tk_be.setdefault(_mic, {
                    'signed': 0.0, 'unsigned': 0.0, 'trades': 0,
                    'last_ts': 0.0,
                })
                ent['unsigned'] += unsigned
                ent['trades']   += 1
                if side != 0:
                    ent['signed'] += signed
                ent['last_ts'] = time.time()

            # ── Improvement #2 — 4-bucket Calls/Puts atomic-action split ──
            # Determine call vs put from contract_type (Schwab field 21)
            # with OSI fallback. Magnitude = abs(signed) so each bucket is
            # always ≥0; the SIGN of intent is encoded in WHICH bucket
            # gets the increment.
            #
            # 2026-05-08 FIX: prior parse used symbol[12] which assumes the
            # underlying is space-padded to 6 chars (standard OSI). Tradier
            # often delivers unpadded symbols ('AAPL240517C00100000') for
            # which position 12 lands on the year, not the type — those
            # trades hit the `_is_call = None` branch and were silently
            # dropped from cum_call_buy/_sell/_put_buy/_sell. Result:
            # atomic breakdown sum was 76% off vs cum_signed_all (live
            # measurement on 2026-05-08).
            #
            # The strike is ALWAYS the last 8 digits and the contract type
            # is ALWAYS the char immediately before it (position -9 from
            # the end), regardless of underlying length or padding. Parse
            # from the end to handle both formats.
            if side != 0:
                _ct = data.get('contract_type') or ''
                if _ct in ('C', 'CALL', 'call'):
                    _is_call = True
                elif _ct in ('P', 'PUT', 'put'):
                    _is_call = False
                elif len(symbol) >= 9 and symbol[-9] in ('C', 'P') and symbol[-8:].isdigit():
                    # OSI-from-end: <underlying><YYMMDD><C|P><STRIKE×1000_8_digit>
                    _is_call = (symbol[-9] == 'C')
                elif len(symbol) >= 13 and symbol[12] in ('C', 'P'):
                    # Legacy fallback for fixed 6-char-padded underlyings
                    _is_call = (symbol[12] == 'C')
                else:
                    _is_call = None  # unknown — skip rather than miscount
                    # 2026-05-08 audit instrumentation: track unparseable
                    # symbols so we can see what's escaping the atomic buckets.
                    # Aggregated counter logged every 1000 misses.
                    if not hasattr(self, '_isCall_unknown_count'):
                        self._isCall_unknown_count = 0
                        self._isCall_unknown_samples = []
                    self._isCall_unknown_count += 1
                    if len(self._isCall_unknown_samples) < 5:
                        self._isCall_unknown_samples.append({
                            'sym': symbol,
                            'ct':  _ct,
                            'len': len(symbol or ''),
                        })
                    if self._isCall_unknown_count % 1000 == 0:
                        log.warning(f"[FLOW-ACC] is_call=None count={self._isCall_unknown_count} "
                                    f"samples={self._isCall_unknown_samples}")
                        self._isCall_unknown_samples = []

                if _is_call is not None:
                    abs_signed = abs(signed)
                    if _is_call:
                        if side == 1:
                            st.cum_call_buy += abs_signed
                        else:
                            st.cum_call_sell += abs_signed
                    else:
                        if side == 1:
                            st.cum_put_buy += abs_signed
                        else:
                            st.cum_put_sell += abs_signed

            # ── Mispricing capture (Improvement #1) ──
            # Filters: both theo and mark > $0.50 (avoid div-by-near-zero on
            # far-OTM where small absolute differences become huge percent
            # mispricings — pure noise). Real signal lives in liquid contracts.
            if theo > 0.50 and mark > 0.50:
                mispricing_pct = (mark - theo) / theo
                tk_misp = self._mispricing.setdefault(ticker, {})
                prev = tk_misp.get(symbol) or {}
                # Volume accumulation in 5-min rolling window via EMA-style
                # decay: vol_t = vol_{t-1} × exp(−Δt/300) + new_size
                # → trades older than ~5min naturally fade out of the rolling
                #   sum without us needing a deque/timestamp scan.
                import math as _m
                _now_ts = time.time()
                last_ts = float(prev.get('last_ts', 0) or 0)
                dt = max(0.0, _now_ts - last_ts) if last_ts > 0 else 0.0
                decay = _m.exp(-dt / 300.0) if dt > 0 else 1.0
                prev_vol = float(prev.get('volume_5min', 0) or 0) * decay
                # Side from contract_type (Schwab field 21) with OSI fallback
                ct = data.get('contract_type') or ''
                if ct in ('C', 'CALL', 'call'):
                    side_cp = 'C'
                elif ct in ('P', 'PUT', 'put'):
                    side_cp = 'P'
                elif len(symbol) >= 13:
                    side_cp = symbol[12] if symbol[12] in ('C', 'P') else 'C'
                else:
                    side_cp = 'C'
                tk_misp[symbol] = {
                    'mispricing_pct': mispricing_pct,
                    'volume_5min':    prev_vol + size,
                    'last_ts':        _now_ts,
                    'strike':         float(data.get('strike') or 0),
                    'side':           side_cp,
                    'dte':            int(dte or 0),
                    'theo':           theo,
                    'mark':           mark,
                }

    def get_by_exchange(self, ticker: str, top_n: int = 10) -> dict:
        """Per-venue flow attribution snapshot for one ticker.

        Returns:
            {
              'ticker': 'QQQ',
              'total_signed':   float (cum across all venues),
              'total_unsigned': float,
              'total_trades':   int,
              'concentration_score': float (0..1, top1 / sum),
              'top1_mic':       str (dominant venue MIC),
              'venues': [
                {'mic', 'signed', 'unsigned', 'trades', 'share_signed_pct',
                 'share_unsigned_pct', 'last_ts'},
                ...   sorted by |signed| desc
              ]
            }

        Concentration interpretation:
          ≥ 0.60  one venue owns the flow → typically institutional algo
                  routing through a single execution path. Strong follow-
                  momentum signal IF aligned with regime mechanics.
          0.30-0.60  natural distribution; mix of inst + retail
          < 0.30   spread across many venues → retail FOMO clicking buy
                  from various brokers (each has different primary venue)
        """
        with self._lock:
            tk = (self._by_exchange.get(ticker) or {})
            if not tk:
                return {
                    'ticker': ticker,
                    'total_signed': 0.0, 'total_unsigned': 0.0,
                    'total_trades': 0,
                    'concentration_score': 0.0,
                    'top1_mic': None,
                    'venues': [],
                }
            entries = []
            total_abs_signed = 0.0
            total_unsigned   = 0.0
            total_trades     = 0
            for mic, e in tk.items():
                s = float(e.get('signed', 0) or 0)
                u = float(e.get('unsigned', 0) or 0)
                tr = int(e.get('trades', 0) or 0)
                total_abs_signed += abs(s)
                total_unsigned   += u
                total_trades     += tr
                entries.append({
                    'mic':       mic,
                    'signed':    round(s, 0),
                    'unsigned':  round(u, 0),
                    'trades':    tr,
                    'last_ts':   float(e.get('last_ts', 0) or 0),
                })
            # Compute share %. Zero-volume legs are reported as 0.0 (not
            # masked through max(X, 1.0)) so a downstream debugger can
            # distinguish "no flow at all" from "flow split evenly". The
            # JSON shape is unchanged — share_*_pct stays a float — but
            # the value reflects ground truth instead of a synthesized
            # 100/1.0 ratio that hid zero-input bugs.
            for x in entries:
                if total_abs_signed > 0:
                    x['share_signed_pct'] = round(100.0 * abs(x['signed']) / total_abs_signed, 2)
                else:
                    x['share_signed_pct'] = 0.0
                if total_unsigned > 0:
                    x['share_unsigned_pct'] = round(100.0 * x['unsigned'] / total_unsigned, 2)
                else:
                    x['share_unsigned_pct'] = 0.0
            # Sort by |signed| descending
            entries.sort(key=lambda r: abs(r['signed']), reverse=True)
            # Concentration: top1 / total_abs_signed
            top1 = entries[0] if entries else None
            concentration = (abs(top1['signed']) / total_abs_signed) if (top1 and total_abs_signed > 0) else 0.0
            # Sum signed (raw, not abs) — directional sum across venues
            total_signed_raw = sum(x['signed'] for x in entries)
            return {
                'ticker': ticker,
                'total_signed':   round(total_signed_raw, 0),
                'total_unsigned': round(total_unsigned, 0),
                'total_trades':   total_trades,
                'concentration_score': round(concentration, 4),
                'top1_mic':       top1['mic'] if top1 else None,
                'venues':         entries[:top_n],
            }

    def get_mispricing(self, ticker: str, top_n: int = 10) -> dict:
        """Theoretical-vs-Mark mispricing snapshot for one ticker.

        Returns:
            {
              'ticker': 'QQQ',
              'avg_mispricing_pct': float,   ticker-level vol-weighted average
              'total_volume_5min': int,      sum of decayed volumes across strikes
              'institutional_premium_score': float,   −100..+100 signed strength
              'top_strikes': [
                {'symbol', 'strike', 'side', 'dte', 'mispricing_pct',
                 'volume_5min', 'theo', 'mark'},
                ...
              ]
            }

        Score interpretation:
          ≥ +50:  HEAVY institutional accumulation paying premium ABOVE fair value
          0..+50: mild premium-paying
          −50..0: mild discount-trading
          ≤ −50:  HEAVY discount-selling (forced/distressed inventory dump)

        Volume-weighted: per-strike decayed 5-min volume is the weight on that
        strike's mispricing reading. A 10% mispricing on 1 contract is noise;
        a 1% mispricing on 1,000 contracts is signal.
        """
        with self._lock:
            tk = self._mispricing.get(ticker, {}) or {}
            if not tk:
                return {
                    'ticker': ticker,
                    'avg_mispricing_pct': 0.0,
                    'total_volume_5min': 0,
                    'institutional_premium_score': 0.0,
                    'top_strikes': [],
                }
            # Apply decay one more time to the snapshot read (so stale
            # entries that haven't ticked in minutes don't dominate).
            import math as _m
            now_ts = time.time()
            entries = []
            total_w = 0.0
            wsum_pct = 0.0
            for sym, e in tk.items():
                age = max(0.0, now_ts - float(e.get('last_ts', 0) or 0))
                if age > 600:    # 10-min hard expiry
                    continue
                w = float(e.get('volume_5min', 0) or 0) * _m.exp(-age / 300.0)
                if w <= 0:
                    continue
                pct = float(e.get('mispricing_pct', 0) or 0)
                wsum_pct += pct * w
                total_w += w
                entries.append({
                    'symbol':         sym,
                    'strike':         e.get('strike'),
                    'side':           e.get('side'),
                    'dte':            e.get('dte'),
                    'mispricing_pct': round(pct * 100, 3),  # report as %
                    'volume_5min':    int(round(w)),
                    'theo':           round(float(e.get('theo', 0) or 0), 4),
                    'mark':           round(float(e.get('mark', 0) or 0), 4),
                })
            avg_pct = (wsum_pct / total_w) if total_w > 0 else 0.0
            # Score: clip ±5% to ±100. So +3% pricing = +60 score.
            score = max(-100.0, min(100.0, avg_pct * 100 * 20.0))
            entries.sort(
                key=lambda r: abs(r['mispricing_pct']) * r['volume_5min'],
                reverse=True,
            )
            return {
                'ticker': ticker,
                'avg_mispricing_pct':           round(avg_pct * 100, 3),
                'total_volume_5min':            int(round(total_w)),
                'institutional_premium_score':  round(score, 1),
                'top_strikes': entries[:top_n],
            }

    def get_diag(self) -> dict:
        """TEMP diagnostic: show what we're classifying trades into."""
        diag = getattr(self, '_classify_diag', {})
        date_diag = getattr(self, '_date_diag', {})
        # Aggregate by (ticker, date) showing trade counts
        by_ticker_date = {}
        for (t, d), c in date_diag.items():
            by_ticker_date.setdefault(t, {})[d] = c
        # Per-ticker call/drop/dedup telemetry (added 2026-05-01)
        with self._lock:
            per_ticker_diag = {t: dict(d) for t, d in self._diag.items()}
        return {
            'classify': {
                f"{t}__{b}__via_{src}": {'symbol': v['example_symbol'], 'dte_field': v['dte_field'], 'count': v['count']}
                for (t, b, src), v in diag.items()
            },
            'trades_by_expiration': by_ticker_date,
            'per_ticker_path_diag': per_ticker_diag,
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
                # Improvement #2 — atomic-action breakdown
                "cum_call_buy":   st.cum_call_buy,
                "cum_call_sell":  st.cum_call_sell,
                "cum_put_buy":    st.cum_put_buy,
                "cum_put_sell":   st.cum_put_sell,
                # Improvement #3 — 6-cohort exp_type/DTE breakdown
                "cohort_0dte_am_signed":   st.cohort_0dte_am_signed,
                "cohort_0dte_am_trades":   st.cohort_0dte_am_trades,
                "cohort_0dte_pm_signed":   st.cohort_0dte_pm_signed,
                "cohort_0dte_pm_trades":   st.cohort_0dte_pm_trades,
                "cohort_weekly_signed":    st.cohort_weekly_signed,
                "cohort_weekly_trades":    st.cohort_weekly_trades,
                "cohort_monthly_signed":   st.cohort_monthly_signed,
                "cohort_monthly_trades":   st.cohort_monthly_trades,
                "cohort_quarterly_signed": st.cohort_quarterly_signed,
                "cohort_quarterly_trades": st.cohort_quarterly_trades,
                "cohort_leaps_signed":     st.cohort_leaps_signed,
                "cohort_leaps_trades":     st.cohort_leaps_trades,
            }

    @staticmethod
    def _classify_setup_mode(call_buy: float, call_sell: float,
                             put_buy: float, put_sell: float) -> dict:
        """Read the four-bucket pattern as a setup-type label.

        Returns dict with:
          mode             — one of: AGG_LONG, AGG_SHORT, VOL_HARVEST,
                             HEDGED_LONG, HEDGED_SHORT, MIXED, IDLE
          mode_confidence  — 0..100 (share of dominant pair vs noise)
          bullish_directional — call_buy + put_sell
          bearish_directional — call_sell + put_buy
          vol_long             — call_buy + put_buy   (long premium)
          vol_short            — call_sell + put_sell (short premium)
        """
        bullish_dir  = call_buy + put_sell
        bearish_dir  = call_sell + put_buy
        vol_long     = call_buy + put_buy
        vol_short    = call_sell + put_sell
        total = bullish_dir + bearish_dir
        if total < 1_000_000:
            return {
                'mode': 'IDLE', 'mode_confidence': 0,
                'bullish_directional': round(bullish_dir, 0),
                'bearish_directional': round(bearish_dir, 0),
                'vol_long':  round(vol_long, 0),
                'vol_short': round(vol_short, 0),
            }
        # Signal A: directional dominance
        # ratio = bigger / smaller; > 1.0 always
        bigger_dir = max(bullish_dir, bearish_dir)
        smaller_dir = min(bullish_dir, bearish_dir)
        dir_ratio = (bigger_dir / max(smaller_dir, 1.0)) if smaller_dir > 0 else 99.0
        # imbalance as fraction of total — useful for confidence scaling
        dir_imbalance = abs(bullish_dir - bearish_dir) / total

        # Signal B: vol-flow direction
        vol_total = vol_long + vol_short
        bigger_vol = max(vol_long, vol_short)
        smaller_vol = min(vol_long, vol_short)
        vol_ratio = (bigger_vol / max(smaller_vol, 1.0)) if smaller_vol > 0 else 99.0
        vol_imbalance = (abs(vol_long - vol_short) / vol_total) if vol_total > 0 else 0

        # Classification thresholds (tightened from empirical session data —
        # SPY/QQQ rarely cross 40% imbalance even on directional days; 15%
        # is the realistic "meaningful directional bias" threshold).
        DIR_TRIGGER_PCT  = 0.15    # ≥15% imbalance = directional setup
        DIR_RATIO_TRIGGER = 1.20   # OR 1.2:1 ratio of bigger/smaller
        VOL_TRIGGER_PCT  = 0.25    # ≥25% vol imbalance = harvest/long bias
        HEDGE_RATIO      = 0.30    # put_buy / call_buy >0.30 = HEDGED label

        is_directional = (dir_imbalance > DIR_TRIGGER_PCT
                          or dir_ratio  > DIR_RATIO_TRIGGER)

        if is_directional:
            if bullish_dir > bearish_dir:
                # Bullish: AGG_LONG (clean) vs HEDGED_LONG (with put insurance)
                if call_buy > 0 and put_buy > call_buy * HEDGE_RATIO:
                    mode = 'HEDGED_LONG'
                else:
                    mode = 'AGG_LONG'
            else:
                if call_sell > 0 and put_sell > call_sell * HEDGE_RATIO:
                    mode = 'HEDGED_SHORT'
                else:
                    mode = 'AGG_SHORT'
            # Confidence: 15% imbalance → 50, 30% → 80, 50%+ → 100
            confidence = int(round(min(100, dir_imbalance * 200 + 20)))
        elif vol_imbalance > VOL_TRIGGER_PCT:
            mode = 'VOL_HARVEST' if vol_short > vol_long else 'VOL_LONG'
            confidence = int(round(min(100, vol_imbalance * 150 + 20)))
        else:
            mode = 'MIXED'
            confidence = int(round(max(dir_imbalance, vol_imbalance) * 100))
        return {
            'mode': mode,
            'mode_confidence': confidence,
            'bullish_directional': round(bullish_dir, 0),
            'bearish_directional': round(bearish_dir, 0),
            'vol_long':  round(vol_long, 0),
            'vol_short': round(vol_short, 0),
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
        # Improvement #2 — classify the 4-bucket pattern into a setup mode
        action = self._classify_setup_mode(
            st.cum_call_buy, st.cum_call_sell,
            st.cum_put_buy,  st.cum_put_sell,
        )
        # Improvement #3 — derive institutional vs retail share from cohorts.
        # Documented framing (Bryzgalova/Pavlova 2023, CBOE participant data):
        #   institutional-skewed = 0dte_am + monthly + quarterly + leaps
        #   retail-skewed        = 0dte_pm + weekly
        #   (this is a SKEW, not a clean split — see code comments in CCS)
        c_inst = (abs(st.cohort_0dte_am_signed)
                  + abs(st.cohort_monthly_signed)
                  + abs(st.cohort_quarterly_signed)
                  + abs(st.cohort_leaps_signed))
        c_retail = (abs(st.cohort_0dte_pm_signed)
                    + abs(st.cohort_weekly_signed))
        c_total = c_inst + c_retail
        institutional_share = (c_inst / c_total) if c_total > 0 else 0.0
        # Improvement #5 — Pan & Poteshman opening-flow alpha metric.
        # Sum the OI-classified buckets:
        #   opening_signed = opening_long + opening_short  (FRESH positioning)
        #   closing_signed = closing_short + closing_long  (POSITION MGMT)
        # The directional alpha lives ONLY in opening_signed. Closing flow
        # is information-free after-the-fact unwinding.
        opening_signed = (st.cohort_opening_long_signed
                          + st.cohort_opening_short_signed)
        closing_signed = (st.cohort_closing_short_signed
                          + st.cohort_closing_long_signed)
        unknown_signed = st.cohort_unknown_signed
        oi_classified_total = (st.cohort_opening_long_trades
                               + st.cohort_opening_short_trades
                               + st.cohort_closing_short_trades
                               + st.cohort_closing_long_trades)
        oi_classified_share = (oi_classified_total
                               / max(1, oi_classified_total + st.cohort_unknown_trades))
        return {
            "ticker": ticker,
            # Legacy 2-way split (flow pane keeps rendering from these)
            "cum_signed_0dte": st.cum_signed_0dte,
            "cum_signed_all": st.cum_signed_all,
            "cum_unsigned_0dte": st.cum_unsigned_0dte,
            "cum_unsigned_all": st.cum_unsigned_all,
            "trades_0dte": st.trades_0dte,
            "trades_all": st.trades_all,
            # Phase 19 (Kobayashi 2025) — Volatility-Delta validation telemetry
            "cum_signed_0dte_raw": st.cum_signed_0dte_raw,
            "cum_signed_all_raw":  st.cum_signed_all_raw,
            "kv_adjusted_trades":  st.kv_adjusted_trades,
            # Improvement #2 — atomic-action 4-bucket breakdown + setup mode
            "cum_call_buy":   st.cum_call_buy,
            "cum_call_sell":  st.cum_call_sell,
            "cum_put_buy":    st.cum_put_buy,
            "cum_put_sell":   st.cum_put_sell,
            "setup_mode":     action.get('mode'),
            "setup_confidence": action.get('mode_confidence'),
            "bullish_directional": action.get('bullish_directional'),
            "bearish_directional": action.get('bearish_directional'),
            "vol_long":            action.get('vol_long'),
            "vol_short":           action.get('vol_short'),
            # Improvement #3 — 6-cohort exp_type/DTE signed flow + share metric
            "cohort_0dte_am_signed":   st.cohort_0dte_am_signed,
            "cohort_0dte_pm_signed":   st.cohort_0dte_pm_signed,
            "cohort_weekly_signed":    st.cohort_weekly_signed,
            "cohort_monthly_signed":   st.cohort_monthly_signed,
            "cohort_quarterly_signed": st.cohort_quarterly_signed,
            "cohort_leaps_signed":     st.cohort_leaps_signed,
            "cohort_0dte_am_trades":   st.cohort_0dte_am_trades,
            "cohort_0dte_pm_trades":   st.cohort_0dte_pm_trades,
            "cohort_weekly_trades":    st.cohort_weekly_trades,
            "cohort_monthly_trades":   st.cohort_monthly_trades,
            "cohort_quarterly_trades": st.cohort_quarterly_trades,
            "cohort_leaps_trades":     st.cohort_leaps_trades,
            "institutional_share":     round(institutional_share, 4),
            # Improvement #5 — OI-classified opening/closing flow
            "cohort_opening_long_signed":  st.cohort_opening_long_signed,
            "cohort_opening_long_trades":  st.cohort_opening_long_trades,
            "cohort_closing_short_signed": st.cohort_closing_short_signed,
            "cohort_closing_short_trades": st.cohort_closing_short_trades,
            "cohort_opening_short_signed": st.cohort_opening_short_signed,
            "cohort_opening_short_trades": st.cohort_opening_short_trades,
            "cohort_closing_long_signed":  st.cohort_closing_long_signed,
            "cohort_closing_long_trades":  st.cohort_closing_long_trades,
            "cohort_unknown_signed":       st.cohort_unknown_signed,
            "cohort_unknown_trades":       st.cohort_unknown_trades,
            "opening_signed_flow":         round(opening_signed, 0),
            "closing_signed_flow":         round(closing_signed, 0),
            "unknown_signed_flow":         round(unknown_signed, 0),
            "oi_classified_share":         round(oi_classified_share, 3),
            # New: per-bucket breakdown for alert labels
            "buckets": bucket_data,
        }

    def evict_stale(self, max_age_s: float = 86400.0) -> int:
        """Drop per-symbol bookkeeping older than `max_age_s`.

        Without periodic eviction, `_last_trade_time`, `_last_trade_oi`,
        `_strike_first_oi`, `_mispricing[*]`, and `_by_exchange[*]` grow
        proportionally to the number of unique option symbols ever seen
        in the process — tens of thousands per market day across QQQ +
        SPY + index roots.

        We use the most recent activity timestamps we already track:
          - `_last_trade_time[symbol]` is the trade_time of the last
             trade we classified on `symbol` (units: int as Schwab sends
             them — varies); we treat absence as "never traded" → safe
             to drop.
          - `_mispricing[ticker][symbol]['last_ts']` is float seconds
             since epoch — directly comparable to time.time().

        Conservative policy: drop _last_trade_oi/_strike_first_oi for
        symbols we haven't observed via _mispricing in `max_age_s`. This
        couples eviction to a wall-clock proxy without needing a third
        timestamp dict.
        """
        cutoff = time.time() - max_age_s
        evicted = 0
        with self._lock:
            # Drop mispricing readings older than cutoff
            for tk, syms in list(self._mispricing.items()):
                for sym, e in list(syms.items()):
                    if float(e.get('last_ts', 0) or 0) < cutoff:
                        del syms[sym]
                        # Tied OI bookkeeping for the same symbol
                        self._last_trade_oi.pop(sym, None)
                        self._strike_first_oi.pop(sym, None)
                        self._last_trade_time.pop(sym, None)
                        evicted += 1
                if not syms:
                    del self._mispricing[tk]
            # Per-exchange ledger: drop venue rows whose last_ts is stale
            for tk, venues in list(self._by_exchange.items()):
                for mic, ent in list(venues.items()):
                    if float(ent.get('last_ts', 0) or 0) < cutoff:
                        del venues[mic]
                if not venues:
                    del self._by_exchange[tk]
        if evicted > 0:
            log.info(f"[FLOW-ACC] evict_stale: dropped {evicted} stale symbols (>{max_age_s/3600:.1f}h)")
        return evicted

    # ── 2h rolling history persistence (added 2026-05-01) ─────────────────
    _HISTORY_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'logs', 'flow_history_buffer.json',
    )

    def _make_history_snapshot(self, ticker: str, st) -> dict:
        """Compact snapshot for history buffer — ONLY the fields the chart
        renders. Trimmed vs full _ticker_dict to keep buffer small."""
        return {
            't':   int(time.time() * 1000),
            's0':  st.cum_signed_0dte,
            'sa':  st.cum_signed_all,
            'u0':  st.cum_unsigned_0dte,
            'ua':  st.cum_unsigned_all,
            # C/P decomposition for that view mode
            'cb':  st.cum_call_buy,
            'cs':  st.cum_call_sell,
            'pb':  st.cum_put_buy,
            'ps':  st.cum_put_sell,
            # 6-cohort drill-down for cohort view mode
            'c_0am': st.cohort_0dte_am_signed,
            'c_0pm': st.cohort_0dte_pm_signed,
            'c_wk':  st.cohort_weekly_signed,
            'c_mo':  st.cohort_monthly_signed,
            'c_qt':  st.cohort_quarterly_signed,
            'c_lp':  st.cohort_leaps_signed,
        }

    def _capture_history_snapshot(self) -> None:
        """Append a snapshot to each active ticker's history buffer."""
        from collections import deque as _deque
        with self._lock:
            for ticker, st in self._state.items():
                if st.trades_all <= 0:
                    continue
                buf = self._history_buffer.get(ticker)
                if buf is None:
                    buf = _deque(maxlen=self._HISTORY_MAXLEN)
                    self._history_buffer[ticker] = buf
                buf.append(self._make_history_snapshot(ticker, st))

    def _persist_history_to_disk(self) -> None:
        """Atomic write of history buffer to disk. Idempotent."""
        try:
            os.makedirs(os.path.dirname(self._HISTORY_FILE), exist_ok=True)
            tmp = self._HISTORY_FILE + '.tmp'
            with self._lock:
                payload = {
                    'date': time.strftime('%Y-%m-%d'),
                    'saved_at': time.time(),
                    'tickers': {
                        ticker: list(buf)
                        for ticker, buf in self._history_buffer.items()
                    },
                }
            import json as _json
            with open(tmp, 'w') as _f:
                _json.dump(payload, _f, separators=(',', ':'))
            os.replace(tmp, self._HISTORY_FILE)
        except Exception as e:
            log.debug(f"[FLOW-ACC] history persist failed: {e}")

    def _restore_history_from_disk(self) -> None:
        """Load history from disk on startup. Only restores if date matches
        today. Stale (yesterday or older) buffers are ignored — flow flips
        sign at session reset so old data is misleading."""
        from collections import deque as _deque
        try:
            if not os.path.exists(self._HISTORY_FILE):
                return
            import json as _json
            with open(self._HISTORY_FILE) as _f:
                payload = _json.load(_f)
            saved_date = payload.get('date', '')
            today = time.strftime('%Y-%m-%d')
            if saved_date != today:
                log.info(f"[FLOW-ACC] history file is from {saved_date}, "
                         f"today is {today} — starting fresh")
                return
            tickers_data = payload.get('tickers', {})
            restored_count = 0
            for ticker, snapshots in tickers_data.items():
                buf = _deque(maxlen=self._HISTORY_MAXLEN)
                for snap in snapshots[-self._HISTORY_MAXLEN:]:
                    buf.append(snap)
                self._history_buffer[ticker] = buf
                restored_count += len(buf)

                # 2026-05-05: also seed the LIVE _state cumulative counters
                # from the last snapshot. Without this, a server restart wipes
                # cum_signed_* to 0 even though the history buffer still shows
                # the correct cum value at the last persist point. Result is
                # a chart "jump" at the restart boundary. By copying terminal
                # snapshot values into _state, live updates continue from
                # exactly where the buffer left off.
                if buf:
                    last = buf[-1]
                    st = self._state.setdefault(ticker, _TickerState())
                    st.cum_signed_0dte    = float(last.get('s0', 0) or 0)
                    st.cum_signed_all     = float(last.get('sa', 0) or 0)
                    st.cum_unsigned_0dte  = float(last.get('u0', 0) or 0)
                    st.cum_unsigned_all   = float(last.get('ua', 0) or 0)
                    # 2026-05-08: restore atomic counters from buffer to keep
                    # the flow chart's call_buy/sell/put_buy/sell lines
                    # CONTINUOUS across bridge restarts. Pre-OCC-fix buffers
                    # carry slight undercounts (un-padded symbols were
                    # skipped), but visually-stable chart > slightly-off
                    # absolute values. New trades after restart accumulate
                    # correctly via the post-fix parser, so the absolute
                    # values self-correct as the day progresses.
                    st.cum_call_buy       = float(last.get('cb', 0) or 0)
                    st.cum_call_sell      = float(last.get('cs', 0) or 0)
                    st.cum_put_buy        = float(last.get('pb', 0) or 0)
                    st.cum_put_sell       = float(last.get('ps', 0) or 0)
                    st.cohort_0dte_am_signed = float(last.get('c_0am', 0) or 0)
                    st.cohort_0dte_pm_signed = float(last.get('c_0pm', 0) or 0)
                    st.cohort_weekly_signed   = float(last.get('c_wk', 0) or 0)
                    st.cohort_monthly_signed  = float(last.get('c_mo', 0) or 0)
                    st.cohort_quarterly_signed= float(last.get('c_qt', 0) or 0)
                    st.cohort_leaps_signed    = float(last.get('c_lp', 0) or 0)

            log.info(f"[FLOW-ACC] Restored {restored_count} history snapshots "
                     f"across {len(tickers_data)} tickers from {self._HISTORY_FILE} "
                     f"(also seeded live _state from terminal snapshots)")
        except Exception as e:
            log.warning(f"[FLOW-ACC] history restore failed: {e}")

    def get_history(self, ticker: str, since_ts_ms: Optional[int] = None) -> list:
        """Return list of compact snapshots for a ticker, optionally filtered
        to those after `since_ts_ms`."""
        with self._lock:
            buf = self._history_buffer.get(ticker, [])
            snapshots = list(buf)
        if since_ts_ms is not None and since_ts_ms > 0:
            snapshots = [s for s in snapshots if s.get('t', 0) > since_ts_ms]
        return snapshots

    def _emit_loop(self) -> None:
        """Background loop: broadcasts per-ticker flow every emit_interval,
        and feeds AlertEngine with (state + spot) so divergence/cross/spike
        detectors can fire on live data."""
        last_evict_ts = time.time()
        EVICT_INTERVAL_S = 3600.0  # hourly housekeeping is plenty
        while self._running:
            time.sleep(self._emit_interval)
            now_for_evict = time.time()
            if (now_for_evict - last_evict_ts) > EVICT_INTERVAL_S:
                try:
                    self.evict_stale(max_age_s=86400.0)
                except Exception as _ev:
                    log.debug(f"[FLOW-ACC] evict_stale err: {_ev}")
                last_evict_ts = now_for_evict
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

            # Broadcast flow_update socket event FIRST so UI state reflects
            # the snapshot that any alert we fire below refers to. Reversing
            # the order races: alerts arriving before the snapshot they
            # describe (pane renders stale data while the alert flashes).
            if self._socketio:
                try:
                    self._socketio.emit(
                        "flow_update",
                        {"t": now_ms, "tickers": snapshot},
                    )
                except Exception as e:
                    log.debug(f"[FLOW-ACC] emit failed: {e}")

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

            # ── 2h history buffer (added 2026-05-01) ────────────────────
            # Snapshot every HISTORY_INTERVAL_S, persist every PERSIST_INTERVAL_S.
            # Persistence is atomic via tmp+rename; safe across server kills.
            try:
                if (now_sec - self._history_last_snapshot_ts) >= self._HISTORY_INTERVAL_S:
                    self._capture_history_snapshot()
                    self._history_last_snapshot_ts = now_sec
                if (now_sec - self._history_last_persist_ts) >= self._HISTORY_PERSIST_INTERVAL_S:
                    self._persist_history_to_disk()
                    self._history_last_persist_ts = now_sec
            except Exception as e:
                log.debug(f"[FLOW-ACC] history snapshot/persist failed: {e}")


# Global singleton, instantiated from schwab_bridge.start_schwab_bridge
_accumulator: Optional[FlowAccumulator] = None


def get_accumulator() -> Optional[FlowAccumulator]:
    return _accumulator


def init_accumulator(socketio) -> FlowAccumulator:
    """Create the global singleton (idempotent)."""
    global _accumulator
    if _accumulator is None:
        _accumulator = FlowAccumulator(socketio=socketio, emit_interval_sec=0.5)
        _accumulator.start()
    return _accumulator
