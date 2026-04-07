// ═══════════════════════════════════════════════════════════════════════════════
// PRESSURE FIELD — 2D Navier-Stokes GPGPU Microstructure Pressure Solver
// ═══════════════════════════════════════════════════════════════════════════════
//
// Grid-based fluid simulation mapped 1:1 to L2 order book rows.
// All fluid quantities are empirically derived — zero magic numbers.
//
// Pipeline: Splat → Advect → Divergence → Jacobi(×20) → GradientSub → Render
//
// Shared state: reads KineticConfig for master sensitivity.
// Canvas: dom-pressure-canvas (sandwiched between ladder & kinetic text)
// ═══════════════════════════════════════════════════════════════════════════════

'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// 1. GLSL SHADERS
// ─────────────────────────────────────────────────────────────────────────────

// Shared fullscreen quad vertex shader — all passes use this
const PF_VERT = `#version 300 es
precision highp float;
in vec2 a_position;
out vec2 v_uv;
void main() {
    v_uv = a_position * 0.5 + 0.5;
    gl_Position = vec4(a_position, 0.0, 1.0);
}
`;

// ── SPLAT: Inject Gaussian force impulse at a trade price ──
// Input: current velocity texture
// Output: velocity + Gaussian impulse at u_point
const PF_SPLAT_FRAG = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_velocity;
uniform vec2 u_point;       // normalized (x, y) splat center
uniform vec2 u_force;       // (fx, fy) force vector — log10(volume) scaled
uniform float u_radius;     // Gaussian radius in UV space
out vec4 fragColor;
void main() {
    vec2 vel = texture(u_velocity, v_uv).xy;
    vec2 d = v_uv - u_point;
    float dist2 = dot(d, d);
    float gauss = exp(-dist2 / (2.0 * u_radius * u_radius));
    vel += u_force * gauss;
    fragColor = vec4(vel, 0.0, 1.0);
}
`;

// ── ADVECT: Semi-Lagrangian advection of velocity field ──
// Traces backwards along velocity, samples old value
// Respects obstacle texture (Dirichlet boundary: u,v = 0 where obstacle > 0.8)
const PF_ADVECT_FRAG = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_velocity;
uniform sampler2D u_obstacles;
uniform float u_dt;
uniform vec2 u_texelSize;    // 1.0 / gridSize
uniform float u_dissipation; // velocity decay (0.99 typical)
out vec4 fragColor;
void main() {
    float obs = texture(u_obstacles, v_uv).r;
    // Dirichlet boundary: solid obstacle kills velocity
    if (obs > 0.8) {
        fragColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    // Semi-Lagrangian: trace backwards
    vec2 vel = texture(u_velocity, v_uv).xy;
    vec2 prevUV = v_uv - vel * u_texelSize * u_dt;
    // Clamp to grid bounds
    prevUV = clamp(prevUV, vec2(0.0), vec2(1.0));
    // Sample from previous position
    vec2 advected = texture(u_velocity, prevUV).xy;
    // Viscosity modulation: vacuum (low obstacle) = lower friction
    float viscMod = mix(u_dissipation, u_dissipation * 0.95, obs);
    fragColor = vec4(advected * viscMod, 0.0, 1.0);
}
`;

