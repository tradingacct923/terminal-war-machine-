"""
Event Calendar — earnings + macro events that drive vol regime expectation.

Knowing META reports tomorrow AM = Mag-8 vol regime expectation flips entirely
for the overnight session. This module surfaces upcoming events with vol-impact
tags so the operator can adjust risk posture.

Data sources (priority order):
  1. JSON file at data/event_calendar.json (manual operator-maintained)
  2. Future: Tradier earnings calendar API (when subscription supports it)
  3. Future: External free APIs (yfinance, finnhub) as fallback

Module reloads from disk every 60 min — events change rarely enough that
high-frequency polling adds no value.

Output:
  {
    next_event:     {ticker, type, impact, ts, time_until_sec, ...},
    in_24hr:        [...],     # events within next 24 hours
    in_7d:          [...],     # events within next 7 days
    vol_warning:    {           # high-impact event within 24hr → flag
       active:    bool,
       event:     {...},
       hours:     float,
    },
    source:         'json_file' | 'static_fallback' | 'no_data',
    last_loaded_ts: float,
    server_time:    float,
    reason:         str | None,
  }

Outcome ledger: not maintained — events are pre-known facts, not predictions.
The module's job is to surface them, not to validate them.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# DST-aware ET clock
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo('America/New_York')
except Exception:
    _ET_TZ = None

# ── CONFIGURED constants (categorized in MEASURED_VALUES.md) ────────────────
RELOAD_INTERVAL_S       = 3600.0      # CONFIGURED — 60 min disk reload cadence
VOL_WARNING_HOURS       = 24.0        # CONFIGURED — high-impact event within X hours = warn
HIGH_IMPACT_TYPES = (                 # STRUCTURAL — types that warrant vol_warning flag
    'earnings_pre_market',
    'earnings_after_close',
    'earnings_during',
    'fomc_decision',
    'cpi_release',
    'nfp_release',
    'pce_release',
)

# Mag-8 (highly correlated with QQQ moves)
MAG_8 = ('AAPL', 'MSFT', 'GOOG', 'GOOGL', 'META', 'AMZN', 'TSLA', 'NVDA', 'AVGO')

# Macro tickers (apply to whole market)
MACRO_TICKERS = ('ALL', 'SPX', 'QQQ', 'SPY')

# ── Module state ────────────────────────────────────────────────────────────
_state_cache: dict = {}
_state_lock = threading.RLock()
_last_load_ts: float = 0.0
_last_load_source: str = ''
_loaded_events: list = []          # parsed [{ts_unix, ticker, type, impact, notes}]


def _calendar_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'data', 'event_calendar.json')


def _parse_ts(s: str) -> Optional[float]:
    """Parse ISO 8601 or 'YYYY-MM-DD HH:MM:SS ET' to unix timestamp."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    # Try ISO 8601 with timezone
    try:
        from datetime import datetime as _dt
        # Replace ' ET' suffix → assume America/New_York
        if s.endswith(' ET'):
            naive_str = s[:-3].strip()
            dt = _dt.strptime(naive_str, '%Y-%m-%d %H:%M:%S')
            if _ET_TZ:
                dt = dt.replace(tzinfo=_ET_TZ)
                return dt.timestamp()
            else:
                # Fallback: assume EDT (-4)
                return dt.timestamp() + 4 * 3600
        # ISO 8601
        try:
            dt = _dt.fromisoformat(s)
            if dt.tzinfo is None and _ET_TZ:
                dt = dt.replace(tzinfo=_ET_TZ)
            return dt.timestamp()
        except Exception:
            return None
    except Exception:
        return None


def _load_from_disk() -> tuple:
    """Read + parse data/event_calendar.json. Returns (events_list, source_str)."""
    global _last_load_ts, _last_load_source
    path = _calendar_path()
    if not os.path.exists(path):
        return ([], 'no_file')
    try:
        with open(path, 'r') as f:
            data = json.load(f) or {}
    except Exception as e:
        log.warning(f"[EVENT-CAL] failed to load {path}: {e}")
        return ([], 'parse_err')

    raw_events = data.get('events') or []
    parsed = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        ts_unix = _parse_ts(ev.get('ts', ''))
        if ts_unix is None:
            continue
        parsed.append({
            'ts_unix':  ts_unix,
            'ts_iso':   ev.get('ts'),
            'ticker':   (ev.get('ticker') or '').upper(),
            'type':     ev.get('type') or 'other',
            'impact':   (ev.get('impact') or 'medium').lower(),
            'notes':    ev.get('notes') or '',
        })
    parsed.sort(key=lambda e: e['ts_unix'])
    _last_load_ts = time.time()
    _last_load_source = 'json_file'
    return (parsed, 'json_file')


