/**
 * WebGL2 Instanced Overlay Renderer
 *
 * Replaces Canvas 2D bubble/VP/zone rendering with GPU-accelerated instanced drawing.
 * Two shader programs: circles (bubbles) and rectangles (VP bars, zones).
 * All overlays rendered in 3 draw calls instead of 1000+ Canvas 2D calls.
 *
 * Usage:
 *   WebGLOverlay.init(canvas)           — create context, compile shaders
 *   WebGLOverlay.resize(w, h)           — update viewport
 *   WebGLOverlay.beginFrame()           — clear buffers for new frame
 *   WebGLOverlay.addCircle(x,y,r, rgba) — queue a circle instance
 *   WebGLOverlay.addRect(x,y,w,h, rgba) — queue a rectangle instance
 *   WebGLOverlay.flush()                — upload buffers + 3 draw calls
 *   WebGLOverlay.destroy()              — clean up
 */
'use strict';

(function() {

// ── Constants ──
const MAX_CIRCLES = 1200;  // bubbles + glow
const MAX_RECTS = 800;     // VP bars + zones + wall lines
const CIRCLE_STRIDE = 16;  // 2f center + 1f radius + 4ub color = 16 bytes
const RECT_STRIDE = 20;    // 2f pos + 2f size + 4ub color = 20 bytes

// ── State ──
let _gl = null;
let _canvas = null;
let _dpr = 1;
let _width = 0;
let _height = 0;

// Programs
let _circleProgram = null;
let _rectProgram = null;

// Uniforms
let _circleResLoc = null;
let _rectResLoc = null;

// VAOs
let _circleVAO = null;
let _rectVAO = null;

// Instance buffers
let _circleVBO = null;
let _rectVBO = null;

// Typed arrays (pre-allocated)
let _circleData = new ArrayBuffer(MAX_CIRCLES * CIRCLE_STRIDE);
let _circleF32 = new Float32Array(_circleData);
let _circleU8 = new Uint8Array(_circleData);
let _circleCount = 0;

let _rectData = new ArrayBuffer(MAX_RECTS * RECT_STRIDE);
let _rectF32 = new Float32Array(_rectData);
let _rectU8 = new Uint8Array(_rectData);
let _rectCount = 0;

// ── Shaders ──
const CIRCLE_VERT = `#version 300 es
precision highp float;
// Unit quad
in vec2 a_quad;
// Per-instance
in vec2 a_center;
in float a_radius;
in vec4 a_color;

uniform vec2 u_resolution;

out vec2 v_local;
out vec4 v_color;

void main() {
    v_local = a_quad;
    v_color = a_color;
    vec2 px = a_center + a_quad * (a_radius + 1.5);
    vec2 clip = (px / u_resolution) * 2.0 - 1.0;
    clip.y = -clip.y;
    gl_Position = vec4(clip, 0.0, 1.0);
}
`;

const CIRCLE_FRAG = `#version 300 es
precision highp float;
in vec2 v_local;
in vec4 v_color;
out vec4 fragColor;
void main() {
    float d = length(v_local);
    float a = 1.0 - smoothstep(0.82, 1.0, d);
    if (a < 0.01) discard;
    fragColor = vec4(v_color.rgb, v_color.a * a);
}
`;

const RECT_VERT = `#version 300 es
precision highp float;
in vec2 a_quad;
in vec2 a_pos;
in vec2 a_size;
in vec4 a_color;

uniform vec2 u_resolution;

out vec4 v_color;

void main() {
    v_color = a_color;
    vec2 px = a_pos + a_quad * a_size;
    vec2 clip = (px / u_resolution) * 2.0 - 1.0;
    clip.y = -clip.y;
    gl_Position = vec4(clip, 0.0, 1.0);
}
`;

const RECT_FRAG = `#version 300 es
precision highp float;
in vec4 v_color;
out vec4 fragColor;
void main() {
    fragColor = v_color;
}
`;

// ── Helpers ──
function _compileShader(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        console.error('[WebGLOverlay] Shader compile error:', gl.getShaderInfoLog(s));
        gl.deleteShader(s);
        return null;
    }
    return s;
}

function _createProgram(gl, vsSrc, fsSrc) {
    const vs = _compileShader(gl, gl.VERTEX_SHADER, vsSrc);
    const fs = _compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
    if (!vs || !fs) return null;
    const prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        console.error('[WebGLOverlay] Program link error:', gl.getProgramInfoLog(prog));
        return null;
    }
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    return prog;
}

