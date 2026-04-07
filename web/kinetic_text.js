// ═══════════════════════════════════════════════════════════════════════════════
// KINETIC TYPOGRAPHY ENGINE — WebGL Damped Harmonic Oscillator Text
// ═══════════════════════════════════════════════════════════════════════════════
//
// GPU-accelerated text rendering for the L2 DOM depth ladder.
// Each price level's text is a textured quad displaced by a damped spring
// computed analytically in the vertex shader:
//
//   x(t) = (S / (m·ωd)) · e^(−γt) · sin(ωd·t)
//
// Where:
//   S = shock force (V_take / L_rest)
//   m = mass (from resting order size)
//   γ = c/(2m) = damping ratio
//   ωd = sqrt(k/m − γ²) = damped angular frequency
//
// Architecture:
//   - Offscreen Canvas 2D renders glyphs into a texture atlas (once)
//   - WebGL2 canvas overlays the existing Canvas 2D depth ladder
//   - Per-price-level physics state driven by WebSocket trade data
//   - rAF loop pushes uniforms to GPU, draws instanced quads
// ═══════════════════════════════════════════════════════════════════════════════

'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// 1. GLSL SHADERS
// ─────────────────────────────────────────────────────────────────────────────

const KINETIC_VERT = `#version 300 es
precision highp float;

// ── Geometry ──
in vec2 a_position;     // quad vertex (0..1, 0..1)
in vec2 a_texCoord;     // corresponding UV into font atlas

// ── Per-instance (per glyph) ──
in vec2 a_offset;       // screen-space (x, y) of this glyph
in vec2 a_size;         // width, height of this glyph quad
in vec4 a_atlasRect;    // (u0, v0, u1, v1) in atlas texture

// ── Physics uniforms (per price level, pushed per frame) ──
uniform float u_shockForce;  // S ∈ [0, 1] normalized
uniform float u_mass;        // m = log2(L_rest + 1)
uniform float u_damping;     // c = 2√(km) / (1 + toxicity)
uniform float u_stiffness;   // k = spring constant (from HUD)
uniform float u_elapsed;     // t = seconds since last shock
uniform float u_impact;      // HUD: shock magnitude multiplier

// ── Directional Tension uniforms ──
uniform float u_tension;     // vertical tension magnitude (px)
uniform float u_direction;   // +1.0 = Buy (upward), -1.0 = Sell (downward)

// ── Global uniforms ──
uniform vec2 u_resolution;   // canvas size in CSS pixels
uniform float u_dpr;         // devicePixelRatio

out vec2 v_texCoord;
out float v_shockIntensity;  // passed to fragment for glow

void main() {
    // ── Damped harmonic oscillator (analytical, horizontal) ──
    float m = max(u_mass, 0.1);
    float k = u_stiffness;
    float c = u_damping;
    float t = u_elapsed;

    float gamma = c / (2.0 * m);
    float omega0_sq = k / m;
    float discriminant = omega0_sq - gamma * gamma;

    float rawDisp = 0.0;

    if (discriminant > 0.0 && u_shockForce > 0.001) {
        // Underdamped: oscillatory decay
        float omegaD = sqrt(discriminant);
        float amplitude = u_shockForce / (m * omegaD);
        amplitude = min(amplitude, 20.0);
        rawDisp = amplitude * exp(-gamma * t) * sin(omegaD * t);
    } else if (discriminant <= 0.0 && u_shockForce > 0.001) {
        // Overdamped / critically damped
        float decayRate = gamma - sqrt(max(gamma * gamma - omega0_sq, 0.0));
        rawDisp = (u_shockForce / m) * exp(-decayRate * t);
        rawDisp = min(rawDisp, 20.0);
    }

    // ── Logarithmic Displacement Scaling ──
    // log10(1 + |x|) compresses large moves, expands small moves
    // Result: 1-lot = ~0.3px subtle vibration, 50-lot sweep = ~1.7px violent
    // u_impact multiplier scales final px displacement from HUD
    float sign = rawDisp >= 0.0 ? 1.0 : -1.0;
    float logDisp = sign * log(1.0 + abs(rawDisp)) / log(10.0);
    float displacement = logDisp * u_impact;

    // ── Directional Tension (vertical, Hooke's Law spring-back) ──
    float yOffset = u_tension * u_direction;

    // ── Position the glyph quad ──
    vec2 pos = a_offset + a_position * a_size;
    pos.x += displacement;
    pos.y += yOffset;

    // Convert from CSS pixel space to clip space (-1..1)
    vec2 clipPos = (pos / u_resolution) * 2.0 - 1.0;
    clipPos.y = -clipPos.y;

    gl_Position = vec4(clipPos, 0.0, 1.0);

    // ── UV mapping into atlas ──
    v_texCoord = mix(a_atlasRect.xy, a_atlasRect.zw, a_texCoord);

    // ── Pass shock intensity for fragment glow ──
    float totalDisp = abs(displacement) + abs(yOffset);
    float rawIntensity = totalDisp / (u_impact * 2.0 + 0.01);
    v_shockIntensity = clamp(rawIntensity, 0.0, 1.0);
}
`;

