#!/bin/bash
# Generate the final investigation report at 09:00 EDT 2026-04-27.
# Aggregates all JSONL files in investigation/ into a markdown summary.
# Self-removes the launchd job at end (one-shot).

INVEST_DIR="/Users/kaali/Desktop/altaris-dev/investigation"
PLIST="$HOME/Library/LaunchAgents/com.kaali.investigation_report.plist"
OUT="${INVEST_DIR}/reports/final_report_$(date '+%Y%m%d_%H%M').md"

/usr/bin/python3 << 'PYEOF' > "${OUT}" 2>&1
import json, os, glob, time
from collections import Counter, defaultdict
from datetime import datetime

INVEST = "/Users/kaali/Desktop/altaris-dev/investigation"

print(f"# Volume Bubbles Investigation Report")
print(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}_\n")

def load_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: rows.append(json.loads(line))
                except: pass
    except FileNotFoundError:
        pass
    return rows

# ── Bar Signals ─────────────────────────────────────────────────────────
print("## 1. Bar Signal Detector Output\n")
bar_files = sorted(glob.glob(f"{INVEST}/bar_signals/*.jsonl"))
all_bar = []
for f in bar_files: all_bar.extend(load_jsonl(f))

print(f"**Total bar-signal computations**: {len(all_bar)}\n")
fired = [r for r in all_bar if r.get('phase') == 'fired']
no_sig = [r for r in all_bar if r.get('phase') == 'no_signal']
print(f"- Fired: **{len(fired)}**  ({100*len(fired)/max(len(all_bar),1):.1f}%)")
print(f"- No signal: {len(no_sig)}  ({100*len(no_sig)/max(len(all_bar),1):.1f}%)\n")

# Signal type breakdown (a bar can fire multiple types)
sig_types = Counter()
for r in fired:
    if r.get('absorption'): sig_types['absorption'] += len(r['absorption']) if isinstance(r['absorption'], list) else 1
    if r.get('exhaustion'): sig_types['exhaustion'] += 1
    if r.get('aggression'): sig_types['aggression'] += 1
print("### Signals fired by type")
for t, n in sig_types.most_common():
    print(f"- **{t}**: {n}")
print()

# Exhaustion side breakdown
exh_sides = Counter()
for r in fired:
    e = r.get('exhaustion')
    if e and isinstance(e, dict):
        exh_sides[e.get('side', 'unknown')] += 1
if exh_sides:
    print("### Exhaustion direction breakdown")
    for s, n in exh_sides.most_common():
        print(f"- {s}: {n}")
    print()

# Aggression side breakdown
agg_sides = Counter()
agg_strength = []
for r in fired:
    a = r.get('aggression')
    if a and isinstance(a, dict):
        agg_sides[a.get('side', 'unknown')] += 1
        agg_strength.append(a.get('strength', 0))
if agg_sides:
    print("### Aggression direction breakdown")
    for s, n in agg_sides.most_common():
        print(f"- {s}: {n}")
    if agg_strength:
        agg_strength.sort()
        print(f"- median strength: {agg_strength[len(agg_strength)//2]:.3f}")
        print(f"- max strength:    {max(agg_strength):.3f}")
    print()

# Absorption side breakdown
abs_sides = Counter()
abs_refill = Counter()
for r in fired:
    abs_list = r.get('absorption')
    if isinstance(abs_list, list):
        for a in abs_list:
            if isinstance(a, dict):
                abs_sides[a.get('side', 'unknown')] += 1
                abs_refill[a.get('refill_class', 'none')] += 1
if abs_sides:
    print("### Absorption breakdown")
    print("**By side**")
    for s, n in abs_sides.most_common():
        print(f"- {s}: {n}")
    print("**By refill class**")
    for s, n in abs_refill.most_common():
        print(f"- {s}: {n}")
    print()

# Bar volume distribution
bar_vols = [r.get('bar_total', 0) for r in all_bar]
if bar_vols:
    bar_vols.sort()
    print("### Bar volume distribution across all observed bars")
    print(f"- min: {bar_vols[0]} | p25: {bar_vols[len(bar_vols)//4]} | median: {bar_vols[len(bar_vols)//2]} | p75: {bar_vols[3*len(bar_vols)//4]} | p95: {bar_vols[int(len(bar_vols)*0.95)]} | max: {bar_vols[-1]}")
    print(f"- avg level count: {sum(r.get('level_count', 0) for r in all_bar)/max(len(all_bar),1):.1f}")
    print()

# ── Big Prints ──────────────────────────────────────────────────────────
print("## 2. Big-Print Events\n")
bp_files = sorted(glob.glob(f"{INVEST}/big_prints/*.jsonl"))
all_bp = []
for f in bp_files: all_bp.extend(load_jsonl(f))
print(f"**Total**: {len(all_bp)}\n")

