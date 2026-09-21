/**
 * AF/NSR/Other/NI classification for the cardiology patient detail page.
 * Captions (text + voice) use the shared common/sections/vocal_caption_section.html
 * partial and static/js/vocal_caption.js -- the same machinery maxillo/brain use,
 * not a cardiology-specific copy.
 */
(function () {
    'use strict';

    function notify(kind, message) {
        if (typeof window.appNotify === 'function') window.appNotify(kind, message);
    }

    function csrfHeaders() {
        return { 'Content-Type': 'application/json', 'X-CSRFToken': window.yggCsrfToken() };
    }

    function initClassification() {
        var group = document.getElementById('cardiologyClassificationButtons');
        if (!group) return;
        var url = group.dataset.updateUrl;

        group.querySelectorAll('button[data-value]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                fetch(url, { method: 'POST', headers: csrfHeaders(), body: JSON.stringify({ value: btn.dataset.value }) })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data.success) { notify('error', data.error || 'Failed to save classification.'); return; }
                        group.querySelectorAll('button[data-value]').forEach(function (b) {
                            b.classList.toggle('active', b.dataset.value === data.value);
                        });
                        notify('success', 'Classification saved: ' + data.display_value);
                    })
                    .catch(function () { notify('error', 'Network error while saving the classification.'); });
            });
        });
    }

    document.addEventListener('DOMContentLoaded', initClassification);
})();
