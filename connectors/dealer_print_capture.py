"""Dealer Print Capture — raw data collector. NO thresholds, NO labels.

Per-print join of:
  - Tradier timesale (with `exchange` field) — source of truth for venue
  - Schwab OPTIONS_BOOK snapshot that was live at print-time (book_before)
  - OPTIONS_BOOK snapshot 5s + 30s after the print (post_book_5s, post_book_30s)
  - Underlying spot at print-time + t+5m/15m/30m

One row per print written to logs/dealer_prints_YYYYMMDD.jsonl. Completed rows
only (all forward samples filled).

DELIBERATELY NO classification of "hedging" / "dealer hit" / "aggressive" etc.
This is capture infrastructure. After ≥1 week of data, sliced analysis finds
which combinations of (size, venue concentration, level_hit, Δmm, spot-move)
actually predict forward drift. Those slices — if any — become detectors per
MEASURED_VALUES.md. Until then, the panel is descriptive, not prescriptive.
"""
import json
import logging
import os
import time
from datetime import datetime
from threading import Lock, Thread
from collections import deque
from typing import Optional, Callable

log = logging.getLogger(__name__)

# ── Per-contract latest book snapshot (compact form) ────────────────────────
# sym -> {'ts', 'b': [[price, size, mm_count, [mm_ids]], ...], 'a': [...]}
_latest_book: dict = {}
_book_lock = Lock()

# ── Pending rows awaiting forward samples ───────────────────────────────────
# Each entry: {'row': {...}, 'deadlines': {...}, 'spot_lookup': callable}
_pending: list = []
_pending_lock = Lock()

# ── Ring buffer of recently completed rows (for live panel) ─────────────────
_recent_completed: deque = deque(maxlen=10000)  # 2026-05-04: was 500 (5s @ peak rate). 10K = ~100s buffer for retrospective audits.
_recent_lock = Lock()

# ── Disk logger ─────────────────────────────────────────────────────────────
# 2026-05-05: switched from single (_dp_log_fh, _dp_log_date) pair to a per-date
# handle dict so prints land in the file dated for THEIR ts, not for now().
# Pre-fix bug: stale prints restored from `state/dealer_pending.json` (ts could
# be from any prior day) were appended to today's file, polluting per-day audit
# files. Audited 2026-05-05: dealer_prints_20260505.jsonl had prints from
# 2025-12-25 through 2026-05-05 — 65K cross-day rows.
_dp_log_fhs: dict = {}     # date_str (YYYYMMDD) → file handle
_dp_log_lock = Lock()
_dp_log_count = 0   # disk-write counter (== "out_total" in capture_rate)

# ── Capture-rate metrics (added 2026-05-04, Improvement #3) ─────────────────
_in_total = 0                          # incremented at top of on_print()
_in_lock = Lock()
_started_at = time.time()              # process start (for uptime)
_default_spot_lookup: Optional[Callable] = None   # set by set_default_spot_lookup()

# ── Pending-queue persistence (added 2026-05-04, Improvement #1) ────────────
# At ~96 prints/sec peak, a 30-min enrichment window holds ~170K entries.
# Each ~300 bytes JSON => ~50MB peak file. Atomic rename gives crash safety.
# Worst-case loss on kill -9 = 10s of prints (~960 entries) between snapshots.
_STATE_DIR = os.path.join(os.path.dirname(__file__), '..', 'state')
_STATE_FILE = os.path.join(_STATE_DIR, 'dealer_pending.json')
_STATE_TMP  = _STATE_FILE + '.tmp'
_STATE_VERSION = 1
_PERSIST_INTERVAL_SEC = 60.0          # how often to snapshot _pending
# 2026-05-06: bumped from 10s → 60s after yappi profiling proved
# persist_pending() takes 4+ seconds per call (JSON-dumps 50K+ pending
# dicts + fsync). At 10s cadence that's 40% gevent freeze → WebSocket
# back-pressure → TCP RST disconnects. At 60s cadence: 7% freeze.
_RESTORE_MAX_AGE_SEC  = 60.0          # skip restore if file older than this
_persist_thread: Optional[Thread] = None
_persist_started = False
_persist_lock = Lock()
_last_persist_ts = 0.0
_last_persist_n = 0
_last_persist_dur_ms = 0.0


