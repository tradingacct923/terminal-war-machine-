"""MM Attribution — per-exchange L2 structural capture.

Reads Schwab OPTIONS_BOOK snapshots + Tradier timesale prints on the 120 QQQ
contracts we subscribe to. Emits TYPED STRUCTURAL events (no magnitude
thresholds, no time cutoffs in classification). Maintains four live views
consumable via REST:

  1. NBBO ownership ribbon — event-driven samples of which exchanges are at
     best-bid and best-ask over time.
  2. Lead-follower arrival sequence — for each new price level, the order
     exchanges arrived in + inter-arrival latencies.
  3. Capture-vs-post — per-exchange: time-integrated presence at NBBO vs.
     count of Tradier prints filled on that exchange, since session open.
  4. Impulse response — after each print on a contract, capture every book
     update until the NEXT print on the same contract lands (structural
     boundary, no fixed time cap).

Every number this module emits is one of:
  - Structural measurement (event-driven boundary)
  - Session-cumulative counter (resets at 09:30 ET)
  - Raw timestamp / price / size

No `>=`, no `<`, no magnitude classification, no "thick/thin/fast/slow"
labels. The module is a structural data layer.

Disk: all events appended to logs/mm_events_YYYYMMDD.jsonl. Finalized impulse
response records are written as a single compact line when the next print
closes the window.
"""
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)

# Forward-declare wall_signals hook — lazy imported to avoid circular risk.
try:
    from connectors import wall_signals as _wall_signals
except Exception:
    _wall_signals = None

# ── Session boundary (natural market-open boundary, not a magnitude cutoff) ─
# US Eastern time. Market cash-session open is 09:30 ET. Reused pattern from
# server.py and logs/replay_engine.py.
_ET_OFFSET_HOURS = -4  # EDT during most of trading calendar; caller-tolerant


def _session_open_epoch_for(ts: float) -> float:
    """Return 09:30 ET epoch for the trading day that contains `ts`."""
    et = timezone(timedelta(hours=_ET_OFFSET_HOURS))
    dt = datetime.fromtimestamp(ts, tz=et)
    open_dt = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    return open_dt.timestamp()


def _yyyymmdd_for(ts: float) -> str:
    """ET trading-day yyyymmdd for the timestamp."""
    et = timezone(timedelta(hours=_ET_OFFSET_HOURS))
    return datetime.fromtimestamp(ts, tz=et).strftime('%Y%m%d')


# ── Tradier single-letter exch code → Schwab MPID ──────────────────────────
# Tradier timesale prints carry single-letter OPRA-style exchange codes;
# Schwab OPTIONS_BOOK levels use full MPID strings. Without normalization the
# capture-vs-post accumulator keys the two sides under different venue
# identities, so every exchange appears twice in capture_rows (once with
# posted%=0 from the print count, once with caught=0 from the book presence).
# Verified on 2026-04-23 against live Schwab MPIDs and Tradier print codes.
_TRADIER_TO_MPID = {
    # OPRA participant codes → Schwab OPTIONS_BOOK MPIDs.
    # Mapping targets chosen from live-observed Schwab MPIDs on 2026-04-23:
    # {AMEX, BATS, BOSX, CBOE, EDGX, GMNI, ISEX, MEMX, MERC, MIAX, NSDQ, NYSE,
    #  PACX, PHLX, XBXO, S}.
    'A': 'AMEX',   # NYSE American Options
    'B': 'BOSX',   # Nasdaq BX Options
    'C': 'CBOE',   # Cboe Options Exchange
    'D': 'MIAX',   # MIAX Pearl Options — OPRA D, folded into MIAX family
                   # in Schwab's OPTIONS_BOOK (no separate Pearl MPID seen).
    'E': 'MIAX',   # MIAX Emerald — OPRA E, folded into MIAX family
                   # in Schwab's OPTIONS_BOOK (no separate Emerald MPID seen).
    'H': 'XBXO',   # BOX Options Exchange
    'I': 'ISEX',   # Nasdaq ISE
    'J': 'MERC',   # Nasdaq MRX (ISE Mercury)
    'M': 'MIAX',   # Miami International (MIAX Options)
    'N': 'PACX',   # NYSE Arca Options (OPRA N)
    'P': 'PACX',   # NYSE Arca Options (OPRA historic alias)
    'Q': 'NSDQ',   # Nasdaq Options Market
    'U': 'MEMX',   # Members Exchange Options
    'W': 'CBOE',   # Cboe C2 — folded into CBOE family (no separate Schwab MPID)
    'X': 'PHLX',   # Nasdaq PHLX
    'Y': 'EDGX',   # Cboe EDGX Options
    'Z': 'BATS',   # Cboe BZX
    # 'S' and 'T' codes still pass through unmapped — they appear in Tradier
    # timesale but Schwab OPTIONS_BOOK doesn't expose 'S'/'T' MPIDs visibly,
    # so we let them keep their own venue identity rather than guess wrong.
    # If they later show up as posted-but-never-caught (or vice versa) in
    # capture_rows, that's the signal to re-map.
}

# Tracks unmapped codes so we warn once per code, not per print.
_WARNED_UNKNOWN_EXCH: set = set()


