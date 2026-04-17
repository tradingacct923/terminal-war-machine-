// ═══════════════════════════════════════════════════════════════════════════════
// KINETIC PHYSICS HUD — Sensitivity Calibration Controller
// ═══════════════════════════════════════════════════════════════════════════════
//
// Floating, draggable, glassmorphic control panel for real-time physics tuning.
// Attached to #terminal (NOT #t-zone-ladder) to survive live data re-renders.
// Settings persist in localStorage across page refreshes.
// ═══════════════════════════════════════════════════════════════════════════════

'use strict';

const _KC_STORAGE_KEY = 'kineticConfig';

// ─────────────────────────────────────────────────────────────────────────────
// 1. KINETIC CONFIG — Global State Store + localStorage Persistence
// ─────────────────────────────────────────────────────────────────────────────

const KineticConfig = {
    // ── 5 Precision Controls ──
    volGate: 2.0,
    impact: 1.0,
    stiffness: 200,
    coolDown: 0.90,
    minTick: 0,

    // ── Presets ──
    _presets: {
        default:     { volGate: 2.0,  impact: 1.0,  stiffness: 200, coolDown: 0.90, minTick: 0 },
        scalper:     { volGate: 1.0,  impact: 2.5,  stiffness: 400, coolDown: 0.70, minTick: 0 },
        whaleHunter: { volGate: 5.0,  impact: 1.5,  stiffness: 100, coolDown: 0.96, minTick: 10 },
    },

    applyPreset(name) {
        const p = this._presets[name];
        if (!p) return;
        this.volGate = p.volGate;
        this.impact = p.impact;
        this.stiffness = p.stiffness;
        this.coolDown = p.coolDown;
        this.minTick = p.minTick;
        _syncSlidersFromConfig();
        _saveConfig();
    },

    updatePhysicsParams() {
        _saveConfig();
    },

    /** Load saved settings from localStorage */
    _load() {
        try {
            const raw = localStorage.getItem(_KC_STORAGE_KEY);
            if (!raw) return;
            const saved = JSON.parse(raw);
            if (saved.volGate !== undefined)  this.volGate  = saved.volGate;
            if (saved.impact !== undefined)   this.impact   = saved.impact;
            if (saved.stiffness !== undefined) this.stiffness = saved.stiffness;
            if (saved.coolDown !== undefined) this.coolDown = saved.coolDown;
            if (saved.minTick !== undefined)  this.minTick  = saved.minTick;
        } catch (e) { /* ignore corrupt data */ }
    }
};

function _saveConfig() {
    try {
        localStorage.setItem(_KC_STORAGE_KEY, JSON.stringify({
            volGate: KineticConfig.volGate,
            impact: KineticConfig.impact,
            stiffness: KineticConfig.stiffness,
            coolDown: KineticConfig.coolDown,
            minTick: KineticConfig.minTick,
        }));
    } catch (e) { /* storage full or blocked */ }
}

// Load saved settings immediately
KineticConfig._load();
window.KineticConfig = KineticConfig;

// ─────────────────────────────────────────────────────────────────────────────
// 2. ENGINE INTEGRATION HOOKS
// ─────────────────────────────────────────────────────────────────────────────

let _enginePatched = false;

