"""_bridge_state — shared on-disk state for bridge ↔ server multiprocess split.

Pattern: intel modules (pin_convergence, hedge_forecaster, vix_term_structure,
spx_qqq_divergence, sweep_detector, etc.) accumulate per-ticker state in
module-level dicts. With the multiprocess split, those dicts are populated
in the BRIDGE process but read by REST endpoints in the SERVER process — and
process memory doesn't cross.

This helper bridges that gap via a tiny atomic-rename JSON-per-(module,ticker)
file pattern. Bridge writes after each compute; server reads on request with
a small TTL cache to avoid disk-thrashing.

Usage from intel module (e.g. pin_convergence.py):

    from connectors._bridge_state import publish, fetch

    # In compute function (runs in bridge process):
    state = {...}
    _state_cache[ticker] = state
    publish('pin', ticker, state)

    # In REST handler (runs in server process):
    cached = _state_cache.get(ticker)
    if cached:
        return cached
    return fetch('pin', ticker) or {}

Files live at: <repo>/state/intel/<module>_<ticker>.json
Atomic rename ensures no half-written reads.

Why not Redis / shared memory:
- Redis = extra dep + extra port
- multiprocessing.SharedMemory = OS-bound, no atomicity for dicts
- Disk JSON = simple, debuggable, durable across restarts, atomic via rename
"""
import os
import json
import time
import threading
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.path.join(_HERE, '..', 'state', 'intel')
os.makedirs(_STATE_DIR, exist_ok=True)

# Per-module-ticker read cache — avoids re-reading disk if requests come fast.
_read_cache: dict = {}     # (module, ticker) -> (state_dict, last_read_ts)
_read_cache_lock = threading.Lock()
_READ_CACHE_TTL_SEC = 0.5   # 500ms — enough to dedupe burst polls without staleness

# Stats (visible at /api/_debug/bridge_state).
_publish_count: dict = {}   # module -> count
_fetch_count: dict = {}     # module -> count


def _path_for(module: str, ticker: str) -> str:
    safe_ticker = (ticker or 'GLOBAL').replace('/', '_').replace('\\', '_')
    return os.path.join(_STATE_DIR, f'{module}_{safe_ticker}.json')


def publish(module: str, ticker: str, state: dict) -> bool:
    """Bridge-side: write state dict to disk via atomic rename. Returns success."""
    if not isinstance(state, dict):
        return False
    path = _path_for(module, ticker)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            # default=str catches deque/datetime/etc. that compute funcs may
            # accidentally include. separators reduces file size ~20%.
            json.dump(state, f, default=str, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _publish_count[module] = _publish_count.get(module, 0) + 1
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        return False


def fetch(module: str, ticker: str) -> Optional[dict]:
    """Server-side: read state dict from disk (or cache). Returns None on miss."""
    key = (module, ticker)
    now = time.time()
    with _read_cache_lock:
        cached = _read_cache.get(key)
        if cached and (now - cached[1]) < _READ_CACHE_TTL_SEC:
            return cached[0]
    path = _path_for(module, ticker)
    if not os.path.exists(path):
        return None
    try:
        # Mtime check — skip if file is older than 5 minutes (likely stale)
        age = now - os.path.getmtime(path)
        if age > 300:
            return None
        with open(path) as f:
            state = json.load(f)
        with _read_cache_lock:
            _read_cache[key] = (state, now)
        _fetch_count[module] = _fetch_count.get(module, 0) + 1
        return state
    except Exception:
        return None


def stats() -> dict:
    """Counts of publish/fetch ops by module — for debugging."""
    return {
        'publish_count': dict(_publish_count),
        'fetch_count':   dict(_fetch_count),
        'cache_size':    len(_read_cache),
        'state_dir':     _STATE_DIR,
    }
