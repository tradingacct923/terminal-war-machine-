"""Wall Signals — verified venue-structural signals around GEX walls.

Emits two conviction scores per session for each key gamma level
(put_wall / call_wall / gamma_flip):

  1. CONTINUATION score — fires when spot breaks a wall AND the strike sees
     prints on pro-rata venues (PHLX, ISEX, MERC) within the lookback window.
     Pro-rata matching is a public exchange rulebook property, so the signal
     is a verified institutional footprint — orders large enough to require
     pro-rata allocation across resting MMs cannot be retail-sized.

  2. FADE score — fires when spot REACHES a wall (inside tolerance but not
     crossed) AND fewer than N distinct venues pulled size at the wall strike
     in the lookback window. "Multi-venue pull" means at least 2 distinct
     exchange MPIDs reduced size at that strike; fewer than 2 = no dealer
     consensus that the wall is breaking = fade the reach.

Verified data only:
  - Wall strikes from server.py /api/walls (GEX/OI computation)
  - Spot price from schwab_bridge spot_update stream
  - Print exchange codes from Tradier TIMESALE_OPTIONS (OPRA feed)
  - Quote-pull events from mm_attribution EXCH_REMOVE events (Schwab book)

All thresholds are CONFIGURED (file-top constants, documented) or STRUCTURAL
(e.g. "< 2 venues" — binary "multi-venue" definition from exchange rulebook
count of 16+ US options venues).

Units throughout: unix epoch seconds for ts, dollar price for strikes,
0.0–1.0 for scores.
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)

# ── Verified venue classification (exchange rulebooks) ─────────────────────
# Source: each exchange's own published matching rules + OPRA participant list.
# These are canonical Schwab MPIDs (what appears in OPTIONS_BOOK.market_makers).
#
# Pro-rata venues — orders too large to fill on one MM get allocated across
# all resting MMs in proportion to their size. Only used for block flow;
# retail orders are never routed here. Institutional footprint = verified.
PRORATA_VENUES = frozenset({
    'PHLX',   # Nasdaq PHLX — pro-rata for complex orders
    'ISEX',   # Nasdaq ISE  — pro-rata for size-driven orders
    'MERC',   # Nasdaq MRX  — same pro-rata mechanics as ISE
    'GMNI',   # Nasdaq GEMX — pro-rata variant (former ISE Gemini)
})

# Auction venues — every print executed through a price-improvement auction.
# Retail PFOF orders land here for mandatory PI (BOX PIP, AMEX CUBE, CBOE AIM).
AUCTION_VENUES = frozenset({
    'XBXO',   # BOX Options Exchange (PIP auction)
})

# Price-time venues — first-in-wins matching. HFT-packed, fast quote updates.
# Used when referring to "modern" lit venues with maker-taker economics.
PRICETIME_VENUES = frozenset({
    'MEMX', 'NSDQ', 'BOSX', 'EDGX', 'BATS', 'PACX',
})

# ── CONFIGURED parameters (documented in docs/MEASURED_VALUES.md) ──────────
# Proximity tolerance: spot is "near" the wall when |spot − wall| / wall ≤ TOL.
# 0.0025 = 0.25% — default tight enough to avoid false alarms on range days,
# wide enough to catch the approach-to-wall phase on trend days.
# User dropdown in the pane can override at runtime.
DEFAULT_PROXIMITY_PCT = 0.0025

# Lookback window for counting prints and pulls at wall strikes.
# 60s — short enough that the signal reflects current microstructure, long
# enough to accumulate stats on liquid strikes. User dropdown can override.
DEFAULT_LOOKBACK_SEC = 60.0

# Multi-venue threshold for FADE signal. Structural: 2 is the minimum count
# that qualifies as "multi". 16+ US options venues exist; fewer than 2 pulling
# means no dealer consensus. Not a magnitude cutoff — structural definition.
MULTI_VENUE_MIN = 2

# Event buffer cap per wall — bounds memory on chatty sessions. Not a signal
# filter: older events age out by wall-clock before this cap is hit on
# anything but pathologically active strikes.
EVENT_BUFFER_CAP = 5_000

# Socket emit cadence. 1Hz — the signal changes slowly; faster adds no value.
EMIT_INTERVAL_SEC = 1.0

# ── State ───────────────────────────────────────────────────────────────────
# Per underlying (QQQ / SPY), we track current walls + spot + recent events.
_walls: dict = {}          # ticker -> {'put_wall', 'call_wall', 'gamma_flip', 'ts'}
_spot: dict = {}           # ticker -> {'price', 'ts', 'prev_price'}
_cross_history: dict = defaultdict(deque)   # ticker -> deque of (ts, wall_name, direction)

# Event buffers keyed by (ticker, wall_strike_int, option_side).
# option_side ∈ {'C', 'P'}. We buffer:
#   - prints: {ts, exch, size, price}
#   - pulls:  {ts, exch}
# Lookback filtering is done on read; we bound the deque to EVENT_BUFFER_CAP.
_prints_at_wall: dict = defaultdict(lambda: deque(maxlen=EVENT_BUFFER_CAP))
_pulls_at_wall: dict  = defaultdict(lambda: deque(maxlen=EVENT_BUFFER_CAP))

_state_lock = Lock()
_last_emit_ts: float = 0.0

# OSI-format option symbol regex: "QQQ   260501C00660000"
# Schwab + Tradier both use this format for options. Strike is last 8 digits
# / 1000; side is the C or P before the strike.
_OSI_RE = re.compile(r'^\s*([A-Z]{1,6})\s+(\d{6})([CP])(\d{8})\s*$')


def _parse_option_sym(sym: str) -> Optional[dict]:
    """Extract {ticker, yymmdd, side, strike} from an OSI symbol.
    Returns None if the symbol doesn't parse.
    """
    if not sym:
        return None
    m = _OSI_RE.match(sym)
    if not m:
        return None
    return {
        'ticker': m.group(1),
        'yymmdd': m.group(2),
        'side':   m.group(3),
        'strike': int(m.group(4)) / 1000.0,
    }


# ── Input API (called by schwab_bridge / mm_attribution) ────────────────────

def update_walls(ticker: str, put_wall: float, call_wall: float,
                 gamma_flip: float,
                 gamma_call_wall: float = 0.0,
                 gamma_put_wall: float = 0.0,
                 dealer_net_at_call: float = 0.0,
                 dealer_net_at_put: float = 0.0,
                 dealer_net_at_gamma_call: float = 0.0,
                 dealer_net_at_gamma_put: float = 0.0,
                 dealer_net_peak: float = 0.0,
                 hp_gamma_shares_1pct: float = 0.0,
                 ts: Optional[float] = None) -> None:
    """Refresh known walls for `ticker` (QQQ / SPY / etc).
    Called from the schwab_bridge flush loop after /api/walls recomputes.

    Signed-gamma context (new):
      - gamma_call_wall / gamma_put_wall: strikes with max DOLLAR-gamma per side
        (distinct from OI-based put_wall/call_wall — reflects where dealer
        hedge pressure actually lives, not where contracts happened to open).
      - dealer_net_at_*: signed dealer gamma AT each wall's strike.
          dealer_net > 0 → dealers NET LONG gamma there (hedge damps moves)
          dealer_net < 0 → dealers NET SHORT gamma there (hedge amplifies moves)
      - dealer_net_peak: |max| across all strikes. Normalizer for 0-1 UI scales.
      - hp_gamma_shares_1pct: dealer rehedge SHARES per +1% spot move
        (Phase 10D — authoritative regime signal).
        > 0 → SHORT-γ at spot (dealers chase rallies, sell dips → trending)
        < 0 → LONG-γ at spot (dealers fade rallies, buy dips → mean-rev)
        Used by `get_state(ticker).regime` to label correctly when the chain
        has multiple sign-crossings (typical when OI clusters are barbelled).
    """
    if ts is None:
        ts = time.time()
    with _state_lock:
        _walls[ticker] = {
            'put_wall':   float(put_wall)   if put_wall   else 0.0,
            'call_wall':  float(call_wall)  if call_wall  else 0.0,
            'gamma_flip': float(gamma_flip) if gamma_flip else 0.0,
            'gamma_call_wall':          float(gamma_call_wall) if gamma_call_wall else 0.0,
            'gamma_put_wall':           float(gamma_put_wall)  if gamma_put_wall  else 0.0,
            'dealer_net_at_call':       float(dealer_net_at_call),
            'dealer_net_at_put':        float(dealer_net_at_put),
            'dealer_net_at_gamma_call': float(dealer_net_at_gamma_call),
            'dealer_net_at_gamma_put':  float(dealer_net_at_gamma_put),
            'dealer_net_peak':          float(dealer_net_peak),
            'hp_gamma_shares_1pct':     float(hp_gamma_shares_1pct),
            'ts': ts,
        }


def update_spot(ticker: str, price: float, ts: Optional[float] = None) -> None:
    """Update spot price for `ticker`. Called on every spot_update event.
    Detects wall-cross events by comparing against the previous spot.
    """
    if ts is None:
        ts = time.time()
    try:
        p = float(price)
    except (TypeError, ValueError):
        return
    if p <= 0:
        return
    with _state_lock:
        prev = _spot.get(ticker, {}).get('price')
        _spot[ticker] = {'price': p, 'ts': ts, 'prev_price': prev}
        walls = _walls.get(ticker)
        if walls and prev is not None:
            # Detect crosses in this spot update. A cross is a sign change of
            # (spot − wall) between prev and now.
            for wname in ('put_wall', 'call_wall', 'gamma_flip'):
                w = walls.get(wname, 0.0)
                if not w:
                    continue
                if (prev - w) * (p - w) < 0:
                    direction = 'up' if p > prev else 'down'
                    _cross_history[ticker].append({
                        'ts': ts, 'wall': wname, 'strike': w,
                        'direction': direction, 'from': prev, 'to': p,
                    })
                    # Bound history to a day's worth of crosses (cheap).
                    while len(_cross_history[ticker]) > 500:
                        _cross_history[ticker].popleft()


def on_print(osi_sym: str, exch: str, price: float, size: int,
             ts: Optional[float] = None) -> None:
    """Record a print on a wall strike. Called from mm_attribution.on_print
    after venue normalization (exch is a Schwab MPID like 'PHLX' or 'MEMX').
    Non-wall-strike prints are ignored.
    """
    if ts is None:
        ts = time.time()
    parsed = _parse_option_sym(osi_sym)
    if not parsed:
        return
    ticker = parsed['ticker']
    strike = parsed['strike']
    side = parsed['side']
    with _state_lock:
        walls = _walls.get(ticker)
        if not walls:
            return
        # A print "at the wall" means its strike matches one of the wall
        # strikes exactly. Wall strikes come from the GEX/OI computation
        # which uses integer-rounded strikes, so equality holds in practice.
        for wname in ('put_wall', 'call_wall', 'gamma_flip'):
            w_strike = walls.get(wname, 0.0)
            if not w_strike:
                continue
            if abs(strike - w_strike) < 0.01:   # float safety; strikes are ints
                key = (ticker, wname, side)
                _prints_at_wall[key].append({
                    'ts': ts, 'exch': (exch or '').upper(),
                    'price': price, 'size': size,
                })
                break


def on_exch_remove(osi_sym: str, exch: str,
                   ts: Optional[float] = None) -> None:
    """Record a venue pulling size at a wall strike. Called from
    mm_attribution diff-logic when an EXCH_REMOVE structural event fires at
    the top of the book.
    """
    if ts is None:
        ts = time.time()
    parsed = _parse_option_sym(osi_sym)
    if not parsed:
        return
    ticker = parsed['ticker']
    strike = parsed['strike']
    side = parsed['side']
    with _state_lock:
        walls = _walls.get(ticker)
        if not walls:
            return
        for wname in ('put_wall', 'call_wall', 'gamma_flip'):
            w_strike = walls.get(wname, 0.0)
            if not w_strike:
                continue
            if abs(strike - w_strike) < 0.01:
                key = (ticker, wname, side)
                _pulls_at_wall[key].append({
                    'ts': ts, 'exch': (exch or '').upper(),
                })
                break


# ── Signal computation ──────────────────────────────────────────────────────

def _recent(evs: deque, now: float, lookback: float) -> list:
    cutoff = now - lookback
    return [e for e in evs if e.get('ts', 0) >= cutoff]


def _recent_crosses(ticker: str, now: float, lookback: float) -> list:
    hist = list(_cross_history.get(ticker, deque()))
    cutoff = now - lookback
    return [c for c in hist if c['ts'] >= cutoff]


def compute_signals(ticker: str,
                    proximity_pct: float = DEFAULT_PROXIMITY_PCT,
                    lookback_sec: float = DEFAULT_LOOKBACK_SEC) -> dict:
    """Compute continuation + fade scores for all known walls on `ticker`.

    Returns:
      {
        'ticker', 'spot', 'spot_ts',
        'walls': [
          {
            'name': 'call_wall', 'strike': 660.0,
            'distance_pct': 0.0012,
            'just_crossed': True, 'cross_direction': 'up', 'cross_age_sec': 4.2,
            'prints_at_strike': 12, 'prorata_prints': 3, 'prorata_ratio': 0.25,
            'venues_pulled': 0, 'multi_venue_pull': False,
            'continuation_score': 0.82,   # 0..1
            'fade_score': 0.0,
            'verdict': 'continuation',    # 'continuation' | 'fade' | null
          }, ...
        ],
      }

    Scores are continuous (0..1); the pane renders raw values so the operator
    picks their own conviction threshold.
    """
    now = time.time()
    with _state_lock:
        walls = dict(_walls.get(ticker, {}))
        spot_rec = dict(_spot.get(ticker, {}))
        crosses = _recent_crosses(ticker, now, lookback_sec)
        # Snapshot all (side, wall) buffers we'll read below.
        snapshot = {}
        for key in list(_prints_at_wall.keys()) + list(_pulls_at_wall.keys()):
            if key[0] != ticker:
                continue
            snapshot[key] = {
                'prints': list(_prints_at_wall.get(key, deque())),
                'pulls':  list(_pulls_at_wall.get(key, deque())),
            }

    spot = spot_rec.get('price') or 0.0
    # ── Regime from authoritative hp_gamma sign at spot (Phase 10D) ─────────
    # Old approach (spot-vs-gamma_flip) fails for chains with multiple
    # dealer-net sign crossings — the gamma_flip value is just the FIRST
    # crossing going up from low strikes, often a deep-ITM-put artifact.
    # Authoritative regime is the SIGN of hp_gamma_shares_1pct, which is
    # derived from the entire chain's signed dn_gamma sum at spot.
    #
    #   hp_gamma_shares_1pct > 0 → dealers BUY shares on +1% rise
    #                              = SHORT-γ at spot (trending bias)
    #   hp_gamma_shares_1pct < 0 → dealers SELL shares on +1% rise
    #                              = LONG-γ at spot (mean-revert bias)
    #
    # Falls back to spot-vs-flip only when hp_gamma_shares_1pct is missing
    # (e.g. wall_signals.update_walls called without the new parameter).
    gamma_flip_val = walls.get('gamma_flip', 0.0)
    hp_gamma = walls.get('hp_gamma_shares_1pct', 0.0) or 0.0
    if abs(hp_gamma) > 1.0:
        # Authoritative path
        if hp_gamma > 0:
            regime = 'short_gamma'
            regime_sign = -1
        else:
            regime = 'long_gamma'
            regime_sign = +1
    elif gamma_flip_val and spot:
        # Legacy fallback (preserves old behaviour)
        if spot > gamma_flip_val:
            regime = 'long_gamma'
            regime_sign = +1
        else:
            regime = 'short_gamma'
            regime_sign = -1
    else:
        regime = 'unknown'
        regime_sign = 0
    peak = walls.get('dealer_net_peak', 0.0) or 1.0  # avoid /0
    out = {
        'ticker': ticker,
        'spot': spot,
        'spot_ts': spot_rec.get('ts', 0),
        'walls': [],
        'proximity_pct': proximity_pct,
        'lookback_sec': lookback_sec,
        'gamma_flip': gamma_flip_val,
        'regime': regime,
        'regime_sign': regime_sign,
        'dealer_net_peak': peak,
    }
    if not spot:
        return out

    # Map each wall name to the dealer_net scalar AT that wall's strike.
    # Note: gamma_flip itself is the zero-crossing so dealer_net_at_flip ≈ 0
    # by construction — we don't track it separately.
    dealer_net_map = {
        'call_wall':  walls.get('dealer_net_at_call', 0.0),
        'put_wall':   walls.get('dealer_net_at_put',  0.0),
        'gamma_flip': 0.0,   # by definition the zero-crossing
    }

    for wname in ('call_wall', 'put_wall', 'gamma_flip'):
        w_strike = walls.get(wname, 0.0)
        if not w_strike:
            continue
        distance_pct = abs(spot - w_strike) / w_strike
        # Matching side for wall: call_wall → C (breakout up), put_wall → P.
        # gamma_flip is sided by spot — above flip, call side; below, put side.
        if wname == 'call_wall':
            side = 'C'
        elif wname == 'put_wall':
            side = 'P'
        else:
            side = 'C' if spot >= w_strike else 'P'

        # Collect recent events at this wall strike + side.
        key = (ticker, wname, side)
        b = snapshot.get(key, {'prints': [], 'pulls': []})
        recent_prints = _recent(deque(b['prints']), now, lookback_sec)
        recent_pulls  = _recent(deque(b['pulls']),  now, lookback_sec)

        prorata_prints = [p for p in recent_prints
                          if p['exch'] in PRORATA_VENUES]
        auction_prints = [p for p in recent_prints
                          if p['exch'] in AUCTION_VENUES]
        total_prints = len(recent_prints)
        prorata_ratio = (len(prorata_prints) / total_prints) if total_prints else 0.0

        distinct_pulling_venues = {p['exch'] for p in recent_pulls if p['exch']}
        venues_pulled = len(distinct_pulling_venues)
        multi_venue_pull = venues_pulled >= MULTI_VENUE_MIN

        # Cross detection: did spot cross this wall inside the lookback?
        my_crosses = [c for c in crosses if c['wall'] == wname]
        just_crossed = bool(my_crosses)
        cross_direction = my_crosses[-1]['direction'] if my_crosses else None
        cross_age_sec = (now - my_crosses[-1]['ts']) if my_crosses else None

        # ── CONTINUATION score ──
        # Fires when (just crossed) AND (pro-rata prints showed up).
        # Score factors:
        #   crossed_factor = 1 if just_crossed in window, decaying linearly
        #                    over the lookback window
        #   prorata_factor = prorata_ratio  (fraction of strike prints that are
        #                    verified institutional)
        #   size_factor    = saturating count of pro-rata prints (more prints
        #                    = more conviction, diminishing returns)
        if just_crossed and cross_age_sec is not None:
            crossed_factor = max(0.0, 1.0 - cross_age_sec / lookback_sec)
        else:
            crossed_factor = 0.0
        size_factor = 1.0 - (1.0 / (1.0 + len(prorata_prints)))  # 0..1, saturates
        continuation_score = crossed_factor * prorata_ratio * size_factor

        # ── FADE score ──
        # Fires when (near wall but not crossed) AND (no multi-venue pull).
        # Score factors:
        #   near_factor = 1 when spot exactly at wall, 0 at/beyond proximity edge
        #   uncrossed_factor = 1 if not crossed in lookback, 0 if crossed
        #   no_pull_factor = 1 if zero venues pulled, drops as venues pull
        if distance_pct <= proximity_pct:
            near_factor = 1.0 - (distance_pct / proximity_pct)
        else:
            near_factor = 0.0
        uncrossed_factor = 0.0 if just_crossed else 1.0
        no_pull_factor = 1.0 - min(1.0, venues_pulled / MULTI_VENUE_MIN)
        fade_score = near_factor * uncrossed_factor * no_pull_factor

        # Verdict (null-unless-strong; threshold is CONFIGURED in the pane).
        if continuation_score >= 0.4:
            verdict = 'continuation'
        elif fade_score >= 0.5:
            verdict = 'fade'
        else:
            verdict = None

        # ── Signed-gamma interpretation per wall ──────────────────────────
        # dealer_net at this strike tells us which side of hedging dealers
        # face if spot crosses here.
        #   dn < 0 (short gamma at strike)  → hedge WITH the move. Cross-up
        #       → dealers BUY underlying → NQ should CONTINUE up.
        #   dn > 0 (long gamma at strike)   → hedge AGAINST the move. Cross-up
        #       → dealers SELL underlying → NQ should FADE down.
        # We emit expected_direction so downstream (ledger) can classify hits
        # against the sign-predicted direction rather than raw cross direction.
        dn = float(dealer_net_map.get(wname, 0.0) or 0.0)
        if peak > 0:
            dn_normalized = max(-1.0, min(1.0, dn / peak))   # -1..+1
        else:
            dn_normalized = 0.0
        if cross_direction == 'up':
            expected_direction = 'up'   if dn < 0 else ('down' if dn > 0 else None)
        elif cross_direction == 'down':
            expected_direction = 'down' if dn < 0 else ('up'   if dn > 0 else None)
        else:
            expected_direction = None
        # Regime consistency flag: is this wall in the same regime side as spot?
        # (wall at strike > flip is "long gamma" strike regardless of spot —
        # its expected_direction uses dn sign, not regime. But it's useful
        # context to surface whether this wall sits above or below the flip.)
        strike_side = 'above_flip' if (gamma_flip_val and w_strike > gamma_flip_val) else 'below_flip'

        out['walls'].append({
            'name': wname,
            'strike': w_strike,
            'side': side,
            'distance_pct': distance_pct,
            'just_crossed': just_crossed,
            'cross_direction': cross_direction,
            'cross_age_sec': cross_age_sec,
            'prints_at_strike': total_prints,
            'prorata_prints': len(prorata_prints),
            'auction_prints': len(auction_prints),
            'prorata_ratio': prorata_ratio,
            'venues_pulled': venues_pulled,
            'venues_pulled_list': sorted(distinct_pulling_venues),
            'multi_venue_pull': multi_venue_pull,
            'continuation_score': round(continuation_score, 3),
            'fade_score': round(fade_score, 3),
            'verdict': verdict,
            # ── NEW: signed-gamma context ──
            'dealer_net_at_strike': dn,
            'dealer_net_normalized': round(dn_normalized, 3),
            'expected_direction': expected_direction,
            'strike_side': strike_side,
        })

    return out


# ── Socket emit + flush ─────────────────────────────────────────────────────

def flush_to_socket(sio) -> int:
    """Called from the schwab_bridge _flush_loop. Emits `wall_signals_update`
    for each ticker with walls known. Gated at EMIT_INTERVAL_SEC.

    Side effect: on every flush, feeds the signal_ledger with any wall
    crossings it sees (cooldown-gated inside the ledger). This closes the
    loop between "score fires" and "did NQ move" — see connectors/signal_ledger.py.
    """
    global _last_emit_ts
    now = time.time()
    if (now - _last_emit_ts) < EMIT_INTERVAL_SEC:
        return 0
    _last_emit_ts = now
    count = 0
    with _state_lock:
        tickers = list(_walls.keys())

    # Lazy import — signal_ledger is optional; if it's not present or throws,
    # wall_signals still emits. Mirrors the wall_signals optional-import pattern
    # used in mm_attribution.py.
    try:
        from connectors import signal_ledger as _slg
    except Exception:
        _slg = None

    for ticker in tickers:
        try:
            state = compute_signals(ticker)
            if not state or not state.get('walls'):
                continue
            sio.emit('wall_signals_update', state)
            count += 1

            # ── Ledger hook: record crossings + maintain arm state ──
            if _slg is not None:
                for w in state['walls']:
                    # Re-arm check on every wall, every flush. Cheap.
                    try:
                        _slg.update_arm_state(
                            ticker=ticker,
                            wall=w['name'],
                            distance_pct=float(w.get('distance_pct') or 0),
                            proximity_pct=float(state.get('proximity_pct') or DEFAULT_PROXIMITY_PCT),
                        )
                    except Exception:
                        pass
                    # Fire-condition: wall just crossed (wall_signals already
                    # computed the boolean from its own cross_history). Write
                    # a ledger entry; signal_ledger handles cooldown internally.
                    if w.get('just_crossed'):
                        try:
                            _slg.record_crossing(
                                ticker=ticker,
                                wall=w['name'],
                                strike=float(w.get('strike') or 0),
                                direction=w.get('cross_direction') or 'up',
                                C=float(w.get('continuation_score') or 0),
                                F=float(w.get('fade_score') or 0),
                                qqq_spot=float(state.get('spot') or 0),
                                prorata_ratio=float(w.get('prorata_ratio') or 0),
                                prints_at_strike=int(w.get('prints_at_strike') or 0),
                                venues_pulled=int(w.get('venues_pulled') or 0),
                                proximity_pct=float(state.get('proximity_pct') or DEFAULT_PROXIMITY_PCT),
                                # ── Signed-gamma context at fire time ──
                                regime=state.get('regime') or 'unknown',
                                dealer_net_at_strike=float(w.get('dealer_net_at_strike') or 0),
                                dealer_net_normalized=float(w.get('dealer_net_normalized') or 0),
                                expected_direction=w.get('expected_direction'),
                                strike_side=w.get('strike_side'),
                                gamma_flip=float(state.get('gamma_flip') or 0),
                                now_ts=now,
                            )
                        except Exception as e:
                            log.debug(f'[wall_signals] ledger record_crossing failed: {e}')
        except Exception:
            pass
    return count


def get_state(ticker: str = 'QQQ',
              proximity_pct: Optional[float] = None,
              lookback_sec: Optional[float] = None) -> dict:
    """REST payload. Exposes the same compute_signals() output with caller-
    chosen proximity / lookback (user dropdowns)."""
    return compute_signals(
        ticker,
        proximity_pct=proximity_pct if proximity_pct is not None else DEFAULT_PROXIMITY_PCT,
        lookback_sec=lookback_sec if lookback_sec is not None else DEFAULT_LOOKBACK_SEC,
    )