function _patchKineticEngine() {
    if (_enginePatched) return;
    if (typeof KineticText === 'undefined') return;

    const origRender = KineticText._render.bind(KineticText);
    KineticText._render = function () {
        const k = KineticConfig.stiffness;
        for (const [, phys] of this.state) {
            phys.stiffness = k;
        }
        origRender();
    };

    KineticText._decayHeat = function () {
        const baseCoolDown = KineticConfig.coolDown;
        const sigma = (typeof SigmaEngine !== 'undefined') ? SigmaEngine.marketVolatility : 1.0;

        for (const [priceStr, phys] of this.state) {
            if (phys.heat > phys.heatScar) {
                phys.heatScar = phys.heat;
            }

            // ── Delta-Decay: e^(-Δt · σ) ──
            // Δt = time since last trade at this price level
            // σ = market volatility (high vol = faster decay)
            const dt = phys.lastTradeTime > 0
                ? (performance.now() - phys.lastTradeTime) / 1000
                : 1.0;
            const deltaCoolDown = Math.exp(-dt * sigma * 0.5);
            // Blend with manual slider (whichever decays faster wins)
            const effectiveDecay = Math.min(baseCoolDown, deltaCoolDown);

            // Check if absorption is locking heat at this price
            let heatLocked = false;
            if (typeof SigmaEngine !== 'undefined') {
                const absCheck = SigmaEngine.checkAbsorption(priceStr);
                if (absCheck.isAbsorb && absCheck.inertia > 3.0) {
                    heatLocked = true; // persistent heat scar
                }
            }

            if (!heatLocked) {
                phys.heat *= effectiveDecay;
                if (phys.heat < 0.005) phys.heat = 0;
            }

            // Heat scar decays slower (persistent afterglow)
            const scarDecay = Math.exp(-dt * sigma * 0.15);
            phys.heatScar *= Math.min(0.97, scarDecay);
            if (phys.heatScar < 0.005) phys.heatScar = 0;
        }
    };

    const origShock = KineticText.applyShock.bind(KineticText);
    KineticText.applyShock = function (priceStr, takeVolume, restingSize, avgVolPerLevel, side) {
        // ── Dynamic Noise Gate: max(manual slider, sigma-driven floor) ──
        const manualFloor = KineticConfig.minTick;
        const sigmaFloor = (typeof SigmaEngine !== 'undefined') ? SigmaEngine.noiseFloor : 0;
        const effectiveFloor = Math.max(manualFloor, sigmaFloor);
        if (takeVolume < effectiveFloor) return;

        origShock(priceStr, takeVolume, restingSize, avgVolPerLevel, side);
        const phys = this.state.get(priceStr);
        if (phys) {
            phys.tensionDisp *= KineticConfig.impact;
            const lRest = Math.max(restingSize, 1);
            const volRatio = phys.cumulativeVol / lRest;
            if (volRatio > KineticConfig.volGate) {
                phys.heat = Math.min(1.0, (volRatio - KineticConfig.volGate) / 3.0);
            } else {
                phys.heat = 0;
            }
        }
    };

    _enginePatched = true;
    console.log('[KineticHUD] Engine patched with config hooks');
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. HUD UI — Permanent Floating Panel
// ─────────────────────────────────────────────────────────────────────────────

const SLIDERS = [
    { id: 'kc-volgate',   key: 'volGate',   label: 'SENSITIVITY',   min: 0.5,  max: 10,   step: 0.1,  fmt: v => parseFloat(v).toFixed(1), unit: '×' },
    { id: 'kc-impact',    key: 'impact',    label: 'SHOCK MAG',     min: 0.1,  max: 10,   step: 0.1,  fmt: v => parseFloat(v).toFixed(1), unit: 'px' },
    { id: 'kc-stiffness', key: 'stiffness', label: 'STIFFNESS K',   min: 10,   max: 500,  step: 5,    fmt: v => Math.round(v),            unit: '' },
    { id: 'kc-cooldown',  key: 'coolDown',  label: 'THERMAL DECAY',  min: 0.50, max: 0.99, step: 0.01, fmt: v => parseFloat(v).toFixed(2), unit: '' },
    { id: 'kc-mintick',   key: 'minTick',   label: 'NOISE FLOOR',   min: 0,    max: 100,  step: 1,    fmt: v => Math.round(v),            unit: 'lots' },
];

function _initPhysicsHUD() {
    // Already exists — just make sure engine is patched
    if (document.getElementById('kinetic-hud')) {
        _patchKineticEngine();
        return;
    }

    const hud = document.createElement('div');
    hud.id = 'kinetic-hud';
    hud.className = 'kinetic-hud';

    const sliderHTML = SLIDERS.map(s => `
        <div class="kh-row">
            <label class="kh-label">${s.label}</label>
            <input type="range" id="${s.id}" class="kh-slider"
                   min="${s.min}" max="${s.max}" step="${s.step}"
                   value="${KineticConfig[s.key]}">
            <span class="kh-val" id="${s.id}-val">${s.fmt(KineticConfig[s.key])}${s.unit ? '<small>' + s.unit + '</small>' : ''}</span>
        </div>
    `).join('');

    hud.innerHTML = `
        <div class="kh-header" id="kh-drag-handle">
            <span class="kh-title">PHYSICS</span>
            <div class="kh-header-right">
                <select class="kh-preset" id="kh-preset">
                    <option value="default">Default</option>
                    <option value="scalper">Scalper</option>
                    <option value="whaleHunter">Whale Hunter</option>
                </select>
                <span class="kh-close" id="kh-close" title="Close">✕</span>
            </div>
        </div>
        <div class="kh-body" id="kh-body">
            ${sliderHTML}
        </div>
    `;

    // ── ATTACH TO #terminal — NOT the ladder zone ──
    // The ladder zone re-renders on every data tick, destroying children.
    // #terminal is the stable parent that never gets innerHTML'd.
    const terminal = document.getElementById('terminal');
    if (terminal) {
        terminal.appendChild(hud);
    } else {
        document.body.appendChild(hud);
    }

    // Restore visibility from localStorage
    const hudVisible = localStorage.getItem('kineticHudVisible');
    if (hudVisible === 'false') {
        hud.style.display = 'none';
    }

    // ── Wire sliders ──
    for (const s of SLIDERS) {
        const el = document.getElementById(s.id);
        const valEl = document.getElementById(s.id + '-val');
        if (!el) continue;
        el.addEventListener('input', () => {
            KineticConfig[s.key] = parseFloat(el.value);
            if (valEl) valEl.innerHTML = s.fmt(el.value) + (s.unit ? '<small>' + s.unit + '</small>' : '');
            KineticConfig.updatePhysicsParams();
        });
    }

    // ── Preset dropdown ──
    const presetEl = document.getElementById('kh-preset');
    if (presetEl) {
        presetEl.addEventListener('change', () => {
            KineticConfig.applyPreset(presetEl.value);
        });
    }

    // ── Close button (hide, not destroy) ──
    const closeBtn = document.getElementById('kh-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            hud.style.display = 'none';
            localStorage.setItem('kineticHudVisible', 'false');
        });
    }

    // ── Draggable ──
    _makeDraggable(hud, document.getElementById('kh-drag-handle'));

    // ── Patch engine ──
    _patchKineticEngine();

    // ── Add toolbar toggle button ──
    _addToolbarButton();

    console.log('[KineticHUD] Physics HUD initialized (permanent)');
}