// ── DIVERGENCE: Compute ∇·u ──
// Central difference on the velocity field
const PF_DIVERGENCE_FRAG = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_velocity;
uniform sampler2D u_obstacles;
uniform vec2 u_texelSize;
out vec4 fragColor;
void main() {
    // Neighbor velocities (central differences)
    float uR = texture(u_velocity, v_uv + vec2(u_texelSize.x, 0.0)).x;
    float uL = texture(u_velocity, v_uv - vec2(u_texelSize.x, 0.0)).x;
    float vT = texture(u_velocity, v_uv + vec2(0.0, u_texelSize.y)).y;
    float vB = texture(u_velocity, v_uv - vec2(0.0, u_texelSize.y)).y;
    // Enforce zero at obstacles (solid boundary)
    float obsR = texture(u_obstacles, v_uv + vec2(u_texelSize.x, 0.0)).r;
    float obsL = texture(u_obstacles, v_uv - vec2(u_texelSize.x, 0.0)).r;
    float obsT = texture(u_obstacles, v_uv + vec2(0.0, u_texelSize.y)).r;
    float obsB = texture(u_obstacles, v_uv - vec2(0.0, u_texelSize.y)).r;
    if (obsR > 0.8) uR = 0.0;
    if (obsL > 0.8) uL = 0.0;
    if (obsT > 0.8) vT = 0.0;
    if (obsB > 0.8) vB = 0.0;
    // ∇·u = (∂u/∂x + ∂v/∂y) / 2
    float div = 0.5 * ((uR - uL) + (vT - vB));
    fragColor = vec4(div, 0.0, 0.0, 1.0);
}
`;

// ── JACOBI: Iterative pressure solve (∇²P = ∇·u) ──
// Run exactly 20 iterations per frame
const PF_JACOBI_FRAG = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_pressure;
uniform sampler2D u_divergence;
uniform sampler2D u_obstacles;
uniform vec2 u_texelSize;
out vec4 fragColor;
void main() {
    float obs = texture(u_obstacles, v_uv).r;
    if (obs > 0.8) {
        fragColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    // Sample neighboring pressure values
    float pR = texture(u_pressure, v_uv + vec2(u_texelSize.x, 0.0)).r;
    float pL = texture(u_pressure, v_uv - vec2(u_texelSize.x, 0.0)).r;
    float pT = texture(u_pressure, v_uv + vec2(0.0, u_texelSize.y)).r;
    float pB = texture(u_pressure, v_uv - vec2(0.0, u_texelSize.y)).r;
    // Enforce Neumann boundary at obstacles (pressure = neighbor)
    float obsR = texture(u_obstacles, v_uv + vec2(u_texelSize.x, 0.0)).r;
    float obsL = texture(u_obstacles, v_uv - vec2(u_texelSize.x, 0.0)).r;
    float obsT = texture(u_obstacles, v_uv + vec2(0.0, u_texelSize.y)).r;
    float obsB = texture(u_obstacles, v_uv - vec2(0.0, u_texelSize.y)).r;
    float pC = texture(u_pressure, v_uv).r;
    if (obsR > 0.8) pR = pC;
    if (obsL > 0.8) pL = pC;
    if (obsT > 0.8) pT = pC;
    if (obsB > 0.8) pB = pC;
    // Jacobi iteration: P_new = (P_neighbors - divergence) / 4
    float div = texture(u_divergence, v_uv).r;
    float pNew = (pL + pR + pB + pT - div) * 0.25;
    fragColor = vec4(pNew, 0.0, 0.0, 1.0);
}
`;