def _normalize_exch(code: str) -> str:
    """Map a Tradier single-letter exch code to the Schwab MPID used in
    OPTIONS_BOOK levels. Unknown short codes pass through unchanged and
    trigger a one-shot warning so the mapping can be extended.

    MPIDs (strings length > 1) pass through unchanged — the feed itself is
    already canonical.
    """
    if not code:
        return code
    if len(code) > 1:
        return code
    mapped = _TRADIER_TO_MPID.get(code)
    if mapped is None:
        if code not in _WARNED_UNKNOWN_EXCH:
            _WARNED_UNKNOWN_EXCH.add(code)
            log.warning("[MM-ATTR] unknown Tradier exch code: %s", code)
        return code
    return mapped


# ── Infrastructure caps (CONFIGURED, not signal thresholds) ────────────────
# These bound in-memory growth so the process doesn't OOM during long
# sessions. They do NOT filter which events get written to disk — the JSONL
# log is the source of truth. See docs/MEASURED_VALUES.md for provenance.
RIBBON_SAMPLE_CAP = 50_000        # ~5h of event-driven ribbon samples per sym
FORMATION_RING_CAP = 10_000       # closed formations kept in-memory per sym


# ── Per-contract state ─────────────────────────────────────────────────────
# Compact snapshot form: {'ts', 'b': [[price, size, mm_count, [exchs]], ...], 'a': [...]}.
_prev_book: dict = {}

# NBBO ribbon. sym -> deque of samples.
# Sample: {'ts', 'bid': [exch_ids], 'ask': [exch_ids], 'bid_size', 'ask_size', 'bid_price', 'ask_price'}
# Event-driven: appended ONLY when the best-bid exchange-list, best-ask
# exchange-list, best-bid price, or best-ask price changes. Size-only changes
# on a deeper level do not create a ribbon sample.
_nbbo_ribbon: dict = {}

# Active formations. sym -> {(side, price): {'first_ts', 'arrivals': [{'t_ms', 'exch', 'size'}...], 'exchs_seen': set}}.
# When the price level vanishes from the top-5 book, the formation is moved
# to the `_formations` ring below.
_active_formations: dict = {}

# Completed/closed formations. sym -> deque (newest first).
_formations: dict = {}

# Capture vs post accumulators.
# sym -> {exch: {'posted_bid_time': float, 'posted_ask_time': float, 'caught_count': int}}.
_capture: dict = {}
# Per-contract total posted time denominator.
# sym -> {'total_bid_time': float, 'total_ask_time': float, 'last_update_ts': float}.
_capture_totals: dict = {}

# Active impulse capture. sym -> {'print_ts', 'print_price', 'print_exch',
# 'print_size', 'print_aggressor', 'ticks': [{'t_ms', 'mm_count', 'total_size', 'exchs'}]} or None.
_impulse_active: dict = {}

# Per-contract event counts since session open (for contract ranking).
_contract_event_counts: dict = {}

# Outbound event queue for socket emission.
_event_queue: deque = deque()

# Ref-counted watcher map: sym -> number of concurrent pane subscribers.
# Populated by server.py socket handlers (watch/unwatch). Drives the push
# cadence of `mm_contract_state`.
_watchers: dict = {}

# Per-symbol last-push timestamp (monotonic). Used to gate cadence so we don't
# re-emit the same fat state payload every 50ms flush tick.
_last_state_push: dict = {}

# CONFIGURED — cadence for pushing contract_state over socket (seconds).
# 250ms = 4Hz, 4× smoother than the old 1s REST poll but still cheap enough
# to run for multiple watched symbols concurrently.
STATE_PUSH_INTERVAL_SEC = 0.25

# Session-open epoch (reset check on every update).
_session_open_ts: float = 0.0

# Locks
_state_lock = Lock()
_queue_lock = Lock()
_log_lock = Lock()
_watch_lock = Lock()

# Disk log
_log_fh = None
_log_date: str = ''
_log_count: int = 0

# Phase D — cached schwab_bridge reference so we don't re-import on every
# IMPULSE_CLOSED enrichment. Initialized on first access under _state_lock.
_sb_ref = None

def _get_schwab_bridge():
    """Lazy cache of the schwab_bridge module to avoid per-print import cost."""
    global _sb_ref
    if _sb_ref is None:
        try:
            from background_engine import schwab_bridge as _sb
            _sb_ref = _sb
        except Exception:
            _sb_ref = None
    return _sb_ref


# ── Disk log helpers ────────────────────────────────────────────────────────

def _log_path(ts: float) -> str:
    logs_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, f'mm_events_{_yyyymmdd_for(ts)}.jsonl')


def _ensure_log(ts: float) -> None:
    global _log_fh, _log_date
    today = _yyyymmdd_for(ts)
    if _log_fh is None or _log_date != today:
        try:
            if _log_fh is not None:
                _log_fh.close()
        except Exception:
            pass
        # 2026-05-07: 64KB buffer (was line-buffered). Each OPTIONS_BOOK
        # update can trigger a log write; at 67 books/sec line-buffered
        # = 67 disk syncs/sec contributing to gevent-loop CPU saturation.
        _log_fh = open(_log_path(ts), 'a', buffering=65536)
        _log_date = today


def _write_log(rec: dict) -> None:
    global _log_count
    try:
        with _log_lock:
            _ensure_log(rec.get('ts', time.time()))
            _log_fh.write(json.dumps(rec, separators=(',', ':'), default=str) + '\n')
            _log_count += 1
    except Exception:
        pass


# ── Session reset ──────────────────────────────────────────────────────────