const KINETIC_FRAG = `#version 300 es
precision highp float;

in vec2 v_texCoord;
in float v_shockIntensity;

uniform sampler2D u_atlas;
uniform vec3 u_textColor;     // base text color
uniform vec3 u_glowColor;     // shock glow color (bid=cyan, ask=red)
uniform float u_heat;         // thermal stress: 0.0 (cool) → 1.0 (white-hot)
uniform float u_heatScar;     // thermal inertia: fading afterglow 0.0—1.0

out vec4 fragColor;

void main() {
    vec4 texel = texture(u_atlas, v_texCoord);
    float alpha = texel.a;

    if (alpha < 0.02) discard;

    // ── Base text color ──
    vec3 color = u_textColor;

    // ═══════════════════════════════════════════════════════════════
    // THERMAL INERTIA — Heat Scar Afterglow
    // ═══════════════════════════════════════════════════════════════
    // Heat scars persist as a faint warm tint after primary heat fades
    // Creates visual 'memory' of where aggressive flow occurred
    if (u_heatScar > 0.01) {
        vec3 warmTint = vec3(1.0, 0.6, 0.2); // amber afterglow
        float scarMix = u_heatScar * 0.25;   // subtle, max 25% blend
        color = mix(color, warmTint, scarMix);
    }

    // ═══════════════════════════════════════════════════════════════
    // THERMAL HEAT RAMP — Non-linear 3-band color mapping
    // ═══════════════════════════════════════════════════════════════
    if (u_heat > 0.01) {
        vec3 cyan = vec3(0.0, 1.0, 1.0);
        vec3 white = vec3(1.0, 1.0, 1.0);

        if (u_heat <= 0.3) {
            float t = u_heat / 0.3;
            color = mix(color, color * 1.2, t);
        } else if (u_heat <= 0.7) {
            float t = (u_heat - 0.3) / 0.4;
            color = mix(color, cyan, t);
        } else {
            float t = (u_heat - 0.7) / 0.3;
            float expBloom = 1.0 - exp(-3.0 * t);
            color = mix(cyan, white, expBloom);
            float bloomMultiplier = 1.0 + expBloom * 0.8;
            color *= bloomMultiplier;
        }

        alpha = min(alpha * (1.0 + u_heat * 0.5), 1.0);
    }

    // ── Shock emission glow (stacks on top of thermal) ──
    if (v_shockIntensity > 0.01) {
        float glowMix = v_shockIntensity * 0.5;
        color = mix(color, u_glowColor, glowMix);
        alpha = min(alpha * (1.0 + v_shockIntensity * 0.3), 1.0);
    }

    fragColor = vec4(clamp(color, 0.0, 2.0), alpha);
}
`;

// ─────────────────────────────────────────────────────────────────────────────
// 2. FONT ATLAS GENERATOR
// ─────────────────────────────────────────────────────────────────────────────

const ATLAS_CONFIG = {
    FONT: '10px "JetBrains Mono", "SF Mono", "Consolas", monospace',
    FONT_SIZE: 10,
    PADDING: 2,
    ATLAS_WIDTH: 512,
    ATLAS_HEIGHT: 64,
    // All glyphs needed for depth ladder
    GLYPHS: '0123456789.,+-$ ABSCRKFOHTLDEWMINPQUVXYZabcdefghijklmnopqrstuvwxyz'
};

/**
 * Generate font texture atlas from Canvas 2D.
 * Returns { canvas, glyphMap: Map<char, {x,y,w,h,u0,v0,u1,v1}> }
 */
