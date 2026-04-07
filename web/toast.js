/**
 * Altaris Terminal — Toast Notification System
 * Pro Max UX Guideline: Transient feedback for non-critical actions
 * Auto-dismiss after 3s · Stacking · 4 variants · aria-live polite
 */
(function () {
  'use strict';

  const ICON_SVG = {
    success: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
    info:    '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    warn:    '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    error:   '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
  };

  const MAX_TOASTS = 4;
  let container = null;

  function getContainer() {
    if (!container) {
      container = document.getElementById('altaris-toast-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'altaris-toast-container';
        container.setAttribute('aria-live', 'polite');
        container.setAttribute('aria-atomic', 'true');
        document.body.appendChild(container);
      }
    }
    return container;
  }

  /**
   * Show a toast notification.
   * @param {string} message — The text to display
   * @param {'success'|'info'|'warn'|'error'} type — Toast variant
   * @param {number} [duration=3000] — Auto-dismiss in ms (0 = manual dismiss)
   */
  function showToast(message, type, duration) {
    type = type || 'info';
    duration = duration !== undefined ? duration : 3000;
    const c = getContainer();

    // Cap max visible toasts
    while (c.children.length >= MAX_TOASTS) {
      const oldest = c.children[c.children.length - 1];
      if (oldest) oldest.remove();
    }

    const el = document.createElement('div');
    el.className = 'altaris-toast toast-' + type;
    el.setAttribute('role', 'status');
    el.innerHTML = (ICON_SVG[type] || '') + '<span>' + message + '</span>';
    c.insertBefore(el, c.firstChild);

    if (duration > 0) {
      setTimeout(function () {
        el.classList.add('toast-out');
        el.addEventListener('animationend', function () { el.remove(); }, { once: true });
        // Fallback removal in case animation doesn't fire (reduced-motion)
        setTimeout(function () { if (el.parentNode) el.remove(); }, 300);
      }, duration);
    }

    return el;
  }

  // ── Public API ──
  window.AltarisToast = {
    show: showToast,
    success: function (msg, ms) { return showToast(msg, 'success', ms); },
    info:    function (msg, ms) { return showToast(msg, 'info', ms); },
    warn:    function (msg, ms) { return showToast(msg, 'warn', ms); },
    error:   function (msg, ms) { return showToast(msg, 'error', ms); },
  };
})();