def _dp_log_path_for_date(date_str: str) -> str:
    """Return the dealer_prints log path for a YYYYMMDD date string."""
    logs_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, f'dealer_prints_{date_str}.jsonl')


def _get_log_fh_for_ts(ts: float):
    """Get (or create) the file handle for the dealer_prints log of a print's
    timestamp's calendar day. Replaces _ensure_log()'s now()-based file selection
    so prints land in the file matching their ts, not the wall-clock write time.
    Caller must hold _dp_log_lock.
    """
    try:
        date_str = datetime.fromtimestamp(float(ts)).strftime('%Y%m%d')
    except Exception:
        date_str = datetime.now().strftime('%Y%m%d')   # fallback if ts is bogus
    fh = _dp_log_fhs.get(date_str)
    if fh is None:
        # 2026-05-07: 64KB buffer (was line-buffered=1). Tradier delivers
        # 300-600 prints/sec at RTH; line-buffered = 300-600 disk syncs/sec
        # blocking the gevent loop. 64KB ≈ 200 records before flush.
        fh = open(_dp_log_path_for_date(date_str), 'a', buffering=65536)
        _dp_log_fhs[date_str] = fh
    return fh


def _pack_levels(levels) -> list:
    """Compact [[price, size, mm_count, [mm_ids]], ...] form, top 5 levels."""
    out = []
    for lvl in (levels or [])[:5]:
        mms = [m.get('id', '') for m in (lvl.get('market_makers') or [])[:8]]
        out.append([
            float(lvl.get('price', 0) or 0),
            int(lvl.get('size', 0) or 0),
            int(lvl.get('mm_count', 0) or 0),
            mms,
        ])
    return out


def update_book(symbol: str, bids: list, asks: list) -> None:
    """Called from _on_options_book upstream. Caches latest snapshot per
    contract (compact form) so on_print can read book_before in O(1)."""
    if not symbol:
        return
    with _book_lock:
        _latest_book[symbol] = {
            'ts': time.time(),
            'b': _pack_levels(bids),
            'a': _pack_levels(asks),
        }


def _snapshot_book(symbol: str) -> Optional[dict]:
    """Thread-safe snapshot copy of current book for a contract."""
    with _book_lock:
        b = _latest_book.get(symbol)
        if not b:
            return None
        return {'ts': b['ts'], 'b': list(b['b']), 'a': list(b['a'])}


def _level_hit(book: dict, side_taken: str, size: int) -> Optional[str]:
    """Which level did this print break through? side_taken='ask' for buys,
    'bid' for sells. Cumulative size across levels until hit size reached."""
    if not book or side_taken not in ('ask', 'bid'):
        return None
    levels = book['a'] if side_taken == 'ask' else book['b']
    cum = 0
    for i, lvl in enumerate(levels):
        cum += int(lvl[1] or 0)
        if size <= cum:
            return f'{side_taken}+{i}'
    return f'{side_taken}+deep'


# Sample deadlines (seconds after print)
_SAMPLE_OFFSETS = {
    'post_book_5s':  5.0,
    'post_book_30s': 30.0,
    'spot_300s':     300.0,
    'spot_900s':     900.0,
    'spot_1800s':    1800.0,
}


