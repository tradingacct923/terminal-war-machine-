#!/usr/bin/env python3 -u
"""
Replay Engine — Historical L2 Data → Iceberg Outcome Backtester

Feeds historical CSV (L1 trades + L2 book updates) through the LIVE
l2_worker.py detection pipeline. Zero reimplementation — uses the
exact same code path as production.

Output: logs/replay_outcomes.jsonl  (same schema as iceberg_outcomes.jsonl)

Usage:
    python logs/replay_engine.py                      # all sessions
    python logs/replay_engine.py 20251218             # single session
    python logs/replay_engine.py 20251218 20251219    # specific sessions
"""

import sys, os, time, json, logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque

# ── Project root ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [REPLAY] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("replay")

# ── Paths ──
L2_DIR = os.path.join(ROOT, "logs", "level2")
OUTPUT_PATH = os.path.join(ROOT, "logs", "replay_outcomes.jsonl")

# ══════════════════════════════════════════════════════════════════════════════
# 1. CSV PARSER — Decode the L1/L2 semicolon-delimited format
# ══════════════════════════════════════════════════════════════════════════════

def parse_datetime(date_str: str) -> float:
    """Convert YYYYMMDDHHmmss -> unix timestamp (ET timezone)."""
    try:
        dt = datetime.strptime(date_str, "%Y%m%d%H%M%S")
        # Data is in ET (Eastern Time)
        et = timezone(timedelta(hours=-5))
        dt = dt.replace(tzinfo=et)
        return dt.timestamp()
    except Exception:
        return 0.0


def is_rth(ts: float) -> bool:
    """Check if timestamp falls within Regular Trading Hours (9:30-16:00 ET)."""
    et = timezone(timedelta(hours=-5))
    dt = datetime.fromtimestamp(ts, tz=et)
    t = dt.hour * 100 + dt.minute
    return 930 <= t < 1600


def parse_csv_line(line: str):
    """Parse a single CSV line into a structured record.
    
    L1 (trade): L1;side;datetime;submillis;price;volume
      side: 0=buy aggressor, 1=sell aggressor, 2/3/5=other
    
    L2 (book):  L2;side;datetime;submillis;action;level;;price;size
      side: 0=ask, 1=bid
      action: 0=new, 1=update, 2=delete
      level: 0-10
    """
    parts = line.split(';')
    if len(parts) < 5:
        return None
    
    rtype = parts[0]
    date_str = parts[2]
    submillis = int(parts[3]) if parts[3].isdigit() else 0
    
    ts = parse_datetime(date_str)
    if ts == 0:
        return None
    
    # Add sub-second precision from submillis field
    # Scale so values stay within 0-1 second range
    ts += submillis / 10_000_000.0
    
    if rtype == 'L1':
        side_code = parts[1]
        price = float(parts[4]) if parts[4] else 0
        volume = int(parts[5]) if len(parts) > 5 and parts[5] else 1
        
        if side_code == '0':
            side = 'buy'    # ask lift = buy aggressor
        elif side_code == '1':
            side = 'sell'   # bid hit = sell aggressor
        else:
            return None     # skip non-standard sides (2, 3, 5)
        
        return {
            'type': 'L1',
            'ts': ts,
            'price': price,
            'volume': volume,
            'side': side,
        }
    
    elif rtype == 'L2':
        book_side = int(parts[1])  # 0=ask, 1=bid
        action = int(parts[4]) if parts[4].isdigit() else 0
        level = int(parts[5]) if parts[5].isdigit() else 0
        price = float(parts[7]) if len(parts) > 7 and parts[7] else 0
        size = int(parts[8]) if len(parts) > 8 and parts[8].isdigit() else 0
        
        return {
            'type': 'L2',
            'ts': ts,
            'book_side': book_side,  # 0=ask, 1=bid
            'action': action,        # 0=new, 1=update, 2=delete
            'level': level,
            'price': price,
            'size': size,
        }
    
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 2. DOM RECONSTRUCTOR — Build full DOM dict from incremental L2 updates
# ══════════════════════════════════════════════════════════════════════════════

