"""
Order Flow Classifier — Probabilistic Institutional vs Retail Detection

Uses Level 2 book data from the Schwab WebSocket streamer to classify
order flow as likely institutional or retail based on:

1. Size Analysis — odd lots vs round lots
2. Multi-Exchange Resting — institutional algo splitting across venues
3. Book Pressure — which side is getting absorbed
4. Sweep Detection — aggressive multilevel hitting
5. Quote Stuffing — rapid add/cancel patterns

Outputs a flow score from 0 (retail) to 100 (institutional) per symbol.

Usage:
    from schwab_streamer import SchwabStreamer
    from flow_classifier import FlowClassifier

    streamer = SchwabStreamer(auth)
    classifier = FlowClassifier(streamer)
    classifier.start()  # Attaches to streamer callbacks

    # Get current flow analysis
    report = classifier.get_report('QQQ')
"""

import time
import threading
from datetime import datetime
from collections import defaultdict, deque


# ═══════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════

# Size thresholds
RETAIL_MAX_SIZE = 10          # <= 10 contracts likely retail
INSTITUTIONAL_MIN_SIZE = 50   # >= 50 contracts likely institutional
BLOCK_SIZE = 200              # >= 200 contracts = block trade

# Institutional routing preferences (options exchanges)
INSTITUTIONAL_VENUES = {'CBOE', 'PHLX', 'MIAX', 'ISEX', 'AMEX', 'NYSE'}
RETAIL_PFOF_VENUES = {'EDGX', 'NSDQ', 'MEMX'}  # Common PFOF destinations

# Rolling window for analysis
HISTORY_WINDOW = 300  # 5 minutes of history