def on_print(evt: dict, spot_lookup=None) -> None:
    """Called from _on_tradier_timesale for each option print.

    evt expected keys:
      ts (seconds), symbol (OCC), root, strike, side_cp (C|P), dte, price, size,
      exchange, session, aggressor (buy|sell|mid|unknown), delta, spot

    spot_lookup: callable(ticker_root) -> float, for underlying spot snapshots.
    """
    # Bump the inbound counter BEFORE any early-exits so the rate metric
    # reflects every print the pipeline accepted as input. on_print is wrapped
    # in try/except so malformed events still count as inputs we received.
    global _in_total
    with _in_lock:
        _in_total += 1

    try:
        sym = evt.get('symbol') or ''
        if not sym:
            return

        book_before = _snapshot_book(sym)
        aggressor = (evt.get('aggressor') or 'unknown').lower()
        size = int(evt.get('size') or 0)

        # Which book level was broken through (only if we have a book snapshot)
        lh = None
        if book_before and aggressor in ('buy', 'sell'):
            lh = _level_hit(book_before, 'ask' if aggressor == 'buy' else 'bid', size)

        row = {
            'ts':         float(evt.get('ts') or time.time()),
            'symbol':     sym,
            'root':       evt.get('root') or '',
            'strike':     float(evt.get('strike') or 0),
            'cp':         evt.get('side_cp') or '',
            'dte':        int(evt.get('dte') or 0),
            'price':      float(evt.get('price') or 0),
            'size':       size,
            'exchange':   evt.get('exchange') or '',
            'session':    evt.get('session') or 'regular',
            'aggressor':  aggressor,
            'delta':      float(evt.get('delta') or 0),
            'spot_t':     float(evt.get('spot') or 0),
            'level_hit':  lh,
            'book_before': book_before,
            # Filled by flush_pending when deadlines hit:
            'post_book_5s':  None,
            'post_book_30s': None,
            'spot_300s':     None,
            'spot_900s':     None,
            'spot_1800s':    None,
        }

        # Schedule forward samples
        t0 = row['ts']
        with _pending_lock:
            _pending.append({
                'row': row,
                'deadlines': {k: t0 + off for k, off in _SAMPLE_OFFSETS.items()},
                'spot_lookup': spot_lookup,
            })

        # Immediately push to recent ring for live panel
        with _recent_lock:
            _recent_completed.append(row)

    except Exception as e:
        # Never let capture errors propagate into the print pipeline
        pass


def flush_pending() -> int:
    """Drive forward-sample filling. Call periodically (e.g., every 500ms).
    Returns number of rows written to disk this call."""
    now = time.time()
    written = 0
    to_write = []
    with _pending_lock:
        survivors = []
        for p in _pending:
            row = p['row']
            deadlines = p['deadlines']
            spot_lookup = p.get('spot_lookup')
            sym = row['symbol']
            ticker = row['root']

            # Fill book samples
            if row['post_book_5s'] is None and now >= deadlines['post_book_5s']:
                row['post_book_5s'] = _snapshot_book(sym)
            if row['post_book_30s'] is None and now >= deadlines['post_book_30s']:
                row['post_book_30s'] = _snapshot_book(sym)
            # Fill spot samples
            if row['spot_300s'] is None and now >= deadlines['spot_300s']:
                row['spot_300s'] = spot_lookup(ticker) if spot_lookup else 0.0
            if row['spot_900s'] is None and now >= deadlines['spot_900s']:
                row['spot_900s'] = spot_lookup(ticker) if spot_lookup else 0.0
            if row['spot_1800s'] is None and now >= deadlines['spot_1800s']:
                row['spot_1800s'] = spot_lookup(ticker) if spot_lookup else 0.0

            # Complete when the longest deadline has passed
            if row['spot_1800s'] is not None:
                to_write.append(row)
            else:
                survivors.append(p)
        _pending[:] = survivors

    if to_write:
        with _dp_log_lock:
            for row in to_write:
                try:
                    fh = _get_log_fh_for_ts(row.get('ts', time.time()))
                    fh.write(json.dumps(row, separators=(',', ':'), default=str) + '\n')
                    written += 1
                except Exception:
                    pass
        global _dp_log_count
        _dp_log_count += written

    # 2026-05-05 FIX A — eagerly persist after a successful flush.
    # 2026-05-06 REVERTED: eager-persist caused 40% gevent freeze (4-sec
    # JSON-dump+fsync of 50K+ pending dicts blocks the gevent loop, fills
    # WebSocket buffers, triggers TCP RST disconnects). Yappi profiling
    # proved persist_pending takes 17s across 4 calls = 4.25s/call.
    # Now: rely only on the 60s timer-based persist. Trade-off: higher
    # duplicate-on-crash rate (back to ~17% per 2026-05-05 audit), but
    # disconnects drop dramatically. Duplicates can be deduped offline;
    # disconnects can't be undone.
    return written