class DOMReconstructor:
    """Maintains a running L2 book from incremental level updates."""
    
    def __init__(self):
        self.bids = {}  # {level: (price, size)}
        self.asks = {}  # {level: (price, size)}
        self._update_count = 0
    
    def update(self, rec: dict):
        """Apply an L2 update and return full DOM dict every N updates.
        
        Returns DOM dict suitable for l2_worker.on_dom_update(), or None.
        """
        side = rec['book_side']
        level = rec['level']
        action = rec['action']
        price = rec['price']
        size = rec['size']
        
        book = self.asks if side == 0 else self.bids
        
        if action == 2:  # delete
            book.pop(level, None)
        else:  # new or update
            if price > 0:
                book[level] = (price, size)
        
        self._update_count += 1
        
        # Emit a DOM snapshot every 50 L2 updates (balance accuracy vs speed)
        if self._update_count % 50 == 0:
            return self._build_dom()
        return None
    
    def _build_dom(self) -> dict:
        """Build full DOM dict matching l2_worker format."""
        bid_levels = sorted(self.bids.items(), key=lambda x: x[1][0], reverse=True)
        ask_levels = sorted(self.asks.items(), key=lambda x: x[1][0])
        
        best_bid = bid_levels[0][1][0] if bid_levels else 0
        best_ask = ask_levels[0][1][0] if ask_levels else 0
        
        bids_dict = {}
        asks_dict = {}
        bid_total = 0
        ask_total = 0
        
        for _, (price, size) in bid_levels[:10]:
            if size > 0:
                bids_dict[str(price)] = size
                bid_total += size
        
        for _, (price, size) in ask_levels[:10]:
            if size > 0:
                asks_dict[str(price)] = size
                ask_total += size
        
        total = bid_total + ask_total
        imbalance = (bid_total - ask_total) / total if total > 0 else 0
        mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0
        spread = best_ask - best_bid if best_ask > 0 and best_bid > 0 else 0
        
        return {
            'bids': bids_dict,
            'asks': asks_dict,
            'best_bid': best_bid,
            'best_ask': best_ask,
            'bid_total': bid_total,
            'ask_total': ask_total,
            'imbalance': round(imbalance, 4),
            'mid_price': round(mid, 4),
            'spread': round(spread, 4),
        }
    
    def reset(self):
        self.bids.clear()
        self.asks.clear()
        self._update_count = 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. ENGINE STATE MANAGER — Reset l2_worker globals between sessions
# ══════════════════════════════════════════════════════════════════════════════

def reset_engine_state():
    """Reset ALL detection state in l2_worker for a fresh session replay."""
    import background_engine.l2_worker as w
    
    # Iceberg detection state
    w._ICE_TRACKER.clear()
    w._ICE_TRACKER_VTAG.clear()
    w._ICE_PRICE_HISTORY.clear()
    w._ICE_LEVEL_MEMORY.clear()
    w._ICE_OUTCOMES.clear()
    w._ICE_PENDING.clear()
    w._ICE_WALL_STATE.clear()
    
    # Kalman / Volume Clock / VPIN
    w._KALMAN_CV.clear()
    w._VOLUME_CLOCKS.clear()
    try:
        from background_engine.l2_worker import VPINEngine
        w._VPIN_ENGINES = defaultdict(VPINEngine)
    except ImportError:
        w._VPIN_ENGINES = {}
    
    # Price / market stats
    w._PRICE_HISTORY.clear()
    w._MARKET_STATS.clear()
    w._STICKINESS_DIST.clear()
    
    # Candle engine
    w._CANDLES.clear()
    w._CURRENT_CANDLE.clear()
    
    # L2 state
    w.L2_STATE = {
        'dom': {}, 'imbalance': {}, 'mid_prices': {},
        'trades': {}, 'quotes': {}, 'signals': {},
        'connected': True, 'last_update': 0,
    }
    
    # Trade tracking
    w._LAST_TRADE_TS.clear()
    
    # DOM history  
    try:
        w._DOM_HISTORY.clear()
        w._HEATMAP_TRADE_BUF.clear()
        w._DOM_SNAPSHOT_STATE.clear()
    except AttributeError:
        pass
    
    # Detection state (sweeps, ignition, spoof)
    try:
        w._SWEEP_STATE.clear()
        w._IGNITION_STATE.clear()
        w._SPOOF_STATE.clear()
    except AttributeError:
        pass
    
    # Regime
    w._CURRENT_REGIME = 'transition'
    
    # Ensure no socket emissions
    w._socketio = None
    w._detection_callback = None
    
    log.info("Engine state fully reset")


# ══════════════════════════════════════════════════════════════════════════════
# 4. OUTCOME INTERCEPTOR — Capture outcomes without touching live file
# ══════════════════════════════════════════════════════════════════════════════

_replay_outcomes = []
_replay_session = ""

