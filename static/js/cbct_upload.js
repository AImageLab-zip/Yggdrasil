(function () {
    'use strict';

    function notify(type, message) {
        if (window.appNotify) window.appNotify(type, message);
        else window.alert(message);
    }

    function acceptedByInput(file, input) {
        if (input.hasAttribute('webkitdirectory')) return true;
        const accept = (input.getAttribute('accept') || '').split(',').map(value => value.trim().toLowerCase()).filter(Boolean);
        if (!accept.length) return true;
        const name = file.name.toLowerCase();
        const type = (file.type || '').toLowerCase();
        return accept.some(rule => {
            if (rule.startsWith('.')) return name.endsWith(rule);
            if (rule.endsWith('/*')) return type.startsWith(rule.slice(0, -1));
            return type === rule;
        });
    }

    function summarizeFiles(input) {
        const zone = input.closest('.upload-dropzone');
        if (!zone) return;
        const summary = zone.querySelector('[data-file-summary]');
        const files = Array.from(input.files || []);
        zone.classList.toggle('has-files', files.length > 0);
        if (!summary) return;
        if (!files.length) {
            summary.textContent = input.multiple ? 'No files selected' : 'No file selected';
        } else if (files.length === 1) {
            summary.textContent = files[0].name;
        } else {
            summary.textContent = `${files.length} files selected`;
        }
    }

    function assignDroppedFiles(input, files) {
        const selected = Array.from(files || []);
        if (!selected.length) return;
        if (selected.some(file => !acceptedByInput(file, input))) {
            notify('warning', 'One or more files are not supported by this modality.');
            return;
        }
        if (!input.multiple && selected.length > 1) {
            notify('warning', 'This modality accepts one file.');
            return;
        }
        const transfer = new DataTransfer();
        selected.forEach(file => transfer.items.add(file));
        input.files = transfer.files;
        input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function initDropZones() {
        document.querySelectorAll('.upload-dropzone').forEach(zone => {
            const input = zone.querySelector('input[type="file"]');
            if (!input) return;
            summarizeFiles(input);
            input.addEventListener('change', () => summarizeFiles(input));
            ['dragenter', 'dragover'].forEach(type => zone.addEventListener(type, event => {
                event.preventDefault();
                zone.classList.add('is-dragging');
            }));
            ['dragleave', 'drop'].forEach(type => zone.addEventListener(type, event => {
                event.preventDefault();
                zone.classList.remove('is-dragging');
            }));
            zone.addEventListener('drop', event => assignDroppedFiles(input, event.dataTransfer.files));
        });
    }

    function setSubmitting(form, submitting) {
        const button = form.querySelector('[type="submit"]');
        if (!button) return;
        button.disabled = submitting;
        const label = button.querySelector('span');
        if (label) label.textContent = submitting ? 'Uploading...' : 'Upload & process';
        const icon = button.querySelector('i');
        if (icon) icon.className = submitting ? 'fas fa-spinner fa-spin' : 'fas fa-arrow-up-from-bracket';
    }

    function promptOrientationSelection() {
        return new Promise(resolve => {
            let overlay = document.getElementById('cbctOrientationModal');
            if (!overlay) {
                overlay = document.createElement('div');
                overlay.id = 'cbctOrientationModal';
                overlay.className = 'cbct-orientation-modal';
                overlay.innerHTML = `
                    <div class="cbct-orientation-dialog">
                        <i class="fas fa-compass text-primary text-xl mb-2" style="font-size: 1.5rem; color: var(--ygg-primary, #0d6efd);"></i>
                        <h4 style="margin: 0.5rem 0 0.25rem; font-weight: 600;">Specify Orientation Metadata</h4>
                        <p style="font-size: 0.85rem; color: var(--ygg-text-muted, #aaa); margin-bottom: 1rem;">
                            This CBCT volume contains no orientation metadata (qform/sform codes are 0).
                            Please select the correct orientation to apply:
                        </p>
                        <div style="margin-bottom: 1rem;">
                            <select id="cbctOrientationSelect" class="form-input" style="width: 100%; padding: 0.5rem; background: var(--ygg-surface, #222); color: #fff; border: 1px solid var(--ygg-border, #444); border-radius: 6px;">
                                <option value="RAS" selected>RAS (Right-Anterior-Superior)</option>
                                <option value="LAS">LAS (Left-Anterior-Superior)</option>
                                <option value="LPS">LPS (Left-Posterior-Superior)</option>
                                <option value="RPS">RPS (Right-Posterior-Superior)</option>
                                <option value="RAI">RAI (Right-Anterior-Inferior)</option>
                                <option value="LAI">LAI (Left-Anterior-Inferior)</option>
                                <option value="LPI">LPI (Left-Posterior-Inferior)</option>
                                <option value="RPI">RPI (Right-Posterior-Inferior)</option>
                            </select>
                        </div>
                        <button type="button" id="cbctOrientationConfirm" class="btn btn-primary btn-sm" style="width: 100%; padding: 0.5rem; background: #0d6efd; color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer;">
                            Apply & Continue
                        </button>
                    </div>
                `;
                document.body.appendChild(overlay);
            }
            overlay.hidden = false;
            const confirmBtn = overlay.querySelector('#cbctOrientationConfirm');
            const selectEl = overlay.querySelector('#cbctOrientationSelect');

            function onConfirm(e) {
                e.preventDefault();
                confirmBtn.removeEventListener('click', onConfirm);
                overlay.hidden = true;
                resolve(selectEl.value || 'RAS');
            }

            confirmBtn.addEventListener('click', onConfirm);
        });
    }

    function uploadFormWithProgress(form) {
        const progress = document.getElementById('uploadProgress');
        const bar = document.getElementById('uploadProgressBar');
        const percent = document.getElementById('uploadProgressPercent');
        const progressLabel = document.getElementById('uploadProgressLabel');
        const xhr = new XMLHttpRequest();
        if (progress) progress.hidden = false;
        setSubmitting(form, true);
        if (xhr.upload && bar && percent) {
            xhr.upload.addEventListener('progress', event => {
                if (!event.lengthComputable) return;
                const value = Math.round((event.loaded / event.total) * 100);
                bar.style.width = `${value}%`;
                percent.textContent = `${value}%`;
                if (value === 100 && progressLabel) progressLabel.textContent = 'Finalizing upload';
            });
        }
        xhr.addEventListener('load', () => {
            let data = null;
            try { data = JSON.parse(xhr.responseText); } catch (error) { /* non-JSON response */ }
            if (data && data.ok) {
                window.location.href = data.redirect;
                return;
            }
            const message = data && data.error ? data.error : `Upload failed (HTTP ${xhr.status}).`;
            notify('danger', message);
            if (progress) progress.hidden = true;
            setSubmitting(form, false);
        });
        xhr.addEventListener('error', () => {
            notify('danger', 'Network error during upload. Please try again.');
            if (progress) progress.hidden = true;
            setSubmitting(form, false);
        });
        xhr.open('POST', form.action || window.location.href, true);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        xhr.send(new FormData(form));
    }


    /**
     * Whether this input's selection has to go through the in-browser converter.
     *
     * The two formats the server cannot store natively, plus the .nii.gz orientation
     * repair the server-side validator demands. DICOM is not among them: the platform
     * has no DICOM path at all any more, and `_validate_and_extract_nifti_orientation`
     * in maxillo/file_utils.py refuses anything that is not a .nii.gz.
     */
    var BROWSER_CONVERTIBLE = /\.(nii|nii\.gz|mha)$/i;

    function needsBrowserConversion(input) {
        if (!input || input.dataset.converted === 'true') return false;
        var files = Array.from(input.files || []);
        return files.length === 1 && BROWSER_CONVERTIBLE.test(files[0].name);
    }

    function initForm() {
        const form = document.getElementById('patientUploadForm');
        if (!form) return;
        form.addEventListener('submit', event => {
            const activeInputs = Array.from(form.querySelectorAll('input[type="file"]:not(:disabled)'));
            const hasFiles = activeInputs.some(input => input.files && input.files.length);
            if (!hasFiles) {
                event.preventDefault();
                notify('warning', 'Add at least one file before uploading.');
                return;
            }
            const photos = form.querySelector('input[name="intraoral-photos"]');
            if (photos && photos.files.length > 10) {
                event.preventDefault();
                notify('warning', 'Select no more than 10 intraoral photographs.');
                return;
            }

            const cbctInput = form.querySelector('input[name="cbct"]');
            const activeCbctInput = cbctInput && !cbctInput.disabled && cbctInput.files && cbctInput.files.length
                ? cbctInput : null;

            if (activeCbctInput && needsBrowserConversion(activeCbctInput) && window.CBCTConvert) {
                event.preventDefault();
                setSubmitting(form, true);

                const progressLabel = document.getElementById('uploadProgressLabel');
                const progress = document.getElementById('uploadProgress');
                if (progress) progress.hidden = false;
                if (progressLabel) progressLabel.textContent = 'Converting CBCT to NIfTI (.nii.gz)...';

                window.CBCTConvert.convertFiles(activeCbctInput.files, {
                    onProgress: (pct, msg) => {
                        if (progressLabel) progressLabel.textContent = msg;
                    },
                    onNeedsOrientation: () => promptOrientationSelection()
                }).then(({ file }) => {
                    const transfer = new DataTransfer();
                    transfer.items.add(file);
                    if (cbctInput) {
                        cbctInput.files = transfer.files;
                        cbctInput.disabled = false;
                        cbctInput.dataset.converted = 'true';
                    }
                    uploadFormWithProgress(form);
                }).catch(err => {
                    notify('danger', 'CBCT Conversion failed: ' + err.message);
                    if (progress) progress.hidden = true;
                    setSubmitting(form, false);
                });
                return;
            }

            const video = form.querySelector('input[name="video"]');
            if (video && video.files.length) {
                event.preventDefault();
                uploadFormWithProgress(form);
            } else {
                event.preventDefault();
                uploadFormWithProgress(form);
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initDropZones();
        initForm();
    });
}());