bp_class = Counter(r.get('classification', '?') for r in all_bp)
bp_extreme = sum(1 for r in all_bp if r.get('extreme'))
bp_at_tier = Counter(r.get('at_level_tier') for r in all_bp if r.get('at_level_tier'))
bp_refill = Counter(r.get('refill_class') for r in all_bp if r.get('refill_class'))
print("### Classifications")
for c, n in bp_class.most_common():
    print(f"- {c}: {n}")
print(f"\n**Extreme (P99)**: {bp_extreme}  ({100*bp_extreme/max(len(all_bp),1):.1f}%)")
if bp_at_tier:
    print("\n### Big prints landing on defended levels")
    for t, n in bp_at_tier.most_common():
        print(f"- {t}: {n}")
if bp_refill:
    print("\n### Refill class at the trade's price level")
    for c, n in bp_refill.most_common():
        print(f"- {c}: {n}")

# Size distribution
bp_sizes = [r.get('size', 0) for r in all_bp]
if bp_sizes:
    bp_sizes.sort()
    print("\n### Big-print size distribution")
    print(f"- p25: {bp_sizes[len(bp_sizes)//4]} | median: {bp_sizes[len(bp_sizes)//2]} | p75: {bp_sizes[3*len(bp_sizes)//4]} | p95: {bp_sizes[int(len(bp_sizes)*0.95)]} | max: {bp_sizes[-1]}")
print()

# ── Adaptive Floors Evolution ───────────────────────────────────────────
print("## 3. Adaptive Floor Evolution\n")
flr_files = sorted(glob.glob(f"{INVEST}/floors_evolution/*.jsonl"))
all_flr = []
for f in flr_files: all_flr.extend(load_jsonl(f))
print(f"**Snapshots captured**: {len(all_flr)}\n")
if all_flr:
    first = all_flr[0]
    last = all_flr[-1]
    print("### First snapshot")
    print(f"- ts: {datetime.fromtimestamp(first['ts_ms']/1000)}")
    print(f"- level_floor: {first.get('level_floor', '?')}  bar_floor: {first.get('bar_floor', '?')}")
    print(f"- level samples: {first.get('level_samples_n', 0)}  bar samples: {first.get('bar_samples_n', 0)}")
    print(f"- median level vol: {first.get('level_median', '?')}  median bar vol: {first.get('bar_median', '?')}")
    print("\n### Last snapshot")
    print(f"- ts: {datetime.fromtimestamp(last['ts_ms']/1000)}")
    print(f"- level_floor: {last.get('level_floor', '?')}  bar_floor: {last.get('bar_floor', '?')}")
    print(f"- level samples: {last.get('level_samples_n', 0)}  bar samples: {last.get('bar_samples_n', 0)}")
    print(f"- median level vol: {last.get('level_median', '?')}  median bar vol: {last.get('bar_median', '?')}")

    # Detect regime shifts (large jumps in floors)
    print("\n### Floor evolution (sampled)")
    step = max(len(all_flr) // 12, 1)
    for i in range(0, len(all_flr), step):
        s = all_flr[i]
        ts_iso = datetime.fromtimestamp(s['ts_ms']/1000).strftime('%H:%M')
        print(f"- {ts_iso}: lf={s.get('level_floor', '?'):.2f}  bf={s.get('bar_floor', '?'):.2f}  lN={s.get('level_samples_n', 0)}  bN={s.get('bar_samples_n', 0)}")
print()

# ── Candle BP Stats ─────────────────────────────────────────────────────
print("## 4. Per-Bar BP Capture\n")
bp_files2 = sorted(glob.glob(f"{INVEST}/candle_bp/*.jsonl"))
total_bars = 0
total_ticks = 0
total_size = 0
for f in bp_files2:
    rows = load_jsonl(f)
    total_bars += len(rows)
    total_size += os.path.getsize(f) if os.path.exists(f) else 0
    for r in rows:
        bp = r.get('bp', {})
        total_ticks += sum(((e[0] or 0) + (e[1] or 0)) for e in bp.values() if isinstance(e, list) and len(e) >= 2)
print(f"- Bars captured: {total_bars}")
print(f"- Total raw volume across all captured bars: {total_ticks}")
print(f"- File size: {total_size/1024:.1f} KB")
print()

# ── Snapshots ───────────────────────────────────────────────────────────
print("## 5. Server State Snapshots\n")
snap_files = sorted(glob.glob(f"{INVEST}/snapshots/snapshot_*.json"))
print(f"- Captures: {len(snap_files)}")
if snap_files:
    print(f"- First: {os.path.basename(snap_files[0])}")
    print(f"- Last:  {os.path.basename(snap_files[-1])}")
print()

print("---\n_Generated automatically by scripts/investigate_report.sh._")
PYEOF

# Self-cleanup
/bin/launchctl unload "$PLIST" 2>/dev/null
/bin/rm -f "$PLIST"

# Drop a breadcrumb on the desktop
/bin/cp -f "$OUT" "$HOME/Desktop/volume_bubbles_investigation_report.md" 2>/dev/null || true

exit 0
