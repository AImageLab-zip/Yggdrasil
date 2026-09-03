/**
 * Export functionality JavaScript
 * Handles statistics updates, status polling, and form submission
 */

// Wrap in IIFE to avoid conflicts if script is loaded multiple times
(function() {
    'use strict';

// Debounce function
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Update statistics panel
//
// The selection is now (folders x artifacts x filters): the fixed
// raw/processed/reports/bite checkboxes are gone, replaced by explicit artifact
// keys from common.export_catalog. `filters` carries every filter_* control,
// including the select and text/date inputs the old version could not express.
function collectSelection() {
    const folderIds = Array.from(document.querySelectorAll('.folder-checkbox:checked'))
        .map(cb => cb.value);
    const artifacts = Array.from(document.querySelectorAll('.artifact-checkbox:checked'))
        .map(cb => cb.value);

    const filters = {};
    document.querySelectorAll('.filter-checkbox:checked').forEach(cb => {
        filters[cb.name.replace(/^filter_/, '')] = true;
    });
    document.querySelectorAll('.filter-input').forEach(input => {
        const value = (input.value || '').trim();
        if (value) filters[input.name.replace(/^filter_/, '')] = value;
    });

    return { folderIds, artifacts, filters };
}

function resetStatistics() {
    document.getElementById('stat-patients').textContent = '0';
    document.getElementById('stat-folders').textContent = '0';
    document.getElementById('stat-modalities').textContent = '0';
    document.getElementById('stat-size').textContent = '-';
    document.getElementById('stat-files').textContent = '0';
}

function updateStatistics() {
    const { folderIds, artifacts, filters } = collectSelection();
    const createBtn = document.getElementById('createExportBtn');

    if (folderIds.length === 0 || artifacts.length === 0) {
        resetStatistics();
        if (createBtn) createBtn.disabled = true;
        return;
    }
    if (createBtn) createBtn.disabled = false;

    const csrftoken = document.querySelector('[name=csrfmiddlewaretoken]')?.value;
    const previewUrl = window.exportPreviewUrl || '/maxillo/export/preview/';
    fetch(previewUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrftoken,
        },
        body: JSON.stringify({
            folder_ids: folderIds,
            artifacts: artifacts,
            filters: filters,
        }),
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('stat-patients').textContent = data.patient_count || 0;
            document.getElementById('stat-folders').textContent = data.folder_count || 0;
            document.getElementById('stat-modalities').textContent = data.artifact_count || 0;
            document.getElementById('stat-size').textContent = data.estimated_size || '-';
            document.getElementById('stat-files').textContent = data.file_count || 0;
        } else {
            console.error('Error updating statistics:', data.error);
        }
    })
    .catch(error => {
        console.error('Error fetching statistics:', error);
    });
}

// Debounced update function (500ms delay)
const debouncedUpdateStatistics = debounce(updateStatistics, 500);

// Initialize export page
function initExportPage() {
    const artifactCheckboxes = document.querySelectorAll('.artifact-checkbox');

    document.querySelectorAll(
        '.folder-checkbox, .artifact-checkbox, .filter-checkbox'
    ).forEach(cb => cb.addEventListener('change', debouncedUpdateStatistics));
    document.querySelectorAll('.filter-input').forEach(input => {
        input.addEventListener('change', debouncedUpdateStatistics);
        input.addEventListener('input', debouncedUpdateStatistics);
    });

    const selectAllBtn = document.getElementById('selectAllArtifacts');
    const deselectAllBtn = document.getElementById('deselectAllArtifacts');

    if (selectAllBtn) {
        selectAllBtn.addEventListener('click', function() {
            // Only what actually exists: a disabled artifact has nothing stored.
            artifactCheckboxes.forEach(cb => {
                if (!cb.disabled) cb.checked = true;
            });
            debouncedUpdateStatistics();
        });
    }

    if (deselectAllBtn) {
        deselectAllBtn.addEventListener('click', function() {
            artifactCheckboxes.forEach(cb => cb.checked = false);
            debouncedUpdateStatistics();
        });
    }

    const exportForm = document.getElementById('exportForm');
    if (exportForm) {
        exportForm.addEventListener('submit', function() {
            const createBtn = document.getElementById('createExportBtn');
            if (createBtn) {
                createBtn.disabled = true;
                createBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Creating Export...';
            }
        });
    }

    debouncedUpdateStatistics();
}

// Status polling for processing exports
const pollingIntervals = {};

