---
description: How to add a new feature pane to the layout system
---

# Add a New Feature Pane — Altaris Terminal

## Steps

1. **Register the feature key** in `web/layout_integration.js`:

```javascript
// In FEATURE_REGISTRY (around line 28)
myfeature: { label: 'MY FEAT', icon: '\u{1f4ca}', desc: 'My Feature Description' },
```

2. **Add to a layout preset** (or create a new one) in `LAYOUT_PRESETS`:

```javascript
'my-layout': { label:'My Layout', slots:3, cols:3, rows:1, defaults:['chart','myfeature','ladder'] },
```

3. **Handle mount in `app.js`** — inside `AltarisLayout.onFeatureMount`:

```javascript
} else if (featureKey === 'myfeature') {
    // Create the DOM container for your feature
    slotEl.innerHTML = `<div id="my-feature-container" style="width:100%;height:100%"></div>`;
    // Initialize your feature module
    if (typeof MyFeature !== 'undefined') MyFeature.init(slotEl.firstElementChild);
}
```

4. **Handle unmount in `app.js`** — inside `AltarisLayout.onFeatureUnmount`:

```javascript
if (featureKey === 'myfeature') {
    if (typeof MyFeature !== 'undefined') MyFeature.destroy();
}
```

5. **If your feature uses ChartCore** (needs price axis):
   - Call `ChartCore.init(container, symbol, 'myfeature')`
   - Candlesticks will be transparent by default (non-chart features)
   - To also route data to it, add your feature key in the data broadcasting loop:

```javascript
// In the candle update handler
ChartCore.getInstances().forEach(inst => {
    if (inst.feature === 'myfeature') {
        // Your feature-specific data routing
    }
});
```

6. **If your feature is standalone** (no chart infrastructure):
   - Just create the DOM and init your module
   - Example: `alpha`, `eqbook`, `ladder`

## Checklist
- [ ] Feature key registered in `FEATURE_REGISTRY`
- [ ] Added to at least one layout preset
- [ ] Mount handler in `onFeatureMount`
- [ ] Unmount handler in `onFeatureUnmount`
- [ ] Data routing added in `app.js` if needed
- [ ] Tested layout switch: Main → YourLayout → Main (no orphan DOM)