def _maybe_reset_session(ts: float) -> None:
    """If ts belongs to a trading day after the one tracked by
    _session_open_ts, reset all cumulative state.

    Caller MUST hold `_state_lock` before calling this (it mutates the
    shared per-contract maps). `_event_queue` is drained under its own
    `_queue_lock` to avoid leaking stale events past the session boundary.
    """
    global _session_open_ts
    new_open = _session_open_epoch_for(ts)
    if _session_open_ts == 0.0 or new_open > _session_open_ts:
        _session_open_ts = new_open
        _prev_book.clear()
        _nbbo_ribbon.clear()
        _active_formations.clear()
        _formations.clear()
        _capture.clear()
        _capture_totals.clear()
        _impulse_active.clear()
        _contract_event_counts.clear()
        with _queue_lock:
            _event_queue.clear()
        # Disk log rolls automatically on next write (new yyyymmdd).


# ── Book diff helpers ──────────────────────────────────────────────────────

def _levels_by_price(levels) -> dict:
    """Compact list of [p,sz,mmc,[exchs]] -> {price: (size, mm_count, [exchs])}."""
    out = {}
    for lvl in (levels or []):
        try:
            price = float(lvl[0])
            size = int(lvl[1] or 0)
            mmc = int(lvl[2] or 0)
            exchs = list(lvl[3] or [])
        except Exception:
            continue
        out[price] = (size, mmc, exchs)
    return out


def _pack_levels(levels) -> list:
    """Convert incoming Schwab book levels (list of dicts) to compact form.

    Accepts every level and every exchange the streamer emits — no cap.
    Schwab OPTIONS_BOOK schema currently publishes 5 levels per side, but we
    do not enforce that here; if the schema ever widens we pass it through.
    """
    out = []
    for lvl in (levels or []):
        mms = [m.get('id', '') for m in (lvl.get('market_makers') or [])]
        out.append([
            float(lvl.get('price', 0) or 0),
            int(lvl.get('size', 0) or 0),
            int(lvl.get('mm_count', 0) or 0),
            mms,
        ])
    return out


# ── Main hook: book update ─────────────────────────────────────────────────

def on_book_update(symbol: str, bids_raw: list, asks_raw: list, ts: float) -> None:
    """Called from _on_options_book. Diffs vs previous snapshot for `symbol`,
    emits structural events, updates capture accumulators, advances any
    active impulse capture.
    """
    if not symbol:
        return
    try:
        new_b = _pack_levels(bids_raw)
        new_a = _pack_levels(asks_raw)
        if not new_b and not new_a:
            return

        with _state_lock:
            _maybe_reset_session(ts)
            prev = _prev_book.get(symbol)
            _diff_and_emit(symbol, prev, new_b, new_a, ts)
            _update_capture(symbol, prev, new_b, new_a, ts)
            _update_ribbon(symbol, prev, new_b, new_a, ts)
            _advance_impulse(symbol, new_b, new_a, ts)
            _prev_book[symbol] = {'ts': ts, 'b': new_b, 'a': new_a}

    except Exception as e:
        # Never let capture errors propagate into the book pipeline.
        pass


# ── Event differ ───────────────────────────────────────────────────────────