# ── Stats for panel ─────────────────────────────────────────────────────────

def live_summary(window_s: float = 300.0) -> dict:
    """Live descriptive stats over recent prints. NO thresholds — just
    distributions the panel can render.

    Percentiles are computed on whatever has been captured so far — they are
    NOT CONFIGURED cutoffs. They describe the population, not a signal.
    """
    now = time.time()
    cutoff = now - window_s
    with _recent_lock:
        recent = [r for r in _recent_completed if r['ts'] >= cutoff]

    n = len(recent)
    if n == 0:
        return {'n': 0, 'window_s': window_s, 'pending': len(_pending), 'log_count': _dp_log_count}

    sizes = sorted(int(r['size']) for r in recent)
    def pct(sorted_vals, p):
        if not sorted_vals: return 0
        i = max(0, min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1)))))
        return sorted_vals[i]

    # Venue mix
    venue_counts = {}
    for r in recent:
        v = r.get('exchange') or 'unknown'
        venue_counts[v] = venue_counts.get(v, 0) + 1
    venues = sorted(venue_counts.items(), key=lambda x: -x[1])

    # Aggressor mix
    aggr = {'buy': 0, 'sell': 0, 'mid': 0, 'unknown': 0}
    for r in recent:
        a = r.get('aggressor') or 'unknown'
        aggr[a] = aggr.get(a, 0) + 1

    # Level-hit mix (only counts prints where we had a book snapshot)
    lh = {}
    for r in recent:
        k = r.get('level_hit') or 'no_book'
        lh[k] = lh.get(k, 0) + 1

    return {
        'n': n,
        'window_s': window_s,
        'pending': len(_pending),
        'log_count': _dp_log_count,
        'size_percentiles': {
            'p50': pct(sizes, 0.50),
            'p75': pct(sizes, 0.75),
            'p90': pct(sizes, 0.90),
            'p95': pct(sizes, 0.95),
            'p99': pct(sizes, 0.99),
            'max': sizes[-1] if sizes else 0,
        },
        'venues': venues,
        'aggressor_mix': aggr,
        'level_hit_mix': sorted(lh.items(), key=lambda x: -x[1]),
    }


def recent_prints(n: int = 50) -> list:
    """Most recent N prints for live display. Excludes book_before detail to
    keep payload small."""
    with _recent_lock:
        items = list(_recent_completed)[-n:][::-1]
    out = []
    for r in items:
        bb = r.get('book_before') or {}
        b0 = (bb.get('b') or [[0,0,0,[]]])[0]
        a0 = (bb.get('a') or [[0,0,0,[]]])[0]
        out.append({
            'ts':        r['ts'],
            'symbol':    r['symbol'],
            'root':      r['root'],
            'strike':    r['strike'],
            'cp':        r['cp'],
            'dte':       r['dte'],
            'price':     r['price'],
            'size':      r['size'],
            'exchange':  r['exchange'],
            'aggressor': r['aggressor'],
            'level_hit': r['level_hit'],
            'delta':     r['delta'],
            'bid1':      {'p': b0[0], 'sz': b0[1], 'mm': b0[2]},
            'ask1':      {'p': a0[0], 'sz': a0[1], 'mm': a0[2]},
        })
    return out


# ── Capture-rate API (Improvement #3, added 2026-05-04) ─────────────────────

