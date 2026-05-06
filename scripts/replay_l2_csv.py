"""
Replay harness for TopStepX L2 CSV exports.

Format (semicolon-delimited):
  L1 row: L1;<side>;<YYYYMMDDhhmmss>;<ns_offset>;<price>;<size>
  L2 row: L2;<side>;<YYYYMMDDhhmmss>;<ns_offset>;<action>;<level>;;<price>;<size>

Side: 0=ASK, 1=BID. Action: 0=insert, 1=update, 2=delete.

This file has NO trade-print records. We synthesize trades from L1 size
deltas at top of book:
  - same price, smaller size      → trade of (prev_size − new_size) at price
  - ask price moved UP            → BUY of prev_size at prev_ask (top consumed)
  - bid price moved DOWN          → SELL of prev_size at prev_bid (top consumed)
  - ask DOWN / bid UP             → quote refresh, no trade
  - same price, larger size       → liquidity added, no trade

Bars are aggregated 1-minute. After streaming, run the detectors imported
from background_engine.l2_worker (in-process) and report fire counts.

Usage:
  python scripts/replay_l2_csv.py /path/to/NQ_MAR26_*.csv [--max-rows N]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

# Make the project root importable
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _parse_ts(date_field: str, ns_field: str) -> float:
    """Convert (YYYYMMDDhhmmss, ns_offset) → epoch seconds (float)."""
    try:
        dt = datetime.strptime(date_field, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.timestamp() + (int(ns_field) / 1e9)
    except Exception:
        return 0.0


# ────────────────────────────────────────────────────────────────────────────
# Bar accumulator
# ────────────────────────────────────────────────────────────────────────────

class BarBuilder:
    """1-minute OHLCV + bp accumulator. bp matches l2_worker shape:
       {price_str: [buy_vol, sell_vol, fp_score, true_abs, max_book_size]}
    """
    BAR_SEC = 60

    def __init__(self):
        self._cur_bucket: int = -1
        self._cur: dict | None = None
        self.bars: list[dict] = []

    def _new_bar(self, bucket: int, price: float) -> dict:
        return {
            "t": bucket * self.BAR_SEC,        # epoch sec at bar open
            "o": price, "h": price, "l": price, "c": price,
            "bp": defaultdict(lambda: [0, 0, 0.0, 0, 0]),
        }

    def add_trade(self, ts: float, price: float, size: int, side: str, top_book_size_for_level: int):
        """side: 'B' = buy (lifted ask), 'S' = sell (hit bid)."""
        bucket = int(ts // self.BAR_SEC)
        if bucket != self._cur_bucket:
            if self._cur is not None:
                self._finalize(self._cur)
            self._cur_bucket = bucket
            self._cur = self._new_bar(bucket, price)
        b = self._cur
        b["c"] = price
        if price > b["h"]:
            b["h"] = price
        if price < b["l"]:
            b["l"] = price
        ps = f"{price:.2f}"
        rec = b["bp"][ps]
        if side == "B":
            rec[0] += size
        else:
            rec[1] += size
        if top_book_size_for_level > rec[4]:
            rec[4] = top_book_size_for_level

    def _finalize(self, bar):
        # Convert defaultdict to plain dict, freeze list contents
        bar["bp"] = {k: list(v) for k, v in bar["bp"].items()}
        self.bars.append(bar)

    def flush(self):
        if self._cur is not None:
            self._finalize(self._cur)
            self._cur = None
            self._cur_bucket = -1


# ────────────────────────────────────────────────────────────────────────────
# L1 → trade synthesizer
# ────────────────────────────────────────────────────────────────────────────

class L1Tracker:
    """Tracks top-of-book bid/ask. Emits synthetic trades from deltas."""

    def __init__(self, bar_builder: BarBuilder):
        self.bb = bar_builder
        self.last_ask: float | None = None
        self.last_ask_size: int = 0
        self.last_bid: float | None = None
        self.last_bid_size: int = 0
        # diagnostics
        self.trades_buy = 0
        self.trades_sell = 0
        self.trades_consumed_top = 0
        self.trades_within_level = 0
        self.l1_rows = 0

    def on_l1_ask(self, ts: float, price: float, size: int):
        self.l1_rows += 1
        if self.last_ask is None:
            self.last_ask, self.last_ask_size = price, size
            return
        prev_p, prev_sz = self.last_ask, self.last_ask_size
        if price > prev_p:
            # Top ask cleared — full prev level consumed by aggressive buy
            if prev_sz > 0:
                self.bb.add_trade(ts, prev_p, prev_sz, "B", prev_sz)
                self.trades_buy += prev_sz
                self.trades_consumed_top += 1
        elif price == prev_p:
            if size < prev_sz:
                vol = prev_sz - size
                if vol > 0:
                    self.bb.add_trade(ts, prev_p, vol, "B", prev_sz)
                    self.trades_buy += vol
                    self.trades_within_level += 1
        # ask price DOWN → quote refresh, no trade
        self.last_ask, self.last_ask_size = price, size

    def on_l1_bid(self, ts: float, price: float, size: int):
        self.l1_rows += 1
        if self.last_bid is None:
            self.last_bid, self.last_bid_size = price, size
            return
        prev_p, prev_sz = self.last_bid, self.last_bid_size
        if price < prev_p:
            # Top bid cleared — full prev level consumed by aggressive sell
            if prev_sz > 0:
                self.bb.add_trade(ts, prev_p, prev_sz, "S", prev_sz)
                self.trades_sell += prev_sz
                self.trades_consumed_top += 1
        elif price == prev_p:
            if size < prev_sz:
                vol = prev_sz - size
                if vol > 0:
                    self.bb.add_trade(ts, prev_p, vol, "S", prev_sz)
                    self.trades_sell += vol
                    self.trades_within_level += 1
        # bid price UP → quote refresh
        self.last_bid, self.last_bid_size = price, size


# ────────────────────────────────────────────────────────────────────────────
# Streaming parser
# ────────────────────────────────────────────────────────────────────────────

def parse_csv(path: str, max_rows: int | None = None, bar_seconds: int = 60) -> tuple[list[dict], dict]:
    bb = BarBuilder()
    bb.BAR_SEC = bar_seconds
    l1 = L1Tracker(bb)

    n = 0
    n_l1 = 0
    n_l2 = 0
    t0 = time.time()
    last_ts = 0.0

    with open(path, "r", buffering=1 << 20) as f:
        for line in f:
            n += 1
            if max_rows and n > max_rows:
                break
            parts = line.rstrip("\n").split(";")
            if len(parts) < 6:
                continue
            kind = parts[0]
            if kind == "L1":
                # L1;<side>;<date>;<ns>;<price>;<size>
                try:
                    side = parts[1]
                    ts = _parse_ts(parts[2], parts[3])
                    price = float(parts[4])
                    size = int(parts[5])
                except Exception:
                    continue
                if side == "0":
                    l1.on_l1_ask(ts, price, size)
                else:
                    l1.on_l1_bid(ts, price, size)
                n_l1 += 1
                last_ts = ts
            elif kind == "L2":
                # We don't currently use L2 depth (Proposal C will hook here).
                # Just count for stats.
                n_l2 += 1
            if n % 5_000_000 == 0:
                el = time.time() - t0
                print(
                    f"  …parsed {n:>10,} rows ({n/1e6:.1f}M)  "
                    f"l1={n_l1:>10,}  l2={n_l2:>10,}  "
                    f"bars={len(bb.bars):>5,}  elapsed={el:5.1f}s",
                    file=sys.stderr,
                )

    bb.flush()
    stats = {
        "rows":                n,
        "l1_rows":             n_l1,
        "l2_rows":             n_l2,
        "bars":                len(bb.bars),
        "synth_buy_volume":    l1.trades_buy,
        "synth_sell_volume":   l1.trades_sell,
        "synth_top_consumed":  l1.trades_consumed_top,
        "synth_within_level":  l1.trades_within_level,
        "elapsed_s":           round(time.time() - t0, 1),
        "session_start_ts":    bb.bars[0]["t"] if bb.bars else 0,
        "session_end_ts":      last_ts,
    }
    return bb.bars, stats


# ────────────────────────────────────────────────────────────────────────────
# Detector runner
# ────────────────────────────────────────────────────────────────────────────

def run_detectors(bars: list[dict], symbol: str = "NQ_REPLAY") -> dict:
    """Replay bars through l2_worker's three detectors. Returns fire counts."""
    # Import here so script can do --help even if l2_worker has import-time
    # heavy init.
    from background_engine import l2_worker as lw

    history: deque = deque(maxlen=50)

    fire_abs = 0
    fire_exh = 0
    fire_agg = 0
    abs_samples: list = []
    exh_samples: list = []
    agg_samples: list = []
    _exh_reasons: dict = {}

    # Seed adaptive floors so they stop returning the fallback
    for bar in bars:
        # Push level samples
        for ps, e in bar["bp"].items():
            tot = (e[0] or 0) + (e[1] or 0)
            if tot > 0:
                lw._LEVEL_VOL_SAMPLES[symbol].append(tot)
        # Push bar sample
        bt = sum((e[0] or 0) + (e[1] or 0) for e in bar["bp"].values())
        if bt > 0:
            lw._BAR_VOL_SAMPLES[symbol].append(bt)

        # Run detectors
        try:
            abs_evts = lw._detect_bar_absorption(symbol, bar)
            if abs_evts:
                fire_abs += len(abs_evts)
                if len(abs_samples) < 5:
                    abs_samples.append({"t": bar["t"], **abs_evts[0]})
        except Exception as e:
            print(f"absorption err: {e}", file=sys.stderr)

        try:
            exh = lw._detect_bar_exhaustion(symbol, "1m", bar, history)
            if exh:
                fire_exh += 1
                # Bucket by reason
                r = exh.get('reason', '?')
                _exh_reasons[r] = _exh_reasons.get(r, 0) + 1
                if len(exh_samples) < 8:
                    exh_samples.append({"t": bar["t"], **exh})
        except Exception as e:
            print(f"exhaustion err: {e}", file=sys.stderr)

        try:
            agg = lw._detect_bar_aggression(symbol, "1m", bar, history)
            if agg:
                fire_agg += 1
                if len(agg_samples) < 5:
                    agg_samples.append({"t": bar["t"], **agg})
        except Exception as e:
            print(f"aggression err: {e}", file=sys.stderr)

        history.append(bar)

    return {
        "bars_processed": len(bars),
        "fire_absorption": fire_abs,
        "fire_exhaustion": fire_exh,
        "fire_aggression": fire_agg,
        "rate_absorption": round(fire_abs / max(len(bars), 1) * 100, 2),
        "rate_exhaustion": round(fire_exh / max(len(bars), 1) * 100, 2),
        "rate_aggression": round(fire_agg / max(len(bars), 1) * 100, 2),
        "absorption_samples": abs_samples,
        "exhaustion_samples": exh_samples,
        "aggression_samples": agg_samples,
        "exhaustion_reasons": _exh_reasons,
    }


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv", help="Path to L2 CSV file")
    p.add_argument("--max-rows", type=int, default=0, help="Cap input rows (0=all)")
    p.add_argument("--bar-seconds", type=int, default=60)
    p.add_argument("--symbol", default="NQ_REPLAY")
    args = p.parse_args()

    print(f"=== REPLAY {os.path.basename(args.csv)} ===", file=sys.stderr)
    print(f"file size: {os.path.getsize(args.csv)/1e9:.2f} GB", file=sys.stderr)
    if args.max_rows:
        print(f"max-rows cap: {args.max_rows:,}", file=sys.stderr)

    bars, stats = parse_csv(args.csv, args.max_rows or None, args.bar_seconds)
    print("\n=== PARSE STATS ===")
    for k, v in stats.items():
        print(f"  {k:<20} {v:,}" if isinstance(v, int) else f"  {k:<20} {v}")
    if not bars:
        print("no bars produced — empty session?")
        return

    bv = sorted([sum((e[0] or 0) + (e[1] or 0) for e in b["bp"].values()) for b in bars])
    print(f"  bar_vol_p50         {bv[len(bv)//2]:,}")
    print(f"  bar_vol_p75         {bv[int(len(bv)*0.75)]:,}")
    print(f"  bar_vol_p95         {bv[int(len(bv)*0.95)]:,}")
    print(f"  bar_vol_max         {bv[-1]:,}")

    print("\n=== RUNNING DETECTORS ===", file=sys.stderr)
    res = run_detectors(bars, args.symbol)
    print("\n=== DETECTOR FIRES ===")
    print(f"  bars_processed       {res['bars_processed']:,}")
    print(f"  absorption fires     {res['fire_absorption']:>5}  ({res['rate_absorption']}% of bars)")
    print(f"  exhaustion fires     {res['fire_exhaustion']:>5}  ({res['rate_exhaustion']}% of bars)")
    print(f"  aggression fires     {res['fire_aggression']:>5}  ({res['rate_aggression']}% of bars)")

    if res["absorption_samples"]:
        print("\n--- absorption samples (first 5) ---")
        for s in res["absorption_samples"]:
            print(f"  t={s['t']:>12} px={s['price']:.2f} side={s['side']:<8} vol={s['volume']:>4} strength={s.get('strength',0)}")
    if res["exhaustion_samples"]:
        print("\n--- exhaustion reason distribution ---")
        for r, n in sorted(res["exhaustion_reasons"].items(), key=lambda x: -x[1]):
            print(f"  {r:<24} {n:>4}")
        print("\n--- exhaustion samples (first 8) ---")
        for s in res["exhaustion_samples"]:
            extra = ""
            if 'cur_3bar_delta' in s:
                extra = f"  cur_3b={s['cur_3bar_delta']:+5} prior_3b={s['prior_3bar_delta']:+5}"
            print(f"  t={s['t']:>12} px={s['price']:.2f} side={s['side']:<16} cur_d={s['cur_delta']:+5} prior_d={s['prior_delta']:+5} reason={s['reason']:<22} strength={s['strength']}{extra}")
    if res["aggression_samples"]:
        print("\n--- aggression samples (first 5) ---")
        for s in res["aggression_samples"]:
            print(f"  t={s['t']:>12} px={s.get('price',0):.2f} side={s.get('side','?'):<8} vol={s.get('volume',0):>4} strength={s.get('strength',0)}")


if __name__ == "__main__":
    main()
