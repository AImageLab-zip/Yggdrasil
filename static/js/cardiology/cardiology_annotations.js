/**
 * AF/NSR/Other/NI classification for the cardiology patient detail page, and the
 * red Reset buttons that clear the classification or the captions after a
 * confirmation modal.
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

    // One modal serves both Reset buttons; `onConfirm` is whichever asked last.
    var onConfirm = null;

    function confirmReset(title, message, action) {
        var modalEl = document.getElementById('cardiologyResetModal');
        if (!modalEl) return;
        modalEl.querySelector('[data-reset-title]').textContent = title;
        modalEl.querySelector('[data-reset-message]').textContent = message;
        onConfirm = action;
        window.YggUI.Modal.getOrCreateInstance(modalEl).show();
    }

    function initResetModal() {
        var modalEl = document.getElementById('cardiologyResetModal');
        if (!modalEl) return;
        var confirmBtn = modalEl.querySelector('[data-reset-confirm]');
        confirmBtn.addEventListener('click', function () {
            var action = onConfirm;
            onConfirm = null;
            window.YggUI.Modal.getOrCreateInstance(modalEl).hide();
            if (action) action();
        });
        modalEl.addEventListener('hidden.bs.modal', function () { onConfirm = null; });
    }

    function initClassification() {
        var group = document.getElementById('cardiologyClassificationButtons');
        if (!group) return;
        var url = group.dataset.updateUrl;
        var resetBtn = document.getElementById('cardiologyResetClassification');

        // The revision this page loaded: a save quoting a stale one is refused (409)
        // rather than silently replacing somebody else's newer call.
        var revision = parseInt(group.dataset.revision || '0', 10);

        if (resetBtn) {
            resetBtn.addEventListener('click', function () {
                confirmReset(
                    'Reset classification',
                    'Remove the rhythm classification from this ECG? The patient will show as unclassified.',
                    function () {
                        resetBtn.disabled = true;
                        fetch(resetBtn.dataset.resetUrl, {
                            method: 'POST',
                            headers: csrfHeaders(),
                            body: JSON.stringify({ revision: revision })
                        })
                            .then(function (r) { return r.json(); })
                            .then(function (data) {
                                if (!data.success) {
                                    resetBtn.disabled = false;
                                    notify('error', data.error || 'Failed to reset the classification.');
                                    return;
                                }
                                revision = data.revision;
                                group.querySelectorAll('button[data-value]').forEach(function (b) {
                                    b.classList.remove('active');
                                });
                                var meta = document.getElementById('cardiologyClassificationMeta');
                                if (meta) meta.remove();
                                notify('success', 'Classification reset.');
                            })
                            .catch(function () {
                                resetBtn.disabled = false;
                                notify('error', 'Network error while resetting the classification.');
                            });
                    }
                );
            });
        }

        group.querySelectorAll('button[data-value]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                fetch(url, {
                    method: 'POST',
                    headers: csrfHeaders(),
                    body: JSON.stringify({ value: btn.dataset.value, revision: revision })
                })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data.success) { notify('error', data.error || 'Failed to save classification.'); return; }
                        revision = data.revision;
                        group.querySelectorAll('button[data-value]').forEach(function (b) {
                            b.classList.toggle('active', b.dataset.value === data.value);
                        });
                        if (resetBtn) resetBtn.disabled = false;
                        notify('success', 'Classification saved: ' + data.display_value);
                    })
                    .catch(function () { notify('error', 'Network error while saving the classification.'); });
            });
        });
    }

    function initCaptionReset() {
        var btn = document.getElementById('cardiologyResetCaptions');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var message = btn.dataset.scope === 'all'
                ? 'Delete every caption on this patient, including those written by other users?'
                : 'Delete all the captions you wrote on this patient? Captions by other users are kept.';
            confirmReset('Reset captions', message, function () {
                btn.disabled = true;
                fetch(btn.dataset.resetUrl, { method: 'POST', headers: csrfHeaders(), body: '{}' })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data.success) {
                            btn.disabled = false;
                            notify('error', data.error || 'Failed to reset the captions.');
                            return;
                        }
                        if (!data.deleted) {
                            btn.disabled = false;
                            notify('info', 'There were no captions to delete.');
                            return;
                        }
                        // The list is the shared partial's (and vocal_caption.js adds to it
                        // live), so redraw it from the server rather than patch it here.
                        window.location.reload();
                    })
                    .catch(function () {
                        btn.disabled = false;
                        notify('error', 'Network error while resetting the captions.');
                    });
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initResetModal();
        initClassification();
        initCaptionReset();
    });
})();