// ─────────────────────────────────────────────────────────────────────────────
// 4. TOOLBAR TOGGLE BUTTON
// ─────────────────────────────────────────────────────────────────────────────

function _addToolbarButton() {
    if (document.getElementById('t-physics-btn')) return;

    const tbRight = document.querySelector('.t-tb-right');
    if (!tbRight) return;

    const btn = document.createElement('button');
    btn.id = 't-physics-btn';
    btn.className = 't-settings-btn';
    btn.title = 'Physics Settings';
    btn.textContent = 'P';
    btn.style.marginRight = '4px';

    btn.addEventListener('click', () => {
        const hud = document.getElementById('kinetic-hud');
        if (!hud) return;
        const isHidden = hud.style.display === 'none';
        hud.style.display = isHidden ? '' : 'none';
        localStorage.setItem('kineticHudVisible', isHidden ? 'true' : 'false');
    });

    // Insert before the existing settings gear
    const settingsBtn = document.getElementById('t-heatmap-settings-btn');
    if (settingsBtn) {
        tbRight.insertBefore(btn, settingsBtn);
    } else {
        tbRight.appendChild(btn);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 5. HELPERS
// ─────────────────────────────────────────────────────────────────────────────

function _syncSlidersFromConfig() {
    for (const s of SLIDERS) {
        const el = document.getElementById(s.id);
        const valEl = document.getElementById(s.id + '-val');
        if (el) {
            el.value = KineticConfig[s.key];
            if (valEl) valEl.innerHTML = s.fmt(KineticConfig[s.key]) + (s.unit ? '<small>' + s.unit + '</small>' : '');
        }
    }
}

function _makeDraggable(el, handle) {
    if (!handle) return;

    handle.style.cursor = 'grab';

    handle.addEventListener('mousedown', (e) => {
        if (e.target.tagName === 'SELECT' || e.target.tagName === 'OPTION' || e.target.id === 'kh-close') return;
        e.preventDefault();
        let sx = e.clientX, sy = e.clientY;
        handle.style.cursor = 'grabbing';

        const onMove = (ev) => {
            const dx = ev.clientX - sx;
            const dy = ev.clientY - sy;
            sx = ev.clientX;
            sy = ev.clientY;
            const rect = el.getBoundingClientRect();
            el.style.top = (rect.top + dy) + 'px';
            el.style.right = 'auto';
            el.style.left = (rect.left + dx) + 'px';
        };

        const onUp = () => {
            handle.style.cursor = 'grab';
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// 6. AUTO-INIT
// ─────────────────────────────────────────────────────────────────────────────

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(_initPhysicsHUD, 200));
} else {
    setTimeout(_initPhysicsHUD, 200);
}
