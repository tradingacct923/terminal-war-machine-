/**
 * AltarisLayout — Feature-Pane Layout Engine (Production Integration)
 *
 * This module provides the dynamic layout system for the Altaris Terminal.
 * It manages a CSS Grid-based pane system where each pane can host any
 * of the available feature renderers (chart, heatmap, ladder, etc.)
 *
 * Features:
 *   - 10 workflow-specific presets (EXEC, SCALP, FLOW, DOM, INTEL, etc.)
 *   - Custom layout builder with save/load
 *   - Draggable resizers for all grid splits
 *   - Workspace state persistence (localStorage)
 *   - Feature-hot-swapping per pane
 *   - Maximize/restore any pane
 *   - Crosshair sync across panes
 *
 * Integration Points:
 *   - `AltarisLayout.onFeatureMount(paneIdx, featureKey, container)` — called when a feature needs to be rendered
 *   - `AltarisLayout.onFeatureUnmount(paneIdx, featureKey, container)` — called when a feature is being removed
 *   - `AltarisLayout.onSymbolChange(sym)` — called when global symbol changes
 *   - `AltarisLayout.onTimeframeChange(tf)` — called when global timeframe changes
 */
(function(){
'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// FEATURES — Each feature maps to a live renderer
// ═══════════════════════════════════════════════════════════════════════════
const FEATURES = {
  chart:    { label: 'CHART',     icon: '\u{1f4c8}', desc: 'Price Chart' },
  ladder:   { label: 'LADDER',    icon: '\u{1f4ca}', desc: 'Depth Ladder' },
  ocheat:   { label: 'OC HEAT',   icon: '\u{1f525}', desc: 'Option Chain Heatmap' },
  eqbook:   { label: 'EQ BOOK',   icon: '\u{1f4d6}', desc: 'QQQ Equity L2 Book' },
  opscr:    { label: 'SCREENER',  icon: '\u{1f50d}', desc: 'Options Unusual Activity' },
  pressure: { label: 'PRESSURE',  icon: '\u{1f300}', desc: 'Navier-Stokes Pressure Field' },
  kinetic:  { label: 'KINETIC',   icon: '\u26a1',    desc: 'Kinetic HUD' },
  eqtape:   { label: 'EQ TAPE',   icon: '\u{1f3f7}', desc: 'Equity Tape (Venue Routing)' },
  dealer:   { label: 'DEALER',    icon: '\u{1f3e6}', desc: 'Dealer Hedge Flow' },
  xdiv:     { label: 'X-DIV',     icon: '\u{1f318}', desc: 'Cross-Market Divergence + Book Quality' },
  volsurf:  { label: 'VOL SURF',  icon: '\u{1f321}', desc: 'IV Surface, 3D Vol + Greek Regime' },
  optflow:  { label: 'OPT FLOW',  icon: '\u{1f4b8}', desc: 'Options Flow Feed' },
  bookms:   { label: 'BOOK MS',   icon: '\u{1f4d1}', desc: 'Book Microstructure (Venue Quality)' },
  vpintel:  { label: 'VP INTEL',  icon: '\u{1f4ca}', desc: 'Volume Profile Intelligence — Absorption, Depth, Zones' },
  flow:     { label: 'FLOW',      icon: '\u{1f4b0}', desc: 'Signed Δ Notional Flow (0DT-Hero-style) — SPY/QQQ/Mag7' },
  movers:   { label: 'MOVERS',    icon: '\u{1f680}', desc: 'Schwab Top Movers — SPX/DJI/NDX/RUT index-scoped' },
  aipanel:  { label: 'AI PANEL',  icon: '\u{1f9e0}', desc: '0DT-Hero Alert Matrix (SPX/SPY/QQQ × 4 detectors) + Message Log' },
};
const FEAT_KEYS = Object.keys(FEATURES);

// ═══════════════════════════════════════════════════════════════════════════
// LAYOUTS — Each defines slots + default features
// ═══════════════════════════════════════════════════════════════════════════
const LAYOUTS = {
  'single':   { label:'Single',       slots:1, cols:1, rows:1, defaults:['chart'] },
  // ── Futures Execution ──
  'exec':     { label:'Execution',    slots:2, cols:2, rows:1, defaults:['chart','ladder'] },
  'scalp':    { label:'Scalp',        slots:2, cols:2, rows:1, defaults:['ladder','chart'] },
  'flow':     { label:'Flow',         slots:3, cols:3, rows:1, defaults:['chart','volsurf','optflow'] },
  'dom':      { label:'DOM',          slots:3, cols:3, rows:1, defaults:['vpintel','ladder','eqbook'] },
  'intel':    { label:'Intel',        slots:3, cols:2, rows:2, defaults:['chart','vpintel','volsurf'] },
  // ── Options Desk ──
  'hedge':    { label:'Hedge',        slots:3, cols:3, rows:1, defaults:['chart','volsurf','optflow'] },
  // ── Full Station ──
  'recon':    { label:'Recon',        slots:6, cols:3, rows:2, defaults:['chart','vpintel','ladder','eqbook','volsurf','optflow'] },
  // ── Market Maker Workstation ──
  'maker':    { label:'Maker',        slots:3, cols:3, rows:1, defaults:['vpintel','ladder','eqbook'] },
  // ── God Mode ──
  'god-mode': { label:'God Mode',     slots:5, cols:3, rows:2, defaults:['chart','chart','vpintel','volsurf','ladder'] },
  // ── Dealer Hedge ──
  'dealer-desk': { label:'Dealer Desk', slots:4, cols:2, rows:2, defaults:['chart','dealer','xdiv','optflow'] },
  'war-room':    { label:'War Room',    slots:6, cols:3, rows:2, defaults:['chart','eqtape','dealer','volsurf','xdiv','optflow'] },
  // ── Flow analytics ──
  'flow-hero':   { label:'Flow Hero',   slots:5, cols:3, rows:2, defaults:['chart','flow','movers','dealer','vpintel'] },
  'hero-v2':     { label:'Hero v2',     slots:6, cols:3, rows:2, defaults:['chart','flow','aipanel','movers','dealer','vpintel'] },
  // ── Pro Default ──
  'pro':         { label:'Pro',         slots:3, cols:3, rows:1, defaults:['chart','ladder','eqbook'] },
};

const MAX_PANES = 6;

// ═══════════════════════════════════════════════════════════════════════════
// GLOBAL STATE
// ═══════════════════════════════════════════════════════════════════════════
let _sym = 'NQ', _tf = '1m';
let _layout = 'single';
let _maximized = -1;
const _panes = [];
const _paneFeature = []; // which feature each pane shows
const _observers = [];
const _resizers = [];
// Track what's currently mounted in each pane for lifecycle management
const _mountedFeatures = []; // { featureKey, containerEl }[]

const grid = document.getElementById('pane-grid');
const dragOverlay = document.getElementById('drag-overlay');

// Callbacks for real renderer integration (set by app.js)
const _callbacks = {
  onFeatureMount: null,    // (paneIdx, featureKey, slotEl) => void
  onFeatureUnmount: null,  // (paneIdx, featureKey, slotEl) => void
  onSymbolChange: null,    // (sym) => void
  onTimeframeChange: null, // (tf) => void
};
let _initComplete = false; // Don't fire mount callbacks until app.js is ready

// ═══════════════════════════════════════════════════════════════════════════
// PANE POOL — Create once, reuse (zero GC)
// ═══════════════════════════════════════════════════════════════════════════
function createPanes() {
  for(let i=0;i<MAX_PANES;i++){
    const p=document.createElement('div');
    p.className='pane'+(i===0?' active':' hidden');
    p.setAttribute('data-slot',i);

    // Header
    const hdr=document.createElement('div'); hdr.className='pane-hdr';
    const tag=document.createElement('div'); tag.className='pane-hdr-tag feat-sel';
    const icon=document.createElement('span'); icon.className='pane-hdr-icon';
    const lbl=document.createElement('span'); lbl.className='pane-hdr-label';
    lbl.style.cursor='pointer'; lbl.title='Click to change feature';
    tag.appendChild(icon); tag.appendChild(lbl);
    // Feature dropdown
    const drop=document.createElement('div'); drop.className='feat-sel-drop';
    FEAT_KEYS.forEach(fk=>{
      const item=document.createElement('div'); item.className='feat-sel-item';
      item.setAttribute('data-feat',fk);
      item.innerHTML=`<span>${FEATURES[fk].icon}</span> ${FEATURES[fk].desc}`;
      item.addEventListener('click',(e)=>{e.stopPropagation();setFeature(i,fk);drop.classList.remove('open')});
      drop.appendChild(item);
    });
    tag.appendChild(drop);
    lbl.addEventListener('click',(e)=>{e.stopPropagation();drop.classList.toggle('open')});

    const ctrls=document.createElement('div'); ctrls.className='pane-hdr-ctrls';
    const maxBtn=document.createElement('button'); maxBtn.className='pane-ctrl'; maxBtn.innerHTML='⬜'; maxBtn.title='Maximize';
    maxBtn.addEventListener('click',(e)=>{e.stopPropagation();toggleMax(i)});
    ctrls.appendChild(maxBtn);
    hdr.appendChild(tag); hdr.appendChild(ctrls);

    // Slot container — this is where real renderers mount their DOM/canvases
    const slot=document.createElement('div'); slot.className='pane-slot';
    slot.setAttribute('data-pane-idx', i);
    slot.style.cssText='position:relative;width:100%;height:100%;flex:1;overflow:hidden';

    const dims=document.createElement('div'); dims.className='pane-dims';
    const chV=document.createElement('div'); chV.className='ch-v';

    p.appendChild(hdr); p.appendChild(slot); p.appendChild(dims); p.appendChild(chV);
    grid.appendChild(p);

    p.addEventListener('mousedown',()=>setActive(i));
    p.addEventListener('mousemove',(e)=>routeCH(i,e));
    p.addEventListener('mouseleave',()=>clearCH());

    _panes.push(p);
    _paneFeature.push('chart');
    _mountedFeatures.push(null);

    // ResizeObserver — notify mounted feature of resize
    let raf=false;
    const ro=new ResizeObserver(()=>{
      if(raf)return; raf=true;
      requestAnimationFrame(()=>{raf=false;if(!p.classList.contains('hidden'))_onPaneResize(i)});
    });
    ro.observe(p);
    _observers.push(ro);
  }
}

window.addEventListener('beforeunload', () => {
  for (const ro of _observers) { try { ro.disconnect(); } catch(_) {} }
  _observers.length = 0;
});

// ═══════════════════════════════════════════════════════════════════════════
// MOUNT / UNMOUNT LIFECYCLE
// ═══════════════════════════════════════════════════════════════════════════
function _getPaneSlot(i) {
  return _panes[i] ? _panes[i].querySelector('.pane-slot') : null;
}

function _mountFeature(i) {
  const feat = _paneFeature[i];
  const slot = _getPaneSlot(i);
  if (!slot) return;

  // Already mounted with same feature? Skip
  if (_mountedFeatures[i] && _mountedFeatures[i].featureKey === feat) return;

  // Unmount previous
  _unmountFeature(i);

  // Notify callback for real renderer (only after init is complete)
  if (_initComplete && _callbacks.onFeatureMount) {
    _callbacks.onFeatureMount(i, feat, slot);
    _mountedFeatures[i] = { featureKey: feat, containerEl: slot };
  }
}

function _unmountFeature(i) {
  const mounted = _mountedFeatures[i];
  if (!mounted) return;

  const slot = _getPaneSlot(i);

  // Notify callback for cleanup
  if (_callbacks.onFeatureUnmount) {
    _callbacks.onFeatureUnmount(i, mounted.featureKey, slot);
  }

  _mountedFeatures[i] = null;
}

function _onPaneResize(i) {
  // Fire a custom event on the slot so renderers can respond
  const slot = _getPaneSlot(i);
  if (slot) {
    slot.dispatchEvent(new CustomEvent('pane-resize', { detail: { paneIdx: i } }));
  }
}

function renderAll() {
  const info=LAYOUTS[_layout]; if(!info)return;
  requestAnimationFrame(()=>{
    for(let i=0;i<info.slots;i++){
      if(_maximized>=0&&i!==_maximized)continue;
      _onPaneResize(i);
    }
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// FEATURE ASSIGNMENT
// ═══════════════════════════════════════════════════════════════════════════
function setFeature(i, feat) {
  _paneFeature[i]=feat;
  updatePaneHeader(i);
  _mountFeature(i);
  autoSave();
}

function updatePaneHeader(i) {
  const p=_panes[i]; let feat=_paneFeature[i];
  if(!FEATURES[feat]){feat='chart';_paneFeature[i]=feat;} // fallback for removed features
  const f=FEATURES[feat];
  p.querySelector('.pane-hdr-icon').textContent=f.icon;
  p.querySelector('.pane-hdr-label').textContent=f.label;
  // Mark current in dropdown
  p.querySelectorAll('.feat-sel-item').forEach(item=>{
    item.classList.toggle('current',item.getAttribute('data-feat')===feat);
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// LAYOUT SWITCHING
// ═══════════════════════════════════════════════════════════════════════════
function setLayout(id) {
  if(!LAYOUTS[id])return;
  _layout=id; _maximized=-1;
  grid.style.removeProperty('grid-template-columns');
  grid.style.removeProperty('grid-template-rows');
  const info=LAYOUTS[id];
  grid.setAttribute('data-layout',id);

  for(let i=0;i<MAX_PANES;i++){
    const p=_panes[i];
    if(i<info.slots){
      p.classList.remove('hidden'); p.setAttribute('data-slot',i);
      p.style.removeProperty('grid-column'); p.style.removeProperty('grid-row');
      // Apply layout defaults
      if(info.defaults[i]) _paneFeature[i]=info.defaults[i];
      updatePaneHeader(i);
      _mountFeature(i);
    } else {
      _unmountFeature(i);
      p.classList.add('hidden');
    }
  }

  if(_activePaneIdx>=info.slots) setActive(0);
  document.querySelectorAll('.ld-opt').forEach(o=>o.classList.toggle('active',o.dataset.layout===id));
  const sLayout = document.getElementById('s-layout');
  if (sLayout) sLayout.textContent='Layout: '+info.label;
  buildResizers();
  renderAll();
  autoSave();
  // Toast feedback for layout switch
  if (typeof AltarisToast !== 'undefined' && _initComplete) AltarisToast.info('Layout: ' + info.label);
}

// ═══════════════════════════════════════════════════════════════════════════
// ACTIVE PANE
// ═══════════════════════════════════════════════════════════════════════════
let _activePaneIdx=0;
function setActive(i) {
  _activePaneIdx=i;
  _panes.forEach((p,j)=>p.classList.toggle('active',j===i));
  const sSym = document.getElementById('s-sym');
  const _f = FEATURES[_paneFeature[i]];
  if (sSym && _f) sSym.textContent=`${_sym} · ${_tf} · ${_f.label}`;
}

// ═══════════════════════════════════════════════════════════════════════════
// MAXIMIZE / RESTORE
// ═══════════════════════════════════════════════════════════════════════════
function toggleMax(i) {
  if(_maximized===i){_maximized=-1;setLayout(_layout);return}
  _maximized=i;
  _resizers.forEach(r=>r.style.display='none');
  for(let j=0;j<MAX_PANES;j++){
    if(j===i){_panes[j].classList.remove('hidden');_panes[j].style.gridColumn='1/-1';_panes[j].style.gridRow='1/-1'}
    else{_unmountFeature(j);_panes[j].classList.add('hidden')}
  }
  setActive(i);
  requestAnimationFrame(()=>_onPaneResize(i));
}

// ═══════════════════════════════════════════════════════════════════════════
// CROSSHAIR ROUTER
// ═══════════════════════════════════════════════════════════════════════════
function routeCH(src, e) {
  const sr=_panes[src].getBoundingClientRect();
  const xr=(e.clientX-sr.left)/sr.width;
  const info=LAYOUTS[_layout]; const slots=_maximized>=0?1:(info?info.slots:0);
  for(let i=0;i<slots;i++){
    const pi=_maximized>=0?_maximized:i;
    const ch=_panes[pi].querySelector('.ch-v'); if(!ch)continue;
    if(pi===src){ch.style.display='block';ch.style.left=(e.clientX-sr.left)+'px'}
    else{const tr=_panes[pi].getBoundingClientRect();ch.style.display='block';ch.style.left=Math.round(xr*tr.width)+'px'}
  }
}
function clearCH(){for(let i=0;i<MAX_PANES;i++){const c=_panes[i].querySelector('.ch-v');if(c)c.style.display='none'}}

// ═══════════════════════════════════════════════════════════════════════════
// RESIZERS — Overlay-based
// ═══════════════════════════════════════════════════════════════════════════
function buildResizers() {
  _resizers.forEach(r=>r.remove()); _resizers.length=0;
  if(_maximized>=0)return;
  requestAnimationFrame(()=>{
    const info=LAYOUTS[_layout]; if(!info||(info.cols<=1&&info.rows<=1))return;
    const cs=getComputedStyle(grid);
    const ct=cs.gridTemplateColumns.split(/\s+/).map(parseFloat);
    const rt=cs.gridTemplateRows.split(/\s+/).map(parseFloat);
    let x=0;
    for(let c=0;c<ct.length-1;c++){
      x+=ct[c];
      const r=document.createElement('div');r.className='resizer r-col';r.style.left=x+'px';r.setAttribute('data-col',c);
      grid.appendChild(r);_resizers.push(r);
      addColDrag(r,c,ct.length);
    }
    let y=0;
    for(let rr=0;rr<rt.length-1;rr++){
      y+=rt[rr];
      const r=document.createElement('div');r.className='resizer r-row';r.style.top=y+'px';r.setAttribute('data-row',rr);
      grid.appendChild(r);_resizers.push(r);
      addRowDrag(r,rr,rt.length);
    }
  });
}

function addColDrag(el,ci,nc) {
  el.addEventListener('mousedown',(e)=>{
    e.preventDefault();e.stopPropagation();el.classList.add('dragging');
    dragOverlay.style.display='block';dragOverlay.style.cursor='col-resize';
    const gr=grid.getBoundingClientRect();let raf=false;
    function mv(ev){if(raf)return;raf=true;requestAnimationFrame(()=>{raf=false;
      const cs=getComputedStyle(grid);const t=cs.gridTemplateColumns.split(/\s+/).map(parseFloat);
      const tot=t.reduce((a,b)=>a+b,0);const cur=t.slice(0,ci+1).reduce((a,b)=>a+b,0);
      const d=ev.clientX-gr.left-cur;
      const MIN_COL=Math.max(120,tot*0.08); // 8% of grid width or 120px, whichever is larger
      t[ci]=Math.max(MIN_COL,t[ci]+d);t[ci+1]=Math.max(MIN_COL,t[ci+1]-d);
      grid.style.gridTemplateColumns=t.map(v=>`minmax(0,${(v/tot).toFixed(4)}fr)`).join(' ');
      reposResizers();
    })}
    function up(){el.classList.remove('dragging');dragOverlay.style.display='none';
      document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);buildResizers()}
    document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);
  });
}

function addRowDrag(el,ri,nr) {
  el.addEventListener('mousedown',(e)=>{
    e.preventDefault();e.stopPropagation();el.classList.add('dragging');
    dragOverlay.style.display='block';dragOverlay.style.cursor='row-resize';
    const gr=grid.getBoundingClientRect();let raf=false;
    function mv(ev){if(raf)return;raf=true;requestAnimationFrame(()=>{raf=false;
      const cs=getComputedStyle(grid);const t=cs.gridTemplateRows.split(/\s+/).map(parseFloat);
      const tot=t.reduce((a,b)=>a+b,0);const cur=t.slice(0,ri+1).reduce((a,b)=>a+b,0);
      const d=ev.clientY-gr.top-cur;
      t[ri]=Math.max(40,t[ri]+d);t[ri+1]=Math.max(40,t[ri+1]-d);
      grid.style.gridTemplateRows=t.map(v=>`minmax(0,${(v/tot).toFixed(4)}fr)`).join(' ');
      reposResizers();
    })}
    function up(){el.classList.remove('dragging');dragOverlay.style.display='none';
      document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);buildResizers()}
    document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);
  });
}

function reposResizers() {
  const cs=getComputedStyle(grid);
  const ct=cs.gridTemplateColumns.split(/\s+/).map(parseFloat);
  const rt=cs.gridTemplateRows.split(/\s+/).map(parseFloat);
  _resizers.forEach(r=>{
    if(r.classList.contains('r-col')){
      const ci=+r.getAttribute('data-col');let x=0;for(let c=0;c<=ci;c++)x+=ct[c]||0;r.style.left=x+'px';
    } else {
      const ri=+r.getAttribute('data-row');let y=0;for(let rr=0;rr<=ri;rr++)y+=rt[rr]||0;r.style.top=y+'px';
    }
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// TOOLBAR — Global symbol/timeframe (production uses #t-symbols/#t-timeframes via app.js)
// Symbol/TF sync is handled via AltarisLayout.onSymbolChange / onTimeframeChange callbacks
// ═══════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════
// DROPDOWN
// ═══════════════════════════════════════════════════════════════════════════
const layoutTrigger = document.getElementById('layout-trigger');
const layoutDropdown = document.getElementById('layout-dropdown');
if (layoutTrigger) {
  layoutTrigger.addEventListener('click',e=>{e.stopPropagation();layoutDropdown.classList.toggle('open')});
}
document.addEventListener('click',()=>{
  if (layoutDropdown) layoutDropdown.classList.remove('open');
  document.querySelectorAll('.feat-sel-drop').forEach(d=>d.classList.remove('open'));
});
if (layoutDropdown) {
  layoutDropdown.addEventListener('click',e=>{
    e.stopPropagation();const o=e.target.closest('.ld-opt');
    if(o&&o.dataset.layout){setLayout(o.dataset.layout);layoutDropdown.classList.remove('open')}
  });
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(_maximized>=0)toggleMax(_maximized);
  if (layoutDropdown) layoutDropdown.classList.remove('open')}});

// ═══════════════════════════════════════════════════════════════════════════
// FPS & RESIZE
// ═══════════════════════════════════════════════════════════════════════════
let fc=0,ft=performance.now();
function fpsLoop(){fc++;const n=performance.now();if(n-ft>=1000){const el=document.getElementById('s-fps');if(el)el.textContent='FPS '+fc;fc=0;ft=n;const ck=document.getElementById('s-clock');if(ck){const d=new Date();ck.textContent=d.toLocaleTimeString('en-US',{hour12:false})}}requestAnimationFrame(fpsLoop)}
let wr=false;
window.addEventListener('resize',()=>{if(wr)return;wr=true;requestAnimationFrame(()=>{wr=false;renderAll();if(_maximized<0)buildResizers()})});

// ═══════════════════════════════════════════════════════════════════════════
// SAVE / LOAD WORKSPACE — Full state persistence
// ═══════════════════════════════════════════════════════════════════════════
const WS_KEY = 'altaris_workspace_v5'; // bumped: pro default layout

function saveWorkspace() {
  const state = {
    layout: _layout,
    features: _paneFeature.slice(0, LAYOUTS[_layout] ? LAYOUTS[_layout].slots : MAX_PANES),
    sym: _sym,
    tf: _tf,
    active: _activePaneIdx,
    gridCols: grid.style.gridTemplateColumns || '',
    gridRows: grid.style.gridTemplateRows || '',
    ts: Date.now(),
  };
  try { localStorage.setItem(WS_KEY, JSON.stringify(state)); } catch(e) {}
  // Toast feedback for save action (UX guideline: confirm successful actions)
  if (typeof AltarisToast !== 'undefined') AltarisToast.success('Workspace saved');
  console.log('[Workspace] Saved:', state.layout, state.features);
}

function loadWorkspace() {
  try {
    const raw = localStorage.getItem(WS_KEY);
    if (!raw) return false;
    const s = JSON.parse(raw);
    if (!s || !s.layout || !LAYOUTS[s.layout]) return false;

    // Restore global state
    if (s.sym) { _sym = s.sym; }
    if (s.tf) { _tf = s.tf; }

    // Apply layout first (sets default features)
    setLayout(s.layout);

    // Override with saved features — re-mount panes that differ from layout defaults
    if (s.features && Array.isArray(s.features)) {
      const info = LAYOUTS[s.layout];
      let remounted = false;
      s.features.forEach((f, i) => {
        if (i < MAX_PANES && FEATURES[f] && _paneFeature[i] !== f) {
          _paneFeature[i] = f;
          updatePaneHeader(i);
          if (i < (info ? info.slots : 0)) { _mountFeature(i); remounted = true; }
        }
      });
    }

    // Restore custom grid tracks (from dragged resizers) with min-width validation
    if (s.gridCols) {
      // Validate: ensure no column is below 8% of total width (prevents squished panes)
      const colVals = s.gridCols.match(/[\d.]+(?=px|fr|%)/g) || s.gridCols.match(/[\d.]+/g);
      if (colVals && colVals.length > 1) {
        const nums = colVals.map(Number);
        const total = nums.reduce((a,b) => a+b, 0);
        const minPct = total * 0.08;
        const allValid = nums.every(v => v >= minPct && v > 0);
        if (allValid) {
          grid.style.gridTemplateColumns = s.gridCols;
        } else {
          console.warn('[Workspace] Skipping gridCols restore — column too narrow:', s.gridCols);
        }
      } else {
        grid.style.gridTemplateColumns = s.gridCols;
      }
    }
    if (s.gridRows) {
      const rowVals = s.gridRows.match(/[\d.]+(?=px|fr|%)/g) || s.gridRows.match(/[\d.]+/g);
      if (rowVals && rowVals.length > 1) {
        const nums = rowVals.map(Number);
        const total = nums.reduce((a,b) => a+b, 0);
        const minPct = total * 0.08;
        const allValid = nums.every(v => v >= minPct && v > 0);
        if (allValid) {
          grid.style.gridTemplateRows = s.gridRows;
        } else {
          console.warn('[Workspace] Skipping gridRows restore — row too narrow:', s.gridRows);
        }
      } else {
        grid.style.gridTemplateRows = s.gridRows;
      }
    }

    // Restore active pane
    if (typeof s.active === 'number') setActive(s.active);

    // Update toolbar buttons (production IDs)
    document.querySelectorAll('#t-symbols .t-btn').forEach(b => b.classList.toggle('active', b.dataset.sym === _sym));
    document.querySelectorAll('#t-timeframes .t-btn').forEach(b => b.classList.toggle('active', b.dataset.tf === _tf));
    const disp = document.getElementById('tb-sym-display');
    if (disp) disp.textContent = `${_sym} · ${_tf}`;

    // Re-mount all visible features
    const info = LAYOUTS[s.layout];
    for (let i = 0; i < (info ? info.slots : 0); i++) {
      _mountFeature(i);
    }

    console.log('[Workspace] Restored:', s.layout, _paneFeature.slice(0, info ? info.slots : 0));
    return true;
  } catch (e) {
    console.warn('[Workspace] Load failed:', e);
    return false;
  }
}

function autoSave() {
  clearTimeout(autoSave._t);
  autoSave._t = setTimeout(saveWorkspace, 500);
}
autoSave._t = null;

// Save button
const tbSave = document.getElementById('tb-save');
if (tbSave) tbSave.addEventListener('click', (e) => { e.stopPropagation(); saveWorkspace(); });

// ═══════════════════════════════════════════════════════════════════════════
// CUSTOM LAYOUT BUILDER
// ═══════════════════════════════════════════════════════════════════════════
const CL_KEY = 'altaris_custom_layouts';
const GRID_SHAPES = [
  { id:'g1',   label:'1',   slots:1, cols:'1fr', rows:'1fr',
    svg:'<rect x="1" y="1" width="22" height="12" rx="1.5"/>' },
  { id:'g2h',  label:'2H',  slots:2, cols:'1fr 1fr', rows:'1fr',
    svg:'<rect x="1" y="1" width="10" height="12" rx="1"/><rect x="13" y="1" width="10" height="12" rx="1"/>' },
  { id:'g2v',  label:'2V',  slots:2, cols:'1fr', rows:'1fr 1fr',
    svg:'<rect x="1" y="1" width="22" height="5" rx="1"/><rect x="1" y="8" width="22" height="5" rx="1"/>' },
  { id:'g3h',  label:'3H',  slots:3, cols:'1fr 1fr 1fr', rows:'1fr',
    svg:'<rect x="1" y="1" width="6" height="12" rx=".8"/><rect x="9" y="1" width="6" height="12" rx=".8"/><rect x="17" y="1" width="6" height="12" rx=".8"/>' },
  { id:'g22',  label:'2×2', slots:4, cols:'1fr 1fr', rows:'1fr 1fr',
    svg:'<rect x="1" y="1" width="10" height="5" rx=".8"/><rect x="13" y="1" width="10" height="5" rx=".8"/><rect x="1" y="8" width="10" height="5" rx=".8"/><rect x="13" y="8" width="10" height="5" rx=".8"/>' },
  { id:'g32',  label:'3×2', slots:6, cols:'1fr 1fr 1fr', rows:'1fr 1fr',
    svg:'<rect x="1" y="1" width="6" height="5" rx=".6"/><rect x="9" y="1" width="6" height="5" rx=".6"/><rect x="17" y="1" width="6" height="5" rx=".6"/><rect x="1" y="8" width="6" height="5" rx=".6"/><rect x="9" y="8" width="6" height="5" rx=".6"/><rect x="17" y="8" width="6" height="5" rx=".6"/>' },
];
const defaultFeatsForSlots = ['chart','vpintel','ladder','gex','dex','ivskew'];
let _clSelectedGrid = null;
let _clSlotSelects = [];

function getCustomLayouts() {
  try { return JSON.parse(localStorage.getItem(CL_KEY)) || []; } catch(e) { return []; }
}
function saveCustomLayouts(arr) {
  try { localStorage.setItem(CL_KEY, JSON.stringify(arr)); } catch(e) {}
}

function registerCustomLayout(cl) {
  const shape = GRID_SHAPES.find(g => g.id === cl.gridId);
  if (!shape) return;
  LAYOUTS[cl.id] = {
    label: cl.name,
    slots: shape.slots,
    cols: shape.cols.split(' ').length,
    rows: shape.rows.split(' ').length,
    defaults: cl.features,
    custom: true,
    gridCols: shape.cols,
    gridRows: shape.rows,
  };
}

function applyCustomGrid(id) {
  const info = LAYOUTS[id];
  if (info && info.custom) {
    grid.style.gridTemplateColumns = info.gridCols.split(' ').map(v => `minmax(0,${v})`).join(' ');
    grid.style.gridTemplateRows = info.gridRows.split(' ').map(v => `minmax(0,${v})`).join(' ');
  }
}

function initBuilder() {
  const pick = document.getElementById('cl-grid-pick');
  if (!pick) return;
  pick.innerHTML = '';
  GRID_SHAPES.forEach(shape => {
    const opt = document.createElement('div');
    opt.className = 'cl-grid-opt';
    opt.innerHTML = `<svg viewBox="0 0 24 14">${shape.svg}</svg>`;
    opt.title = shape.label;
    opt.addEventListener('click', (e) => {
      e.stopPropagation();
      _clSelectedGrid = shape;
      pick.querySelectorAll('.cl-grid-opt').forEach(o => o.classList.remove('selected'));
      opt.classList.add('selected');
      buildSlotSelectors(shape.slots);
    });
    pick.appendChild(opt);
  });
}

function buildSlotSelectors(n) {
  const container = document.getElementById('cl-slots');
  if (!container) return;
  container.innerHTML = '';
  _clSlotSelects = [];
  for (let i = 0; i < n; i++) {
    const row = document.createElement('div');
    row.className = 'cl-slot-row';
    const lbl = document.createElement('span');
    lbl.className = 'cl-slot-lbl';
    lbl.textContent = `SLOT ${i + 1}`;
    const sel = document.createElement('select');
    sel.className = 'cl-slot-sel';
    FEAT_KEYS.forEach(fk => {
      const opt = document.createElement('option');
      opt.value = fk;
      opt.textContent = `${FEATURES[fk].icon} ${FEATURES[fk].desc}`;
      if (fk === defaultFeatsForSlots[i % defaultFeatsForSlots.length]) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener('click', e => e.stopPropagation());
    row.appendChild(lbl);
    row.appendChild(sel);
    container.appendChild(row);
    _clSlotSelects.push(sel);
  }
}

function renderSavedList() {
  const list = document.getElementById('cl-saved-list');
  if (!list) return;
  list.innerHTML = '';
  const customs = getCustomLayouts();
  if (customs.length === 0) {
    list.innerHTML = '<span style="font-size:6.5px;color:rgba(140,160,200,.25);font-family:\'JetBrains Mono\',monospace">No saved layouts yet</span>';
    return;
  }
  customs.forEach(cl => {
    registerCustomLayout(cl);
    const item = document.createElement('div');
    item.className = 'cl-saved-item' + (_layout === cl.id ? ' active' : '');
    const nameSpan = document.createElement('span');
    nameSpan.textContent = cl.name;
    nameSpan.style.cursor = 'pointer';
    nameSpan.addEventListener('click', (e) => {
      e.stopPropagation();
      cl.features.forEach((f, i) => { if (i < MAX_PANES && FEATURES[f]) _paneFeature[i] = f; });
      setLayout(cl.id);
      applyCustomGrid(cl.id);
      const info = LAYOUTS[cl.id];
      for (let i = 0; i < (info ? info.slots : 0); i++) { updatePaneHeader(i); _mountFeature(i); }
      buildResizers();
      if (layoutDropdown) layoutDropdown.classList.remove('open');
    });
    const del = document.createElement('span');
    del.className = 'cl-del';
    del.textContent = '×';
    del.title = 'Delete';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      const arr = getCustomLayouts().filter(c => c.id !== cl.id);
      delete LAYOUTS[cl.id];
      saveCustomLayouts(arr);
      renderSavedList();
    });
    item.appendChild(nameSpan);
    item.appendChild(del);
    list.appendChild(item);
  });
}

// Builder events
const clNewBtn = document.getElementById('cl-new-btn');
if (clNewBtn) {
  clNewBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    document.getElementById('cl-builder').classList.toggle('open');
    if (!_clSelectedGrid) {
      const pick = document.getElementById('cl-grid-pick');
      const opts = pick.querySelectorAll('.cl-grid-opt');
      if (opts[1]) { opts[1].click(); }
    }
  });
}

const clCancelBtn = document.getElementById('cl-cancel-btn');
if (clCancelBtn) {
  clCancelBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    document.getElementById('cl-builder').classList.remove('open');
  });
}

const clSaveBtn = document.getElementById('cl-save-btn');
if (clSaveBtn) {
  clSaveBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const name = document.getElementById('cl-name').value.trim();
    if (!name) { document.getElementById('cl-name').style.borderColor='rgba(224,48,96,.5)'; return; }
    if (!_clSelectedGrid) return;

    const features = _clSlotSelects.map(s => s.value);
    const id = 'custom_' + Date.now();
    const cl = { id, name, gridId: _clSelectedGrid.id, features };

    const arr = getCustomLayouts();
    arr.push(cl);
    saveCustomLayouts(arr);

    registerCustomLayout(cl);
    cl.features.forEach((f, i) => { if (i < MAX_PANES && FEATURES[f]) _paneFeature[i] = f; });
    setLayout(id);
    applyCustomGrid(id);
    const info = LAYOUTS[id];
    for (let i = 0; i < (info ? info.slots : 0); i++) { updatePaneHeader(i); _mountFeature(i); }
    buildResizers();

    document.getElementById('cl-builder').classList.remove('open');
    document.getElementById('cl-name').value = '';
    document.getElementById('cl-name').style.borderColor = '';
    renderSavedList();
    if (layoutDropdown) layoutDropdown.classList.remove('open');
    if (typeof AltarisToast !== 'undefined') AltarisToast.success('Custom layout \u201c' + name + '\u201d saved');
    console.log('[Custom Layout] Saved:', name, features);
  });
}

