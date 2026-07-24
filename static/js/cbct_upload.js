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

    function initUploadToggle() {
        const container = document.querySelector('.volume-upload-container');
        if (!container) return;
        const hiddenType = document.querySelector('input[name="cbct_upload_type"]');
        const fileRadio = container.querySelector('#cbct_file_upload');
        const folderRadio = container.querySelector('#cbct_folder_upload');
        const fileSection = container.querySelector('.cbct-file-section');
        const folderSection = container.querySelector('.cbct-folder-section');
        const fileInput = fileSection && fileSection.querySelector('input[type="file"]');
        const folderInput = folderSection && folderSection.querySelector('input[type="file"]');

        function setMode(mode) {
            const folderMode = mode === 'folder';
            if (hiddenType) hiddenType.value = mode;
            if (fileSection) fileSection.hidden = folderMode;
            if (folderSection) folderSection.hidden = !folderMode;
            if (fileInput) fileInput.disabled = folderMode;
            if (folderInput) folderInput.disabled = !folderMode;
            const cleared = folderMode ? fileInput : folderInput;
            if (cleared) {
                cleared.value = '';
                summarizeFiles(cleared);
            }
        }

        fileRadio.addEventListener('change', () => setMode('file'));
        folderRadio.addEventListener('change', () => setMode('folder'));
        setMode(folderRadio.checked ? 'folder' : 'file');
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

    function uploadVideoWithProgress(form) {
        const progress = document.getElementById('uploadProgress');
        const bar = document.getElementById('uploadProgressBar');
        const percent = document.getElementById('uploadProgressPercent');
        const progressLabel = document.getElementById('uploadProgressLabel');
        const xhr = new XMLHttpRequest();
        progress.hidden = false;
        setSubmitting(form, true);
        xhr.upload.addEventListener('progress', event => {
            if (!event.lengthComputable) return;
            const value = Math.round((event.loaded / event.total) * 100);
            bar.style.width = `${value}%`;
            percent.textContent = `${value}%`;
            if (value === 100) progressLabel.textContent = 'Finalizing upload';
        });
        xhr.addEventListener('load', () => {
            let data = null;
            try { data = JSON.parse(xhr.responseText); } catch (error) { /* non-JSON response */ }
            if (data && data.ok) {
                window.location.href = data.redirect;
                return;
            }
            const message = data && data.error ? data.error : `Upload failed (HTTP ${xhr.status}).`;
            notify('danger', message);
            progress.hidden = true;
            setSubmitting(form, false);
        });
        xhr.addEventListener('error', () => {
            notify('danger', 'Network error during upload. Please try again.');
            progress.hidden = true;
            setSubmitting(form, false);
        });
        xhr.open('POST', form.action || window.location.href, true);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        xhr.send(new FormData(form));
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
            const video = form.querySelector('input[name="video"]');
            if (video && video.files.length) {
                event.preventDefault();
                uploadVideoWithProgress(form);
            } else {
                setSubmitting(form, true);
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initDropZones();
        initUploadToggle();
        initForm();
    });
}());
