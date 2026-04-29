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
function updateStatistics() {
    const folderCheckboxes = document.querySelectorAll('.folder-checkbox:checked');
    const modalityCheckboxes = document.querySelectorAll('.modality-checkbox:checked');
    const filterCheckboxes = document.querySelectorAll('.filter-checkbox:checked');
    const includeRaw = document.getElementById('include_raw')?.checked ?? true;
    const includeProcessed = document.getElementById('include_processed')?.checked ?? true;
    
    const folderIds = Array.from(folderCheckboxes).map(cb => cb.value);
    const modalitySlugs = Array.from(modalityCheckboxes).map(cb => cb.value);
    
    const filters = {};
    filterCheckboxes.forEach(cb => {
        if (cb.name.startsWith('filter_')) {
            const filterName = cb.name.replace('filter_', '');
            filters[filterName] = true;
        }
    });
    
    // Don't make request if no folders or modalities selected
    if (folderIds.length === 0 || modalitySlugs.length === 0 || (!includeRaw && !includeProcessed)) {
        // Reset statistics
        document.getElementById('stat-patients').textContent = '0';
        document.getElementById('stat-folders').textContent = '0';
        document.getElementById('stat-modalities').textContent = '0';
        document.getElementById('stat-size').textContent = '-';
        document.getElementById('stat-files').textContent = '0';
        
        // Disable create button
        const createBtn = document.getElementById('createExportBtn');
        if (createBtn) {
            createBtn.disabled = true;
        }
        return;
    }
    
    // Enable create button
    const createBtn = document.getElementById('createExportBtn');
    if (createBtn) {
        createBtn.disabled = false;
    }
    
    // Get CSRF token
    const csrftoken = document.querySelector('[name=csrfmiddlewaretoken]')?.value || 
                     document.querySelector('input[name="csrfmiddlewaretoken"]')?.value;
    
    // Make AJAX request
    const previewUrl = window.exportPreviewUrl || '/maxillo/export/preview/';
    fetch(previewUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrftoken,
        },
        body: JSON.stringify({
            folder_ids: folderIds,
            modality_slugs: modalitySlugs,
            filters: filters,
            include_raw: includeRaw,
            include_processed: includeProcessed,
        }),
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Update statistics cards
            document.getElementById('stat-patients').textContent = data.patient_count || 0;
            document.getElementById('stat-folders').textContent = data.folder_count || 0;
            document.getElementById('stat-modalities').textContent = data.modality_count || 0;
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
    // Listen for changes to checkboxes
    const folderCheckboxes = document.querySelectorAll('.folder-checkbox');
    const modalityCheckboxes = document.querySelectorAll('.modality-checkbox');
    const filterCheckboxes = document.querySelectorAll('.filter-checkbox');
    const contentCheckboxes = document.querySelectorAll('.content-checkbox');
    
    folderCheckboxes.forEach(cb => {
        cb.addEventListener('change', debouncedUpdateStatistics);
    });
    
    modalityCheckboxes.forEach(cb => {
        cb.addEventListener('change', debouncedUpdateStatistics);
    });
    
    filterCheckboxes.forEach(cb => {
        cb.addEventListener('change', debouncedUpdateStatistics);
    });

    contentCheckboxes.forEach(cb => {
        cb.addEventListener('change', debouncedUpdateStatistics);
    });
    
    // Select All / Deselect All for modalities
    const selectAllBtn = document.getElementById('selectAllModalities');
    const deselectAllBtn = document.getElementById('deselectAllModalities');
    
    if (selectAllBtn) {
        selectAllBtn.addEventListener('click', function() {
            modalityCheckboxes.forEach(cb => cb.checked = true);
            debouncedUpdateStatistics();
        });
    }
    
    if (deselectAllBtn) {
        deselectAllBtn.addEventListener('click', function() {
            modalityCheckboxes.forEach(cb => cb.checked = false);
            debouncedUpdateStatistics();
        });
    }
    
    // Show/hide report filters based on selected modalities
    modalityCheckboxes.forEach(cb => {
        cb.addEventListener('change', function() {
            const modalitySlug = this.value;
            const reportFilter = document.querySelector(`.filter-report-${modalitySlug}`);
            if (reportFilter) {
                reportFilter.style.display = this.checked ? 'block' : 'none';
            }
        });
    });
    
    // Form submission handler
    const exportForm = document.getElementById('exportForm');
    if (exportForm) {
        exportForm.addEventListener('submit', function(e) {
            const createBtn = document.getElementById('createExportBtn');
            if (createBtn) {
                createBtn.disabled = true;
                createBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Creating Export...';
            }
            // Form will submit normally
        });
    }
    
    // Initial statistics update
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
    if (!modeSelect) {
        return;
    }

    const csrftoken = document.querySelector('[name=csrfmiddlewaretoken]')?.value ||
                     document.querySelector('input[name="csrfmiddlewaretoken"]')?.value;
    const shareUrl = (window.exportShareUpdateUrl || '/maxillo/export/{id}/share/').replace('{id}', exportId);

    fetch(shareUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrftoken,
        },
        body: JSON.stringify({
            share_mode: modeSelect.value,
        }),
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
