/**
 * Bulk patient upload (administrators).
 *
 * Posts one file per request rather than one request for the whole selection:
 * each CBCT needs its own client-side conversion pass (.nii / .mha -> .nii.gz),
 * a 200-volume request would be neither resumable nor reportable, and
 * per-file requests let the server report per-file outcomes as they happen.
 *
 * Conversion runs sequentially on purpose - each pass materializes a whole
 * volume in memory, so converting a folder in parallel would exhaust the tab.
 */
(function () {
    'use strict';

    // DICOM is deliberately absent: the platform has no DICOM path at all. Only
    // the two formats the server cannot store natively are converted here.
    var CONVERTIBLE = /\.(nii|mha)$/i;

    function element(id) { return document.getElementById(id); }

    function notify(type, message) {
        if (window.appNotify) window.appNotify(type, message);
        else window.alert(message);
    }

    function csrfToken(form) {
        var input = form.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }

    /** One row per file, so a 200-file batch stays readable while it runs. */
    function addResultRow(list, file) {
        var row = document.createElement('li');
        row.className = 'bulk-results__row';
        var name = document.createElement('span');
        name.className = 'bulk-results__name';
        name.textContent = file.name;
        var state = document.createElement('span');
        state.className = 'bulk-results__state';
        state.textContent = 'Queued';
        row.appendChild(name);
        row.appendChild(state);
        list.appendChild(row);
        return {
            pending: function (text) { state.textContent = text; },
            done: function (text) {
                row.classList.add('is-done');
                state.textContent = text;
            },
            failed: function (message) {
                row.classList.add('is-failed');
                state.textContent = 'Failed';
                var error = document.createElement('span');
                error.className = 'bulk-results__error';
                error.textContent = message;
                row.appendChild(error);
            }
        };
    }

    function postOne(form, file, folderId, modalitySlug) {
        var data = new FormData();
        data.append('csrfmiddlewaretoken', csrfToken(form));
        data.append('folder', folderId);
        if (modalitySlug) data.append('modality', modalitySlug);
        data.append('files', file, file.name);

        return fetch(form.action, {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrfToken(form) },
            body: data
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
                if (payload.error) throw new Error(payload.error);
                var result = (payload.results || [])[0];
                if (!result) throw new Error('Upload failed (HTTP ' + response.status + ').');
                if (!result.ok) throw new Error(result.error || 'Upload failed.');
                return result;
            });
        });
    }

    /**
     * Convert one file to .nii.gz when the server cannot take it as-is.
     * `orientationChoice` is remembered across the batch: a folder of volumes
     * from one scanner shares an orientation, and asking 200 times is not usable.
     */
    function prepareFile(file, state, progress) {
        if (!CONVERTIBLE.test(file.name) || !window.CBCTConvert) {
            return Promise.resolve(file);
        }
        progress('Converting');
        return window.CBCTConvert.convertFiles([file], {
            onProgress: function (_percent, message) { progress(message); },
            onNeedsOrientation: function () {
                if (state.orientationChoice) return Promise.resolve(state.orientationChoice);
                return promptOrientation().then(function (orientation) {
                    state.orientationChoice = orientation;
                    return orientation;
                });
            }
        }).then(function (converted) {
            // Keep the original stem: the server names each patient after its file.
            var stem = file.name.replace(/\.(nii|mha)$/i, '');
            return new File([converted.file], stem + '.nii.gz', { type: converted.file.type });
        });
    }

    function promptOrientation() {
        var choice = window.prompt(
            'These files declare no orientation metadata (qform/sform are 0).\n' +
            'Enter the orientation to apply to the whole batch (e.g. RAS, LAS, LPS):',
            'RAS'
        );
        if (!choice) return Promise.reject(new Error('No orientation chosen.'));
        return Promise.resolve(choice.trim().toUpperCase());
    }

    function setSubmitting(submitting) {
        var button = element('bulkSubmit');
        if (!button) return;
        button.disabled = submitting;
        var label = button.querySelector('span');
        if (label) label.textContent = submitting ? 'Uploading...' : 'Upload all';
        var icon = button.querySelector('i');
        if (icon) icon.className = submitting ? 'fas fa-spinner fa-spin' : 'fas fa-layer-group';
    }

    function init() {
        var form = element('bulkUploadForm');
        if (!form) return;

        form.addEventListener('submit', function (event) {
            var input = element('bulkFiles');
            var files = Array.from((input && input.files) || []);
            if (!files.length) {
                event.preventDefault();
                notify('warning', 'Select at least one file before uploading.');
                return;
            }
            var folderId = (element('bulkFolder') || {}).value;
            if (!folderId) {
                event.preventDefault();
                notify('warning', 'This project has no folder to upload into.');
                return;
            }

            // From here the batch is driven by fetch, not by the form post.
            event.preventDefault();

            var modalitySlug = (element('bulkModality') || {}).value || '';
            var list = element('bulkResults');
            var progress = element('bulkProgress');
            var bar = element('bulkProgressBar');
            var count = element('bulkProgressCount');
            var label = element('bulkProgressLabel');

            list.innerHTML = '';
            list.hidden = false;
            if (progress) progress.hidden = false;
            setSubmitting(true);

            var rows = files.map(function (file) { return addResultRow(list, file); });
            var state = { orientationChoice: null };
            var created = 0;
            var failed = 0;

            function updateProgress(index) {
                if (count) count.textContent = index + '/' + files.length;
                if (bar) bar.style.width = Math.round((index / files.length) * 100) + '%';
            }

            updateProgress(0);

            // Sequential reduce: one conversion + one upload in flight at a time.
            files.reduce(function (chain, file, index) {
                return chain.then(function () {
                    var row = rows[index];
                    if (label) label.textContent = 'Uploading ' + file.name;
                    return prepareFile(file, state, row.pending)
                        .then(function (prepared) {
                            row.pending('Uploading');
                            return postOne(form, prepared, folderId, modalitySlug);
                        })
                        .then(function (result) {
                            created += 1;
                            row.done('Patient ' + result.patient_id);
                        })
                        .catch(function (error) {
                            failed += 1;
                            row.failed(error.message || 'Upload failed.');
                        })
                        .then(function () { updateProgress(index + 1); });
                });
            }, Promise.resolve()).then(function () {
                setSubmitting(false);
                if (label) label.textContent = 'Finished';
                var summary = created + ' patient(s) created';
                if (failed) summary += ', ' + failed + ' file(s) failed';
                notify(failed ? 'warning' : 'success', summary + '.');
            });
        });
    }

    document.addEventListener('DOMContentLoaded', init);
}());
