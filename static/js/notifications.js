/*
 * Yggdrasil toast notifications — self-contained (no Bootstrap).
 * Public API (unchanged): window.appNotify(type, message, options)
 *   type:    'success' | 'danger' | 'warning' | 'info' (aliases: error/warn/ok)
 *   options: { autohide?: bool (default true), delay?: ms (default 4500) }
 * Styling lives in the Tailwind layer (.ygg-toast*); icons come from the
 * Lucide sprite at window.YGG_SPRITE.
 */
(function () {
    'use strict';

    function normalizeType(type) {
        const value = String(type || 'info').toLowerCase();
        if (value === 'error') return 'danger';
        if (value === 'warn') return 'warning';
        if (value === 'ok') return 'success';
        if (['success', 'danger', 'warning', 'info'].indexOf(value) === -1) return 'info';
        return value;
    }

    function toastTitle(type) {
        return { success: 'Success', danger: 'Error', warning: 'Warning', info: 'Info' }[type] || 'Info';
    }

    function toastIcon(type) {
        return { success: 'circle-check', danger: 'circle-alert', warning: 'triangle-alert', info: 'info' }[type] || 'info';
    }

    function ensureContainer() {
        let container = document.getElementById('yggToastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'yggToastContainer';
            container.className = 'ygg-toast-container';
            document.body.appendChild(container);
        }
        return container;
    }

    function dismiss(toast) {
        if (!toast || toast.classList.contains('is-leaving')) return;
        toast.classList.add('is-leaving');
        setTimeout(function () { if (toast.parentNode) toast.remove(); }, 160);
    }

    function appNotify(type, message, options) {
        const t = normalizeType(type);
        const text = String(message || '').trim();
        if (!text) return;
        const settings = options || {};
        const sprite = window.YGG_SPRITE || '/static/icons/lucide-sprite.svg';

        const container = ensureContainer();
        const toast = document.createElement('div');
        toast.className = 'ygg-toast ygg-toast--' + t;
        toast.setAttribute('role', 'alert');
        toast.setAttribute('aria-live', t === 'danger' ? 'assertive' : 'polite');

        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('class', 'ygg-icon ygg-toast__icon');
        svg.setAttribute('aria-hidden', 'true');
        const use = document.createElementNS(svgNS, 'use');
        use.setAttribute('href', sprite + '#lc-' + toastIcon(t));
        svg.appendChild(use);

        const body = document.createElement('div');
        body.className = 'ygg-toast__body';
        const title = document.createElement('div');
        title.className = 'ygg-toast__title';
        title.textContent = toastTitle(t);
        const msg = document.createElement('div');
        msg.className = 'ygg-toast__msg';
        msg.textContent = text;
        body.appendChild(title);
        body.appendChild(msg);

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'ygg-toast__close';
        close.setAttribute('aria-label', 'Close');
        close.innerHTML = '&times;';
        close.addEventListener('click', function () { dismiss(toast); });

        toast.appendChild(svg);
        toast.appendChild(body);
        toast.appendChild(close);
        container.appendChild(toast);

        if (settings.autohide !== false) {
            setTimeout(function () { dismiss(toast); }, settings.delay || 4500);
        }
    }

    window.appNotify = appNotify;
    window.showNotification = appNotify;
})();
