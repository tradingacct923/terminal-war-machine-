---
description: Debug layout rendering issues across panes
---

# Debug Layout — Altaris Terminal

// turbo-all

## Steps

1. Open the terminal at http://localhost:3001
2. Open browser DevTools Console (Cmd+Option+J)
3. Run this diagnostic in the console:

```javascript
(function() {
    var r = {};
    r.chartCore = typeof ChartCore !== 'undefined';
    r.instances = r.chartCore ? ChartCore.getInstances().map(function(i, idx) {
        return { idx: idx, feature: i.feature, id: i.container.id, bubbles: !!i.bubbleSeries, heatmap: !!i.heatmapCanvas };
    }) : [];
    r.ladderCanvas = !!document.getElementById('dom-ladder-canvas');
    r.useCanvasLadder = typeof _useCanvasLadder !== 'undefined' ? _useCanvasLadder : 'N/A';
    r.activeFeature = window._activeChartFeature;
    r.dom2dSnaps = typeof DOM2D !== 'undefined' ? DOM2D._snapshots.length : 0;
    console.table(r.instances);
    console.log(JSON.stringify(r, null, 2));
})();
```

4. Switch layouts using the Layout dropdown and verify:
   - **Main**: 1 chart instance with feature='chart', has bubbles
   - **Flow**: 3 instances (chart+gex+dex), only chart has bubbles
   - **DOM**: 1 heatmap instance + ladder canvas + eqbook tape
   - **God Mode**: 5 instances (chart×2 + heatmap + alpha + ladder)

5. Check for errors: `ChartCore.getInstances()` should never be empty after layout switch

## Common Issues

| Symptom | Likely Cause |
|---|---|
| Blank pane after switch | Feature mount didn't fire → check `onFeatureMount` |
| Candlesticks on heatmap/gex/dex | `showCandles` flag not set → check `chart_core.js` |
| Ladder blank | No L2 DOM data → check `/api/l2` response |
| Duplicate WallLines labels | `WallLines.attachToSeries` called multiple times |
| Data stops flowing | Legacy `_l2CandleSeries` guard → replace with `ChartCore.getInstances()` |