def _diff_and_emit(sym: str, prev: Optional[dict], new_b: list, new_a: list, ts: float) -> None:
    """Compare prev snapshot to new per-level state. Emit structural events
    (LEVEL_FORMED, EXCH_ADD, EXCH_REMOVE, SIZE_CHANGE, LEVEL_VANISHED)."""
    # Best-price references for top-of-book filtering (used by wall_signals
    # hook: we only treat pulls at the top-of-book level as "venue-pull"
    # signal material — deeper-level churn is not dealer pulling).
    best_bid_price = new_b[0][0] if new_b else None
    best_ask_price = new_a[0][0] if new_a else None

    for side, prev_list, new_list in (
        ('bid', (prev or {}).get('b', []), new_b),
        ('ask', (prev or {}).get('a', []), new_a),
    ):
        prev_map = _levels_by_price(prev_list)
        new_map = _levels_by_price(new_list)

        prev_prices = set(prev_map.keys())
        new_prices = set(new_map.keys())

        # New price levels — LEVEL_FORMED
        for price in new_prices - prev_prices:
            size, mmc, exchs = new_map[price]
            key = (side, price)
            _active_formations.setdefault(sym, {})[key] = {
                'first_ts': ts,
                'arrivals': [{'t_ms': 0, 'exch': e, 'size': size} for e in exchs],
                'exchs_seen': set(exchs),
            }
            _enqueue({
                'type': 'LEVEL_FORMED',
                'ts': ts,
                'sym': sym,
                'side': side,
                'price': price,
                'size': size,
                'mm_count': mmc,
                'exchs': list(exchs),
            })
            _bump_events(sym)

        # Vanished levels — LEVEL_VANISHED
        for price in prev_prices - new_prices:
            key = (side, price)
            af = _active_formations.get(sym, {}).pop(key, None)
            if af:
                formation_rec = {
                    'ts_formed': af['first_ts'],
                    'ts_vanished': ts,
                    'side': side,
                    'price': price,
                    'arrivals': af['arrivals'],
                }
                _formations.setdefault(sym, deque(maxlen=FORMATION_RING_CAP)).appendleft(formation_rec)
                # Persist the full formation lifecycle to disk.
                _write_log({
                    'type': 'FORMATION_CLOSED',
                    'ts': ts,
                    'sym': sym,
                    'side': side,
                    'price': price,
                    'first_ts': af['first_ts'],
                    'arrivals': af['arrivals'],
                })
            _enqueue({
                'type': 'LEVEL_VANISHED',
                'ts': ts,
                'sym': sym,
                'side': side,
                'price': price,
            })
            _bump_events(sym)

        # Levels in both — diff exchanges & size
        for price in prev_prices & new_prices:
            p_size, p_mmc, p_exchs = prev_map[price]
            n_size, n_mmc, n_exchs = new_map[price]
            p_set = set(p_exchs)
            n_set = set(n_exchs)
            added = n_set - p_set
            removed = p_set - n_set

            key = (side, price)
            af = _active_formations.get(sym, {}).get(key)
            for exch in added:
                if af is not None and exch not in af['exchs_seen']:
                    af['exchs_seen'].add(exch)
                    af['arrivals'].append({
                        't_ms': int((ts - af['first_ts']) * 1000),
                        'exch': exch,
                        'size': n_size,
                    })
                _enqueue({
                    'type': 'EXCH_ADD',
                    'ts': ts,
                    'sym': sym,
                    'side': side,
                    'price': price,
                    'exch': exch,
                    'size_after': n_size,
                    'mm_count_after': n_mmc,
                })
                _bump_events(sym)
            for exch in removed:
                _enqueue({
                    'type': 'EXCH_REMOVE',
                    'ts': ts,
                    'sym': sym,
                    'side': side,
                    'price': price,
                    'exch': exch,
                    'size_after': n_size,
                    'mm_count_after': n_mmc,
                })
                _bump_events(sym)
                # Wall-signals hook: top-of-book pulls only. A pull at a
                # deeper level isn't a dealer-consensus signal — it's
                # just inventory rebalancing. Structural gate, not magnitude.
                if _wall_signals is not None:
                    try:
                        is_top = (
                            (side == 'bid' and best_bid_price is not None
                             and abs(price - best_bid_price) < 1e-9)
                            or (side == 'ask' and best_ask_price is not None
                                and abs(price - best_ask_price) < 1e-9)
                        )
                        if is_top:
                            _wall_signals.on_exch_remove(sym, exch, ts)
                    except Exception:
                        pass
            if p_size != n_size and not added and not removed:
                _enqueue({
                    'type': 'SIZE_CHANGE',
                    'ts': ts,
                    'sym': sym,
                    'side': side,
                    'price': price,
                    'size_before': p_size,
                    'size_after': n_size,
                })
                _bump_events(sym)


# ── Capture-vs-post accumulator ───────────────────────────────────────────

def _update_capture(sym: str, prev: Optional[dict], new_b: list, new_a: list, ts: float) -> None:
    """Integrate presence time at best-bid and best-ask per exchange.
    Δt = ts - prev_ts. Every exchange present at prev best-bid accrues
    Δt seconds of 'posted_bid_time'; same for ask side.
    """
    if not prev:
        return
    prev_ts = prev.get('ts', ts)
    dt = max(0.0, ts - prev_ts)
    if dt == 0:
        return

    cap = _capture.setdefault(sym, {})
    tot = _capture_totals.setdefault(sym, {'total_bid_time': 0.0, 'total_ask_time': 0.0})

    # Best-bid exchanges in prev snapshot
    prev_b = prev.get('b') or []
    if prev_b:
        prev_bid_exchs = list(prev_b[0][3] or [])
        for e in prev_bid_exchs:
            cap.setdefault(e, {'posted_bid_time': 0.0, 'posted_ask_time': 0.0, 'caught_count': 0})
            cap[e]['posted_bid_time'] += dt
        tot['total_bid_time'] += dt

    prev_a = prev.get('a') or []
    if prev_a:
        prev_ask_exchs = list(prev_a[0][3] or [])
        for e in prev_ask_exchs:
            cap.setdefault(e, {'posted_bid_time': 0.0, 'posted_ask_time': 0.0, 'caught_count': 0})
            cap[e]['posted_ask_time'] += dt
        tot['total_ask_time'] += dt


# ── NBBO ribbon ───────────────────────────────────────────────────────────

def _update_ribbon(sym: str, prev: Optional[dict], new_b: list, new_a: list, ts: float) -> None:
    """Append a ribbon sample only when best-bid or best-ask composition
    (price or exchange list) changes. Size-only changes do NOT create a
    ribbon sample."""
    best_bid_price = new_b[0][0] if new_b else None
    best_bid_exchs = list(new_b[0][3] or []) if new_b else []
    best_bid_size = int(new_b[0][1] or 0) if new_b else 0
    best_ask_price = new_a[0][0] if new_a else None
    best_ask_exchs = list(new_a[0][3] or []) if new_a else []
    best_ask_size = int(new_a[0][1] or 0) if new_a else 0

    prev_bid_price = None
    prev_bid_exchs = []
    prev_ask_price = None
    prev_ask_exchs = []
    if prev:
        pb = prev.get('b') or []
        pa = prev.get('a') or []
        if pb:
            prev_bid_price = pb[0][0]
            prev_bid_exchs = list(pb[0][3] or [])
        if pa:
            prev_ask_price = pa[0][0]
            prev_ask_exchs = list(pa[0][3] or [])

    changed = (
        best_bid_price != prev_bid_price
        or best_ask_price != prev_ask_price
        or best_bid_exchs != prev_bid_exchs
        or best_ask_exchs != prev_ask_exchs
    )
    if not changed and prev is not None:
        return

    sample = {
        'ts': ts,
        'bid_price': best_bid_price,
        'bid_size': best_bid_size,
        'bid_exchs': best_bid_exchs,
        'ask_price': best_ask_price,
        'ask_size': best_ask_size,
        'ask_exchs': best_ask_exchs,
    }
    _nbbo_ribbon.setdefault(sym, deque(maxlen=RIBBON_SAMPLE_CAP)).append(sample)


