/*
 * Yggdrasil top-nav behaviour — replaces Bootstrap's dropdown + collapse on the
 * global chrome (Bootstrap is no longer loaded on non-viewer pages).
 *   [data-dropdown]         wrapper
 *     [data-dropdown-toggle]  button (aria-expanded toggled)
 *     [data-dropdown-menu]    menu (hidden attr toggled)
 *   [data-mobile-toggle] / [data-mobile-nav]  mobile menu
 */
(function () {
    'use strict';

    function closeAllDropdowns(except) {
        document.querySelectorAll('[data-dropdown-menu]').forEach(function (menu) {
            if (menu === except || menu.hidden) return;
            menu.hidden = true;
            var toggle = menu.closest('[data-dropdown]').querySelector('[data-dropdown-toggle]');
            if (toggle) toggle.setAttribute('aria-expanded', 'false');
        });
    }

    /* Theme toggle: flip data-theme on <html>, persist, swap the button icon.
       Any element with [data-theme-toggle] works (rail button, nav button). The
       pre-paint stamp lives in base.html's <head>. */
    function currentTheme() {
        return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }
    function syncThemeToggles(theme) {
        document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
            var sun = btn.querySelector('[data-theme-icon="sun"]');
            var moon = btn.querySelector('[data-theme-icon="moon"]');
            if (sun) sun.hidden = theme === 'light';
            if (moon) moon.hidden = theme === 'dark';
            btn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
        });
    }
    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        try { localStorage.setItem('ygg-theme', theme); } catch (e) {}
        syncThemeToggles(theme);
    }
    document.addEventListener('DOMContentLoaded', function () { syncThemeToggles(currentTheme()); });

    document.addEventListener('click', function (e) {
        var themeBtn = e.target.closest('[data-theme-toggle]');
        if (themeBtn) {
            e.preventDefault();
            applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
            return;
        }

        var toggle = e.target.closest('[data-dropdown-toggle]');
        if (toggle) {
            e.preventDefault();
            var menu = toggle.closest('[data-dropdown]').querySelector('[data-dropdown-menu]');
            var willOpen = menu.hidden;
            closeAllDropdowns(willOpen ? menu : null);
            menu.hidden = !willOpen;
            toggle.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
            return;
        }

        var mToggle = e.target.closest('[data-mobile-toggle]');
        if (mToggle) {
            var panel = document.querySelector('[data-mobile-nav]');
            if (panel) {
                panel.hidden = !panel.hidden;
                mToggle.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
            }
            return;
        }

        if (!e.target.closest('[data-dropdown-menu]')) {
            closeAllDropdowns(null);
        }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeAllDropdowns(null);
    });
})();