def _intercept_persist(symbol, outcome):
    """Replace l2_worker._persist_iceberg_outcome to capture to our list."""
    outcome['_replay_session'] = _replay_session
    _replay_outcomes.append(outcome)
    
    side_label = 'SHORT' if outcome.get('side') == 'b' else 'LONG'  # bid iceberg=LONG, ask=SHORT
    result = outcome.get('outcome_30s', 0)
    price = outcome.get('price', 0)
    regime = outcome.get('regime', '?')
    cv = outcome.get('kalman_cv', 0)
    conf = outcome.get('confidence', '?')
    
    win_marker = '✅' if result > 0 else '❌'
    log.info(
        f"{win_marker} {symbol} {side_label} @ {price:.2f} → "
        f"{result:+.2f} pts | {regime} | cv={cv:.4f} | {conf}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. REPLAY LOOP — Process one session CSV
# ══════════════════════════════════════════════════════════════════════════════

def replay_session(csv_path: str, symbol: str = "NQ"):
    """Replay one CSV session through the detection pipeline."""
    global _replay_session
    
    import background_engine.l2_worker as w
    
    fname = os.path.basename(csv_path)
    date_str = fname.replace('.csv', '')
    _replay_session = date_str
    
    log.info(f"{'='*60}")
    log.info(f"REPLAYING: {fname} ({os.path.getsize(csv_path)/1e6:.0f} MB)")
    log.info(f"{'='*60}")
    
    # Reset all engine state
    reset_engine_state()
    
    # Disable expensive frameworks — we only need iceberg detection + outcome tracking
    # Skip _init_frameworks() entirely (Shannon, Ising, Reynolds, LPPL, etc.)
    # These add ~10x overhead and don't affect iceberg detection
    w._shannon = None
    w._ising = None
    w._reynolds = None
    w._lppl = None
    w._powerlaw = None
    w._transfer = None
    w._percolation = None
    w._mutual = None
    
    # Monkey-patch the persist function to intercept outcomes
    original_persist = w._persist_iceberg_outcome
    w._persist_iceberg_outcome = _intercept_persist
    
    dom_recon = DOMReconstructor()
    
    # Stats
    trades_fed = 0
    dom_updates = 0
    rth_trades = 0
    ice_detected = 0
    start_time = time.time()
    last_progress = 0
    
    try:
        with open(csv_path, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                
                rec = parse_csv_line(line)
                if rec is None:
                    continue
                
                ts = rec['ts']
                
                # Progress reporting every 2M lines
                if line_num - last_progress >= 2_000_000:
                    elapsed = time.time() - start_time
                    rate = line_num / elapsed if elapsed > 0 else 0
                    et = timezone(timedelta(hours=-5))
                    dt = datetime.fromtimestamp(ts, tz=et)
                    log.info(
                        f"  Line {line_num/1e6:.1f}M | "
                        f"{dt.strftime('%H:%M:%S')} ET | "
                        f"trades={rth_trades:,} | dom={dom_updates:,} | "
                        f"icebergs={ice_detected} | "
                        f"{rate/1e6:.2f}M lines/sec"
                    )
                    last_progress = line_num
                
                if rec['type'] == 'L1':
                    # Only process RTH trades for outcome measurement
                    if not is_rth(ts):
                        continue
                    
                    trade = {
                        'price': rec['price'],
                        'volume': rec['volume'],
                        'side': rec['side'],
                        'timestamp': ts,
                    }
                    
                    # Track iceberg count before calling on_trade
                    pending_before = len(w._ICE_PENDING.get(symbol, []))
                    
                    try:
                        w.on_trade(symbol, trade)
                    except Exception as e:
                        if 'ising' not in str(e).lower():
                            log.debug(f"on_trade error at line {line_num}: {e}")
                    
                    rth_trades += 1
                    trades_fed += 1
                    
                    # Check if iceberg detection fired
                    pending_after = len(w._ICE_PENDING.get(symbol, []))
                    if pending_after > pending_before:
                        ice_detected += (pending_after - pending_before)
                
                elif rec['type'] == 'L2':
                    # Always process L2 to maintain book state
                    dom = dom_recon.update(rec)
                    if dom is not None and dom.get('mid_price', 0) > 0:
                        try:
                            w.on_dom_update(symbol, dom)
                            dom_updates += 1
                        except Exception as e:
                            log.debug(f"on_dom_update error: {e}")
    
    except KeyboardInterrupt:
        log.warning("Interrupted!")
    finally:
        # Restore original persist function
        w._persist_iceberg_outcome = original_persist
    
    elapsed = time.time() - start_time
    outcomes_this_session = [o for o in _replay_outcomes if o.get('_replay_session') == date_str]
    
    log.info(f"")
    log.info(f"SESSION COMPLETE: {date_str}")
    log.info(f"  RTH Trades Fed:    {rth_trades:>10,}")
    log.info(f"  DOM Updates:       {dom_updates:>10,}")
    log.info(f"  Icebergs Detected: {ice_detected:>10}")
    log.info(f"  Outcomes Logged:   {len(outcomes_this_session):>10}")
    log.info(f"  Time Elapsed:      {elapsed:>10.1f}s")
    log.info(f"  Speed:             {(trades_fed + dom_updates) / elapsed / 1e6:.2f}M events/sec")
    
    if outcomes_this_session:
        wins = sum(1 for o in outcomes_this_session 
                   if o.get('outcome_30s', 0) > 0 and o.get('confidence') == 'high')
        total = sum(1 for o in outcomes_this_session if o.get('confidence') == 'high')
        pnl = sum(o.get('outcome_30s', 0) for o in outcomes_this_session 
                  if o.get('confidence') == 'high')
        wr = wins / max(total, 1) * 100
        log.info(f"  High-Conf WR:      {wr:.1f}% ({wins}/{total})")
        log.info(f"  High-Conf PnL:     {pnl:+.2f} pts")


# ══════════════════════════════════════════════════════════════════════════════
# 6. MAIN — Run backtest across sessions
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # Get session list
    csv_files = sorted([
        f for f in os.listdir(L2_DIR)
        if f.endswith('.csv')
    ])
    
    # Filter by command line args
    if len(sys.argv) > 1:
        requested = set(sys.argv[1:])
        csv_files = [f for f in csv_files if f.replace('.csv', '') in requested]
    
    # Skip holidays / tiny sessions (< 100 MB)
    MIN_SIZE = 100 * 1e6  # 100 MB
    csv_files = [
        f for f in csv_files
        if os.path.getsize(os.path.join(L2_DIR, f)) >= MIN_SIZE
    ]
    
    log.info(f"Replay Engine v1.0")
    log.info(f"Sessions to process: {len(csv_files)}")
    log.info(f"Output: {OUTPUT_PATH}")
    log.info(f"")
    
    for csv_file in csv_files:
        csv_path = os.path.join(L2_DIR, csv_file)
        replay_session(csv_path, symbol="NQ")
    
    # Write all outcomes to JSONL
    log.info(f"\n{'='*60}")
    log.info(f"WRITING {len(_replay_outcomes)} outcomes to {OUTPUT_PATH}")
    
    with open(OUTPUT_PATH, 'w') as f:
        for outcome in _replay_outcomes:
            f.write(json.dumps(outcome, default=str) + '\n')
    
    # Final summary
    high_conf = [o for o in _replay_outcomes if o.get('confidence') == 'high']
    total = len(high_conf)
    wins = sum(1 for o in high_conf if o.get('outcome_30s', 0) > 0)
    pnl = sum(o.get('outcome_30s', 0) for o in high_conf)
    
    log.info(f"\n{'='*60}")
    log.info(f"BACKTEST COMPLETE")
    log.info(f"{'='*60}")
    log.info(f"Sessions:        {len(csv_files)}")
    log.info(f"Total Outcomes:  {len(_replay_outcomes)}")
    log.info(f"High-Conf:       {total}")
    log.info(f"Win Rate:        {wins/max(total,1)*100:.1f}%")
    log.info(f"Net PnL:         {pnl:+.2f} pts")
    log.info(f"Profit Factor:   {sum(o.get('outcome_30s',0) for o in high_conf if o.get('outcome_30s',0)>0)/max(abs(sum(o.get('outcome_30s',0) for o in high_conf if o.get('outcome_30s',0)<0)),0.01):.2f}")
    
    # Per-session breakdown
    log.info(f"\nPer-Session Breakdown:")
    sessions = sorted(set(o.get('_replay_session', '?') for o in high_conf))
    for sess in sessions:
        so = [o for o in high_conf if o.get('_replay_session') == sess]
        sw = sum(1 for o in so if o.get('outcome_30s', 0) > 0)
        sp = sum(o.get('outcome_30s', 0) for o in so)
        swr = sw / max(len(so), 1) * 100
        log.info(f"  {sess}: {len(so):4d} trades | WR={swr:5.1f}% | PnL={sp:+8.1f}")


if __name__ == "__main__":
    main()