# ── Impulse response ──────────────────────────────────────────────────────

def _advance_impulse(sym: str, new_b: list, new_a: list, ts: float) -> None:
    """If an impulse capture is active for this contract, append a tick
    reflecting the state of the book at the print's price level."""
    active = _impulse_active.get(sym)
    if not active:
        return
    price = active['print_price']

    # Find the level at print_price on either side.
    mm_count = 0
    total_size = 0
    exchs: list = []
    for side_levels in (new_b, new_a):
        for lvl in side_levels:
            try:
                if abs(float(lvl[0]) - float(price)) < 1e-9:
                    total_size = int(lvl[1] or 0)
                    mm_count = int(lvl[2] or 0)
                    exchs = list(lvl[3] or [])
                    break
            except Exception:
                continue
        if exchs or total_size:
            break

    t_ms = int((ts - active['print_ts']) * 1000)
    active['ticks'].append({
        't_ms': t_ms,
        'mm_count': mm_count,
        'total_size': total_size,
        'exchs': exchs,
    })
    _enqueue({
        'type': 'IMPULSE_TICK',
        'ts': ts,
        'sym': sym,
        'print_ts': active['print_ts'],
        't_ms': t_ms,
        'mm_count': mm_count,
        'total_size': total_size,
        'exchs': exchs,
    })


# ── Main hook: print ──────────────────────────────────────────────────────

def on_print(evt: dict) -> None:
    """Called from _on_tradier_timesale. Accepts only prints for symbols we
    have an active Schwab OPTIONS_BOOK subscription for — attribution requires
    the book join (ribbon / formations / posted-time all depend on book data).
    Tradier covers single-name universe for tape, but Schwab OPTIONS_BOOK is
    QQQ-only (Phase 1), so single-name prints are dropped here. Otherwise
    those symbols would rank high on 'prints' / 'events' while ribbon,
    formations, and posted% are all empty — creating the exact tape-feed
    illusion the pane was built to replace.
    """
    try:
        sym = evt.get('symbol') or ''
        if not sym:
            return
        # Gate on book subscription: if we've never received an OPTIONS_BOOK
        # snapshot for this symbol, we cannot attribute. Drop the print.
        if sym not in _prev_book:
            return
        ts = float(evt.get('ts') or time.time())
        exch = _normalize_exch((evt.get('exchange') or '').upper())
        price = float(evt.get('price') or 0)
        size = int(evt.get('size') or 0)

        # Wall-signals hook — fires only for prints at known wall strikes
        # (internally gated inside wall_signals.on_print). Non-wall-strike
        # prints short-circuit after the regex parse.
        if _wall_signals is not None:
            try:
                _wall_signals.on_print(sym, exch, price, size, ts)
            except Exception:
                pass

        with _state_lock:
            _maybe_reset_session(ts)
            # Book at print time — must resolve BEFORE capture counting so
            # `caught_at_level` can gate on whether the print's exchange was
            # actually posting at that level (true attribution vs. raw routing).
            prev = _prev_book.get(sym)
            book_exchs_at_level: list = []
            book_size_at_level = 0
            book_mm_count_at_level = 0
            # Track whether the print's price matched the best-bid or best-ask
            # (top of book) vs. a deeper level. Only top-of-book prints compare
            # apples-to-apples against `posted_time` (which also measures top).
            at_top_of_book = False
            if prev:
                for side_key in ('b', 'a'):
                    side_levels = prev.get(side_key) or []
                    for lvl_idx, lvl in enumerate(side_levels):
                        try:
                            if abs(float(lvl[0]) - price) < 1e-9:
                                book_size_at_level = int(lvl[1] or 0)
                                book_mm_count_at_level = int(lvl[2] or 0)
                                book_exchs_at_level = list(lvl[3] or [])
                                at_top_of_book = (lvl_idx == 0)
                                break
                        except Exception:
                            continue
                    if book_exchs_at_level:
                        break

            # Capture counting — two complementary counters.
            # `caught_count`       : every print on this exchange, regardless of
            #                        book state. Useful for raw routing volume.
            # `caught_at_level`    : only prints where the print's exchange was
            #                        actually posting at the matched book level.
            #                        This is the apples-to-apples pair with
            #                        `posted_time` for computing the
            #                        capture-vs-post DIFF metric.
            # `caught_at_top`      : further narrowed to top-of-book prints so
            #                        the DIFF matches the top-of-book
            #                        `posted_time` integration exactly.
            if exch:
                cap = _capture.setdefault(sym, {})
                cap.setdefault(exch, {
                    'posted_bid_time': 0.0,
                    'posted_ask_time': 0.0,
                    'caught_count': 0,
                    'caught_at_level': 0,
                    'caught_at_top': 0,
                })
                # Back-fill new fields for rows created before this change.
                cap[exch].setdefault('caught_at_level', 0)
                cap[exch].setdefault('caught_at_top', 0)
                cap[exch]['caught_count'] += 1
                if exch in book_exchs_at_level:
                    cap[exch]['caught_at_level'] += 1
                    if at_top_of_book:
                        cap[exch]['caught_at_top'] += 1

            # Finalize any existing active impulse (next print on same contract
            # is the structural boundary).
            old_active = _impulse_active.get(sym)
            if old_active:
                # Phase D — enrich with equity-tape context inside the impulse
                # window. Structural: [prev_option_print_ts, curr_option_print_ts).
                # hp_context is additive — existing consumers using .get() are
                # unaffected if this fails.
                hp_context = None
                try:
                    _sb = _get_schwab_bridge()
                    if _sb is not None and hasattr(_sb, 'lookup_equity_window'):
                        prev_ms = int(old_active['print_ts'] * 1000)
                        curr_ms = int(ts * 1000)
                        eq_prints = _sb.lookup_equity_window('QQQ', prev_ms, curr_ms)
                        observed_signed_shares = sum(s * sign for _, _, s, _, sign in eq_prints)
                        observed_total_volume  = sum(s for _, _, s, _, _ in eq_prints)
                        observed_by_exch = {}
                        for _, _, s, mic, _ in eq_prints:
                            if mic:
                                observed_by_exch[mic] = observed_by_exch.get(mic, 0) + s
                        hp_context = {
                            'equity_prints_in_window': len(eq_prints),
                            'observed_signed_shares':  observed_signed_shares,
                            'observed_total_volume':   observed_total_volume,
                            'observed_by_exch':        observed_by_exch,
                            'window_start_ms':         prev_ms,
                            'window_end_ms':           curr_ms,
                        }
                except Exception:
                    hp_context = None
                _write_log({
                    'type': 'IMPULSE_CLOSED',
                    'ts': ts,
                    'sym': sym,
                    'print_ts': old_active['print_ts'],
                    'print_price': old_active['print_price'],
                    'print_exch': old_active['print_exch'],
                    'print_size': old_active['print_size'],
                    'print_aggressor': old_active.get('print_aggressor'),
                    'next_print_ts': ts,
                    'tick_count': len(old_active['ticks']),
                    'ticks': old_active['ticks'],
                    'hp_context': hp_context,
                })

            # Start new impulse for this print
            _impulse_active[sym] = {
                'print_ts': ts,
                'print_price': price,
                'print_exch': exch,
                'print_size': size,
                'print_aggressor': evt.get('aggressor'),
                'book_exchs_at_level': book_exchs_at_level,
                'book_size_at_level': book_size_at_level,
                'book_mm_count_at_level': book_mm_count_at_level,
                'ticks': [],
            }

            # Emit PRINT_MATCH for the live event stream.
            _enqueue({
                'type': 'PRINT_MATCH',
                'ts': ts,
                'sym': sym,
                'exch': exch,
                'price': price,
                'size': size,
                'aggressor': evt.get('aggressor'),
                'book_exchs_at_level': book_exchs_at_level,
                'book_size_at_level': book_size_at_level,
                'book_mm_count_at_level': book_mm_count_at_level,
            })
            _bump_events(sym)

    except Exception:
        pass


