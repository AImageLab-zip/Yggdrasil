/**
 * Yggdrasil UI runtime — the replacement for Bootstrap's JS bundle.
 *
 * Implements the four Bootstrap behaviours this app actually used (collapse,
 * modal, dropdown, tooltip) in ~300 lines of vanilla JS, driven by `data-ygg-*`
 * attributes.
 *
 * It ALSO exposes a `window.bootstrap` compatibility shim. That is deliberate,
 * not laziness: three call sites construct Bootstrap objects directly, and one
 * of them (modality_viewers/intraoral.js) is viewer code we are contractually
 * not allowed to touch. The shim lets those files keep working verbatim:
 *
 *   - new bootstrap.Modal(el) / .show() / .hide() / Modal.getOrCreateInstance(el)
 *   - bootstrap.Collapse.getOrCreateInstance(el, { toggle: false }) / .show() / .hide()
 *   - bootstrap.Tooltip(el)
 *
 * Legacy `data-bs-*` attributes are honoured alongside `data-ygg-*`, and the
 * `shown.bs.collapse` / `hidden.bs.collapse` events are still emitted, because
 * collapse_sections.js and patient_list.js listen for them.
 */
(function () {
    'use strict';

    /* ---------------------------------------------------------------- utils */

    function attr(el, name) {
        // Prefer the new namespace, fall back to Bootstrap's during migration.
        return el.getAttribute('data-ygg-' + name) || el.getAttribute('data-bs-' + name);
    }

    function emit(el, type) {
        el.dispatchEvent(new CustomEvent(type, { bubbles: true }));
    }

    function targetOf(trigger) {
        var sel = attr(trigger, 'target');
        if (sel) {
            try { return document.querySelector(sel); } catch (e) { return null; }
        }
        var href = trigger.getAttribute('href');
        if (href && href.charAt(0) === '#' && href.length > 1) {
            try { return document.querySelector(href); } catch (e) { return null; }
        }
        return null;
    }

    /* ------------------------------------------------------------- collapse */

    /**
     * Height-animated show/hide. `.collapse` panels start hidden; `.show` marks
     * an open one. Mirrors Bootstrap's class contract so existing markup and the
     * `shown/hidden.bs.collapse` listeners keep working.
     */
    function Collapse(el) {
        this._el = el;
    }

    Collapse.prototype.isShown = function () {
        return this._el.classList.contains('show');
    };

    Collapse.prototype.show = function () {
        var el = this._el;
        if (el.classList.contains('show')) return;
        el.classList.add('show');
        el.style.height = 'auto';
        var target = el.scrollHeight;
        el.style.height = '0px';
        // Force reflow so the transition has a start value to animate from.
        void el.offsetHeight;
        el.style.height = target + 'px';
        var done = function () {
            el.style.height = '';
            el.removeEventListener('transitionend', done);
            emit(el, 'shown.bs.collapse');
        };
        el.addEventListener('transitionend', done);
        // Fallback if the transition never fires (reduced motion, display:none).
        setTimeout(function () {
            if (el.style.height !== '') done();
        }, 400);
    };

    Collapse.prototype.hide = function () {
        var el = this._el;
        if (!el.classList.contains('show')) return;
        el.style.height = el.scrollHeight + 'px';
        void el.offsetHeight;
        el.style.height = '0px';
        var done = function () {
            el.classList.remove('show');
            el.style.height = '';
            el.removeEventListener('transitionend', done);
            emit(el, 'hidden.bs.collapse');
        };
        el.addEventListener('transitionend', done);
        setTimeout(function () {
            if (el.classList.contains('show') && el.style.height === '0px') done();
        }, 400);
    };

    Collapse.prototype.toggle = function () {
        this.isShown() ? this.hide() : this.show();
    };

    Collapse.getOrCreateInstance = function (el, config) {
        if (!el) return null;
        if (!el._yggCollapse) el._yggCollapse = new Collapse(el);
        if (config && config.toggle) el._yggCollapse.toggle();
        return el._yggCollapse;
    };

    /* ---------------------------------------------------------------- modal */

    function Modal(el) {
        this._el = el;
        var self = this;
        if (el._yggModalBound) return;
        el._yggModalBound = true;

        // Backdrop click closes, matching Bootstrap's default.
        el.addEventListener('click', function (e) {
            if (e.target === el) self.hide();
        });
        el.querySelectorAll('[data-ygg-dismiss="modal"], [data-bs-dismiss="modal"]').forEach(function (btn) {
            btn.addEventListener('click', function () { self.hide(); });
        });
    }

    Modal.prototype.show = function () {
        var el = this._el;
        el.classList.add('show');
        el.style.display = 'block';
        el.removeAttribute('aria-hidden');
        document.body.classList.add('ygg-modal-open');
        Modal._open.push(this);
        emit(el, 'shown.bs.modal');
    };

    Modal.prototype.hide = function () {
        var el = this._el;
        el.classList.remove('show');
        el.style.display = 'none';
        el.setAttribute('aria-hidden', 'true');
        var i = Modal._open.indexOf(this);
        if (i !== -1) Modal._open.splice(i, 1);
        if (!Modal._open.length) document.body.classList.remove('ygg-modal-open');
        emit(el, 'hidden.bs.modal');
    };

    Modal.prototype.toggle = function () {
        this._el.classList.contains('show') ? this.hide() : this.show();
    };

    Modal._open = [];

    Modal.getOrCreateInstance = function (el) {
        if (!el) return null;
        if (!el._yggModal) el._yggModal = new Modal(el);
        return el._yggModal;
    };

    /* ------------------------------------------------------------- tooltip */

    function Tooltip(el) {
        if (el._yggTooltipBound) return;
        el._yggTooltipBound = true;

        var tip = null;
        var title = el.getAttribute('title') || attr(el, 'title') || '';
        if (!title) return;
        // Stash the title so the native browser tooltip doesn't double up.
        el.setAttribute('data-ygg-tip-text', title);
        el.removeAttribute('title');

        function place() {
            tip = document.createElement('div');
            tip.className = 'ygg-tooltip';
            tip.textContent = el.getAttribute('data-ygg-tip-text');
            document.body.appendChild(tip);
            var r = el.getBoundingClientRect();
            var t = tip.getBoundingClientRect();
            var top = r.top - t.height - 8;
            // Flip below when there is no room above.
            if (top < 4) top = r.bottom + 8;
            var left = r.left + (r.width - t.width) / 2;
            left = Math.max(4, Math.min(left, window.innerWidth - t.width - 4));
            tip.style.top = (top + window.scrollY) + 'px';
            tip.style.left = (left + window.scrollX) + 'px';
            tip.classList.add('show');
        }

        function remove() {
            if (tip && tip.parentNode) tip.parentNode.removeChild(tip);
            tip = null;
        }

        el.addEventListener('mouseenter', place);
        el.addEventListener('mouseleave', remove);
        el.addEventListener('focus', place);
        el.addEventListener('blur', remove);
        // A tooltip left dangling over a removed/clicked control looks broken.
        el.addEventListener('click', remove);
    }

    /* ------------------------------------------------------- global wiring */

    function closeAllDropdowns(except) {
        document.querySelectorAll('.ygg-dropdown-menu.show').forEach(function (menu) {
            if (menu === except) return;
            menu.classList.remove('show');
            var owner = menu.previousElementSibling;
            if (owner) owner.setAttribute('aria-expanded', 'false');
        });
    }

    document.addEventListener('click', function (e) {
        // Bootstrap's alert-dismiss button — still emitted by nifti_metadata.js.
        var dismissAlert = e.target.closest('[data-bs-dismiss="alert"], [data-ygg-dismiss="alert"]');
        if (dismissAlert) {
            var alertEl = dismissAlert.closest('.alert');
            if (alertEl) alertEl.remove();
            return;
        }

        var toggle = e.target.closest('[data-ygg-toggle], [data-bs-toggle]');

        if (!toggle) {
            // A click landing inside an open dropdown's own menu (e.g. dragging
            // a range input in the CBCT windowing dropdown) must not close it —
            // only a genuine outside click should.
            if (e.target.closest('.ygg-dropdown-menu')) return;
            closeAllDropdowns(null);
            return;
        }

        var kind = attr(toggle, 'toggle');

        if (kind === 'collapse') {
            e.preventDefault();
            var panel = targetOf(toggle);
            if (panel) Collapse.getOrCreateInstance(panel).toggle();
            return;
        }

        if (kind === 'modal') {
            e.preventDefault();
            var modal = targetOf(toggle);
            if (modal) Modal.getOrCreateInstance(modal).show();
            return;
        }

        if (kind === 'dropdown') {
            e.preventDefault();
            e.stopPropagation();
            var menu = toggle.nextElementSibling;
            if (menu && menu.classList.contains('ygg-dropdown-menu')) {
                var willShow = !menu.classList.contains('show');
                closeAllDropdowns(menu);
                menu.classList.toggle('show', willShow);
                toggle.setAttribute('aria-expanded', String(willShow));
            }
            return;
        }

        closeAllDropdowns(null);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        closeAllDropdowns(null);
        if (Modal._open.length) Modal._open[Modal._open.length - 1].hide();
    });

    function initTooltips(root) {
        (root || document)
            .querySelectorAll('[data-ygg-toggle="tooltip"], [data-bs-toggle="tooltip"]')
            .forEach(function (el) { new Tooltip(el); });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initTooltips(document);
    });

    /* ------------------------------------------------------------- exports */

    window.YggUI = {
        Collapse: Collapse,
        Modal: Modal,
        Tooltip: Tooltip,
        initTooltips: initTooltips,
    };

    // Compatibility shim. Keeps modality_viewers/intraoral.js (viewer code we do
    // not touch), vocal_caption.js and patient_list.js working unmodified.
    window.bootstrap = window.bootstrap || {
        Modal: Modal,
        Collapse: Collapse,
        Tooltip: Tooltip,
    };
})();