def _maybe_reload() -> None:
    """Reload from disk if RELOAD_INTERVAL_S elapsed."""
    global _loaded_events, _last_load_source
    now = time.time()
    if (now - _last_load_ts) >= RELOAD_INTERVAL_S or _last_load_ts == 0:
        events, source = _load_from_disk()
        with _state_lock:
            _loaded_events = events
            _last_load_source = source


def _is_high_impact(ev: dict) -> bool:
    if (ev.get('impact') or '').lower() == 'high':
        return True
    return ev.get('type') in HIGH_IMPACT_TYPES


def compute_state() -> dict:
    """Build snapshot of upcoming events. Cached."""
    _maybe_reload()
    now = time.time()

    with _state_lock:
        events_copy = list(_loaded_events)
        source      = _last_load_source

    # Filter to future events only
    future = [e for e in events_copy if e['ts_unix'] >= now - 300]   # tiny grace = 5min past
    if not future:
        if not events_copy:
            empty_reason = 'no_events_in_calendar'      # file loaded but events array empty
        else:
            empty_reason = 'all_events_past'             # all calendar entries are in the past
        empty = {
            'next_event':     None,
            'in_24hr':        [],
            'in_7d':          [],
            'vol_warning':    {'active': False, 'event': None, 'hours': None},
            'source':         source if source else 'no_data',
            'last_loaded_ts': _last_load_ts,
            'server_time':    now,
            'reason':         empty_reason,
        }
        with _state_lock:
            _state_cache['latest'] = empty
        return empty

    # Bucket
    in_24hr = []
    in_7d   = []
    cutoff_24h = now + 24 * 3600
    cutoff_7d  = now + 7 * 24 * 3600
    for e in future:
        e_out = dict(e)
        e_out['time_until_sec'] = round(e['ts_unix'] - now, 1)
        e_out['time_until_hours'] = round((e['ts_unix'] - now) / 3600, 2)
        e_out['mag_8'] = (e['ticker'] in MAG_8)
        e_out['macro'] = (e['ticker'] in MACRO_TICKERS)
        e_out['high_impact'] = _is_high_impact(e)
        if e['ts_unix'] <= cutoff_24h:
            in_24hr.append(e_out)
        if e['ts_unix'] <= cutoff_7d:
            in_7d.append(e_out)

    next_event = future[0] if future else None
    if next_event:
        next_event = dict(next_event)
        next_event['time_until_sec'] = round(next_event['ts_unix'] - now, 1)
        next_event['time_until_hours'] = round((next_event['ts_unix'] - now) / 3600, 2)
        next_event['mag_8'] = (next_event['ticker'] in MAG_8)
        next_event['macro'] = (next_event['ticker'] in MACRO_TICKERS)
        next_event['high_impact'] = _is_high_impact(next_event)

    # Vol warning: any high-impact event within VOL_WARNING_HOURS
    vol_warning = {'active': False, 'event': None, 'hours': None}
    for e in in_24hr:
        if e.get('high_impact') and e['time_until_hours'] <= VOL_WARNING_HOURS:
            vol_warning = {
                'active': True,
                'event':  e,
                'hours':  e['time_until_hours'],
            }
            break

    state = {
        'next_event':     next_event,
        'in_24hr':        in_24hr,
        'in_7d':          in_7d,
        'vol_warning':    vol_warning,
        'source':         source,
        'last_loaded_ts': _last_load_ts,
        'server_time':    now,
        'reason':         None,
    }
    with _state_lock:
        _state_cache['latest'] = state
    return state


def get_state() -> dict:
    """REST handler — return cached state (cheap snapshot)."""
    with _state_lock:
        cached = _state_cache.get('latest')
        if cached:
            return cached
    return compute_state()


def get_vol_warning() -> dict:
    """External API — small dict for other modules (CCS, vix_term) to read."""
    state = get_state()
    return state.get('vol_warning') or {'active': False, 'event': None, 'hours': None}