class FlowClassifier:
    """
    Real-time probabilistic order flow classifier.
    Attaches to SchwabStreamer and classifies L2 updates as institutional vs retail.
    """

    def __init__(self, streamer=None):
        """
        Args:
            streamer: SchwabStreamer instance (optional, can attach later)
        """
        self.streamer = streamer

        # Per-symbol tracking
        self._data = defaultdict(lambda: {
            # Book snapshots for diff analysis
            'prev_book': None,
            'curr_book': None,

            # Size distribution tracking
            'size_history': deque(maxlen=500),     # (timestamp, side, size, venue, price)
            'book_snapshots': deque(maxlen=100),

            # Sweep detection
            'sweep_events': deque(maxlen=50),

            # Aggregated scores
            'size_score': 50,          # 0=retail, 100=institutional
            'venue_score': 50,
            'pressure_score': 50,      # 0=selling pressure, 100=buying pressure
            'sweep_score': 50,
            'overall_score': 50,

            # Counters
            'total_updates': 0,
            'institutional_events': 0,
            'retail_events': 0,
            'sweep_detections': 0,

            # Volume tracking
            'bid_volume': 0,
            'ask_volume': 0,
            'net_delta': 0,  # positive = buying, negative = selling
        })

        self._lock = threading.Lock()

    def start(self):
        """Attach to streamer and start classifying."""
        if self.streamer:
            # Listen to all book services
            self.streamer.on('NASDAQ_BOOK', self._on_book_update)
            self.streamer.on('NYSE_BOOK', self._on_book_update)
            self.streamer.on('OPTIONS_BOOK', self._on_book_update)
            print("[FLOW] ✅ Flow classifier attached to streamer")

    def attach(self, streamer):
        """Attach to a streamer instance."""
        self.streamer = streamer
        self.start()

    # ─── CORE ANALYSIS ──────────────────────────────────

    def _on_book_update(self, book_data):
        """Process incoming Level 2 book update."""
        symbol = book_data.get('symbol', 'UNKNOWN')
        now = time.time()

        with self._lock:
            d = self._data[symbol]
            d['total_updates'] += 1

            # Save previous book for diff
            d['prev_book'] = d['curr_book']
            d['curr_book'] = {
                'timestamp': now,
                'bids': book_data.get('bids', []),
                'asks': book_data.get('asks', []),
            }
            d['book_snapshots'].append(d['curr_book'])

            # Run all analysis
            self._analyze_size_distribution(symbol, book_data, now)
            self._analyze_venue_routing(symbol, book_data, now)
            self._analyze_book_pressure(symbol, book_data, now)
            self._detect_sweeps(symbol, now)
            self._compute_overall_score(symbol)

    def _analyze_size_distribution(self, symbol, book_data, now):
        """Classify orders by size — small = retail, large = institutional."""
        d = self._data[symbol]
        retail_count = 0
        institutional_count = 0
        total_size_weighted = 0
        total_weight = 0

        for side_name, levels in [('bid', book_data.get('bids', [])),
                                   ('ask', book_data.get('asks', []))]:
            for level in levels:
                size = level.get('size', 0)
                price = level.get('price', 0)
                mm_count = level.get('mm_count', 0)

                # Per-market-maker analysis
                for mm in level.get('market_makers', []):
                    mm_size = mm.get('size', 0)
                    mm_id = mm.get('id', '')

                    # Record individual MM order
                    d['size_history'].append((now, side_name, mm_size, mm_id, price))

                    if mm_size <= RETAIL_MAX_SIZE:
                        retail_count += 1
                    elif mm_size >= INSTITUTIONAL_MIN_SIZE:
                        institutional_count += 1

                    # Size-weighted score
                    if mm_size >= BLOCK_SIZE:
                        total_size_weighted += 100 * mm_size
                    elif mm_size >= INSTITUTIONAL_MIN_SIZE:
                        total_size_weighted += 80 * mm_size
                    elif mm_size >= 20:
                        total_size_weighted += 50 * mm_size
                    else:
                        total_size_weighted += 10 * mm_size
                    total_weight += mm_size

        if total_weight > 0:
            raw_score = total_size_weighted / total_weight
            # EMA smoothing
            d['size_score'] = 0.3 * raw_score + 0.7 * d['size_score']

        d['retail_events'] += retail_count
        d['institutional_events'] += institutional_count

    def _analyze_venue_routing(self, symbol, book_data, now):
        """
        Analyze which exchanges have the most size.
        Institutional flow tends to route to CBOE, PHLX, MIAX.
        Retail PFOF tends to route to EDGX, NSDQ, MEMX.
        """
        d = self._data[symbol]
        institutional_volume = 0
        retail_volume = 0
        total_volume = 0

        for levels in [book_data.get('bids', []), book_data.get('asks', [])]:
            for level in levels:
                for mm in level.get('market_makers', []):
                    mm_id = mm.get('id', '').upper()
                    mm_size = mm.get('size', 0)
                    total_volume += mm_size

                    if mm_id in INSTITUTIONAL_VENUES:
                        institutional_volume += mm_size
                    elif mm_id in RETAIL_PFOF_VENUES:
                        retail_volume += mm_size

        if total_volume > 0:
            inst_pct = institutional_volume / total_volume
            raw_score = inst_pct * 100
            d['venue_score'] = 0.3 * raw_score + 0.7 * d['venue_score']

    def _analyze_book_pressure(self, symbol, book_data, now):
        """
        Analyze bid vs ask pressure.
        More size on bids = buying pressure, more on asks = selling pressure.
        """
        d = self._data[symbol]
        total_bid_size = sum(l.get('size', 0) for l in book_data.get('bids', []))
        total_ask_size = sum(l.get('size', 0) for l in book_data.get('asks', []))

        d['bid_volume'] = total_bid_size
        d['ask_volume'] = total_ask_size

        total = total_bid_size + total_ask_size
        if total > 0:
            bid_pct = total_bid_size / total * 100
            d['pressure_score'] = 0.3 * bid_pct + 0.7 * d['pressure_score']
            d['net_delta'] = total_bid_size - total_ask_size

    def _detect_sweeps(self, symbol, now):
        """
        Detect sweep behavior — multiple levels getting hit simultaneously.
        Sweeps = aggressive institutional execution.
        """
        d = self._data[symbol]
        snapshots = d['book_snapshots']
        if len(snapshots) < 2:
            return

        prev = snapshots[-2]
        curr = snapshots[-1]

        # Check if multiple ask levels disappeared (buy sweep)
        prev_ask_levels = len(prev.get('asks', []))
        curr_ask_levels = len(curr.get('asks', []))

        prev_bid_levels = len(prev.get('bids', []))
        curr_bid_levels = len(curr.get('bids', []))

        # Sweep = 2+ levels disappearing in one tick
        if prev_ask_levels - curr_ask_levels >= 2:
            d['sweep_events'].append(('buy_sweep', now, prev_ask_levels - curr_ask_levels))
            d['sweep_detections'] += 1
        elif prev_bid_levels - curr_bid_levels >= 2:
            d['sweep_events'].append(('sell_sweep', now, prev_bid_levels - curr_bid_levels))
            d['sweep_detections'] += 1

        # Score
        recent_sweeps = sum(1 for _, t, _ in d['sweep_events'] if now - t < 60)
        d['sweep_score'] = min(100, 50 + recent_sweeps * 15)

    def _compute_overall_score(self, symbol):
        """
        Compute weighted overall institutional score.
        0 = definitely retail, 100 = definitely institutional.
        """
        d = self._data[symbol]

        # Weighted combination
        d['overall_score'] = (
            d['size_score'] * 0.50 +           # Size is strongest signal
            d['venue_score'] * 0.25 +           # Venue routing
            d['sweep_score'] * 0.25 +           # Sweep activity
            d['pressure_score'] * 0.10 * 0      # Pressure is directional, not inst/retail
        ) / (0.50 + 0.25 + 0.25)

    # ─── PUBLIC API ─────────────────────────────────────

    def get_report(self, symbol):
        """
        Get current flow classification report for a symbol.

        Returns dict with:
            overall_score: 0-100 (0=retail, 100=institutional)
            classification: 'RETAIL', 'MIXED', 'INSTITUTIONAL'
            size_score, venue_score, sweep_score, pressure_score
            bid_volume, ask_volume, net_delta
            institutional_events, retail_events
            sweep_detections
        """
        with self._lock:
            d = self._data[symbol]

            score = d['overall_score']
            if score >= 70:
                classification = 'INSTITUTIONAL'
            elif score <= 30:
                classification = 'RETAIL'
            else:
                classification = 'MIXED'

            return {
                'symbol': symbol,
                'overall_score': round(score, 1),
                'classification': classification,
                'size_score': round(d['size_score'], 1),
                'venue_score': round(d['venue_score'], 1),
                'sweep_score': round(d['sweep_score'], 1),
                'pressure_score': round(d['pressure_score'], 1),
                'pressure_direction': 'BUYING' if d['pressure_score'] > 55 else ('SELLING' if d['pressure_score'] < 45 else 'NEUTRAL'),
                'bid_volume': d['bid_volume'],
                'ask_volume': d['ask_volume'],
                'net_delta': d['net_delta'],
                'total_updates': d['total_updates'],
                'institutional_events': d['institutional_events'],
                'retail_events': d['retail_events'],
                'sweep_detections': d['sweep_detections'],
                'recent_sweeps': [
                    {'type': t, 'time': datetime.fromtimestamp(ts).strftime('%H:%M:%S'), 'levels': lvl}
                    for t, ts, lvl in d['sweep_events']
                ][-5:],  # last 5 sweeps
            }

    def get_all_reports(self):
        """Get reports for all tracked symbols."""
        with self._lock:
            symbols = list(self._data.keys())
        return {sym: self.get_report(sym) for sym in symbols}

    def get_top_institutional(self, n=5):
        """Get symbols with highest institutional flow score."""
        reports = self.get_all_reports()
        sorted_reports = sorted(reports.values(), key=lambda x: x['overall_score'], reverse=True)
        return sorted_reports[:n]


