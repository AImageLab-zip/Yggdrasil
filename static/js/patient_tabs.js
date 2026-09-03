/**
 * Patient-view sidebar tabs — replaces the old collapse-card stack.
 *
 * Plain show/hide by data-tab-target/data-tab-pane; the first tab is active by
 * default (see the template's initial `is-active` classes) so nothing needs a
 * hidden-until-JS-runs state. Deliberately does not touch the modality tab
 * strip on the left (that is a native radio/label group patient_detail.js
 * already owns) or anything with an id ending in "-viewer".
 */
(function () {
    'use strict';

    document.querySelectorAll('.side-panel').forEach(function (panel) {
        var tabs = panel.querySelectorAll('.side-tab');
        var panes = panel.querySelectorAll('.side-tab-pane');

        tabs.forEach(function (tab) {
            tab.addEventListener('click', function () {
                var target = tab.dataset.tabTarget;

                tabs.forEach(function (t) { t.classList.toggle('is-active', t === tab); });
                panes.forEach(function (pane) {
                    pane.classList.toggle('is-active', pane.dataset.tabPane === target);
                });
            });
        });
    });
})();