# ── Event queue + socket flush ────────────────────────────────────────────

def _enqueue(ev: dict) -> None:
    with _queue_lock:
        _event_queue.append(ev)
    _write_log(ev)


def _bump_events(sym: str) -> None:
    _contract_event_counts[sym] = _contract_event_counts.get(sym, 0) + 1


def flush_events_to_socket(sio) -> int:
    """Called from _flush_loop. Drains the event queue and emits one
    'mm_event_batch' Socket.IO event. Returns number of events emitted."""
    batch: list = []
    with _queue_lock:
        while _event_queue:
            batch.append(_event_queue.popleft())
    if not batch:
        return 0
    try:
        sio.emit('mm_event_batch', {
            'events': batch,
            '_emit_ms': int(time.time() * 1000),
        })
    except Exception:
        pass
    return len(batch)


def watch(sym: str) -> int:
    """Register a client's interest in `sym`. Returns the new refcount.
    Called by server.py socket handler on `mm_attribution:watch`."""
    with _watch_lock:
        _watchers[sym] = _watchers.get(sym, 0) + 1
        return _watchers[sym]


def unwatch(sym: str) -> int:
    """Decrement refcount for `sym`. Returns remaining count."""
    with _watch_lock:
        n = _watchers.get(sym, 0) - 1
        if n <= 0:
            _watchers.pop(sym, None)
            _last_state_push.pop(sym, None)
            return 0
        _watchers[sym] = n
        return n


def watched_symbols() -> list:
    """Snapshot of currently-watched symbols."""
    with _watch_lock:
        return list(_watchers.keys())


def flush_contract_states_to_socket(sio) -> int:
    """Called from _flush_loop. For each watched symbol, emit a fresh
    contract_state snapshot to its room (`mma:<sym>`). Gated by
    STATE_PUSH_INTERVAL_SEC so we don't spam the 50ms flush tick."""
    syms = watched_symbols()
    if not syms:
        return 0
    now = time.time()
    pushed = 0
    for sym in syms:
        last = _last_state_push.get(sym, 0.0)
        if (now - last) < STATE_PUSH_INTERVAL_SEC:
            continue
        try:
            state = contract_state(sym)
            sio.emit('mm_contract_state', state, to=f'mma:{sym}')
            _last_state_push[sym] = now
            pushed += 1
        except Exception:
            pass
    return pushed


