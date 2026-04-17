"""
Kyle's λ vs current abs_ratio — empirical head-to-head (v2, fixed labeling).

Pulls historical NQ candles from /api/l2/candles, computes both signals per
price level using only past data at evaluation time, labels forward-looking
outcomes via END-OF-WINDOW price (not "any wick through"), and scores both
signals with AUC.

Fixes vs v1:
  1. Only evaluate levels within ±30 ticks of current price (MM-relevant).
  2. Outcome = where does close sit at end of forward window (not any wick).
  3. Forward window tightened to 15 min (1m candles).
  4. "Held" vs "broke" is w.r.t. level's role as support/resistance at eval
     time (side of current price).

Run:
    source venv/bin/activate
    python backtest/kyle_lambda_backtest.py
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from collections import defaultdict
from dataclasses import dataclass

AUTH_TOKEN = "69e18746.4b61616c6934343236.64cb41a6492e1e8a6affa7d544162a10"
API_URL = "http://localhost:3001/api/l2/candles?symbol=NQ&tf=1m"

TICK = 0.25
FORWARD_MIN = 15            # outcome window (candles on 1m tf)
MAX_DIST_TICKS = 30         # only evaluate levels within ±this many ticks of price_now
MIN_HITS = 3                # per-level min observations for reliable fit
BREAK_TICKS = 2             # close must be this far past p (opposite side) to count as break
REEVAL_EVERY = 5            # evaluate every N candles (speeds run, reduces temporal correlation)


@dataclass
class Hit:
    t: int
    open: float
    close: float
    high: float
    low: float
    buy_v: float
    sell_v: float


def fetch_candles() -> list[dict]:
    req = urllib.request.Request(API_URL, headers={"X-Auth-Token": AUTH_TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("candles", [])


def build_level_hits(candles: list[dict]) -> dict[str, list[Hit]]:
    by_level: dict[str, list[Hit]] = defaultdict(list)
    for c in candles:
        bp = c.get("bp") or {}
        for ps, vols in bp.items():
            if not isinstance(vols, (list, tuple)) or len(vols) < 2:
                continue
            b, s = float(vols[0]), float(vols[1])
            if b + s <= 0:
                continue
            by_level[ps].append(Hit(
                t=int(c["time"]),
                open=float(c["open"]),
                close=float(c["close"]),
                high=float(c["high"]),
                low=float(c["low"]),
                buy_v=b,
                sell_v=s,
            ))
    return by_level


# ── SIGNAL 1: current abs_ratio (mirrors server.py:2550-2558) ────────────────
def current_abs_ratio(hits: list[Hit]) -> float:
    if len(hits) < 2:
        return 0.0
    total_vol = sum(h.buy_v + h.sell_v for h in hits)
    ranges = [max(h.high - h.low, TICK) for h in hits]
    avg_range = sum(ranges) / len(ranges)
    if avg_range <= 0:
        return 0.0
    time_factor = max(1, len(hits)) ** 0.5
    return total_vol / avg_range / time_factor


# ── SIGNAL 2: Kyle's λ — OLS of Δp = λ · sign(Q) · √V ────────────────────────
def ols_slope(xs: list[float], ys: list[float]) -> float:
    num = sum(x * y for x, y in zip(xs, ys))
    den = sum(x * x for x in xs)
    return num / den if den > 0 else 0.0


def compute_kyle_lambdas(by_level: dict[str, list[Hit]]) -> tuple[dict[str, float], float]:
    lambdas: dict[str, float] = {}
    for ps, hits in by_level.items():
        if len(hits) < MIN_HITS:
            continue
        xs, ys = [], []
        for h in hits:
            V = h.buy_v + h.sell_v
            Q = h.buy_v - h.sell_v
            if V <= 0 or Q == 0:
                continue
            xs.append(math.copysign(math.sqrt(V), Q))
            ys.append(h.close - h.open)
        if len(xs) < MIN_HITS:
            continue
        lambdas[ps] = abs(ols_slope(xs, ys))
    if not lambdas:
        return {}, 0.0
    sv = sorted(lambdas.values())
    return lambdas, sv[len(sv) // 2]


def kyle_abs_score(lambda_p: float, lambda_market: float) -> float:
    if lambda_market <= 0:
        return 0.0
    return max(0.0, 1.0 - lambda_p / lambda_market)


# ── Additional benchmark signals ─────────────────────────────────────────────
def raw_volume(hits: list[Hit]) -> float:
    return sum(h.buy_v + h.sell_v for h in hits)


def volume_delta(hits: list[Hit]) -> float:
    """Absolute net delta — |buy - sell|. Large imbalance = directional memory."""
    return abs(sum(h.buy_v - h.sell_v for h in hits))


def hit_count(hits: list[Hit]) -> float:
    """How many candles touched p. Persistence proxy."""
    return float(len(hits))


def recency_weighted_vol(hits: list[Hit], now_ts: int, half_life_s: int = 1800) -> float:
    """Exp-decayed volume — recent hits matter more (half-life = 30 min)."""
    lam = math.log(2) / half_life_s
    s = 0.0
    for h in hits:
        age = max(0, now_ts - h.t)
        s += (h.buy_v + h.sell_v) * math.exp(-lam * age)
    return s


def rejection_count(hits: list[Hit], price: float) -> float:
    """Candles that wicked past p by ≥ 1 tick then closed within 1 tick of p."""
    n = 0
    for h in hits:
        pushed = (h.high > price + TICK) or (h.low < price - TICK)
        closed_near = abs(h.close - price) < TICK
        if pushed and closed_near:
            n += 1
    return float(n)


def reversion_score(hits: list[Hit], price: float) -> float:
    """Avg abs(close - p) — lower = price anchors to level = absorbing.
    Inverted so higher = stronger anchor."""
    if not hits:
        return 0.0
    d = [abs(h.close - price) for h in hits]
    avg = sum(d) / len(d)
    return 1.0 / (1.0 + avg)  # 0..1, higher = tighter anchor


# ── OUTCOME LABELER (v2: end-of-window close-based) ──────────────────────────
def label_outcome(price: float, price_now: float, future_candles: list[dict]) -> int | None:
    """
    At evaluation time:
      - price_now = current NQ price
      - level p = absorber candidate
      - role = 'support' if p < price_now, 'resistance' if p > price_now

    At end of forward window (= close of last candle in window):
      - support held:     end_close > p - BREAK_TICKS·tick      → 1
      - support broken:   end_close ≤ p - BREAK_TICKS·tick      → 0
      - resistance held:  end_close < p + BREAK_TICKS·tick      → 1
      - resistance broken:end_close ≥ p + BREAK_TICKS·tick      → 0
    Returns None if no candles in window (shouldn't happen at eval_cutoff).
    """
    if not future_candles:
        return None
    end_close = float(future_candles[-1]["close"])
    role = "support" if p_below(price, price_now) else "resistance"
    thr = BREAK_TICKS * TICK
    if role == "support":
        return 1 if end_close > price - thr else 0
    else:
        return 1 if end_close < price + thr else 0


def p_below(p: float, price_now: float) -> bool:
    return p < price_now


# ── AUC (Mann-Whitney U) ─────────────────────────────────────────────────────
def auc(scores_labels: list[tuple[float, int]]) -> float:
    pos = [s for s, y in scores_labels if y == 1]
    neg = [s for s, y in scores_labels if y == 0]
    if not pos or not neg:
        return float("nan")
    n_pos, n_neg = len(pos), len(neg)
    combined = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg], key=lambda x: x[0])
    rank_sum_pos = 0.0
    i = 0
    while i < len(combined):
        j = i
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2
        for k in range(i, j):
            if combined[k][1] == 1:
                rank_sum_pos += avg_rank
        i = j
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2
    return u / (n_pos * n_neg)


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main() -> int:
    print("Fetching candles from /api/l2/candles?tf=1m ...")
    try:
        candles = fetch_candles()
    except Exception as e:
        print(f"FATAL fetch failed: {e}")
        return 1
    candles = [c for c in candles if c.get("bp")]
    candles.sort(key=lambda c: int(c["time"]))
    if len(candles) < 60:
        print(f"FATAL: only {len(candles)} usable candles.")
        return 1
    print(f"  {len(candles)} candles with bp, "
          f"{(candles[-1]['time'] - candles[0]['time']) / 60:.0f} min span")

    eval_cutoff = int(candles[-1]["time"]) - FORWARD_MIN * 60
    print(f"  eval window: t <= {eval_cutoff} "
          f"(each candle looks {FORWARD_MIN}min forward)")
    print(f"  MAX_DIST = ±{MAX_DIST_TICKS} ticks from price_now, "
          f"MIN_HITS = {MIN_HITS}, BREAK = {BREAK_TICKS} ticks, "
          f"re-eval every {REEVAL_EVERY} candles")

    # rows: (signals_dict, outcome)
    rows: list[tuple[dict[str, float], int]] = []
    skipped_far = 0
    skipped_few_hits = 0

    for idx, c in enumerate(candles):
        if idx % REEVAL_EVERY != 0:
            continue
        t = int(c["time"])
        if t > eval_cutoff:
            break
        price_now = float(c["close"])
        past = candles[: idx + 1]
        by_level = build_level_hits(past)
        if not by_level:
            continue
        lambdas, lambda_market = compute_kyle_lambdas(by_level)
        if lambda_market <= 0:
            continue
        # Forward window
        future = []
        for fc in candles[idx + 1 :]:
            if int(fc["time"]) > t + FORWARD_MIN * 60:
                break
            future.append(fc)
        if not future:
            continue

        for ps, hits in by_level.items():
            price = float(ps)
            if abs(price - price_now) > MAX_DIST_TICKS * TICK:
                skipped_far += 1
                continue
            if len(hits) < MIN_HITS:
                skipped_few_hits += 1
                continue
            # Don't evaluate levels AT the current price (can't be support or resistance)
            if abs(price - price_now) < 0.5 * TICK:
                continue
            outcome = label_outcome(price, price_now, future)
            if outcome is None:
                continue
            sigs = {
                "abs_ratio_current":  current_abs_ratio(hits),
                "kyle_lambda":        kyle_abs_score(lambdas.get(ps, 0.0), lambda_market),
                "raw_volume":         raw_volume(hits),
                "volume_delta_abs":   volume_delta(hits),
                "hit_count":          hit_count(hits),
                "recency_vol":        recency_weighted_vol(hits, t),
                "rejection_count":    rejection_count(hits, price),
                "reversion_anchor":   reversion_score(hits, price),
            }
            rows.append((sigs, outcome))

    if not rows:
        print("FATAL: no rows.")
        return 1

    held = sum(1 for _, y in rows if y == 1)
    broke = len(rows) - held
    print("")
    print(f"Collected {len(rows)} (signals, outcome) observations")
    print(f"  held:  {held} ({100 * held / len(rows):.1f}%)")
    print(f"  broke: {broke} ({100 * broke / len(rows):.1f}%)")
    print(f"  skipped (too far from price):  {skipped_far}")
    print(f"  skipped (too few hits):        {skipped_few_hits}")
    print("")

    # Score every signal
    signal_names = list(rows[0][0].keys())
    results: list[tuple[str, float, float]] = []  # (name, auc, |auc-0.5|)
    for name in signal_names:
        pairs = [(sigs[name], y) for sigs, y in rows]
        a = auc(pairs)
        results.append((name, a, abs(a - 0.5)))

    # Sort by absolute edge (both over/under 0.5 matter — can flip sign)
    results.sort(key=lambda r: r[2], reverse=True)

    print("═══════════════════════════════════════════════════════════════")
    print(f"  {'Signal':<22s} {'AUC (hold)':>11s}  {'Edge':>7s}  {'Dir':>4s}")
    print("───────────────────────────────────────────────────────────────")
    for name, a, edge in results:
        direction = "hold" if a > 0.5 else "break" if a < 0.5 else "—"
        effective = a if a >= 0.5 else (1 - a)
        print(f"  {name:<22s} {a:>11.4f}  {effective:>7.4f}  {direction:>4s}")
    print("═══════════════════════════════════════════════════════════════")
    print("")

    # Best signal
    best = max(results, key=lambda r: r[2])
    name, a, edge = best
    eff = a if a >= 0.5 else 1 - a
    print(f"BEST SIGNAL: {name}")
    print(f"  effective AUC (hold or break predictor): {eff:.4f}")

    if eff > 0.60:
        print("  → real edge. Build this into production.")
    elif eff > 0.55:
        print("  → weak edge. Combine with other signals (ensemble).")
    elif eff > 0.52:
        print("  → marginal. Likely noise, need more data to confirm.")
    else:
        print("  → essentially random on this data. Absorption labels are DECORATIVE.")
        print("  → On 1m NQ with ~5hr of data, no volume-based signal predicts")
        print("    hold/break. Either: (a) need more data, (b) need tick-level L2")
        print("    not aggregated OHLCV, or (c) this is just noise at this timeframe.")

    # Specific verdict on Kyle vs current
    kyle_auc = next(r[1] for r in results if r[0] == "kyle_lambda")
    curr_auc = next(r[1] for r in results if r[0] == "abs_ratio_current")
    k_eff = kyle_auc if kyle_auc >= 0.5 else 1 - kyle_auc
    c_eff = curr_auc if curr_auc >= 0.5 else 1 - curr_auc
    print("")
    print(f"Kyle's λ vs current abs_ratio (as predictors, any direction):")
    print(f"  Kyle effective AUC:    {k_eff:.4f}")
    print(f"  Current effective AUC: {c_eff:.4f}")
    print(f"  Delta:                 {k_eff - c_eff:+.4f}")
    if k_eff > c_eff + 0.03:
        print("  → Kyle wins by ≥ 3 pts — SHIP Kyle's λ.")
    elif k_eff >= c_eff - 0.01:
        print("  → tie or marginal. Current is fine. Skip the HMM stack.")
    else:
        print("  → current formula better. Don't rebuild.")

    print("")
    print("Interpretation:")
    print("  AUC 0.50 = random.  0.55 = weak.  0.60 = real edge.  0.65+ = strong.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