function startStatusPolling(exportId) {
    // Clear any existing polling for this export
    if (pollingIntervals[exportId]) {
        clearInterval(pollingIntervals[exportId]);
    }
    
    // Poll every 2 seconds
    pollingIntervals[exportId] = setInterval(function() {
        const statusUrl = (window.exportStatusUrl || '/maxillo/export/{id}/').replace('{id}', exportId);
        fetch(statusUrl)
            .then(response => {
                if (!response.ok) {
                    throw new Error('Network response was not ok');
                }
                return response.json();
            })
            .then(data => {
                const badge = document.getElementById(`status-badge-${exportId}`);
                if (!badge) {
                    clearInterval(pollingIntervals[exportId]);
                    delete pollingIntervals[exportId];
                    return;
                }

                const progressWrap = document.getElementById(`export-progress-wrap-${exportId}`);
                const progressBar = document.getElementById(`export-progress-bar-${exportId}`);
                const progressMsg = document.getElementById(`export-progress-msg-${exportId}`);
                const progressPct = document.getElementById(`export-progress-pct-${exportId}`);

                // Update badge text
                let badgeText = data.status.charAt(0).toUpperCase() + data.status.slice(1);
                if (data.status === 'processing' && data.patient_count != null) {
                    badgeText += ' (' + data.patient_count + ' patients)';
                }
                badge.textContent = badgeText;

                // Update badge class
                badge.className = 'badge';
                if (data.status === 'pending') {
                    badge.classList.add('bg-secondary');
                } else if (data.status === 'processing') {
                    badge.classList.add('bg-info');
                    if (progressWrap) {
                        progressWrap.style.display = 'block';
                    }
                    // Live progress: bar and message
                    if (progressBar && data.progress_percent != null) {
                        progressBar.style.width = data.progress_percent + '%';
                        progressBar.setAttribute('aria-valuenow', data.progress_percent);
                        if (progressPct) {
                            progressPct.textContent = data.progress_percent + '%';
                        }
                    }
                    if (progressMsg) {
                        progressMsg.textContent = data.progress_message || 'Processing...';
                    }
                } else if (data.status === 'completed') {
                    badge.classList.add('bg-success');
                    if (progressWrap) progressWrap.style.display = 'none';
                    clearInterval(pollingIntervals[exportId]);
                    delete pollingIntervals[exportId];
                    setTimeout(() => location.reload(), 1000);
                } else if (data.status === 'failed') {
                    badge.classList.add('bg-danger');
                    if (progressWrap) progressWrap.style.display = 'none';
                    clearInterval(pollingIntervals[exportId]);
                    delete pollingIntervals[exportId];
                }
            })
            .catch(error => {
                console.error('Error polling export status:', error);
                // Stop polling on error
                clearInterval(pollingIntervals[exportId]);
                delete pollingIntervals[exportId];
            });
    }, 2000);
}

function saveShareSettings(exportId) {
    const modeSelect = document.getElementById(`share-mode-${exportId}`);
    const copyBtn = document.getElementById(`copy-share-btn-${exportId}`);
    const statusEl = document.getElementById(`share-status-${exportId}`);
    const linkInput = document.getElementById(`share-link-${exportId}`);
    const expirySelect = document.getElementById(`share-expiry-${exportId}`);
    if (!modeSelect) {
        return;
    }

    const csrftoken = document.querySelector('[name=csrfmiddlewaretoken]')?.value ||
                     document.querySelector('input[name="csrfmiddlewaretoken"]')?.value;
    const shareUrl = (window.exportShareUpdateUrl || '/maxillo/export/{id}/share/').replace('{id}', exportId);

    const payload = { share_mode: modeSelect.value };
    if (expirySelect && expirySelect.value) {
        payload.expires_in_days = expirySelect.value;
    }

    fetch(shareUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrftoken,
        },
        body: JSON.stringify(payload),
    })
    .then(response => response.json())
    .then(data => {
        if (!data.success) {
            throw new Error(data.error || 'Could not update share settings');
        }

        if (linkInput) {
            linkInput.value = data.share_url || '';
        }

        if (copyBtn) {
            copyBtn.disabled = !data.share_url;
        }

        if (statusEl) {
            statusEl.textContent = data.share_url ? 'Link active' : 'Sharing disabled';
        }

        if (typeof window.appNotify === 'function') {
            window.appNotify('success', 'Share settings updated');
        }
    })
    .catch(error => {
        console.error('Error updating share settings:', error);
        if (typeof window.appNotify === 'function') {
            window.appNotify('error', 'Error updating share settings');
        }
    });
}

function copyShareLink(exportId) {
    const linkInput = document.getElementById(`share-link-${exportId}`);
    if (!linkInput || !linkInput.value) {
        if (typeof window.appNotify === 'function') {
            window.appNotify('warning', 'No active share link yet. Select a sharing mode first.');
        }
        return;
    }

    const url = linkInput.value.startsWith('http') ? linkInput.value : new URL(linkInput.value, window.location.origin).href;
    navigator.clipboard.writeText(url)
        .then(() => {
            if (typeof window.appNotify === 'function') {
                window.appNotify('success', 'Share link copied');
            }
        })
        .catch(error => {
            console.error('Error copying share link:', error);
            if (typeof window.appNotify === 'function') {
                window.appNotify('error', 'Could not copy link');
            }
        });
}

// Clean up polling intervals on page unload
window.addEventListener('beforeunload', function() {
    Object.keys(pollingIntervals).forEach(exportId => {
        clearInterval(pollingIntervals[exportId]);
    });
});

// Expose functions to global scope
window.initExportPage = initExportPage;
window.startStatusPolling = startStatusPolling;
window.saveShareSettings = saveShareSettings;
window.copyShareLink = copyShareLink;

})(); // End IIFE