# ═══════════════════════════════════════════════════════════
#  PRETTY PRINTING
# ═══════════════════════════════════════════════════════════

def format_flow_report(report):
    """Format a flow report for terminal display."""
    if not report:
        return "No data"

    sym = report['symbol']
    score = report['overall_score']
    classification = report['classification']

    # Color coding via emoji
    if classification == 'INSTITUTIONAL':
        indicator = '🏦'
        bar_char = '█'
    elif classification == 'RETAIL':
        indicator = '🧑'
        bar_char = '░'
    else:
        indicator = '⚖️'
        bar_char = '▒'

    # Score bar
    filled = int(score / 5)
    bar = bar_char * filled + '░' * (20 - filled)

    lines = []
    lines.append(f"\n{'─' * 60}")
    lines.append(f"  {indicator} {sym} — {classification} ({score:.0f}/100)")
    lines.append(f"  [{bar}]")
    lines.append(f"{'─' * 60}")

    lines.append(f"  Component Scores:")
    lines.append(f"    Size:     {_score_bar(report['size_score'])}  {report['size_score']:.0f}")
    lines.append(f"    Venue:    {_score_bar(report['venue_score'])}  {report['venue_score']:.0f}")
    lines.append(f"    Sweep:    {_score_bar(report['sweep_score'])}  {report['sweep_score']:.0f}")

    lines.append(f"\n  Book Pressure: {report['pressure_direction']}")
    lines.append(f"    Bid vol: {report['bid_volume']:,} | Ask vol: {report['ask_volume']:,} | Net: {report['net_delta']:+,}")

    lines.append(f"\n  Event Counts ({report['total_updates']} updates):")
    lines.append(f"    Institutional-size events: {report['institutional_events']:,}")
    lines.append(f"    Retail-size events:        {report['retail_events']:,}")
    lines.append(f"    Sweep detections:          {report['sweep_detections']}")

    if report.get('recent_sweeps'):
        lines.append(f"\n  Recent Sweeps:")
        for s in report['recent_sweeps']:
            lines.append(f"    {s['time']} — {s['type']} ({s['levels']} levels)")

    return '\n'.join(lines)


def _score_bar(score, width=15):
    """Generate an inline score bar."""
    filled = int(score / 100 * width)
    return '█' * filled + '░' * (width - filled)


def format_flow_summary(reports):
    """Format a multi-symbol flow summary table."""
    lines = []
    lines.append(f"\n{'═' * 70}")
    lines.append(f"  ORDER FLOW CLASSIFICATION SUMMARY")
    lines.append(f"  {datetime.now().strftime('%H:%M:%S ET')}")
    lines.append(f"{'═' * 70}")
    lines.append(f"  {'Symbol':<12s} {'Score':>5s}  {'Class':<15s} {'Size':>5s} {'Venue':>5s} {'Sweep':>5s} {'Pressure':<8s}")
    lines.append(f"  {'─' * 65}")

    for r in sorted(reports.values(), key=lambda x: x['overall_score'], reverse=True):
        sym = r['symbol'][:11]
        lines.append(
            f"  {sym:<12s} {r['overall_score']:>5.0f}  {r['classification']:<15s} "
            f"{r['size_score']:>5.0f} {r['venue_score']:>5.0f} "
            f"{r['sweep_score']:>5.0f} "
            f"{r['pressure_direction']:<8s}"
        )

    lines.append(f"  {'─' * 65}")
    return '\n'.join(lines)