def capture_rate() -> dict:
    """Return live in/out/pending counts. Used by /api/_debug/capture_rate to
    monitor pipeline health continuously instead of via probed disk audits.

    Definitions:
      in_total       — every print the pipeline accepted from on_print()
                       (since process start; reset on restart)
      out_total      — every print successfully written to disk after the full
                       30-min enrichment window (== _dp_log_count)
      pending        — entries currently sitting in _pending awaiting forward
                       sample fills
      stale_pending  — entries whose deadlines all passed >2 min ago but still
                       sit in _pending. Non-zero indicates a flush bug.
      rate           — out_total / in_total. Will be <1.0 normally (because
                       in-flight entries are still in _pending). Steady-state
                       (>30 min after start, no in-flight) it should approach 1.

    rate ≈ 1 − pending/in_total during steady-state. If rate diverges from that
    relation, prints were dropped between input and disk.
    """
    now = time.time()
    with _in_lock:
        n_in = _in_total
    n_out = _dp_log_count
    with _pending_lock:
        n_pend = len(_pending)
        # "Stale" = past the 1800s spot deadline by >120s. flush_pending should
        # have written these. If stale > 0, something is wrong with the flush
        # loop or the spot_lookup.
        stale_cutoff = now - 120.0
        n_stale = sum(
            1 for p in _pending
            if p.get('deadlines') and max(p['deadlines'].values()) < stale_cutoff
        )
    rate = (n_out / n_in) if n_in > 0 else 1.0
    expected_rate = (1.0 - n_pend / n_in) if n_in > 0 else 1.0
    return {
        'in_total':       n_in,
        'out_total':      n_out,
        'pending':        n_pend,
        'stale_pending':  n_stale,
        'rate':           round(rate, 6),
        'expected_rate':  round(expected_rate, 6),  # rate if zero drops
        'drift':          round(expected_rate - rate, 6),  # >0 means drops
        'uptime_sec':     round(now - _started_at, 1),
        # Persistence telemetry (Improvement #1)
        'persist_last_ts':      _last_persist_ts,
        'persist_last_count':   _last_persist_n,
        'persist_last_dur_ms':  _last_persist_dur_ms,
    }


# ── Pending-queue persistence (Improvement #1, added 2026-05-04) ────────────

def set_default_spot_lookup(fn: Callable) -> None:
    """Register the default spot-lookup callable so restored entries can
    re-attach it. Called once from schwab_bridge at startup."""
    global _default_spot_lookup
    _default_spot_lookup = fn


def persist_pending() -> int:
    """Snapshot _pending to disk via atomic rename. Returns # entries saved.

    Format: JSON document with version + saved_at + entries[]. Each entry is
    {'row': dict, 'deadlines': dict}. spot_lookup is NOT serialized (it's a
    Python callable); restore_pending re-attaches the module-default.

    Crash safety: write to .tmp + fsync + os.replace. Reader always sees old
    or new version, never partial.

    2026-05-07 FIX: at peak RTH the pending queue grows to 200K+ entries.
    json.dump on the full payload was a monolithic 6-8s blocking call that
    froze the gevent event loop (no candle_updates, no Tradier reads, no
    Socket.IO emits during the dump). Symptom: chart "lag building up,
    candles not flowing" every 60s as persist fired.

    Now: stream-write entries one at a time and call gevent.sleep(0) every
    1000 entries to yield control back to the event loop. Total elapsed
    persist time is about the same (the JSON work still happens) but the
    main loop interleaves with it, so live data keeps flowing through.
    """
    global _last_persist_ts, _last_persist_n, _last_persist_dur_ms
    try:
        import gevent
    except Exception:
        gevent = None  # fallback to monolithic write
    t0 = time.time()
    with _pending_lock:
        # Tuple form is faster to copy than dict form
        snapshot = [(p['row'], p['deadlines']) for p in _pending]
    n = len(snapshot)
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_STATE_TMP, 'w') as f:
            f.write('{"version":')
            f.write(str(_STATE_VERSION))
            f.write(',"saved_at":')
            f.write(str(t0))
            f.write(',"entries":[')
            for i, (row, deadlines) in enumerate(snapshot):
                if i > 0:
                    f.write(',')
                f.write(json.dumps(
                    {'row': row, 'deadlines': deadlines},
                    default=str, separators=(',', ':')
                ))
                # Yield to gevent every 1000 entries — keeps the event loop
                # responsive so chart/Socket.IO traffic doesn't pile up.
                if gevent is not None and (i & 1023) == 1023:
                    gevent.sleep(0)
            f.write(']}')
            f.flush()
            os.fsync(f.fileno())
        os.replace(_STATE_TMP, _STATE_FILE)
    except Exception as e:
        log.warning(f"[DPC-PERSIST] save failed: {e}")
        return 0
    _last_persist_ts = t0
    _last_persist_n = n
    _last_persist_dur_ms = round((time.time() - t0) * 1000, 1)
    return n


