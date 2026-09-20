/*
 * Single-patient rerun picker (#rerunProcessingModal, templates/common/partials/rerun_modal.html).
 *
 * Both entry points -- a row's rerun button on the patients list and the Rerun button in
 * the patient-detail header -- post the same body to the same endpoint, and differ only in
 * what they do afterwards. So the modal lives here once and each page passes its own
 * `onSuccess`: the list flips that row's status pills, the detail page reloads.
 */
(function () {
    'use strict';


    function notify(type, message) {
        if (window.appNotify) window.appNotify(type, message);
    }

    function labels() {
        if (window.rerunModalityLabels) return window.rerunModalityLabels;
        var el = document.getElementById('rerun-modality-labels');
        var parsed = {};
        if (el) {
            try {
                parsed = JSON.parse(el.textContent || '{}');
            } catch (_err) {
                parsed = {};
            }
        }
        window.rerunModalityLabels = parsed;
        return parsed;
    }

    function renderOptions(slugs) {
        var container = document.getElementById('rerunModalityOptions');
        if (!container) return;
        container.innerHTML = '';
        if (!slugs.length) {
            container.innerHTML = '<p class="modal-note">No rerunnable processing steps available for this patient.</p>';
            return;
        }
        var map = labels();
        slugs.forEach(function (slug, index) {
            var safeSlug = String(slug || '').trim();
            if (!safeSlug) return;
            var wrapper = document.createElement('div');
            wrapper.className = 'check-row';
            var checkboxId = 'rerunModality_' + safeSlug + '_' + index;
            var label = map[safeSlug] || safeSlug.replace(/_/g, ' ');
            wrapper.innerHTML =
                '<input class="rerun-modality-checkbox" type="checkbox" value="' + safeSlug + '" id="' + checkboxId + '" data-modality-slug="' + safeSlug + '">' +
                '<label for="' + checkboxId + '"></label>';
            wrapper.querySelector('label').textContent = label;
            container.appendChild(wrapper);
        });
    }

    /** Normalize the several shapes callers hold slugs in: array, or a "a,b,c" data attribute. */
    function normalizeSteps(steps) {
        var list = Array.isArray(steps) ? steps : String(steps || '').split(',');
        return list
            .map(function (s) { return String(s || '').trim(); })
            .filter(Boolean)
            .filter(function (slug, idx, arr) { return arr.indexOf(slug) === idx; });
    }

    var state = { patientId: null, onSuccess: null };
    var modal = null;
    var wired = false;

    function submit(button) {
        var jobs = Array.prototype.slice
            .call(document.querySelectorAll('.rerun-modality-checkbox:checked'))
            .map(function (el) { return el.value; });
        if (!jobs.length) {
            notify('error', 'Select at least one job to rerun');
            return;
        }
        var label = button.querySelector('.label');
        var spinner = button.querySelector('.spinner');
        button.disabled = true;
        if (label) label.classList.add('hidden');
        if (spinner) spinner.classList.remove('hidden');

        var token = window.yggCsrfToken();
        if (!token) {
            notify('error', 'Security token missing. Please refresh the page.');
            button.disabled = false;
            if (label) label.classList.remove('hidden');
            if (spinner) spinner.classList.add('hidden');
            return;
        }
        fetch('/' + window.projectNamespace + '/patient/' + state.patientId + '/rerun-processing/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
            body: JSON.stringify({ jobs: jobs })
        }).then(function (response) {
            return response.json().catch(function () {
                throw new Error('Server error (' + response.status + ')');
            });
        }).then(function (data) {
            if (!data.success) {
                notify('error', data.error || 'Failed to rerun jobs');
                return;
            }
            notify('success', data.message || 'Jobs set to pending');
            if (modal) modal.hide();
            if (state.onSuccess) state.onSuccess(jobs, data);
        }).catch(function (error) {
            notify('error', error.message || 'Network error');
        }).finally(function () {
            button.disabled = false;
            if (label) label.classList.remove('hidden');
            if (spinner) spinner.classList.add('hidden');
        });
    }

    function ensureWired() {
        if (wired) return true;
        var modalEl = document.getElementById('rerunProcessingModal');
        if (!modalEl || !window.bootstrap) return false;
        modal = new window.bootstrap.Modal(modalEl);
        var confirmBtn = document.getElementById('confirmRerunBtn');
        if (confirmBtn) {
            confirmBtn.addEventListener('click', function () { submit(this); });
        }
        wired = true;
        return true;
    }

    window.YggRerunModal = {
        /** Slug -> display name, parsed once from #rerun-modality-labels. */
        labels: labels,

        /**
         * @param {object} options
         * @param {number|string} options.patientId  patient the rerun applies to
         * @param {string}        [options.patientName]  shown as the modal subtitle
         * @param {string[]|string} options.steps   rerunnable step slugs (array or "a,b,c")
         * @param {function}      [options.onSuccess]  called with (jobs, data) after a rerun
         */
        open: function (options) {
            if (!ensureWired()) return;
            state.patientId = options.patientId;
            state.onSuccess = options.onSuccess || null;
            var subtitle = document.getElementById('rerunScanSubtitle');
            if (subtitle) subtitle.textContent = options.patientName || ('Scan #' + options.patientId);
            renderOptions(normalizeSteps(options.steps));
            modal.show();
        }
    };
})();
