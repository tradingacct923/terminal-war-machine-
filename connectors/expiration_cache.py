"""
Expiration metadata cache.

Schwab's /expirationchain endpoint returns rich metadata per expiration:
  expirationType: W (weekly), M (monthly), Q (quarterly), S (standard/LEAPS)
  settlementType: P (physical), C (cash — index options like SPX)
  daysToExpiration: integer days
  standard: bool

Our previous _schwab_expirations() flattened this to date strings only, losing
the type info needed to label flow alerts as [0dte] vs [weekly] vs [monthly].

This cache preserves the full metadata, indexed by (ticker, expiration_date),
and exposes a classify(symbol_str) helper that takes a full Schwab option
symbol (e.g., 'SPY   260420P00710000') and returns its expiration bucket.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExpInfo:
    date: str            # YYYY-MM-DD
    dte: int
    type: str            # 'W' | 'M' | 'Q' | 'S' | '?'
    settlement: str      # 'P' | 'C' | '?'
    standard: bool = True


# Bucket tags used by downstream consumers (FlowAccumulator, alert engine)
BUCKET_0DTE = "0dte"
BUCKET_WEEKLY = "weekly"       # W type, DTE 1-7
BUCKET_MONTHLY = "monthly"     # M type OR W with monthly-Friday pattern
BUCKET_QUARTERLY = "quarterly"  # Q type
BUCKET_LEAPS = "leaps"         # S type, DTE > 60
BUCKET_UNKNOWN = "unknown"


def _bucket_for(dte: int, exp_type: str) -> str:
    """Map (dte, type) → flow-analysis bucket label."""
    if dte == 0:
        return BUCKET_0DTE
    if exp_type == "M":
        return BUCKET_MONTHLY
    if exp_type == "Q":
        return BUCKET_QUARTERLY
    if exp_type == "S":
        return BUCKET_LEAPS if dte > 60 else BUCKET_MONTHLY
    if exp_type == "W":
        return BUCKET_WEEKLY if dte <= 7 else BUCKET_MONTHLY
    return BUCKET_UNKNOWN


class ExpirationCache:
    """Thread-safe cache of per-ticker expiration metadata, refreshed daily."""

    def __init__(self, refresh_interval_sec: int = 3600):
        self._refresh_interval = refresh_interval_sec
        self._lock = threading.Lock()
        # { ticker: { 'YYYY-MM-DD': ExpInfo } }
        self._by_ticker: dict[str, dict[str, ExpInfo]] = {}
        # { ticker: last_refresh_ts }
        self._last_refresh: dict[str, float] = {}

    def refresh(self, ticker: str, schwab_fetcher) -> int:
        """Fetch fresh metadata from Schwab /expirationchain.

        schwab_fetcher: callable(endpoint, params) → dict. We use the existing
        server._schwab_get to avoid duplicating auth.

        Returns number of expirations loaded.
        """
        try:
            data = schwab_fetcher("/marketdata/v1/expirationchain", {"symbol": ticker})
        except Exception as e:
            log.warning(f"[EXP-CACHE] {ticker}: fetch failed: {e}")
            return 0

        raw = data.get("expirationList", []) or []
        parsed: dict[str, ExpInfo] = {}
        for e in raw:
            date = e.get("expirationDate")
            if not date:
                continue
            try:
                parsed[date] = ExpInfo(
                    date=date,
                    dte=int(e.get("daysToExpiration", 0) or 0),
                    type=(e.get("expirationType") or "?").upper()[:1],
                    settlement=(e.get("settlementType") or "?").upper()[:1],
                    standard=bool(e.get("standard", True)),
                )
            except (TypeError, ValueError):
                continue

        with self._lock:
            self._by_ticker[ticker] = parsed
            self._last_refresh[ticker] = time.time()

        log.info(f"[EXP-CACHE] {ticker}: cached {len(parsed)} expirations")
        return len(parsed)

    def get(self, ticker: str, date: str) -> Optional[ExpInfo]:
        """Look up one expiration for a ticker."""
        with self._lock:
            return self._by_ticker.get(ticker, {}).get(date)

    def all(self, ticker: str) -> list[ExpInfo]:
        """Return all expirations for a ticker, sorted by DTE."""
        with self._lock:
            d = self._by_ticker.get(ticker, {})
            return sorted(d.values(), key=lambda e: e.dte)

    def by_type(self, ticker: str, exp_type: str) -> list[ExpInfo]:
        """Return expirations filtered by type ('W','M','Q','S')."""
        return [e for e in self.all(ticker) if e.type == exp_type.upper()]

    def needs_refresh(self, ticker: str) -> bool:
        with self._lock:
            last = self._last_refresh.get(ticker, 0)
        return time.time() - last > self._refresh_interval

    def classify_symbol(self, schwab_sym: str) -> tuple[str, str]:
        """
        Given a full Schwab option symbol (e.g. 'SPY   260420P00710000'),
        return (ticker, bucket). bucket is one of BUCKET_* constants.

        Parses expiration from symbol chars 6-12 (YYMMDD). Looks up the
        cached ExpInfo; falls back to DTE-based heuristic if missing.
        """
        if not schwab_sym or len(schwab_sym) < 15:
            return ("", BUCKET_UNKNOWN)

        ticker = schwab_sym[:6].strip()
        yymmdd = schwab_sym[6:12]
        if len(yymmdd) != 6 or not yymmdd.isdigit():
            return (ticker, BUCKET_UNKNOWN)

        # YYMMDD → YYYY-MM-DD
        try:
            yy, mm, dd = yymmdd[:2], yymmdd[2:4], yymmdd[4:6]
            year = 2000 + int(yy)
            date_iso = f"{year}-{mm}-{dd}"
        except ValueError:
            return (ticker, BUCKET_UNKNOWN)

        info = self.get(ticker, date_iso)
        if info is not None:
            return (ticker, _bucket_for(info.dte, info.type))

        # Fallback: DTE from date arithmetic, assume weekly type
        try:
            from datetime import date as _d
            y, m, d = map(int, date_iso.split("-"))
            dte = (_d(y, m, d) - _d.today()).days
            return (ticker, _bucket_for(max(dte, 0), "W"))
        except (ValueError, TypeError):
            return (ticker, BUCKET_UNKNOWN)


_cache: Optional[ExpirationCache] = None


def get_cache() -> Optional[ExpirationCache]:
    return _cache


def init_cache(refresh_interval_sec: int = 3600) -> ExpirationCache:
    global _cache
    if _cache is None:
        _cache = ExpirationCache(refresh_interval_sec=refresh_interval_sec)
    return _cache
