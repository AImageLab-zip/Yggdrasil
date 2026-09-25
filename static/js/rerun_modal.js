/*
 * Single-patient rerun picker (#rerunProcessingModal, templates/common/partials/rerun_modal.html).
 *
 * Two kinds of option. Pipeline steps are Job rows: their slugs go to rerun-processing/
 * in one POST. "Report structuring" is not a job -- it has no Job row and never touches
 * the runner API -- so it is run from here instead, caption by caption, through the same
 * request the patient page's Structure button makes (CaptionStructuring.rerunPatientCaptions).
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

    function renderStructuringOption(container) {
        var wrapper = document.createElement('div');
        wrapper.className = 'check-row';
        wrapper.innerHTML =
            '<input class="rerun-structuring-checkbox" type="checkbox" id="rerunReportStructuring">' +
            '<label for="rerunReportStructuring">Report structuring ' +
            '<span class="modal-note">(files each finished caption into its report template again)</span></label>';
        container.appendChild(wrapper);
    }

    function renderOptions(slugs, structuring) {
        var container = document.getElementById('rerunModalityOptions');
        if (!container) return;
        container.innerHTML = '';
        if (!slugs.length && !structuring) {
            container.innerHTML = '<p class="modal-note">No rerunnable processing steps available for this patient.</p>';
            return;
        }
        if (structuring) renderStructuringOption(container);
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

    function patientBase() {
        return '/' + window.projectNamespace + '/patient/' + state.patientId;
    }

    function rerunPipeline(jobs, token) {
        return fetch(patientBase() + '/rerun-processing/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
            body: JSON.stringify({ jobs: jobs })
        }).then(function (response) {
            return response.json().catch(function () {
                throw new Error('Server error (' + response.status + ')');
            });
        }).then(function (data) {
            if (!data.success) throw new Error(data.error || 'Failed to rerun jobs');
            notify('success', data.message || 'Jobs set to pending');
            if (state.onSuccess) state.onSuccess(jobs, data);
        });
    }

    function rerunStructuring(token) {
        var CS = window.CaptionStructuring;
        if (!CS || !CS.rerunPatientCaptions) {
            throw new Error('Report structuring is not available on this page.');
        }
        var progress = document.getElementById('rerunStructuringProgress');
        if (progress) {
            progress.textContent = 'Finding captions to structure…';
            progress.classList.remove('hidden');
        }
        return CS.rerunPatientCaptions({
            base: patientBase(),
            token: token,
            onProgress: function (index, total) {
                if (progress) progress.textContent = 'Structuring caption ' + index + ' of ' + total + '…';
            }
        }).then(function (summary) {
            var message = CS.rerunSummaryMessage(summary);
            notify(message.type, message.text);
        }).finally(function () {
            if (progress) progress.classList.add('hidden');
        });
    }

    function submit(button) {
        var jobs = Array.prototype.slice
            .call(document.querySelectorAll('.rerun-modality-checkbox:checked'))
            .map(function (el) { return el.value; });
        var structuring = !!document.querySelector('.rerun-structuring-checkbox:checked');
        if (!jobs.length && !structuring) {
            notify('error', 'Select at least one job to rerun');
            return;
        }
        var label = button.querySelector('.label');
        var spinner = button.querySelector('.spinner');
        function busy(on) {
            button.disabled = on;
            if (label) label.classList.toggle('hidden', on);
            if (spinner) spinner.classList.toggle('hidden', !on);
        }
        busy(true);

        var token = window.yggCsrfToken();
        if (!token) {
            notify('error', 'Security token missing. Please refresh the page.');
            busy(false);
            return;
        }
        // Pipeline first: it is one quick POST. Structuring then keeps the dialog open,
        // with its progress line, until the last caption is done.
        (jobs.length ? rerunPipeline(jobs, token) : Promise.resolve())
            .then(function () { return structuring ? rerunStructuring(token) : null; })
            .then(function () { if (modal) modal.hide(); })
            .catch(function (error) { notify('error', error.message || 'Network error'); })
            .finally(function () { busy(false); });
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
         * @param {boolean}       [options.structuring]  also offer "Report structuring"
         * @param {function}      [options.onSuccess]  called with (jobs, data) after a rerun
         */
        open: function (options) {
            if (!ensureWired()) return;
            state.patientId = options.patientId;
            state.onSuccess = options.onSuccess || null;
            var subtitle = document.getElementById('rerunScanSubtitle');
            if (subtitle) subtitle.textContent = options.patientName || ('Scan #' + options.patientId);
            renderOptions(normalizeSteps(options.steps), !!options.structuring);
            modal.show();
        }
    };
})();
