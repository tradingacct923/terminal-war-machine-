const fs = require('fs');
const path = require('path');

const p = path.join(__dirname, 'web', 'app.js');
const target = path.join(__dirname, 'web', 'features', 'dashboard_charts.js');

const content = fs.readFileSync(p, 'utf8').split('\n');

// Wrap in IIFE or just preserve global scope. We will just preserve global scope for now 
// since all these functions were originally global in app.js.
const dashboardLines = content.slice(99, 4015).join('\n');
const appLines = content.slice(0, 99).concat(content.slice(4015)).join('\n');

fs.writeFileSync(target, dashboardLines);
fs.writeFileSync(p, appLines);

console.log(`Original app.js: ${content.length} lines`);
console.log(`Extracted dashboard_charts.js: ${dashboardLines.split('\n').length} lines`);
console.log(`New app.js: ${appLines.split('\n').length} lines`);