// Stop propagation on builder clicks
const clBuilder = document.getElementById('cl-builder');
if (clBuilder) clBuilder.addEventListener('click', e => e.stopPropagation());
const clName = document.getElementById('cl-name');
if (clName) clName.addEventListener('click', e => e.stopPropagation());

// ═══════════════════════════════════════════════════════════════════════════
// PUBLIC API
// ═══════════════════════════════════════════════════════════════════════════
window.AltarisLayout = {
  // Core
  setLayout,
  setActive,
  toggleMax,
  setFeature,
  renderAll,
  saveWorkspace,
  loadWorkspace,
  // State
  getState: () => ({
    layout: _layout,
    active: _activePaneIdx,
    maximized: _maximized,
    sym: _sym,
    tf: _tf,
    features: _paneFeature.slice(),
  }),
  getSym: () => _sym,
  getTf: () => _tf,
  getPane: i => _panes[i],
  getPaneSlot: _getPaneSlot,
  getPaneFeature: i => _paneFeature[i],
  // Registry
  LAYOUTS,
  FEATURES,
  FEAT_KEYS,
  MAX_PANES,
  // Integration hooks — set these from app.js
  set onFeatureMount(fn) {
    _callbacks.onFeatureMount = fn;
    // Auto-trigger initial mounts as soon as app.js wires the callback.
    // This is resilient to DOMContentLoaded/rAF ordering issues where
    // triggerInitialMounts() may never fire if the DCL handler errors earlier.
    if (!_initComplete && fn) {
      _initComplete = true;
      const info = LAYOUTS[_layout];
      if (info) {
        for (let i = 0; i < info.slots; i++) _mountFeature(i);
        console.log('[AltarisLayout] Auto-mounted', info.slots, 'panes on callback wire');
      }
    }
  },
  set onFeatureUnmount(fn)  { _callbacks.onFeatureUnmount = fn; },
  set onSymbolChange(fn)    { _callbacks.onSymbolChange = fn; },
  set onTimeframeChange(fn) { _callbacks.onTimeframeChange = fn; },
  // Kept for backwards-compat; callable by app.js if it wants to force a re-mount pass.
  triggerInitialMounts() {
    _initComplete = true;
    const info = LAYOUTS[_layout];
    if (!info) return;
    for (let i = 0; i < info.slots; i++) _mountFeature(i);
    console.log('[AltarisLayout] Initial mounts triggered for', info.slots, 'panes');
  },
};

// ═══════════════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════════════
createPanes();
// Register all saved custom layouts
getCustomLayouts().forEach(cl => registerCustomLayout(cl));
initBuilder();
renderSavedList();

// URL ?layout= param overrides saved workspace (e.g. ?layout=dom)
const _urlParams = new URLSearchParams(window.location.search);
const _urlLayout = _urlParams.get('layout');

if (_urlLayout && LAYOUTS[_urlLayout]) {
  // URL-forced layout — skip localStorage restore
  setLayout(_urlLayout);
  console.log('[AltarisLayout] URL-forced layout:', _urlLayout);
} else {
  const didRestore = loadWorkspace();
  if (!didRestore) setLayout('pro');
}

requestAnimationFrame(fpsLoop);
console.log('[AltarisLayout] Production layout engine ready');

})();