// ── GRADIENT SUBTRACTION: Make velocity divergence-free ──
// v_new = v - ∇P (projection step)
const PF_GRADSUB_FRAG = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_velocity;
uniform sampler2D u_pressure;
uniform sampler2D u_obstacles;
uniform vec2 u_texelSize;
out vec4 fragColor;
void main() {
    float obs = texture(u_obstacles, v_uv).r;
    if (obs > 0.8) {
        fragColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    // Pressure gradient (central difference)
    float pR = texture(u_pressure, v_uv + vec2(u_texelSize.x, 0.0)).r;
    float pL = texture(u_pressure, v_uv - vec2(u_texelSize.x, 0.0)).r;
    float pT = texture(u_pressure, v_uv + vec2(0.0, u_texelSize.y)).r;
    float pB = texture(u_pressure, v_uv - vec2(0.0, u_texelSize.y)).r;
    // ∇P
    vec2 gradP = 0.5 * vec2(pR - pL, pT - pB);
    // Subtract gradient from velocity → divergence-free
    vec2 vel = texture(u_velocity, v_uv).xy;
    vel -= gradP;
    fragColor = vec4(vel, 0.0, 1.0);
}
`;

// ── RENDER: Visualize pressure shimmer + velocity direction ──
// No "pretty colors" — functional vector field visualization
const PF_RENDER_FRAG = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_velocity;
uniform sampler2D u_pressure;
uniform sampler2D u_obstacles;
uniform float u_masterSensitivity;
out vec4 fragColor;
void main() {
    vec2 vel = texture(u_velocity, v_uv).xy;
    float pressure = texture(u_pressure, v_uv).r;
    float obstacle = texture(u_obstacles, v_uv).r;

    // ── Background: Pressure-driven brightness ──
    // High pressure = high friction = brighter glow
    float pMag = abs(pressure) * u_masterSensitivity;
    float brightness = clamp(pMag * 2.0, 0.0, 1.0);

    // Pressure color ramp: dark → cyan → white
    vec3 pColor = vec3(0.0);
    if (brightness > 0.01) {
        vec3 dim = vec3(0.0, 0.08, 0.12);    // deep blue-black
        vec3 mid = vec3(0.0, 0.6, 0.8);      // cyan
        vec3 hot = vec3(0.9, 0.95, 1.0);     // near-white
        if (brightness < 0.5) {
            pColor = mix(dim, mid, brightness * 2.0);
        } else {
            pColor = mix(mid, hot, (brightness - 0.5) * 2.0);
        }
    }

    // ── Vector Field: Velocity magnitude → directional encoding ──
    float speed = length(vel) * u_masterSensitivity;
    float speedNorm = clamp(speed * 4.0, 0.0, 1.0);

    // Encode velocity direction as color hue:
    // Up (buy) = green channel, Down (sell) = red channel
    vec3 velColor = vec3(0.0);
    if (speedNorm > 0.01) {
        float upness = clamp(vel.y * 4.0, 0.0, 1.0);   // buy pressure
        float downness = clamp(-vel.y * 4.0, 0.0, 1.0); // sell pressure
        velColor = vec3(downness * 0.8, upness * 0.9, speedNorm * 0.3);
    }

    // ── Arrow grid pattern ──
    // Create a repeating arrow grid to show flow direction
    vec2 cell = fract(v_uv * vec2(16.0, 50.0)); // 16 columns, 1 per price row
    vec2 cellCenter = cell - 0.5;
    // Arrow body: thin line in velocity direction
    vec2 velDir = speed > 0.001 ? normalize(vel) : vec2(0.0);
    float arrowBody = 1.0 - smoothstep(0.0, 0.08, abs(dot(cellCenter, vec2(-velDir.y, velDir.x))));
    // Arrow only shows where there is flow
    float arrowMask = arrowBody * speedNorm * 0.6;

    // ── Obstacle visualization: dim where walls exist ──
    float obsDim = 1.0 - obstacle * 0.5;

    // ── Composite ──
    vec3 color = pColor + velColor * 0.5;
    color += vec3(arrowMask * 0.3);
    color *= obsDim;

    float alpha = clamp(brightness * 0.7 + speedNorm * 0.4 + arrowMask * 0.3, 0.0, 0.85);
    fragColor = vec4(color, alpha);
}
`;

// ─────────────────────────────────────────────────────────────────────────────
// 2. GPGPU ENGINE
// ─────────────────────────────────────────────────────────────────────────────

const JACOBI_ITERATIONS = 20;
const GRID_WIDTH = 64;  // Fixed horizontal resolution
const DISSIPATION_DEFAULT = 0.985; // Fallback when SigmaEngine unavailable
const SPLAT_RADIUS = 0.02; // UV-space Gaussian radius

const PressureField = {
    gl: null,
    _canvas: null,
    _ready: false,
    _destroyed: false,

    // ── Grid dimensions ──
    _gridW: GRID_WIDTH,
    _gridH: 50, // Updated to match visible price rows

    // ── Ping-pong textures (Float32) ──
    _velocityTex: [null, null],
    _velocityFB: [null, null],
    _pressureTex: [null, null],
    _pressureFB: [null, null],
    _divergenceTex: null,
    _divergenceFB: null,
    _obstacleTex: null,
    _obstacleFB: null,
    _velRead: 0,  // ping-pong index

    // ── Shader programs ──
    _splatProg: null,
    _advectProg: null,
    _divProg: null,
    _jacobiProg: null,
    _gradSubProg: null,
    _renderProg: null,

    // ── Geometry ──
    _quadVAO: null,
    _quadVBO: null,

    // ── Price row mapping ──
    _priceToRow: new Map(),  // price string → row index (0..gridH-1)
    _visiblePrices: [],

    // ── Absorption data (fed from SigmaEngine) ──
    _absorptionData: {},     // { priceStr: { agg_vol, intensity, score, side } }

    // ─────────────────────────────────────────────────────────────────────
    // INIT
    // ─────────────────────────────────────────────────────────────────────

    init(canvas) {
        this._canvas = canvas;
        const gl = canvas.getContext('webgl2', {
            alpha: true,
            premultipliedAlpha: false,
            antialias: false,
            preserveDrawingBuffer: false,
        });
        if (!gl) {
            console.error('[PressureField] WebGL2 not available');
            return;
        }
        this.gl = gl;

        // Check Float32 render target support
        const extFloat = gl.getExtension('EXT_color_buffer_float');
        if (!extFloat) {
            console.warn('[PressureField] EXT_color_buffer_float not available, using HALF_FLOAT');
        }

        gl.getExtension('OES_texture_float_linear');

        // ── Compile all shader programs ──
        this._splatProg = this._createProgram(PF_VERT, PF_SPLAT_FRAG);
        this._advectProg = this._createProgram(PF_VERT, PF_ADVECT_FRAG);
        this._divProg = this._createProgram(PF_VERT, PF_DIVERGENCE_FRAG);
        this._jacobiProg = this._createProgram(PF_VERT, PF_JACOBI_FRAG);
        this._gradSubProg = this._createProgram(PF_VERT, PF_GRADSUB_FRAG);
        this._renderProg = this._createProgram(PF_VERT, PF_RENDER_FRAG);

        if (!this._splatProg || !this._advectProg || !this._divProg ||
            !this._jacobiProg || !this._gradSubProg || !this._renderProg) {
            console.error('[PressureField] Shader compilation failed');
            return;
        }

        // ── Fullscreen quad ──
        this._setupQuad();

        // ── Allocate textures + framebuffers ──
        this._allocateTextures(this._gridW, this._gridH);

        this._ready = true;
        console.log('[PressureField] Initialized', this._gridW, '×', this._gridH);
    },

    // ─────────────────────────────────────────────────────────────────────
    // PUBLIC API
    // ─────────────────────────────────────────────────────────────────────

    /**
     * Inject a Gaussian force impulse from an aggressive trade.
     * @param {string} priceStr - price key (e.g. "24250.00")
     * @param {number} volume - trade volume (lots)
     * @param {string} side - 'bid'/'buy' or 'ask'/'sell'
     */
    injectForce(priceStr, volume, side) {
        if (!this._ready) return;

        // ── Sigma-driven splat gate: reject noise trades ──
        if (typeof SigmaEngine !== 'undefined' && volume < SigmaEngine.noiseFloor) return;

        const row = this._priceToRow.get(priceStr);
        if (row === undefined) return;

        const gl = this.gl;
        const prog = this._splatProg;
        gl.useProgram(prog);

        // Normalized splat position: center X, row Y
        const px = 0.5;
        const py = (row + 0.5) / this._gridH;

        // Force = log10(volume) — the No-Guessing rule
        const impact = (typeof KineticConfig !== 'undefined') ? KineticConfig.impact : 1.0;
        const forceMag = Math.log10(Math.max(volume, 1)) * impact * 0.1;

        // Direction: buy = upward (+Y), sell = downward (-Y)
        const isBuy = (side === 'bid' || side === 'buy');
        const fx = 0;
        const fy = isBuy ? forceMag : -forceMag;

        gl.uniform2f(gl.getUniformLocation(prog, 'u_point'), px, py);
        gl.uniform2f(gl.getUniformLocation(prog, 'u_force'), fx, fy);
        gl.uniform1f(gl.getUniformLocation(prog, 'u_radius'), SPLAT_RADIUS);

        // Bind current velocity as input, write to other
        const src = this._velRead;
        const dst = 1 - src;
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this._velocityTex[src]);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_velocity'), 0);

        gl.bindFramebuffer(gl.FRAMEBUFFER, this._velocityFB[dst]);
        gl.viewport(0, 0, this._gridW, this._gridH);
        this._drawQuad();
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);

        this._velRead = dst;
    },

    /**
     * Update obstacle texture from L2 book data.
     * Bid sizes → left half obstacles, Ask sizes → right half obstacles.
     * @param {Object} bids - { priceStr: size }
     * @param {Object} asks - { priceStr: size }
     * @param {Array} visiblePrices - array of visible price numbers
     * @param {number} maxDepth - max depth for normalization
     */
    updateObstacles(bids, asks, visiblePrices, maxDepth) {
        if (!this._ready) return;

        const gl = this.gl;
        const w = this._gridW;
        const h = visiblePrices.length;

        // Resize if row count changed
        if (h !== this._gridH && h > 0) {
            this._gridH = h;
            this._allocateTextures(w, h);
        }

        // Update price-to-row mapping
        this._priceToRow.clear();
        this._visiblePrices = visiblePrices;
        for (let i = 0; i < visiblePrices.length; i++) {
            const pKey = parseFloat(visiblePrices[i]).toFixed(2);
            this._priceToRow.set(pKey, i);
        }

        // Build obstacle data: Float32 per texel
        const halfW = Math.floor(w / 2);
        const data = new Float32Array(w * h * 4); // RGBA
        const md = Math.max(maxDepth, 1);

        for (let row = 0; row < h; row++) {
            const price = visiblePrices[row];
            const pKey2 = parseFloat(price).toFixed(2);
            const pKey = price.toString();
            const bidSz = bids[pKey2] || bids[pKey] || 0;
            const askSz = asks[pKey2] || asks[pKey] || 0;

            const bidNorm = Math.min(bidSz / md, 1.0);
            const askNorm = Math.min(askSz / md, 1.0);

            for (let x = 0; x < w; x++) {
                const idx = (row * w + x) * 4;
                // Left half = bid obstacle, right half = ask obstacle
                data[idx] = (x < halfW) ? bidNorm : askNorm;
                data[idx + 1] = 0;
                data[idx + 2] = 0;
                data[idx + 3] = 1;
            }
        }

        // Upload to obstacle texture
        gl.bindTexture(gl.TEXTURE_2D, this._obstacleTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, w, h, 0, gl.RGBA, gl.FLOAT, data);
    },

    /**
     * Step the full Navier-Stokes solver.
     * Called once per frame from the rAF loop.
     * @param {number} dt - frame delta in seconds
     */
    update(dt) {
        if (!this._ready) return;

        const gl = this.gl;
        const w = this._gridW;
        const h = this._gridH;
        const texelSize = [1.0 / w, 1.0 / h];

        gl.viewport(0, 0, w, h);

        // ── Pass 1: ADVECTION ──
        this._advect(texelSize, dt);

        // ── Pass 2: DIVERGENCE ──
        this._divergence(texelSize);

        // ── Pass 3: JACOBI PRESSURE SOLVE (×20) ──
        this._solvePressure(texelSize);

        // ── Pass 4: GRADIENT SUBTRACTION ──
        this._gradientSub(texelSize);
    },

    /**
     * Render the pressure/velocity field to the display canvas.
     */
    render() {
        if (!this._ready) return;
        // PressureField only renders on heatmap pane (DOM depth view)
        if (window._activeChartFeature && window._activeChartFeature !== 'heatmap') return;

        const gl = this.gl;
        const canvas = this._canvas;
        const dpr = window.devicePixelRatio || 1;

        // Resize canvas to match container
        const parent = canvas.parentElement;
        if (!parent) return;
        const cssW = parent.clientWidth;
        const cssH = parent.clientHeight;
        const pxW = Math.round(cssW * dpr);
        const pxH = Math.round(cssH * dpr);

        if (canvas.width !== pxW || canvas.height !== pxH) {
            canvas.width = pxW;
            canvas.height = pxH;
        }

        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, pxW, pxH);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);

        const prog = this._renderProg;
        gl.useProgram(prog);

        // Bind textures
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this._velocityTex[this._velRead]);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_velocity'), 0);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this._pressureTex[0]);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_pressure'), 1);

        gl.activeTexture(gl.TEXTURE2);
        gl.bindTexture(gl.TEXTURE_2D, this._obstacleTex);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_obstacles'), 2);

        const sensitivity = (typeof KineticConfig !== 'undefined') ? KineticConfig.impact : 1.0;
        gl.uniform1f(gl.getUniformLocation(prog, 'u_masterSensitivity'), sensitivity);

        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
        this._drawQuad();
        gl.disable(gl.BLEND);
    },

    // ─────────────────────────────────────────────────────────────────────
    // SOLVER PASSES (PRIVATE)
    // ─────────────────────────────────────────────────────────────────────

    _advect(texelSize, dt) {
        const gl = this.gl;
        const prog = this._advectProg;
        gl.useProgram(prog);

        const src = this._velRead;
        const dst = 1 - src;

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this._velocityTex[src]);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_velocity'), 0);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this._obstacleTex);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_obstacles'), 1);

        gl.uniform1f(gl.getUniformLocation(prog, 'u_dt'), dt);
        gl.uniform2f(gl.getUniformLocation(prog, 'u_texelSize'), texelSize[0], texelSize[1]);

        // ── Delta-Decay: e^{-Δt · σ} replaces fixed DISSIPATION ──
        const sigma = (typeof SigmaEngine !== 'undefined') ? SigmaEngine.marketVolatility : 1.0;
        let dissipation = Math.max(Math.exp(-dt * sigma * 2.0), 0.5);
        // Low-latency override: force faster dissipation
        if (this._dissipationOverride) dissipation = Math.min(dissipation, this._dissipationOverride);
        gl.uniform1f(gl.getUniformLocation(prog, 'u_dissipation'), dissipation);

        gl.bindFramebuffer(gl.FRAMEBUFFER, this._velocityFB[dst]);
        this._drawQuad();
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);

        this._velRead = dst;
    },

    _divergence(texelSize) {
        const gl = this.gl;
        const prog = this._divProg;
        gl.useProgram(prog);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this._velocityTex[this._velRead]);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_velocity'), 0);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this._obstacleTex);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_obstacles'), 1);

        gl.uniform2f(gl.getUniformLocation(prog, 'u_texelSize'), texelSize[0], texelSize[1]);

        gl.bindFramebuffer(gl.FRAMEBUFFER, this._divergenceFB);
        this._drawQuad();
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    },

    _solvePressure(texelSize) {
        const gl = this.gl;
        const prog = this._jacobiProg;
        gl.useProgram(prog);

        gl.uniform2f(gl.getUniformLocation(prog, 'u_texelSize'), texelSize[0], texelSize[1]);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this._divergenceTex);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_divergence'), 1);

        gl.activeTexture(gl.TEXTURE2);
        gl.bindTexture(gl.TEXTURE_2D, this._obstacleTex);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_obstacles'), 2);

        // Jacobi iterations: default 20, low-latency override cuts to 10
        const iterations = this._jacobiOverride || JACOBI_ITERATIONS;
        let pRead = 0;
        for (let i = 0; i < iterations; i++) {
            const pWrite = 1 - pRead;

            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, this._pressureTex[pRead]);
            gl.uniform1i(gl.getUniformLocation(prog, 'u_pressure'), 0);

            gl.bindFramebuffer(gl.FRAMEBUFFER, this._pressureFB[pWrite]);
            this._drawQuad();

            pRead = pWrite;
        }
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);

        // Ensure the "current" pressure is in slot 0 for reading
        // Swap references if needed
        if (pRead !== 0) {
            [this._pressureTex[0], this._pressureTex[1]] = [this._pressureTex[1], this._pressureTex[0]];
            [this._pressureFB[0], this._pressureFB[1]] = [this._pressureFB[1], this._pressureFB[0]];
        }
    },

    _gradientSub(texelSize) {
        const gl = this.gl;
        const prog = this._gradSubProg;
        gl.useProgram(prog);

        const src = this._velRead;
        const dst = 1 - src;

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this._velocityTex[src]);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_velocity'), 0);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this._pressureTex[0]);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_pressure'), 1);

        gl.activeTexture(gl.TEXTURE2);
        gl.bindTexture(gl.TEXTURE_2D, this._obstacleTex);
        gl.uniform1i(gl.getUniformLocation(prog, 'u_obstacles'), 2);

        gl.uniform2f(gl.getUniformLocation(prog, 'u_texelSize'), texelSize[0], texelSize[1]);

        gl.bindFramebuffer(gl.FRAMEBUFFER, this._velocityFB[dst]);
        this._drawQuad();
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);

        this._velRead = dst;
    },

    // ─────────────────────────────────────────────────────────────────────
    // WEBGL HELPERS
    // ─────────────────────────────────────────────────────────────────────

    _setupQuad() {
        const gl = this.gl;
        // Fullscreen quad: two triangles covering [-1,1]
        const verts = new Float32Array([
            -1, -1,  1, -1,  -1, 1,
            -1,  1,  1, -1,   1, 1,
        ]);
        this._quadVAO = gl.createVertexArray();
        gl.bindVertexArray(this._quadVAO);

        this._quadVBO = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this._quadVBO);
        gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(0);
        gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

        gl.bindVertexArray(null);
    },

    _drawQuad() {
        const gl = this.gl;
        gl.bindVertexArray(this._quadVAO);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
        gl.bindVertexArray(null);
    },

    _createProgram(vertSrc, fragSrc) {
        const gl = this.gl;
        const vs = gl.createShader(gl.VERTEX_SHADER);
        gl.shaderSource(vs, vertSrc);
        gl.compileShader(vs);
        if (!gl.getShaderParameter(vs, gl.COMPILE_STATUS)) {
            console.error('[PressureField] Vertex shader error:', gl.getShaderInfoLog(vs));
            gl.deleteShader(vs);
            return null;
        }

        const fs = gl.createShader(gl.FRAGMENT_SHADER);
        gl.shaderSource(fs, fragSrc);
        gl.compileShader(fs);
        if (!gl.getShaderParameter(fs, gl.COMPILE_STATUS)) {
            console.error('[PressureField] Fragment shader error:', gl.getShaderInfoLog(fs));
            gl.deleteShader(vs);
            gl.deleteShader(fs);
            return null;
        }

        const prog = gl.createProgram();
        gl.attachShader(prog, vs);
        gl.attachShader(prog, fs);
        gl.linkProgram(prog);
        gl.deleteShader(vs);
        gl.deleteShader(fs);

        if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
            console.error('[PressureField] Link error:', gl.getProgramInfoLog(prog));
            gl.deleteProgram(prog);
            return null;
        }
        return prog;
    },

    _createFloat32Texture(w, h) {
        const gl = this.gl;
        const tex = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, w, h, 0, gl.RGBA, gl.FLOAT, null);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        return tex;
    },

    _createFramebuffer(tex) {
        const gl = this.gl;
        const fb = gl.createFramebuffer();
        gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
        const status = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
        if (status !== gl.FRAMEBUFFER_COMPLETE) {
            console.error('[PressureField] Framebuffer incomplete:', status);
        }
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        return fb;
    },

    _allocateTextures(w, h) {
        const gl = this.gl;

        // Clean up old textures
        const oldTex = [
            ...this._velocityTex, ...this._pressureTex,
            this._divergenceTex, this._obstacleTex
        ];
        for (const t of oldTex) {
            if (t) gl.deleteTexture(t);
        }
        const oldFB = [
            ...this._velocityFB, ...this._pressureFB,
            this._divergenceFB, this._obstacleFB
        ];
        for (const f of oldFB) {
            if (f) gl.deleteFramebuffer(f);
        }

        // Velocity (RG = u, v) — ping-pong pair
        this._velocityTex[0] = this._createFloat32Texture(w, h);
        this._velocityTex[1] = this._createFloat32Texture(w, h);
        this._velocityFB[0] = this._createFramebuffer(this._velocityTex[0]);
        this._velocityFB[1] = this._createFramebuffer(this._velocityTex[1]);

        // Pressure (R = P) — ping-pong pair
        this._pressureTex[0] = this._createFloat32Texture(w, h);
        this._pressureTex[1] = this._createFloat32Texture(w, h);
        this._pressureFB[0] = this._createFramebuffer(this._pressureTex[0]);
        this._pressureFB[1] = this._createFramebuffer(this._pressureTex[1]);

        // Divergence (R = ∇·u) — single
        this._divergenceTex = this._createFloat32Texture(w, h);
        this._divergenceFB = this._createFramebuffer(this._divergenceTex);

        // Obstacles (R = boundary, 0–1) — single
        this._obstacleTex = this._createFloat32Texture(w, h);
        this._obstacleFB = this._createFramebuffer(this._obstacleTex);

        this._velRead = 0;

        console.log('[PressureField] Textures allocated:', w, '×', h,
            '(' + (w * h * 4 * 4 * 8 / 1024).toFixed(1) + ' KB)');
    },

    // ─────────────────────────────────────────────────────────────────────
    // LIFECYCLE
    // ─────────────────────────────────────────────────────────────────────

    destroy() {
        this._destroyed = true;
        this._ready = false;
        if (this.gl) {
            const ext = this.gl.getExtension('WEBGL_lose_context');
            if (ext) ext.loseContext();
        }
        this.gl = null;
        console.log('[PressureField] Destroyed');
    },

    /**
     * Receive absorption data from backend.
     * Absorption levels with high intensity inject continuous pressure.
     * @param {Object} absData - { priceStr: { score, intensity, agg_vol, side, ... } }
     */
    feedAbsorption(absData) {
        if (!this._ready || !absData) return;
        this._absorptionData = absData;

        // Inject continuous force at high-intensity absorption levels
        for (const [priceStr, abs] of Object.entries(absData)) {
            if (!abs || abs.score < 1.0) continue;

            const row = this._priceToRow.get(priceStr);
            if (row === undefined) continue;

            const gl = this.gl;
            const prog = this._splatProg;
            gl.useProgram(prog);

            // Position: center X, row Y
            const px = 0.5;
            const py = (row + 0.5) / this._gridH;

            // Force proportional to intensity (hits per second)
            const intensity = abs.intensity || 0;
            const forceMag = Math.min(intensity * 0.05, 0.3);
            // Direction: bid absorption = sell pressure (-Y), ask absorption = buy pressure (+Y)
            const fy = (abs.side === 'bid') ? -forceMag : forceMag;

            gl.uniform2f(gl.getUniformLocation(prog, 'u_point'), px, py);
            gl.uniform2f(gl.getUniformLocation(prog, 'u_force'), 0, fy);
            gl.uniform1f(gl.getUniformLocation(prog, 'u_radius'), SPLAT_RADIUS * 1.5);

            const src = this._velRead;
            const dst = 1 - src;
            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, this._velocityTex[src]);
            gl.uniform1i(gl.getUniformLocation(prog, 'u_velocity'), 0);

            gl.bindFramebuffer(gl.FRAMEBUFFER, this._velocityFB[dst]);
            gl.viewport(0, 0, this._gridW, this._gridH);
            this._drawQuad();
            gl.bindFramebuffer(gl.FRAMEBUFFER, null);

            this._velRead = dst;
        }
    },
};