def restore_pending(spot_lookup: Optional[Callable] = None) -> int:
    """Load _pending from disk if a fresh snapshot exists. Returns # restored.

    Drops any entry whose ALL deadlines have already passed (those should have
    been flushed to disk before the previous shutdown — if they're here it's
    because the process died mid-flush, in which case writing them now would
    duplicate or use stale spot data; the safer choice is to skip).

    spot_lookup: re-attached to each restored entry. Falls back to the
    module-default registered via set_default_spot_lookup().
    """
    if not os.path.exists(_STATE_FILE):
        log.info("[DPC-RESTORE] no state file — fresh start")
        return 0
    age = time.time() - os.path.getmtime(_STATE_FILE)
    if age > _RESTORE_MAX_AGE_SEC:
        log.info(f"[DPC-RESTORE] state file age {age:.0f}s > {_RESTORE_MAX_AGE_SEC:.0f}s, skipping")
        return 0
    try:
        with open(_STATE_FILE) as f:
            payload = json.load(f)
    except Exception as e:
        log.warning(f"[DPC-RESTORE] failed to load: {e}")
        return 0
    if payload.get('version') != _STATE_VERSION:
        log.warning(f"[DPC-RESTORE] schema version mismatch (file={payload.get('version')}, code={_STATE_VERSION}), skipping")
        return 0

    spot_fn = spot_lookup or _default_spot_lookup
    now = time.time()
    restored = 0
    skipped_stale = 0
    entries = payload.get('entries', [])
    with _pending_lock:
        for entry in entries:
            deadlines = entry.get('deadlines') or {}
            row = entry.get('row') or {}
            if not deadlines or not row:
                continue
            # Skip entries fully past deadline — flush_pending would have
            # written them before shutdown if it had a chance.
            if max(deadlines.values()) < now:
                skipped_stale += 1
                continue
            _pending.append({
                'row': row,
                'deadlines': deadlines,
                'spot_lookup': spot_fn,
            })
            restored += 1

    # Count restored entries toward in_total. They are real prints that the
    # pipeline accepted in the previous session — including them keeps the
    # invariant `in_total ≈ out_total + pending` valid post-recovery, so that
    # `drift` (in capture_rate) reads cleanly as "prints lost between input
    # and disk." Without this, drift goes negative after recovery as restored
    # entries flush to disk without ever appearing in in_total.
    if restored > 0:
        global _in_total
        with _in_lock:
            _in_total += restored

    saved_at = payload.get('saved_at', 0)
    saved_age = now - saved_at
    log.info(f"[DPC-RESTORE] restored {restored} entries, skipped {skipped_stale} stale "
             f"(snapshot was {saved_age:.0f}s old, file age {age:.0f}s)")
    return restored


def _persistence_loop() -> None:
    """Daemon thread — snapshot _pending every _PERSIST_INTERVAL_SEC."""
    while True:
        try:
            time.sleep(_PERSIST_INTERVAL_SEC)
            persist_pending()
        except Exception as e:
            log.warning(f"[DPC-PERSIST] loop error: {e}")
            time.sleep(_PERSIST_INTERVAL_SEC)


def start_persistence() -> bool:
    """Start the background persistence thread. Idempotent — safe to call
    multiple times. Returns True if a thread was started this call."""
    global _persist_thread, _persist_started
    with _persist_lock:
        if _persist_started:
            return False
        _persist_thread = Thread(
            target=_persistence_loop,
            daemon=True,
            name='dpc-persist',
        )
        _persist_thread.start()
        _persist_started = True
        log.info(f"[DPC-PERSIST] started daemon (interval={_PERSIST_INTERVAL_SEC}s)")
        return True
