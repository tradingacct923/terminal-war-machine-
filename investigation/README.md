# Volume Bubbles Investigation — captured between session start and 09:00 EDT 2026-04-27

## Folder layout

```
investigation/
├── bar_signals/           One JSONL per day. Every bar_signal computation —
│                          including candidates that were REJECTED by a detector,
│                          tagged with the rejection reason. Use to find false
│                          negatives (signals we should have caught but didn't).
│
├── big_prints/            Every big_print socket event emitted by the backend.
│                          Includes classification, p90/p99 thresholds at the
│                          moment, book context, refill class, level tier.
│
├── candle_bp/             Every closed 1m candle's bp dict — full per-price
│                          buy/sell/fp_score/true_abs/book_size matrix. Use to
│                          replay any signal computation offline.
│
├── floors_evolution/      Every 30s snapshot of the adaptive floors:
│                            - _adaptive_level_floor(NQ)
│                            - _adaptive_bar_floor(NQ)
│                            - sample counts in _LEVEL_VOL_SAMPLES / _BAR_VOL_SAMPLES
│                          Use to track how regime adaptation evolved.
│
├── snapshots/             Every 5min: full curl /api/l2 + /api/walls capture.
│                          Server-side state at the moment of capture.
│
├── raw_bar_context/       Every bar's full context — OHLC + bp + bar_delta + range +
│                          recent_delta_history. Lets us replay detectors.
│
└── reports/               final_report.md and any intermediate analyses.
```

## Schema (JSONL — one object per line)

### bar_signals/*.jsonl
```json
{
  "ts_ms":             <int>,           // wall clock when row was written
  "bar_t":             <int>,           // bar boundary UTC seconds (l2_worker convention)
  "symbol":            "NQ",
  "tf":                "1m",
  "phase":             "candidate" | "fired" | "rejected",
  "signal_type":       "absorption" | "exhaustion" | "aggression",
  "details":           { ... per-detector specifics, see below ... },
  "rejection_reason":  null | "low_volume" | "imbalance_too_high" | "no_extreme_proximity" | "wilson_below_threshold" | "no_swing_match" | "no_stacked_imbalance" | "no_follow_through",
  "bar_total":         <int>,
  "level_count":       <int>,
  "thresholds_at_emit": {
    "level_floor":     <float>,         // _adaptive_level_floor at the moment
    "bar_floor":       <float>,         // _adaptive_bar_floor at the moment
    "level_p75":       <float>,         // P75 of bar's level volumes (for absorption)
    "delta_p75_recent": <float>          // P75 of |delta| last 10 bars (for aggression)
  }
}
```

### big_prints/*.jsonl
Direct dump of the emitted big_print socket payload, plus a wall-clock receipt.

### candle_bp/*.jsonl
```json
{
  "ts_ms":   <int>,
  "bar_t":   <int>,
  "symbol":  "NQ",
  "tf":      "1m",
  "ohlc":    {"o": ..., "h": ..., "l": ..., "c": ..., "v": ...},
  "bp":      { "<price_str>": [buy, sell, fp_score, true_abs, book_size], ... },
  "delta":   <int>
}
```

### floors_evolution/*.jsonl
```json
{
  "ts_ms":              <int>,
  "level_floor_NQ":     <float>,
  "bar_floor_NQ":       <float>,
  "level_samples_n":    <int>,
  "bar_samples_n":      <int>,
  "level_median":       <float>,
  "bar_median":         <float>
}
```

## Investigation plan after 09:00 EDT

1. Count bar_signals fired by type (sweep / block / etc).
2. Count rejections by reason — surface the most-rejected categories.
3. Diff: is the system OVER-firing (too many signals) or UNDER-firing (missing real ones)?
4. Cross-check: when bar_signal aggression fired, did price actually follow through in next 3 bars?
5. Adaptive floor curve: how fast did it adapt during overnight → pre-market transition?

Auto-generated report at `reports/final_report_*.md`.