window.PressureField = PressureField;

// ── Auto-init: wait for DOM, find canvas (max 10 retries) ──
let _pfInitRetries = 0;
function _initPressureField() {
    const canvas = document.getElementById('dom-pressure-canvas');
    if (!canvas) {
        _pfInitRetries++;
        if (_pfInitRetries <= 10) {
            setTimeout(_initPressureField, 500);
        } else {
            console.log('[PressureField] Canvas not in current layout — skipping init');
        }
        return;
    }
    if (!PressureField._ready) {
        PressureField.init(canvas);
        PressureField.monitorPerformance();
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(_initPressureField, 300));
} else {
    setTimeout(_initPressureField, 300);
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAC MINI GPU PERFORMANCE MONITOR + AUTO-THROTTLE
// ═══════════════════════════════════════════════════════════════════════════════
//
// Built directly into PressureField for zero-config production use.
//
// Console API:
//   startQuantAudit()            — verbose FPS + sigma logging (manual)
//   stopQuantAudit()             — stop verbose logging
//   setLowLatencyMode(true)      — force low-latency (manual override)
//   setLowLatencyMode(false)     — force high-fidelity
//
// Auto-throttle runs silently in background after PressureField.init().
// ═══════════════════════════════════════════════════════════════════════════════

// ── Auto-Throttle (always-on, silent) ──
PressureField.monitorPerformance = function() {
    if (this._perfMonitorActive) return;
    this._perfMonitorActive = true;

    let frameCount = 0;
    let startTime = performance.now();
    const self = this;

    const check = () => {
        frameCount++;
        const now = performance.now();

        if (now - startTime >= 1000) {
            const fps = frameCount;

            // Auto-throttle: FPS < 55 → cut Jacobi in half
            if (fps < 55 && (!self._jacobiOverride || self._jacobiOverride > 10)) {
                self._jacobiOverride = 10;
                self._dissipationOverride = 0.92;
                self._lowLatencyMode = true;
                console.warn(`%c ⚠️ AUTO-THROTTLE: FPS=${fps} → Jacobi 20→10`, 'color: #ffaa00; font-weight: bold;');
            }
            // Auto-restore: FPS ≥ 60 → restore full Navier-Stokes
            else if (fps >= 60 && self._lowLatencyMode && !self._manualLowLatency) {
                self._jacobiOverride = null;
                self._dissipationOverride = null;
                self._lowLatencyMode = false;
                console.log(`%c ✅ AUTO-RESTORE: FPS=${fps} → Jacobi 10→20 (full fidelity)`, 'color: #00ff00;');
            }

            frameCount = 0;
            startTime = now;
        }
        requestAnimationFrame(check);
    };
    check();
};

// ── Verbose Quant Audit (console logging, manual) ──
let _quantAuditRAF = null;

function startQuantAudit() {
    if (_quantAuditRAF) {
        console.log('[QuantAudit] Already running');
        return;
    }

    let lastT = performance.now();
    let frames = 0;

    console.log('%c 🕵️‍♂️ MONITORING MAC MINI GPU IMPACT...', 'color: #00ff00; font-weight: bold;');

    const audit = () => {
        const now = performance.now();
        const delta = now - lastT;
        frames++;

        if (delta >= 1000) {
            const fps = Math.round((frames * 1000) / delta);
            const frameTime = (1000 / fps).toFixed(2);

            const statusColor = fps > 58 ? 'color: #00ff00' : 'color: #ff4444';
            const message = fps > 58 ? 'STABLE (Quant Grade)' : 'LAG DETECTED (GPU Throttled)';
            const mode = PressureField._lowLatencyMode ? '⚡LOW-LAT' : '🎯HI-FI';

            console.log(`%c [${new Date().toLocaleTimeString()}] FPS: ${fps} | ${frameTime}ms | ${mode} | Jacobi: ${PressureField._jacobiOverride || 20} | ${message}`, statusColor);

            // Sigma Engine stats
            if (typeof SigmaEngine !== 'undefined' && SigmaEngine.marketVolatility > 0) {
                console.log(
                    `%c   σ=${SigmaEngine.marketVolatility.toFixed(4)} | NoiseGate=${SigmaEngine.noiseFloor.toFixed(2)} | InstThresh=${SigmaEngine.instThreshold.toFixed(1)} | Samples=${SigmaEngine._tradeVols.length}`,
                    'color: #888'
                );
            }

            frames = 0;
            lastT = now;
        }
        _quantAuditRAF = requestAnimationFrame(audit);
    };
    audit();
}

function stopQuantAudit() {
    if (_quantAuditRAF) {
        cancelAnimationFrame(_quantAuditRAF);
        _quantAuditRAF = null;
        console.log('[QuantAudit] Stopped');
    }
}

window.startQuantAudit = startQuantAudit;
window.stopQuantAudit = stopQuantAudit;

// ── MM Safety Switch (manual override) ──
function setLowLatencyMode(active = true) {
    if (!window.PressureField) {
        console.warn('[LowLatency] PressureField not available');
        return;
    }

    if (active) {
        PressureField._lowLatencyMode = true;
        PressureField._manualLowLatency = true;   // Prevent auto-restore
        PressureField._jacobiOverride = 10;
        PressureField._dissipationOverride = 0.92;
        console.warn('%c ⚠️ LOW LATENCY MODE ACTIVE: Physics simplified for speed.', 'color: #ffaa00; font-weight: bold;');
    } else {
        PressureField._lowLatencyMode = false;
        PressureField._manualLowLatency = false;
        PressureField._jacobiOverride = null;
        PressureField._dissipationOverride = null;
        console.log('%c ✅ HIGH FIDELITY MODE: Full Navier-Stokes active.', 'color: #00ff00; font-weight: bold;');
    }
}

window.setLowLatencyMode = setLowLatencyMode;