# ── REST payload builders ─────────────────────────────────────────────────

def purge_unbooked() -> int:
    """Drop any tracked state for symbols we have no OPTIONS_BOOK subscription
    for. Runs at the start of every rank_contracts call so stale tape-only
    state from prior builds gets evicted immediately — no wait for session
    rollover. Returns number of symbols purged.
    """
    purged = 0
    with _state_lock:
        booked = set(_prev_book.keys())
        for bag in (_contract_event_counts, _capture, _capture_totals,
                    _formations, _active_formations, _nbbo_ribbon, _impulse_active):
            stale = [s for s in bag.keys() if s not in booked]
            for s in stale:
                bag.pop(s, None)
                purged += 1
    return purged


def rank_contracts(metric: str = 'events', limit: int = 50) -> list:
    """Return contracts ranked by the chosen cumulative metric since session
    open. `limit` is a display convenience (not a data filter).

    Only contracts with a Schwab OPTIONS_BOOK subscription (i.e., we've seen
    at least one book snapshot) are ranked — attribution requires the book
    side of the join. Tape-only symbols would otherwise appear with empty
    ribbon / formations / posted-time, defeating the pane's purpose.
    """
    purge_unbooked()
    with _state_lock:
        booked = set(_prev_book.keys())
        counts = {s: n for s, n in _contract_event_counts.items() if s in booked}
        caps = {s: sum(c.get('caught_count', 0) for c in exchs.values())
                for s, exchs in _capture.items() if s in booked}
        forms = {s: len(dq) for s, dq in _formations.items() if s in booked}

    all_syms = set(counts) | set(caps) | set(forms)
    rows = []
    for s in all_syms:
        rows.append({
            'sym': s,
            'events': counts.get(s, 0),
            'prints': caps.get(s, 0),
            'formations': forms.get(s, 0),
        })
    key = metric if metric in ('events', 'prints', 'formations') else 'events'
    rows.sort(key=lambda r: -r[key])
    return rows[:limit]


def venue_rollup_across_symbols(symbols: list) -> dict:
    """Cross-contract per-exchange capture rollup.

    Returns {exch: {posted_time_s, caught_count, contracts_touched}} summed over
    the supplied symbols. Used by schwab_bridge.get_hedge_pressure_by_exchange()
    to weight per-strike dealer gamma by each venue's posted/caught share.

    No thresholds, no magnitude cutoffs — this is pure arithmetic over the
    already-measured _capture bag.
    """
    out: dict = {}
    with _state_lock:
        for sym in symbols:
            exchs = _capture.get(sym)
            if not exchs:
                continue
            for exch, rec in exchs.items():
                row = out.setdefault(exch, {
                    'posted_time_s':      0.0,
                    'caught_count':       0,
                    'contracts_touched':  0,
                })
                row['posted_time_s'] += (rec.get('posted_bid_time', 0.0) +
                                         rec.get('posted_ask_time', 0.0))
                row['caught_count']  += int(rec.get('caught_count', 0) or 0)
                row['contracts_touched'] += 1
    return out


