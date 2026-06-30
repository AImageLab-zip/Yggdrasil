(function () {
    'use strict';

    function debounce(func, wait) {
        var timeout;
        return function () {
            var args = arguments;
            clearTimeout(timeout);
            timeout = setTimeout(function () { func.apply(null, args); }, wait);
        };
    }

    function resetStats() {
        var stats = {
            'lap-stat-patients': '0',
            'lap-stat-exportable': '0',
            'lap-stat-size': '0 B',
            'lap-stat-files': '0'
        };
        Object.keys(stats).forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.textContent = stats[id];
        });
    }

    function setCreateDisabled(disabled) {
        var button = document.getElementById('createLaparoscopyExportBtn');
        if (button) button.disabled = !!disabled;
    }

    function updatePreview() {
        var selectedFolders = Array.from(document.querySelectorAll('.lap-folder-checkbox:checked'))
            .map(function (checkbox) { return checkbox.value; });

        if (!selectedFolders.length) {
            resetStats();
            setCreateDisabled(true);
            return;
        }

        var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
        fetch(window.laparoscopyExportPreviewUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({ folder_ids: selectedFolders }),
        })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (!data || !data.success) {
                    resetStats();
                    setCreateDisabled(true);
                    return;
                }

                var patientEl = document.getElementById('lap-stat-patients');
                var exportableEl = document.getElementById('lap-stat-exportable');
                var sizeEl = document.getElementById('lap-stat-size');
                var filesEl = document.getElementById('lap-stat-files');
                if (patientEl) patientEl.textContent = String(data.patient_count || 0);
                if (exportableEl) exportableEl.textContent = String(data.exportable_patient_count || 0);
                if (sizeEl) sizeEl.textContent = data.estimated_size || '0 B';
                if (filesEl) filesEl.textContent = String(data.file_count || 0);
                setCreateDisabled(!(data.exportable_patient_count > 0));
            })
            .catch(function () {
                resetStats();
                setCreateDisabled(true);
            });
    }

    function initLaparoscopyExportPage() {
        var checkboxes = document.querySelectorAll('.lap-folder-checkbox');
        var debouncedUpdate = debounce(updatePreview, 250);
        checkboxes.forEach(function (checkbox) {
            checkbox.addEventListener('change', debouncedUpdate);
        });

        var form = document.getElementById('laparoscopyExportForm');
        if (form) {
            form.addEventListener('submit', function () {
                var button = document.getElementById('createLaparoscopyExportBtn');
                if (!button) return;
                button.disabled = true;
                button.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Creating Export...';
            });
        }

        resetStats();
        setCreateDisabled(true);
        debouncedUpdate();
    }

    window.initLaparoscopyExportPage = initLaparoscopyExportPage;
})();