function _buildFontAtlas() {
    const { FONT, FONT_SIZE, PADDING, ATLAS_WIDTH, ATLAS_HEIGHT, GLYPHS } = ATLAS_CONFIG;
    const dpr = window.devicePixelRatio || 1;

    const canvas = document.createElement('canvas');
    canvas.width = ATLAS_WIDTH * dpr;
    canvas.height = ATLAS_HEIGHT * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    ctx.font = FONT;
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#ffffff';
    ctx.clearRect(0, 0, ATLAS_WIDTH, ATLAS_HEIGHT);

    const glyphMap = new Map();
    let cursorX = PADDING;
    let cursorY = PADDING;
    const lineH = FONT_SIZE + PADDING * 2;

    for (const ch of GLYPHS) {
        const metrics = ctx.measureText(ch);
        const charW = Math.ceil(metrics.width) + PADDING;

        // Wrap to next line if needed
        if (cursorX + charW > ATLAS_WIDTH) {
            cursorX = PADDING;
            cursorY += lineH;
        }
        if (cursorY + lineH > ATLAS_HEIGHT) break; // atlas full

        ctx.fillText(ch, cursorX, cursorY);

        // Store glyph rect in both pixel and UV coords
        glyphMap.set(ch, {
            x: cursorX,
            y: cursorY,
            w: charW - PADDING,
            h: FONT_SIZE,
            // UV coordinates (0..1)
            u0: cursorX / ATLAS_WIDTH,
            v0: cursorY / ATLAS_HEIGHT,
            u1: (cursorX + charW - PADDING) / ATLAS_WIDTH,
            v1: (cursorY + FONT_SIZE) / ATLAS_HEIGHT,
        });

        cursorX += charW;
    }

    return { canvas, glyphMap, dpr };
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. PHYSICS STATE MANAGER
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Per-price-level physics state.
 * Updated from WebSocket trade data each frame.
 */
class PhysicsRow {
    constructor() {
        // ── Horizontal shock (damped harmonic oscillator) ──
        this.shockForce = 0;    // S ∈ [0, 1] normalized
        this.mass = 1;          // m = log2(L_rest + 1)
        this.damping = 10;      // c = critical damping by default
        this.stiffness = 200;   // k = spring constant (overridden by KineticConfig)
        this.shockTime = 0;     // timestamp (ms) of last shock event
        this.side = 'bid';      // 'bid' or 'ask'

        // ── Directional Tension (vertical Hooke's Law spring-back) ──
        this.tensionDisp = 0;   // current vertical displacement (px)
        this.tensionVel = 0;    // current velocity (px/s)
        this.direction = 0;     // +1 buy, -1 sell, 0 neutral

        // ── Thermal Heat ──
        this.heat = 0;          // 0.0 (cool) → 1.0 (white-hot)
        this.heatScar = 0;      // thermal inertia afterglow (decays slower than heat)
        this.cumulativeVol = 0; // total volume traded at this level
        this.restingAtShock = 1;

        // ── Zero-Baseline tracking ──
        this.lastTradeTime = 0;
    }

    /**
     * Apply a new shock from aggressive trade data.
     * 
     * Volume Scaling: shockForce = clamp(tradeVolume / avgTickVolume, 0, 1)
     *   - Small trades (1-5 lots) → barely visible vibration
     *   - Large sweeps → violent shatter (force approaches 1.0)
     *
     * Non-Linear Heat: only glows once cumulative volume > 200% of resting
     *   - heat = clamp((cumVol / resting - 2.0) / 3.0, 0, 1)
     *   - This means: 0-200% = cold, 200-500% = warming, 500%+ = white-hot
     */
    shock(takeVolume, restingSize, avgTickVolume, side) {
        const lRest = Math.max(restingSize, 1);
        this.restingAtShock = lRest;

        // ── Volume-Normalized Shock Force ──
        // clamp(TradeVolume / AverageTickVolume, 0, 1)
        const avgTick = Math.max(avgTickVolume, 1);
        this.shockForce = Math.min(Math.max(takeVolume / avgTick, 0), 1.0);

        this.mass = Math.max(1.0, Math.log2(lRest + 1));
        this.side = side;

        // Toxicity scales damping: high volume relative to avg = low damping
        const toxicity = takeVolume / avgTick;
        const criticalDamping = 2.0 * Math.sqrt(this.stiffness * this.mass);
        this.damping = criticalDamping / (1.0 + toxicity);
        this.shockTime = performance.now();
        this.lastTradeTime = performance.now();

        // ── Directional tension impulse ──
        this.direction = (side === 'bid' || side === 'buy') ? -1 : 1;
        // Scale tension impulse by shockForce: small trades → sub-pixel stretch
        this.tensionDisp = 2.0 * this.direction * this.shockForce;
        this.tensionVel = 0;

        // ── Non-Linear Heat Accumulation ──
        // Accumulate total traded volume at this level
        this.cumulativeVol += takeVolume;
        // Heat only activates once cumulative volume > 200% of resting liquidity
        const volRatio = this.cumulativeVol / lRest;
        if (volRatio > 2.0) {
            // Map 200%-500% → 0.0-1.0 heat
            this.heat = Math.min(1.0, (volRatio - 2.0) / 3.0);
        }
        // else: heat stays at current value (could be 0 or decaying)
    }

    /**
     * Zero-Baseline: force all physics to zero.
     * Called when no trades have hit this level for >100ms.
     */
    zeroBaseline() {
        this.shockForce = 0;
        this.heat = 0;
        this.heatScar = 0;
        this.cumulativeVol = 0;
    }

    /**
     * Step the Hooke's Law spring-back physics.
     */
    stepTension(dt) {
        if (Math.abs(this.tensionDisp) < 0.001 && Math.abs(this.tensionVel) < 0.001) {
            this.tensionDisp = 0;
            this.tensionVel = 0;
            return;
        }
        // Use this.stiffness so HUD slider controls both horizontal AND vertical spring
        const TENSION_C = 0.15;
        const force = -this.stiffness * this.tensionDisp - TENSION_C * this.tensionVel;
        this.tensionVel += force * dt;
        this.tensionDisp += this.tensionVel * dt;
    }

    /** Elapsed seconds since last shock */
    elapsed() {
        return (performance.now() - this.shockTime) / 1000;
    }

    /** True if horizontal physics has decayed to negligible */
    isSettled() {
        return this.elapsed() > 3.0 || this.shockForce < 0.001;
    }

    /** True if no trade has hit this level in >100ms */
    isIdle() {
        return this.lastTradeTime > 0 && (performance.now() - this.lastTradeTime) > 100;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 4. WEBGL ENGINE
// ─────────────────────────────────────────────────────────────────────────────

const KineticText = {
    // ── State ──
    gl: null,
    program: null,
    programValid: false,
    atlas: null,
    atlasTexture: null,
    glyphMap: null,
    state: new Map(),           // price (string) → PhysicsRow
    _rafId: null,
    _canvas: null,
    _destroyed: false,
    _lastFrameTime: 0,          // for dt calculation in tension physics
    _heatDecayTimer: null,      // setInterval for 500ms heat decay
    _avgTickVolume: 1,          // EWMA of average trade volume per tick
    _avgTickAlpha: 0.1,         // EWMA smoothing factor

    // ── Glyph batch buffers ──
    _positionBuffer: null,
    _texCoordBuffer: null,
    _instanceBuffer: null,
    _maxInstances: 2048,        // max glyphs per frame

    // ── Cached data from ladder ──
    _ladderData: null,          // set by renderDepthLadder hook

    // ── Uniform locations ──
    _uniforms: {},

    /**
     * Initialize the WebGL2 kinetic text engine.
     * @param {HTMLCanvasElement} canvas - the overlay canvas element
     */
    init(canvas) {
        if (this._destroyed) return false;

        this._canvas = canvas;
        const gl = canvas.getContext('webgl2', {
            alpha: true,
            premultipliedAlpha: false,
            antialias: true,
            preserveDrawingBuffer: false,
        });

        if (!gl) {
            console.warn('[KineticText] WebGL2 not available, falling back to static text');
            return false;
        }

        this.gl = gl;

        // ── Compile shaders ──
        const vs = this._compileShader(gl.VERTEX_SHADER, KINETIC_VERT);
        const fs = this._compileShader(gl.FRAGMENT_SHADER, KINETIC_FRAG);
        if (!vs || !fs) return false;

        const program = gl.createProgram();
        gl.attachShader(program, vs);
        gl.attachShader(program, fs);
        gl.linkProgram(program);

        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            console.error('[KineticText] Shader link error:', gl.getProgramInfoLog(program));
            return false;
        }

        this.program = program;
        this.programValid = true;
        gl.useProgram(program);

        // ── Cache uniform locations ──
        const unis = [
            'u_shockForce', 'u_mass', 'u_damping', 'u_stiffness',
            'u_elapsed', 'u_resolution', 'u_dpr', 'u_atlas',
            'u_textColor', 'u_glowColor',
            'u_tension', 'u_direction', 'u_heat',
            'u_impact', 'u_heatScar'
        ];
        for (const name of unis) {
            this._uniforms[name] = gl.getUniformLocation(program, name);
        }

        // ── Build font atlas ──
        const atlas = _buildFontAtlas();
        this.atlas = atlas;
        this.glyphMap = atlas.glyphMap;
        this._uploadAtlas(atlas);

        // ── Create geometry buffers ──
        this._createBuffers();

        // ── Enable blending for transparent text ──
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

        // ── Start render loop ──
        this._startLoop();

        // ── Start heat decay timer (every 500ms, multiply all heat by 0.90) ──
        this._heatDecayTimer = setInterval(() => this._decayHeat(), 500);

        console.log('[KineticText] WebGL2 kinetic text engine initialized (thermal + tension)');
        return true;
    },

    _compileShader(type, source) {
        const gl = this.gl;
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            const label = type === gl.VERTEX_SHADER ? 'VERTEX' : 'FRAGMENT';
            console.error(`[KineticText] ${label} shader error:`, gl.getShaderInfoLog(shader));
            gl.deleteShader(shader);
            return null;
        }
        return shader;
    },

    _uploadAtlas(atlas) {
        const gl = this.gl;
        const tex = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, atlas.canvas);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        this.atlasTexture = tex;
        gl.uniform1i(this._uniforms.u_atlas, 0);
    },

    _createBuffers() {
        const gl = this.gl;

        // Unit quad: two triangles
        const quadVerts = new Float32Array([
            0, 0, 1, 0, 0, 1,
            1, 0, 1, 1, 0, 1,
        ]);
        const quadUVs = new Float32Array([
            0, 0, 1, 0, 0, 1,
            1, 0, 1, 1, 0, 1,
        ]);

        // Position buffer (shared unit quad)
        const aPos = gl.getAttribLocation(this.program, 'a_position');
        this._positionBuffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this._positionBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, quadVerts, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(aPos);
        gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

        // Tex coord buffer (shared unit UVs)
        const aUV = gl.getAttribLocation(this.program, 'a_texCoord');
        this._texCoordBuffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this._texCoordBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, quadUVs, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(aUV);
        gl.vertexAttribPointer(aUV, 2, gl.FLOAT, false, 0, 0);
    },

    // ─────────────────────────────────────────────────────────────────────
    // 5. RENDER LOOP
    // ─────────────────────────────────────────────────────────────────────

    _startLoop() {
        const render = () => {
            if (this._destroyed) return;
            this._rafId = requestAnimationFrame(render);
            this._render();
        };
        this._rafId = requestAnimationFrame(render);
    },

    /**
     * Main render pass — called once per animation frame.
     * Iterates visible price levels, pushes physics uniforms, draws glyphs.
     */
    _render() {
        const gl = this.gl;
        const data = this._ladderData;
        if (!gl || !data || !this.programValid) return;

        // ── Calculate frame delta for tension physics ──
        const now = performance.now();
        const dt = this._lastFrameTime > 0 ? Math.min((now - this._lastFrameTime) / 1000, 0.05) : 0.016;
        this._lastFrameTime = now;

        const canvas = this._canvas;
        const dpr = window.devicePixelRatio || 1;
        const cssW = data.cssW;
        const cssH = data.cssH;

        // Resize canvas to match ladder
        const pxW = Math.round(cssW * dpr);
        const pxH = Math.round(cssH * dpr);
        if (canvas.width !== pxW || canvas.height !== pxH) {
            canvas.width = pxW;
            canvas.height = pxH;
        }

        gl.viewport(0, 0, pxW, pxH);
        gl.clearColor(0, 0, 0, 0); // transparent
        gl.clear(gl.COLOR_BUFFER_BIT);

        gl.useProgram(this.program);
        gl.uniform2f(this._uniforms.u_resolution, cssW, cssH);
        gl.uniform1f(this._uniforms.u_dpr, dpr);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.atlasTexture);

        // ── Draw each visible price level's text ──
        // Also feed PressureField with obstacle data
        const hasPF = typeof PressureField !== 'undefined' && PressureField._ready;
        const { visiblePrices, bids, asks, ladderToY, priceLeft, PRICE_COL_W,
            bidBarRight, askBarLeft, ROW_H, bestBid, bestAsk, mid,
            bidEntries, askEntries, maxDepth } = data;

        for (const price of visiblePrices) {
            const y = ladderToY(price);
            if (y === null) continue;

            const pKey2 = price.toFixed(2);
            const pKey = price.toString();
            const bidSize = bids[pKey2] || bids[pKey] || 0;
            const askSize = asks[pKey2] || asks[pKey] || 0;
            const isCurrentPrice = Math.abs(price - mid) < 0.25 * 0.6;

            // ── Get or create physics state ──
            const physKey = pKey2;
            let phys = this.state.get(physKey);
            if (!phys) {
                phys = new PhysicsRow();
                this.state.set(physKey, phys);
            }

            // ── Step tension spring physics for this price level ──
            phys.stepTension(dt);

            // ── ZERO-BASELINE: 100ms idle → force zero ──
            // If no trade has hit this level in >100ms, suppress all physics
            if (phys.isIdle() && phys.isSettled()) {
                phys.zeroBaseline();
            }

            // ── Push physics uniforms ──
            const elapsed = phys.isSettled() ? 999.0 : phys.elapsed();
            gl.uniform1f(this._uniforms.u_shockForce, phys.shockForce);
            gl.uniform1f(this._uniforms.u_mass, phys.mass);
            gl.uniform1f(this._uniforms.u_damping, phys.damping);
            gl.uniform1f(this._uniforms.u_stiffness, phys.stiffness);
            gl.uniform1f(this._uniforms.u_elapsed, elapsed);

            // ── Push HUD-driven uniforms ──
            const impact = (typeof KineticConfig !== 'undefined') ? KineticConfig.impact : 1.0;
            gl.uniform1f(this._uniforms.u_impact, impact);

            // ── Push tension + heat + thermal inertia uniforms ──
            gl.uniform1f(this._uniforms.u_tension, Math.abs(phys.tensionDisp));
            gl.uniform1f(this._uniforms.u_direction, phys.direction);
            gl.uniform1f(this._uniforms.u_heat, phys.heat);
            gl.uniform1f(this._uniforms.u_heatScar, phys.heatScar);

            // ── Glow color based on side ──
            if (phys.side === 'bid') {
                gl.uniform3f(this._uniforms.u_glowColor, 0.0, 0.9, 0.8); // cyan
            } else {
                gl.uniform3f(this._uniforms.u_glowColor, 1.0, 0.23, 0.36); // red
            }

            // ── Price label (center column) ──
            const priceStr = _fmtPrice(price);
            const priceColor = isCurrentPrice
                ? [1.0, 0.86, 0.2]   // yellow
                : [0.65, 0.7, 0.82]; // default silver
            gl.uniform3fv(this._uniforms.u_textColor, priceColor);
            this._drawString(priceStr, priceLeft + 4, y - 5, 'left');

            // ── Bid size label ──
            if (bidSize > 0) {
                const norm = Math.min(bidSize / maxDepth, 1.0);
                const barW = norm * (bidBarRight - 32);
                const barX = bidBarRight - barW;
                gl.uniform3f(this._uniforms.u_textColor, 1.0, 1.0, 1.0);

                if (barW > 24) {
                    this._drawString(bidSize.toString(), barX + 4, y - 5, 'left');
                } else {
                    gl.uniform3f(this._uniforms.u_textColor, 0.0, 0.91, 0.48);
                    this._drawString(bidSize.toString(), barX - 3, y - 5, 'right');
                }
            }

            // ── Ask size label ──
            if (askSize > 0) {
                const norm = Math.min(askSize / maxDepth, 1.0);
                const barW = norm * (data.cssW - askBarLeft - 32);
                const barEnd = askBarLeft + barW;
                gl.uniform3f(this._uniforms.u_textColor, 1.0, 1.0, 1.0);

                if (barW > 24) {
                    this._drawString(askSize.toString(), barEnd - 4, y - 5, 'right');
                } else {
                    gl.uniform3f(this._uniforms.u_textColor, 1.0, 0.23, 0.36);
                    this._drawString(askSize.toString(), barEnd + 3, y - 5, 'left');
                }
            }
        }

        // ── Expire old physics entries ──
        for (const [key, phys] of this.state) {
            if (phys.elapsed() > 5.0) this.state.delete(key);
        }

        // ── Step PressureField solver ──
        if (hasPF) {
            PressureField.updateObstacles(
                bids, asks, visiblePrices, maxDepth
            );
            PressureField.update(dt);
            PressureField.render();
        }
    },

    /**
     * Draw a string of glyphs as individual textured quads.
     * Each glyph is a draw call with its own atlas rect.
     */
    _drawString(str, x, y, align) {
        const gl = this.gl;
        if (!gl || !this.glyphMap) return;

        // Measure total width for alignment
        let totalW = 0;
        const charWidths = [];
        for (const ch of str) {
            const g = this.glyphMap.get(ch);
            const w = g ? g.w : 6;
            charWidths.push(w);
            totalW += w;
        }

        let cursorX = x;
        if (align === 'right') cursorX = x - totalW;
        else if (align === 'center') cursorX = x - totalW / 2;

        const aOffset = gl.getAttribLocation(this.program, 'a_offset');
        const aSize = gl.getAttribLocation(this.program, 'a_size');
        const aAtlasRect = gl.getAttribLocation(this.program, 'a_atlasRect');

        for (let i = 0; i < str.length; i++) {
            const ch = str[i];
            const g = this.glyphMap.get(ch);
            if (!g) { cursorX += charWidths[i]; continue; }

            // Set per-glyph attributes as uniforms (simpler than instancing for small batch)
            // We use vertexAttrib* for non-instanced single-quad draws
            gl.disableVertexAttribArray(aOffset);
            gl.disableVertexAttribArray(aSize);
            gl.disableVertexAttribArray(aAtlasRect);
            gl.vertexAttrib2f(aOffset, cursorX, y);
            gl.vertexAttrib2f(aSize, g.w, g.h);
            gl.vertexAttrib4f(aAtlasRect, g.u0, g.v0, g.u1, g.v1);

            // Bind quad geometry
            gl.bindBuffer(gl.ARRAY_BUFFER, this._positionBuffer);
            const aPosLoc = gl.getAttribLocation(this.program, 'a_position');
            gl.enableVertexAttribArray(aPosLoc);
            gl.vertexAttribPointer(aPosLoc, 2, gl.FLOAT, false, 0, 0);

            gl.bindBuffer(gl.ARRAY_BUFFER, this._texCoordBuffer);
            const aUVLoc = gl.getAttribLocation(this.program, 'a_texCoord');
            gl.enableVertexAttribArray(aUVLoc);
            gl.vertexAttribPointer(aUVLoc, 2, gl.FLOAT, false, 0, 0);

            gl.drawArrays(gl.TRIANGLES, 0, 6);

            cursorX += charWidths[i];
        }
    },

    // ─────────────────────────────────────────────────────────────────────
    // 6. PUBLIC API
    // ─────────────────────────────────────────────────────────────────────

    /**
     * Feed ladder render data for the next frame.
     * Called from renderDepthLadder() hook.
     */
    setLadderData(data) {
        this._ladderData = data;
    },

    /**
     * Apply a shock to a price level from trade data.
     * Called when aggressive trades hit a resting level.
     */
    applyShock(priceStr, takeVolume, restingSize, avgVolPerLevel, side) {
        let phys = this.state.get(priceStr);
        if (!phys) {
            phys = new PhysicsRow();
            this.state.set(priceStr, phys);
        }
        phys.shock(takeVolume, restingSize, avgVolPerLevel, side);
    },

    /**
     * Process incoming trade data to generate shocks.
     * Called from the WebSocket snapshot handler.
     */
    processTrades(trades, bids, asks) {
        if (!trades || !trades.length) return;

        // Aggregate trades by price level (PRICE-SPECIFIC, not global)
        const tradesByPrice = new Map();
        let totalVolThisTick = 0;
        for (const trade of trades) {
            const p = trade.p !== undefined ? trade.p : trade.price;
            const v = trade.v !== undefined ? trade.v : trade.vol || trade.volume || 1;
            const s = trade.s !== undefined ? trade.s : trade.side || 'buy';
            if (p === undefined) continue;

            const pKey = parseFloat(p).toFixed(2);
            const existing = tradesByPrice.get(pKey) || { vol: 0, side: s };
            existing.vol += v;
            tradesByPrice.set(pKey, existing);
            totalVolThisTick += v;
        }

        // Update EWMA of average tick volume (smoothed denominator for normalization)
        if (tradesByPrice.size > 0) {
            const avgThisTick = totalVolThisTick / tradesByPrice.size;
            this._avgTickVolume = this._avgTickAlpha * avgThisTick
                + (1 - this._avgTickAlpha) * this._avgTickVolume;
        }

        // Generate price-specific shocks using normalized avg tick volume
        for (const [priceStr, data] of tradesByPrice) {
            const restingBid = bids[priceStr] || 0;
            const restingAsk = asks[priceStr] || 0;
            const resting = Math.max(restingBid, restingAsk, 1);
            const side = data.side === 'sell' || data.side === 's' ? 'ask' : 'bid';
            this.applyShock(priceStr, data.vol, resting, this._avgTickVolume, side);

            // ── Sigma-Driven Absorption Heat Scars ──
            const phys = this.state.get(priceStr);
            if (phys && typeof SigmaEngine !== 'undefined') {
                // Institutional peak: 3σ+ trade → white-hot + max displacement
                if (data.vol >= SigmaEngine.instThreshold) {
                    phys.heat = 1.0;
                    phys.heatScar = 1.0;
                    phys.shockForce = 1.0; // max log₁₀ displacement
                }

                // Absorption heat scar: high inertia → lock heat
                const absCheck = SigmaEngine.checkAbsorption(priceStr);
                if (absCheck.isAbsorb && absCheck.inertia > 3.0) {
                    phys.heat = 1.0;
                    phys.heatScar = 1.0;
                }
            }

            // Feed PressureField: empirical force injection
            if (typeof PressureField !== 'undefined' && PressureField._ready) {
                PressureField.injectForce(priceStr, data.vol, side);
            }
        }
    },

    /**
     * Rebuild the font atlas (e.g., after DPR change).
     */
    rebuildAtlas() {
        if (!this.gl) return;
        const atlas = _buildFontAtlas();
        this.atlas = atlas;
        this.glyphMap = atlas.glyphMap;
        if (this.atlasTexture) {
            this.gl.deleteTexture(this.atlasTexture);
        }
        this.gl.useProgram(this.program);
        this._uploadAtlas(atlas);
        console.log('[KineticText] Font atlas rebuilt for DPR', window.devicePixelRatio);
    },

    /**
     * Decay heat and thermal inertia. Called every 500ms.
     * Heat decay rate is overridden by KineticConfig.coolDown via monkey-patch.
     * Heat scars decay slower (0.97x) to create visual inertia.
     */
    _decayHeat() {
        for (const [, phys] of this.state) {
            // Record scar before decay: if heat was high, scar stays longer
            if (phys.heat > phys.heatScar) {
                phys.heatScar = phys.heat;
            }
            // Primary heat decays (rate overridden by HUD patch)
            phys.heat *= 0.90;
            if (phys.heat < 0.005) phys.heat = 0;
            // Scar decays slower — thermal inertia
            phys.heatScar *= 0.97;
            if (phys.heatScar < 0.005) phys.heatScar = 0;
        }
    },

    destroy() {
        this._destroyed = true;
        if (this._rafId) {
            cancelAnimationFrame(this._rafId);
            this._rafId = null;
        }
        if (this._heatDecayTimer) {
            clearInterval(this._heatDecayTimer);
            this._heatDecayTimer = null;
        }
        if (this.gl) {
            if (this.atlasTexture) this.gl.deleteTexture(this.atlasTexture);
            if (this._positionBuffer) this.gl.deleteBuffer(this._positionBuffer);
            if (this._texCoordBuffer) this.gl.deleteBuffer(this._texCoordBuffer);
            if (this.program) this.gl.deleteProgram(this.program);
            const ext = this.gl.getExtension('WEBGL_lose_context');
            if (ext) ext.loseContext();
        }
        this.state.clear();
        this._ladderData = null;
        this.gl = null;
        this.program = null;
        this.programValid = false;
        this._lastFrameTime = 0;
        console.log('[KineticText] Destroyed');
    },

    /**
     * Re-initialize after destroy (e.g., symbol switch).
     */
    restart() {
        this._destroyed = false;
        if (this._canvas) {
            this.init(this._canvas);
        }
    }
};

// ─────────────────────────────────────────────────────────────────────────────
// 7. HELPERS
// ─────────────────────────────────────────────────────────────────────────────

function _fmtPrice(p) {
    const parts = p.toFixed(2).split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.join('.');
}

// ─────────────────────────────────────────────────────────────────────────────
// 8. EXPORTS
// ─────────────────────────────────────────────────────────────────────────────

window.KineticText = KineticText;