def contract_state(symbol: str) -> dict:
    """Full state payload for a contract — used by the pane on 1s poll."""
    with _state_lock:
        ribbon = list(_nbbo_ribbon.get(symbol, []))
        formations = list(_formations.get(symbol, deque()))
        capture = dict(_capture.get(symbol, {}))
        totals = dict(_capture_totals.get(symbol, {'total_bid_time': 0.0, 'total_ask_time': 0.0}))
        active_impulse = _impulse_active.get(symbol)
        prev = _prev_book.get(symbol)
        # Also include currently-active formations so the UI can show forming
        # levels not yet closed.
        active_forms = []
        for (side, price), af in _active_formations.get(symbol, {}).items():
            active_forms.append({
                'ts_formed': af['first_ts'],
                'ts_vanished': None,
                'side': side,
                'price': price,
                'arrivals': list(af['arrivals']),
            })

    # Combine active + closed, newest first by formation-time.
    all_forms = active_forms + formations
    all_forms.sort(key=lambda f: -f['ts_formed'])

    # `total_posted` = wall-clock time the book was alive (bid_time + ask_time).
    # Divisor for `posted_pct` = "how often was this MM at top-of-book?"
    # Multiple MMs can be at top simultaneously so Σ posted_pct can exceed 100%.
    total_posted = max(1e-9, totals['total_bid_time'] + totals['total_ask_time'])
    # `total_posted_across_mms` = Σ each MM's posted_time. Divisor for
    # `posted_share` which sums to exactly 100% across MMs — apples-to-apples
    # with `caught_at_top_pct` (also a share). Used for `diff_pct`.
    total_posted_across_mms = max(1e-9, sum(
        rec['posted_bid_time'] + rec['posted_ask_time'] for rec in capture.values()
    ))
    capture_rows = []
    for exch, rec in capture.items():
        posted = rec['posted_bid_time'] + rec['posted_ask_time']
        capture_rows.append({
            'exch': exch,
            'posted_time_s': posted,
            'posted_pct':    posted / total_posted,
            'posted_share':  posted / total_posted_across_mms,
            'caught_count':    rec.get('caught_count', 0),
            'caught_at_level': rec.get('caught_at_level', 0),
            'caught_at_top':   rec.get('caught_at_top', 0),
        })
    # Sort by share descending (the apples-to-apples number).
    capture_rows.sort(key=lambda r: -r['posted_share'])
    total_caught          = sum(r['caught_count']    for r in capture_rows)
    total_caught_at_level = sum(r['caught_at_level'] for r in capture_rows)
    total_caught_at_top   = sum(r['caught_at_top']   for r in capture_rows)
    # `diff_pct` = caught_at_top_share − posted_share. Both sum to 100%, so Σ
    # diff_pct = 0 by construction. Positive values = MM caught more than they
    # posted for (flow-attracting); negative = they posted a lot but got fewer
    # fills than their share (over-quoting / defensive pricing).
    for r in capture_rows:
        r['caught_pct']          = r['caught_count']    / total_caught          if total_caught          > 0 else 0.0
        r['caught_at_level_pct'] = r['caught_at_level'] / total_caught_at_level if total_caught_at_level > 0 else 0.0
        r['caught_at_top_pct']   = r['caught_at_top']   / total_caught_at_top   if total_caught_at_top   > 0 else 0.0
        r['diff_pct'] = r['caught_at_top_pct'] - r['posted_share']

    last_impulse = None
    if active_impulse:
        last_impulse = dict(active_impulse)
        # Impulse is not yet closed — pass as is. The UI shows "live" badge.
        last_impulse['closed'] = False
    else:
        # Try to pull the most recent closed impulse from disk. Cheap: only if
        # the active one is None, which is rare (between sessions mostly).
        last_impulse = _load_last_closed_impulse(symbol)

    return {
        'sym': symbol,
        'session_open_ts': _session_open_ts,
        'now_ts': time.time(),
        'total_posted_time_s':  total_posted,
        'total_caught':         total_caught,
        'total_caught_at_level': total_caught_at_level,
        'total_caught_at_top':  total_caught_at_top,
        'ribbon': ribbon,
        'formations': all_forms,
        'capture': capture_rows,
        'last_impulse': last_impulse,
        'prev_book': prev,
    }


def _load_last_closed_impulse(symbol: str) -> Optional[dict]:
    """Best-effort read of the most recent IMPULSE_CLOSED entry for `symbol`
    from today's disk log. Returns None if not found."""
    try:
        path = _log_path(time.time())
        if not os.path.exists(path):
            return None
        # Tail-scan. Not elegant but the log is line-buffered JSONL.
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            # Scan backwards in 64KB chunks.
            chunk = 64 * 1024
            pos = max(0, size - chunk)
            fh.seek(pos)
            tail = fh.read()
            lines = tail.decode('utf-8', errors='ignore').split('\n')
        for line in reversed(lines):
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get('type') == 'IMPULSE_CLOSED' and rec.get('sym') == symbol:
                    rec['closed'] = True
                    return rec
            except Exception:
                continue
    except Exception:
        return None
    return None


def impulse_for_print(symbol: str, print_ts: float) -> Optional[dict]:
    """Lookup a specific impulse in today's disk log. Used by prev/next nav.
    Scans the log; for long sessions this becomes linear but acceptable
    given the expected call rate (one per UI nav click)."""
    try:
        path = _log_path(time.time())
        if not os.path.exists(path):
            return None
        target = float(print_ts)
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            matches = []
            for line in fh:
                if '"IMPULSE_CLOSED"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get('sym') != symbol:
                    continue
                if abs(float(rec.get('print_ts', 0)) - target) < 1e-3:
                    rec['closed'] = True
                    return rec
                matches.append(rec)
            # Navigator helper: if target matches none exactly, return the
            # closest by print_ts (useful for "next" when caller passes
            # current_ts + tiny offset).
            if matches:
                matches.sort(key=lambda r: abs(float(r.get('print_ts', 0)) - target))
                matches[0]['closed'] = True
                return matches[0]
    except Exception:
        return None
    return None


def impulse_list(symbol: str, limit: int = 50) -> list:
    """Return list of {print_ts, print_exch, print_price, print_size} for
    closed impulses on `symbol` in today's log. For prev/next UI."""
    out = []
    try:
        path = _log_path(time.time())
        if not os.path.exists(path):
            return out
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            for line in fh:
                if '"IMPULSE_CLOSED"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get('sym') != symbol:
                    continue
                out.append({
                    'print_ts': rec.get('print_ts'),
                    'print_exch': rec.get('print_exch'),
                    'print_price': rec.get('print_price'),
                    'print_size': rec.get('print_size'),
                    'tick_count': rec.get('tick_count', 0),
                    'next_print_ts': rec.get('next_print_ts'),
                })
    except Exception:
        pass
    out.sort(key=lambda r: -float(r.get('print_ts') or 0))
    return out[:limit]


def module_summary() -> dict:
    """Top-level counters for pane header."""
    with _state_lock:
        return {
            'session_open_ts': _session_open_ts,
            'contracts_tracked': len(_prev_book),
            'total_events': sum(_contract_event_counts.values()),
            'total_prints': sum(sum(c.get('caught_count', 0) for c in v.values()) for v in _capture.values()),
            'total_formations': sum(len(dq) for dq in _formations.values()),
            'active_impulses': len(_impulse_active),
            'disk_log_count': _log_count,
        }
