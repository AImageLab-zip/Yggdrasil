/*
 * Phone-only: keep the modality strip and the viewer toolbars on screen.
 *
 * On a phone the viewers stack down a long page, so the controls that pick what is
 * shown -- the modality strip, and each viewer's View / tools toolbar -- scrolled away
 * with the first swipe. patient_detail.css makes them `position: sticky` below 640px;
 * this script only tells the CSS where "below the topbar" and "below the strip" are,
 * because both heights vary: the maintenance banner pushes the topbar down, and the
 * strip wraps onto a second row when a patient has many modalities.
 *
 *   --ygg-sticky-top     bottom edge of the sticky topbar
 *   --ygg-sticky-strip   height of the modality strip (0 when there is none)
 *
 * Desktop and tablet never read either variable.
 */
(function () {
    'use strict';

    var PHONE = window.matchMedia ? window.matchMedia('(max-width: 640px)') : null;
    var root = document.documentElement;

    function strip() {
        return document.querySelector('.modality-tabs, .modality-chips-bar');
    }

    function measure() {
        if (!PHONE || !PHONE.matches) {
            root.style.removeProperty('--ygg-sticky-top');
            root.style.removeProperty('--ygg-sticky-strip');
            return;
        }
        var topbar = document.querySelector('.ygg-topbar');
        var top = 0;
        if (topbar) {
            top = (parseFloat(getComputedStyle(topbar).top) || 0) + topbar.offsetHeight;
        }
        var bar = strip();
        var stripHeight = bar && bar.offsetParent !== null ? bar.offsetHeight : 0;
        root.style.setProperty('--ygg-sticky-top', top + 'px');
        root.style.setProperty('--ygg-sticky-strip', stripHeight + 'px');
    }

    document.addEventListener('DOMContentLoaded', function () {
        measure();
        if (window.ResizeObserver) {
            var observer = new ResizeObserver(measure);
            var bar = strip();
            var topbar = document.querySelector('.ygg-topbar');
            if (bar) observer.observe(bar);
            if (topbar) observer.observe(topbar);
        }
        if (PHONE) {
            if (PHONE.addEventListener) PHONE.addEventListener('change', measure);
            else if (PHONE.addListener) PHONE.addListener(measure);
        }
    });
})();