// ── Init ──
function init(canvas) {
    _canvas = canvas;
    _dpr = window.devicePixelRatio || 1;
    _gl = canvas.getContext('webgl2', {
        alpha: true,
        premultipliedAlpha: false,
        antialias: false,
        preserveDrawingBuffer: false,
    });
    if (!_gl) {
        console.warn('[WebGLOverlay] WebGL2 not available — falling back to Canvas 2D');
        return false;
    }
    const gl = _gl;

    // Compile programs
    _circleProgram = _createProgram(gl, CIRCLE_VERT, CIRCLE_FRAG);
    _rectProgram = _createProgram(gl, RECT_VERT, RECT_FRAG);
    if (!_circleProgram || !_rectProgram) {
        console.error('[WebGLOverlay] Failed to compile shaders');
        return false;
    }

    // Get uniform locations
    _circleResLoc = gl.getUniformLocation(_circleProgram, 'u_resolution');
    _rectResLoc = gl.getUniformLocation(_rectProgram, 'u_resolution');

    // ── Circle VAO ──
    _circleVAO = gl.createVertexArray();
    gl.bindVertexArray(_circleVAO);

    // Unit quad (2 triangles) for circles: [-1,-1] to [1,1]
    const quadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
        -1, -1,  1, -1,  -1, 1,  1, 1
    ]), gl.STATIC_DRAW);
    const aQuadC = gl.getAttribLocation(_circleProgram, 'a_quad');
    gl.enableVertexAttribArray(aQuadC);
    gl.vertexAttribPointer(aQuadC, 2, gl.FLOAT, false, 0, 0);

    // Instance buffer
    _circleVBO = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, _circleVBO);
    gl.bufferData(gl.ARRAY_BUFFER, MAX_CIRCLES * CIRCLE_STRIDE, gl.DYNAMIC_DRAW);

    const aCenterC = gl.getAttribLocation(_circleProgram, 'a_center');
    const aRadiusC = gl.getAttribLocation(_circleProgram, 'a_radius');
    const aColorC = gl.getAttribLocation(_circleProgram, 'a_color');

    // a_center: 2 floats at offset 0
    gl.enableVertexAttribArray(aCenterC);
    gl.vertexAttribPointer(aCenterC, 2, gl.FLOAT, false, CIRCLE_STRIDE, 0);
    gl.vertexAttribDivisor(aCenterC, 1);

    // a_radius: 1 float at offset 8
    gl.enableVertexAttribArray(aRadiusC);
    gl.vertexAttribPointer(aRadiusC, 1, gl.FLOAT, false, CIRCLE_STRIDE, 8);
    gl.vertexAttribDivisor(aRadiusC, 1);

    // a_color: 4 unsigned bytes normalized at offset 12
    gl.enableVertexAttribArray(aColorC);
    gl.vertexAttribPointer(aColorC, 4, gl.UNSIGNED_BYTE, true, CIRCLE_STRIDE, 12);
    gl.vertexAttribDivisor(aColorC, 1);

    // ── Rectangle VAO ──
    _rectVAO = gl.createVertexArray();
    gl.bindVertexArray(_rectVAO);

    // Unit quad for rects: [0,0] to [1,1]
    const rQuadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, rQuadBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
        0, 0,  1, 0,  0, 1,  1, 1
    ]), gl.STATIC_DRAW);
    const aQuadR = gl.getAttribLocation(_rectProgram, 'a_quad');
    gl.enableVertexAttribArray(aQuadR);
    gl.vertexAttribPointer(aQuadR, 2, gl.FLOAT, false, 0, 0);

    // Instance buffer
    _rectVBO = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, _rectVBO);
    gl.bufferData(gl.ARRAY_BUFFER, MAX_RECTS * RECT_STRIDE, gl.DYNAMIC_DRAW);

    const aPosR = gl.getAttribLocation(_rectProgram, 'a_pos');
    const aSizeR = gl.getAttribLocation(_rectProgram, 'a_size');
    const aColorR = gl.getAttribLocation(_rectProgram, 'a_color');

    // a_pos: 2 floats at offset 0
    gl.enableVertexAttribArray(aPosR);
    gl.vertexAttribPointer(aPosR, 2, gl.FLOAT, false, RECT_STRIDE, 0);
    gl.vertexAttribDivisor(aPosR, 1);

    // a_size: 2 floats at offset 8
    gl.enableVertexAttribArray(aSizeR);
    gl.vertexAttribPointer(aSizeR, 2, gl.FLOAT, false, RECT_STRIDE, 8);
    gl.vertexAttribDivisor(aSizeR, 1);

    // a_color: 4 unsigned bytes normalized at offset 16
    gl.enableVertexAttribArray(aColorR);
    gl.vertexAttribPointer(aColorR, 4, gl.UNSIGNED_BYTE, true, RECT_STRIDE, 16);
    gl.vertexAttribDivisor(aColorR, 1);

    gl.bindVertexArray(null);

    // Initial state
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0, 0, 0, 0);

    console.log('[WebGLOverlay] Initialized — WebGL2 instanced rendering active');
    return true;
}

