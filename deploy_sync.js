// ═══════════════════════════════════════════════════════════════════════════════
// deploy_sync.js — Cache-Bust + Deploy Sync
// ═══════════════════════════════════════════════════════════════════════════════
// Usage: node deploy_sync.js
// Forces all sigma-engine script tags to bypass browser cache with unique buildID.
// ═══════════════════════════════════════════════════════════════════════════════

const fs = require('fs');
const path = require('path');
const buildID = Date.now(); // Unique timestamp

const htmlPath = path.join(__dirname, 'web', 'index.html');
let html = fs.readFileSync(htmlPath, 'utf8');

// Force-update all script tags to bypass browser cache
const scripts = [
    'sigma_engine.js',
    'kinetic_text.js',
    'kinetic_hud.js',
    'pressure_field.js',
    'volume_bubbles.js',
    'app.js',
];

let updated = 0;
scripts.forEach(s => {
    const regex = new RegExp(`${s.replace('.', '\\.')}(\\?v=[a-zA-Z0-9_]+)?`, 'g');
    const before = html;
    html = html.replace(regex, `${s}?v=${buildID}`);
    if (html !== before) updated++;
});

fs.writeFileSync(htmlPath, html);
console.log(`✅ Cache-busting complete. Build ID: ${buildID}`);
console.log(`   Updated ${updated} script tags in index.html`);