// ── Resize ──
function resize(cssW, cssH) {
    if (!_gl || !_canvas) return;
    _dpr = window.devicePixelRatio || 1;
    const pxW = Math.round(cssW * _dpr);
    const pxH = Math.round(cssH * _dpr);
    if (_canvas.width !== pxW || _canvas.height !== pxH) {
        _canvas.width = pxW;
        _canvas.height = pxH;
    }
    _width = pxW;
    _height = pxH;
    _gl.viewport(0, 0, pxW, pxH);
}

// ── Begin Frame ──
function beginFrame() {
    _circleCount = 0;
    _rectCount = 0;
    if (_gl) {
        _gl.clear(_gl.COLOR_BUFFER_BIT);
    }
}

// ── Add Circle ──
// x, y in CSS pixels; r in CSS pixels; r,g,b 0-255; a 0-255
function addCircle(x, y, radius, r, g, b, a) {
    if (_circleCount >= MAX_CIRCLES) return;
    const i = _circleCount;
    const f = i * CIRCLE_STRIDE / 4; // float index
    const u = i * CIRCLE_STRIDE;     // byte index

    _circleF32[f] = x * _dpr;
    _circleF32[f + 1] = y * _dpr;
    _circleF32[f + 2] = radius * _dpr;
    _circleU8[u + 12] = r;
    _circleU8[u + 13] = g;
    _circleU8[u + 14] = b;
    _circleU8[u + 15] = a;

    _circleCount++;
}

// ── Add Rectangle ──
// x, y (top-left) in CSS pixels; w, h in CSS pixels; r,g,b 0-255; a 0-255
function addRect(x, y, w, h, r, g, b, a) {
    if (_rectCount >= MAX_RECTS) return;
    const i = _rectCount;
    const f = i * RECT_STRIDE / 4;
    const u = i * RECT_STRIDE;

    _rectF32[f] = x * _dpr;
    _rectF32[f + 1] = y * _dpr;
    _rectF32[f + 2] = w * _dpr;
    _rectF32[f + 3] = h * _dpr;
    _rectU8[u + 16] = r;
    _rectU8[u + 17] = g;
    _rectU8[u + 18] = b;
    _rectU8[u + 19] = a;

    _rectCount++;
}

// ── Flush: upload buffers + draw ──
function flush() {
    if (!_gl) return;
    const gl = _gl;

    // Ensure correct blend state (may have been changed by other GL contexts)
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    // Rects first (behind bubbles)
    if (_rectCount > 0) {
        gl.useProgram(_rectProgram);
        gl.uniform2f(_rectResLoc, _width, _height);
        gl.bindVertexArray(_rectVAO);
        gl.bindBuffer(gl.ARRAY_BUFFER, _rectVBO);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, _rectU8.subarray(0, _rectCount * RECT_STRIDE));
        gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, _rectCount);
    }

    // Circles on top
    if (_circleCount > 0) {
        gl.useProgram(_circleProgram);
        gl.uniform2f(_circleResLoc, _width, _height);
        gl.bindVertexArray(_circleVAO);
        gl.bindBuffer(gl.ARRAY_BUFFER, _circleVBO);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, _circleU8.subarray(0, _circleCount * CIRCLE_STRIDE));
        gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, _circleCount);
    }

    gl.bindVertexArray(null);
}

// ── Destroy ──
function destroy() {
    if (_gl) {
        _gl.getExtension('WEBGL_lose_context')?.loseContext();
    }
    _gl = null;
    _canvas = null;
}

// ── Is Available ──
function isReady() {
    return _gl !== null && _circleProgram !== null && _rectProgram !== null;
}

// ── Export ──
window.WebGLOverlay = {
    init,
    resize,
    beginFrame,
    addCircle,
    addRect,
    flush,
    destroy,
    isReady,
};

})();
