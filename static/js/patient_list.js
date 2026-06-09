// Page jump validation
function handlePageJumpSubmit(event, maxPages) {
    const form = event.target;
    const pageInput = form.querySelector('input[name="page"]');
    const pageNumber = parseInt(pageInput.value);
    
    if (!pageNumber || pageNumber < 1 || pageNumber > maxPages) {
        event.preventDefault();
        showNotification('warning', `Please enter a valid page number between 1 and ${maxPages}`);
        pageInput.value = '';
        pageInput.focus();
        return false;
    }
    
    return true;
}

// Filter management
function toggleFilters() {
    const content = document.getElementById('filterContent');
    const chevron = document.getElementById('filterChevron');
    
    // Only proceed if both elements exist
    if (!content || !chevron) {
        return;
    }
    
    content.classList.toggle('show');
    chevron.classList.toggle('fa-chevron-down');
    chevron.classList.toggle('fa-chevron-up');
}

// Clean form submission to only include non-empty values
function cleanFormSubmission() {
    const form = document.getElementById('filterForm');
    if (!form) return;
    
    // Get all form inputs
    const inputs = form.querySelectorAll('input, select');
    
    inputs.forEach(input => {
        // For hidden filter inputs, remove them if they have no value
        if (
            input.name &&
            (input.name.startsWith('has_') || input.name.startsWith('status_') || input.name === 'tags' || input.name === 'search') &&
            input.value === ''
        ) {
            input.disabled = true; // Disable empty inputs so they're not submitted
        }
    });
    
    // Re-enable inputs after a short delay to allow for future submissions
    setTimeout(() => {
        inputs.forEach(input => {
            if (input.disabled) {
                input.disabled = false;
            }
        });
    }, 100);
}

// Update URL to reflect current filter state (for bookmarking/sharing)
function updateFilterURL() {
    const url = new URL(window.location);
    const form = document.getElementById('filterForm');
    
    if (!form) return;
    
    // Clear existing filter parameters (legacy)
    url.searchParams.delete('has_ios');
    url.searchParams.delete('has_cbct');
    url.searchParams.delete('has_voice');
    url.searchParams.delete('has_bite');
    url.searchParams.delete('has_reports');
    // Clear dynamic status_<slug> params
    Array.from(url.searchParams.keys()).forEach(key => {
        if (key.startsWith('status_')) {
            url.searchParams.delete(key);
        }
    });
    url.searchParams.delete('tags');
    url.searchParams.delete('search');
    
    // Add non-empty filter values
    const inputs = form.querySelectorAll('input[name^="has_"], input[name^="status_"], input[name="tags"], input[name="search"]');
    inputs.forEach(input => {
        if (input.value && input.value.trim() !== '') {
            url.searchParams.set(input.name, input.value.trim());
        }
    });
    
    // Update browser URL without reloading the page
    window.history.replaceState({}, '', url.toString());
}

// Handle per_page change with clean submission
function handlePerPageChange(selectElement) {
    cleanFormSubmission();
    selectElement.form.submit();
}

function clearAllFilters() {
    const url = new URL(window.location);
    
    // Keep only essential parameters (folder, per_page)
    const newSearchParams = new URLSearchParams();
    
    // Preserve folder if it's not 'all'
    const folder = url.searchParams.get('folder');
    if (folder && folder !== 'all') {
        newSearchParams.set('folder', folder);
    }
    
    // Preserve per_page if it's not the default
    const perPage = url.searchParams.get('per_page');
    if (perPage && perPage !== '20') {
        newSearchParams.set('per_page', perPage);
    }
    
    url.search = newSearchParams.toString();
    window.location.href = url.toString();
}

// Auto-expand filters if any are active
function autoExpandFilters() {
    // Check if filter elements exist on this page
    const filterContent = document.getElementById('filterContent');
    if (!filterContent) {
        return; // No filter UI on this page
    }
    
    // Check if any filters are applied by looking at URL parameters
    const url = new URL(window.location);
    const hasFilters = url.searchParams.has('search') || 
                      url.searchParams.has('has_ios') || 
                      url.searchParams.has('has_cbct') || 
                      url.searchParams.has('has_bite') || 
                      url.searchParams.has('has_reports') || 
                      url.searchParams.has('has_voice') || 
                      Array.from(url.searchParams.keys()).some(k => k.startsWith('status_')) ||
                      url.searchParams.has('tags');
    
    if (hasFilters) {
        toggleFilters();
    }
}

// Inline name editing functionality for list view
function initListNameEditing() {
    document.querySelectorAll('.btn-edit-name-list').forEach(editBtn => {
        editBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            const scanId = this.dataset.scanId;
            const row = this.closest('.patient-row, .scan-row');
            const nameDisplay = row ? row.querySelector('.scan-name-text, .scan-name-display') : null;
            
            if (!nameDisplay) return;
            
            const currentName = nameDisplay.textContent.trim();
            const parentElement = nameDisplay.parentNode;
            let isSaving = false;
            
            if (!parentElement) {
                console.error('Parent element not found');
                return;
            }
            
            // Create input field
            const input = document.createElement('input');
            input.type = 'text';
            input.value = currentName;
            input.className = 'name-edit-input-list';
            
            // Replace display with input
            parentElement.replaceChild(input, nameDisplay);
            input.focus();
            input.select();
            
            // Handle save
            function saveName() {
                const newName = input.value.trim();
                if (!newName) {
                    input.value = currentName;
                    return;
                }
                if (isSaving) {
                    return;
                }
                isSaving = true;
                
                secureFetch(`/${window.projectNamespace}/patient/${scanId}/update-name/`, {
                    method: 'POST',
                    body: JSON.stringify({
                        name: newName
                    })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        nameDisplay.textContent = data.name;
                        if (input.parentNode) {
                            input.parentNode.replaceChild(nameDisplay, input);
                        }
                    } else {
                        showNotification('error', 'Error saving name: ' + (data.error || 'Unknown error'));
                        if (input.parentNode) {
                            input.parentNode.replaceChild(nameDisplay, input);
                        }
                    }
                })
                .catch(error => {
                    console.error('Error:', error);
                    showNotification('error', error.message || 'Error saving name');
                    if (input.parentNode) {
                        input.parentNode.replaceChild(nameDisplay, input);
                    }
                })
                .finally(() => {
                    isSaving = false;
                });
            }
            
            // Handle cancel
            function cancelEdit() {
                if (input.parentNode) {
                    input.parentNode.replaceChild(nameDisplay, input);
                }
            }
            
            // Event handlers
            input.addEventListener('blur', saveName);
            input.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    saveName();
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    cancelEdit();
                }
            });
        });
    });
}

// Admin action handlers
function initAdminActions() {
    // Delete scan handler
    document.querySelectorAll('.btn-delete-scan').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            
            const scanId = this.dataset.scanId;
            const scanName = this.dataset.scanName || `Scan #${scanId}`;
            const patientId = this.dataset.patientId;
            
            if (!confirm(`Are you sure you want to delete ${scanName}?\n\nThis will remove the scan from all lists and views, but files and related data will be kept.`)) {
                return;
            }
            
            // Disable button and show loading
            this.disabled = true;
            const originalContent = this.innerHTML;
            this.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            
            secureFetch(`/${window.projectNamespace}/patient/${scanId}/delete/`, {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    // Remove the row with animation
                    const row = this.closest('.patient-row') ||
                        this.closest('.scan-row') ||
                        document.querySelector(`.patient-row[data-scan-id="${scanId}"]`) ||
                        document.querySelector(`.scan-row[data-scan-id="${scanId}"]`);

                    if (row) {
                        row.style.transition = 'opacity 0.3s, transform 0.3s';
                        row.style.opacity = '0';
                        row.style.transform = 'translateX(-20px)';

                        setTimeout(() => {
                            row.remove();
                            // Show success message
                            showNotification('success', data.message || 'Scan deleted successfully');
                        }, 300);
                    } else {
                        showNotification('success', data.message || 'Scan deleted successfully');
                        window.location.reload();
                    }
                } else {
                    throw new Error(data.error || 'Failed to delete scan');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showNotification('error', error.message || 'Error deleting scan');
                // Re-enable button
                this.disabled = false;
                this.innerHTML = originalContent;
            });
        });
    });
    
    // Rerun processing handler with modal selection
    const rerunModalEl = document.getElementById('rerunProcessingModal');
    let rerunModal = null;
    let rerunTargetScanId = null;
    let rerunSelectedSlugs = [];
    const rerunModalityOptionsEl = document.getElementById('rerunModalityOptions');
    const rerunLabelsEl = document.getElementById('rerun-modality-labels');
    let rerunModalityLabels = {};
    if (rerunLabelsEl) {
        try {
            rerunModalityLabels = JSON.parse(rerunLabelsEl.textContent || '{}');
        } catch (_err) {
            rerunModalityLabels = {};
        }
    }
    window.rerunModalityLabels = rerunModalityLabels;

    function renderRerunOptions(modalitySlugs) {
        if (!rerunModalityOptionsEl) return;
        rerunModalityOptionsEl.innerHTML = '';
        if (!modalitySlugs.length) {
            rerunModalityOptionsEl.innerHTML = '<small class="text-muted">No rerunnable modalities available for this patient.</small>';
            return;
        }

        modalitySlugs.forEach((slug, index) => {
            const safeSlug = String(slug || '').trim();
            if (!safeSlug) return;
            const wrapper = document.createElement('div');
            wrapper.className = 'form-check';
            const checkboxId = `rerunModality_${safeSlug}_${index}`;
            const label = rerunModalityLabels[safeSlug] || safeSlug.replace(/_/g, ' ');
            wrapper.innerHTML = `
                <input class="form-check-input rerun-modality-checkbox" type="checkbox" value="${safeSlug}" id="${checkboxId}" data-modality-slug="${safeSlug}">
                <label class="form-check-label" for="${checkboxId}">${label}</label>
            `;
            rerunModalityOptionsEl.appendChild(wrapper);
        });
    }

    if (rerunModalEl && window.bootstrap) {
        rerunModal = new window.bootstrap.Modal(rerunModalEl);
    }
    document.querySelectorAll('.btn-rerun-processing').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            rerunTargetScanId = this.dataset.scanId;
            const scanName = this.dataset.scanName || `Scan #${rerunTargetScanId}`;
            const subtitle = document.getElementById('rerunScanSubtitle');
            if (subtitle) subtitle.textContent = scanName;
            rerunSelectedSlugs = (this.dataset.availableModalities || '')
                .split(',')
                .map(s => s.trim())
                .filter(Boolean)
                .filter((slug, idx, arr) => arr.indexOf(slug) === idx)
                .filter(slug => slug !== 'rawzip');
            renderRerunOptions(rerunSelectedSlugs);
            if (rerunModal) rerunModal.show();
        });
    });
    const confirmRerunBtn = document.getElementById('confirmRerunBtn');
    if (confirmRerunBtn) {
        confirmRerunBtn.addEventListener('click', function() {
            const jobs = Array.from(document.querySelectorAll('.rerun-modality-checkbox:checked')).map(el => el.value);
            if (!jobs.length) {
                showNotification('error', 'Select at least one job to rerun');
                return;
            }
            const label = this.querySelector('.label');
            const spinner = this.querySelector('.spinner');
            this.disabled = true;
            if (label) label.classList.add('d-none');
            if (spinner) spinner.classList.remove('d-none');
            secureFetch(`/${window.projectNamespace}/patient/${rerunTargetScanId}/rerun-processing/`, {
                method: 'POST',
                body: JSON.stringify({ jobs })
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    showNotification('success', data.message || 'Jobs set to pending');
                    if (rerunModal) rerunModal.hide();
                    // Update status indicators for this row based on selected jobs
                    const row = document.querySelector(`.patient-row[data-scan-id="${rerunTargetScanId}"]`) || document.querySelector(`.scan-row[data-scan-id="${rerunTargetScanId}"]`);
                    if (row) {
                        jobs.forEach(slug => {
                            const pill = row.querySelector(`.status-pill[data-modality-slug="${slug}"]`);
                            if (!pill) return;
                            pill.classList.remove('status-processed', 'status-failed', 'status-pending', 'status-absent');
                            pill.classList.add('status-processing');
                        });
                    }
                } else {
                    showNotification('error', data.error || 'Failed to rerun jobs');
                }
            }).catch(() => showNotification('error', 'Network error')).finally(() => {
                confirmRerunBtn.disabled = false;
                if (label) label.classList.remove('d-none');
                if (spinner) spinner.classList.add('d-none');
            });
        });
    }
}

// Utility function to get CSRF token with validation
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

// SECURITY: Enhanced CSRF token validation
function getCSRFToken() {
    // When CSRF_USE_SESSIONS = True, token is in hidden form, not cookies
    const csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (csrfInput) {
        return csrfInput.value;
    }
    
    // Fallback to cookie method for backwards compatibility
    const token = getCookie('csrftoken');
    if (!token) {
        console.error('SECURITY: CSRF token not found. This may indicate a security issue.');
        showNotification('error', 'Security token missing. Please refresh the page.');
        return null;
    }
    return token;
}

// SECURITY: Enhanced fetch wrapper with CSRF validation
function secureFetch(url, options = {}) {
    const token = getCSRFToken();
    if (!token && options.method && options.method !== 'GET') {
        return Promise.reject(new Error('CSRF token missing'));
    }
    
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers
    };
    
    if (token && options.method && options.method !== 'GET') {
        headers['X-CSRFToken'] = token;
    }
    
    return fetch(url, {
        ...options,
        headers
    }).then(response => {
        // SECURITY: Check for CSRF failure
        if (response.status === 403 && response.headers.get('Content-Type')?.includes('application/json')) {
            return response.json().then(data => {
                if (data.error && data.error.includes('CSRF')) {
                    console.error('SECURITY: CSRF validation failed');
                    showNotification('error', 'Security validation failed. Please refresh the page.');
                    throw new Error('CSRF validation failed');
                }
                throw new Error(data.error || 'Request failed');
            });
        }
        return response;
    });
}

// Show notification
function showNotification(type, message) {
    if (typeof window.appNotify === 'function') {
        window.appNotify(type, message);
        return;
    }
}

// Initialize filter remove buttons
function initFilterRemoveButtons() {
    document.querySelectorAll('.remove[data-filter]').forEach(removeBtn => {
        removeBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            const filterType = this.dataset.filter;
            // This function is no longer needed as filters are removed from URL
            // Keeping it for now in case it's re-added or used elsewhere, but it won't do anything.
            console.warn(`Filter removal for type "${filterType}" is not implemented.`);
        });
    });
}

// Bulk selection and move functionality
function initBulkSelection() {
    const selectAll = document.getElementById('selectAll');
    const rows = document.querySelectorAll('.patient-row, .scan-row');
    const toolbar = document.getElementById('bulkToolbar');
    const bulkCard = document.getElementById('bulkToolbarCard');
    const countEl = document.getElementById('selectedCount');
    const clearBtn = document.getElementById('btnClearSelection');
    const moveBtn = document.getElementById('btnMoveSelected');
    const moveSelect = document.getElementById('moveFolderSelect');
    const bulkRerunBtn = document.getElementById('btnBulkRerunSelected');
    
    // Only proceed if essential elements exist
    if (!toolbar || !countEl) {
        return;
    }
    
    function updateToolbar() {
        const selected = document.querySelectorAll('.row-select:checked');
        const count = selected.length;
        if (countEl) countEl.textContent = `${count} selected`;
        if (toolbar) toolbar.style.display = 'flex';
        if (bulkCard) bulkCard.style.display = '';
        // Enable/disable controls based on selection
        const bulkToolbarEl = document.getElementById('bulkToolbar');
        if (bulkToolbarEl) {
            bulkToolbarEl.querySelectorAll('select, button').forEach(el => {
                if (el.id !== 'btnClearSelection' && !el.classList.contains('collapse-toggle')) {
                    el.disabled = count === 0;
                }
            });
        }
        if (selectAll) selectAll.checked = count > 0 && document.querySelectorAll('.row-select').length === count;
    }
    
    if (selectAll) {
        selectAll.addEventListener('change', function() {
            document.querySelectorAll('.row-select').forEach(cb => {
                cb.checked = selectAll.checked;
                const row = cb.closest('.patient-row') || cb.closest('.scan-row');
                if (row) row.classList.toggle('selected', cb.checked);
            });
            updateToolbar();
        });
    }

    // Initialize bulk controls disabled state on load
    updateToolbar();
    
    document.querySelectorAll('.row-select').forEach(cb => {
        cb.addEventListener('change', function() {
            const row = cb.closest('.patient-row') || cb.closest('.scan-row');
            if (row) row.classList.toggle('selected', cb.checked);
            updateToolbar();
        });
    });
    
    if (clearBtn) {
        clearBtn.addEventListener('click', function() {
            document.querySelectorAll('.row-select').forEach(cb => {
                cb.checked = false;
                const row = cb.closest('.patient-row') || cb.closest('.scan-row');
                if (row) row.classList.remove('selected');
            });
            updateToolbar();
        });
    }
    
    if (moveBtn && moveSelect) {
        moveBtn.addEventListener('click', function() {
            const ids = Array.from(document.querySelectorAll('.row-select:checked')).map(cb => parseInt(cb.value));
            if (!ids.length) return;
            const folder_id = moveSelect.value;
            const originalContent = moveBtn.innerHTML;
            moveBtn.disabled = true;
            moveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            secureFetch(`/${window.projectNamespace}/folders/move-patients/`, {
                method: 'POST',
                body: JSON.stringify({ scan_ids: ids, folder_id })
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    showNotification('success', 'Scans moved successfully');
                    window.location.reload();
                } else {
                    showNotification('error', data.error || 'Failed to move scans');
                }
            }).catch(() => showNotification('error', 'Network error')).finally(() => {
                moveBtn.disabled = false;
                moveBtn.innerHTML = originalContent;
            });
        });
    }

    // Add bulk delete functionality
    const deleteBtn = document.getElementById('btnDeleteSelected');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', function() {
            const ids = Array.from(document.querySelectorAll('.row-select:checked')).map(cb => parseInt(cb.value));
            if (!ids.length) return;
            
            // Show confirmation dialog
            const count = ids.length;
            const confirmMessage = `Are you sure you want to delete ${count} scan${count > 1 ? 's' : ''}? Deleted scans are removed from lists and views but data is retained.`;
            
            if (!confirm(confirmMessage)) {
                return;
            }
            
            deleteBtn.disabled = true;
            deleteBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Deleting...';
            
            secureFetch(`/${window.projectNamespace}/patients/bulk-delete/`, {
                method: 'POST',
                body: JSON.stringify({ scan_ids: ids })
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    showNotification('success', data.message || 'Scans deleted successfully');
                    window.location.reload();
                } else {
                    showNotification('error', data.error || 'Failed to delete scans');
                }
            }).catch(() => showNotification('error', 'Network error')).finally(() => {
                deleteBtn.disabled = false;
                deleteBtn.innerHTML = '<i class="fas fa-trash me-1"></i>Delete';
            });
        });
    }

    const bulkRerunModalEl = document.getElementById('bulkRerunProcessingModal');
    const bulkRerunModalityOptionsEl = document.getElementById('bulkRerunModalityOptions');
    const bulkRerunSubtitleEl = document.getElementById('bulkRerunScanSubtitle');
    const confirmBulkRerunBtn = document.getElementById('confirmBulkRerunBtn');
    let bulkRerunModal = null;
    if (bulkRerunModalEl && window.bootstrap) {
        bulkRerunModal = new window.bootstrap.Modal(bulkRerunModalEl);
    }

    function getSelectedPatientIds() {
        return Array.from(document.querySelectorAll('.row-select:checked')).map(cb => parseInt(cb.value, 10)).filter(Number.isFinite);
    }

    function collectAvailableModalitiesForSelectedRows() {
        const selectedRows = Array.from(document.querySelectorAll('.row-select:checked'))
            .map(cb => cb.closest('.patient-row') || cb.closest('.scan-row'))
            .filter(Boolean);
        const slugSet = new Set();
        selectedRows.forEach(row => {
            row.querySelectorAll('.status-pill[data-modality-slug]').forEach(pill => {
                const slug = (pill.dataset.modalitySlug || '').trim();
                if (!slug || slug === 'rawzip') return;
                if (pill.classList.contains('status-absent')) return;
                if (slug === 'voice') {
                    slugSet.add('voice');
                    return;
                }
                slugSet.add(slug);
            });
        });
        return Array.from(slugSet).sort((a, b) => a.localeCompare(b));
    }

    function renderBulkRerunOptions(modalitySlugs) {
        if (!bulkRerunModalityOptionsEl) return;
        bulkRerunModalityOptionsEl.innerHTML = '';
        if (!modalitySlugs.length) {
            bulkRerunModalityOptionsEl.innerHTML = '<small class="text-muted">No rerunnable modalities available for the selected scans.</small>';
            return;
        }
        modalitySlugs.forEach((slug, index) => {
            const safeSlug = String(slug || '').trim();
            if (!safeSlug) return;
            const wrapper = document.createElement('div');
            wrapper.className = 'form-check';
            const checkboxId = `bulkRerunModality_${safeSlug}_${index}`;
            const label = (window.rerunModalityLabels && window.rerunModalityLabels[safeSlug]) || safeSlug.replace(/_/g, ' ');
            wrapper.innerHTML = `
                <input class="form-check-input bulk-rerun-modality-checkbox" type="checkbox" value="${safeSlug}" id="${checkboxId}" data-modality-slug="${safeSlug}">
                <label class="form-check-label" for="${checkboxId}">${label}</label>
            `;
            bulkRerunModalityOptionsEl.appendChild(wrapper);
        });
    }

    if (bulkRerunBtn) {
        bulkRerunBtn.addEventListener('click', function() {
            const ids = getSelectedPatientIds();
            if (!ids.length) return;
            const modalities = collectAvailableModalitiesForSelectedRows();
            renderBulkRerunOptions(modalities);
            if (bulkRerunSubtitleEl) {
                bulkRerunSubtitleEl.textContent = `${ids.length} selected patient${ids.length === 1 ? '' : 's'}`;
            }
            if (bulkRerunModal) bulkRerunModal.show();
        });
    }

    if (confirmBulkRerunBtn) {
        confirmBulkRerunBtn.addEventListener('click', function() {
            const scan_ids = getSelectedPatientIds();
            const jobs = Array.from(document.querySelectorAll('.bulk-rerun-modality-checkbox:checked')).map(el => el.value);
            if (!scan_ids.length) {
                showNotification('error', 'Select at least one scan');
                return;
            }
            if (!jobs.length) {
                showNotification('error', 'Select at least one modality to rerun');
                return;
            }

            const label = this.querySelector('.label');
            const spinner = this.querySelector('.spinner');
            this.disabled = true;
            if (label) label.classList.add('d-none');
            if (spinner) spinner.classList.remove('d-none');

            secureFetch(`/${window.projectNamespace}/patients/bulk-rerun-processing/`, {
                method: 'POST',
                body: JSON.stringify({ scan_ids, jobs })
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    showNotification('success', data.message || 'Bulk rerun queued');
                    if (bulkRerunModal) bulkRerunModal.hide();
                    window.location.reload();
                } else {
                    showNotification('error', data.error || 'Failed to rerun jobs');
                }
            }).catch(() => showNotification('error', 'Network error')).finally(() => {
                confirmBulkRerunBtn.disabled = false;
                if (label) label.classList.remove('d-none');
                if (spinner) spinner.classList.add('d-none');
            });
        });
    }
}

function initCreateFolder() {
    const btn = document.getElementById('btnCreateFolder');
    if (!btn) return;
    btn.addEventListener('click', function() {
        const name = prompt('Folder name');
        if (!name) return;
        const current = new URL(window.location).searchParams.get('folder');
        const parent_id = current && current !== 'all' ? current : null; // parent_id is now ignored by backend
        secureFetch(`/${window.projectNamespace}/folders/create/`, {
            method: 'POST',
            body: JSON.stringify({ name, parent_id })
        }).then(r => r.json()).then(data => {
            if (data.success) {
                showNotification('success', 'Folder created');
                const url = new URL(window.location);
                // If we're currently viewing "all", stay there, otherwise go to the new folder
                if (url.searchParams.get('folder') !== 'all') {
                    url.searchParams.set('folder', data.folder.id);
                }
                window.location.href = url.toString();
            } else {
                showNotification('error', data.error || 'Failed to create folder');
            }
        }).catch(error => showNotification('error', error.message || 'Network error'));
    });
}

function initFolderContextMenu() {
    const menu = document.getElementById('folderContextMenu');
    if (!menu) return;

    let selectedFolder = null;
    const modalEl = document.getElementById('folderPermissionsModal');
    const modal = modalEl && window.bootstrap ? new window.bootstrap.Modal(modalEl) : null;

    function hideMenu() {
        menu.style.display = 'none';
    }

    document.querySelectorAll('.folder-node').forEach(node => {
        node.addEventListener('contextmenu', function (evt) {
            evt.preventDefault();
            selectedFolder = {
                id: this.dataset.id,
                name: this.dataset.name || 'Folder',
            };
            menu.style.display = 'block';
            menu.style.left = `${evt.clientX}px`;
            menu.style.top = `${evt.clientY}px`;
        });
    });

    document.addEventListener('click', hideMenu);

    const statsBtn = document.getElementById('folderMenuStats');
    if (statsBtn) {
        statsBtn.addEventListener('click', function () {
            if (!selectedFolder) return;
            secureFetch(`/${window.projectNamespace}/folders/${selectedFolder.id}/stats/`)
                .then(r => r.json())
                .then(data => {
                    if (!data.success) throw new Error(data.error || 'Failed to load stats');
                    alert(`Folder: ${data.folder.name}\nPatients: ${data.stats.patient_count}`);
                })
                .catch(err => showNotification('error', err.message || 'Failed to load stats'));
        });
    }

    const renameBtn = document.getElementById('folderMenuRename');
    if (renameBtn) {
        renameBtn.addEventListener('click', function () {
            if (!selectedFolder) return;
            const name = prompt('New folder name', selectedFolder.name || '');
            if (!name) return;
            secureFetch(`/${window.projectNamespace}/folders/${selectedFolder.id}/rename/`, {
                method: 'POST',
                body: JSON.stringify({ name }),
            }).then(r => r.json()).then(data => {
                if (!data.success) throw new Error(data.error || 'Failed to rename folder');
                window.location.reload();
            }).catch(err => showNotification('error', err.message || 'Failed to rename folder'));
        });
    }

    const permBtn = document.getElementById('folderMenuPermissions');
    if (permBtn) {
        permBtn.addEventListener('click', function () {
            if (!selectedFolder || !modal) return;
            loadFolderPermissions(selectedFolder.id, selectedFolder.name);
            modal.show();
        });
    }

    function loadFolderPermissions(folderId, folderName) {
        const title = document.getElementById('folderPermissionsModalLabel');
        if (title) title.textContent = `Folder Permissions - ${folderName}`;
        secureFetch(`/${window.projectNamespace}/folders/${folderId}/permissions/`)
            .then(r => r.json())
            .then(data => {
                if (!data.success) throw new Error(data.error || 'Failed to load permissions');
                const userSel = document.getElementById('folderPermUser');
                const body = document.querySelector('#folderPermTable tbody');
                if (userSel) {
                    userSel.innerHTML = '<option value="">Select user</option>';
                    data.users.forEach(u => {
                        const opt = document.createElement('option');
                        opt.value = String(u.id);
                        opt.textContent = u.username;
                        userSel.appendChild(opt);
                    });
                }
                if (body) {
                    body.innerHTML = '';
                    data.permissions.forEach(row => {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `<td>${row.username}</td><td>${row.role}</td><td class="text-end"><button class="btn btn-sm btn-outline-danger" data-user-id="${row.user_id}">Remove</button></td>`;
                        body.appendChild(tr);
                    });
                    body.querySelectorAll('button[data-user-id]').forEach(btn => {
                        btn.addEventListener('click', function () {
                            const uid = this.dataset.userId;
                            secureFetch(`/${window.projectNamespace}/folders/${folderId}/permissions/${uid}/delete/`, { method: 'DELETE' })
                                .then(r => r.json())
                                .then(resp => {
                                    if (!resp.success) throw new Error(resp.error || 'Failed to remove permission');
                                    loadFolderPermissions(folderId, folderName);
                                })
                                .catch(err => showNotification('error', err.message || 'Failed to remove permission'));
                        });
                    });
                }

                const saveBtn = document.getElementById('folderPermAddBtn');
                if (saveBtn) {
                    saveBtn.onclick = function () {
                        const userId = document.getElementById('folderPermUser')?.value;
                        const role = document.getElementById('folderPermRole')?.value;
                        if (!userId || !role) return;
                        secureFetch(`/${window.projectNamespace}/folders/${folderId}/permissions/upsert/`, {
                            method: 'POST',
                            body: JSON.stringify({ user_id: Number(userId), role }),
                        }).then(r => r.json()).then(resp => {
                            if (!resp.success) throw new Error(resp.error || 'Failed to save permission');
                            loadFolderPermissions(folderId, folderName);
                        }).catch(err => showNotification('error', err.message || 'Failed to save permission'));
                    };
                }
            })
            .catch(err => showNotification('error', err.message || 'Failed to load permissions'));
    }
}

function initTagAddInline() {
    document.querySelectorAll('.btn-add-tag').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            const scanId = this.dataset.scanId;
            const tag = prompt('New tag');
            if (!tag) return;
            fetch(`/${window.projectNamespace}/patient/${scanId}/tags/add/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ tag })
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    const tagsCol = this.closest('.tags-col');
                    if (tagsCol) {
                        // Remove placeholder '-'
                        const dash = tagsCol.querySelector('small.text-muted');
                        if (dash) dash.remove();
                        // Append chip if not exists
                        if (!tagsCol.querySelector(`span.tag-badge[data-tag="${CSS.escape(tag)}"]`)) {
                            const span = document.createElement('span');
                            span.className = 'tag-badge';
                            span.setAttribute('data-tag', tag);
                            span.innerHTML = `
                                ${tag}
                                <button type="button" class="btn-remove-tag-inline" data-scan-id="${scanId}" data-tag="${tag}" title="Remove tag">&times;</button>
                            `;
                            tagsCol.insertBefore(span, this);
                            

                            
                            // Refresh the tags dropdown to include the new tag
                            if (window.refreshTagsDropdown) {
                                window.refreshTagsDropdown();
                            }

                        }
                    }
                    showNotification('success', 'Tag added');
                } else {
                    showNotification('error', data.error || 'Failed to add tag');
                }
            }).catch(() => showNotification('error', 'Network error'));
        });
    });
}

function initTagFilter() {
    const tagSearchInput = document.getElementById('tagSearchInput');
    const tagsDropdown = document.getElementById('tagsDropdown');
    const tagsInput = document.getElementById('tagsInput');
    
    if (!tagSearchInput || !tagsDropdown || !tagsInput) return;
    
    let selectedTags = new Set();
    
    // Initialize selected tags from hidden input
    const initialTags = tagsInput.value ? tagsInput.value.split(',').filter(t => t.trim()) : [];
    initialTags.forEach(tag => selectedTags.add(tag.trim()));
    updateSelectedTagsDisplay();
    
    // Populate dropdown with available tags
    populateTagsDropdown();
    
    // Show dropdown on focus
    tagSearchInput.addEventListener('focus', function() {
        tagsDropdown.classList.add('show');
        updateTagsDropdown();
    });
    
    // Hide dropdown on blur (with delay to allow clicking)
    tagSearchInput.addEventListener('blur', function() {
        setTimeout(() => {
            tagsDropdown.classList.remove('show');
        }, 200);
    });
    
    // Search functionality
    tagSearchInput.addEventListener('input', function() {
        updateTagsDropdown();
    });
    
    // Tag selection
    tagsDropdown.addEventListener('click', function(e) {
        const tagOption = e.target.closest('.tag-option');
        if (!tagOption) return;
        
        const tagName = tagOption.dataset.tag;
        if (selectedTags.has(tagName)) {
            selectedTags.delete(tagName);
        } else {
            selectedTags.add(tagName);
        }
        
        updateSelectedTagsDisplay();
        updateTagsDropdown();
        
        // Update URL to reflect current tag selection
        updateFilterURL();
        tagSearchInput.value = '';
        
        // Update URL to reflect current tag selection
        updateFilterURL();
    });
    
    // Remove tag
    tagSearchInput.parentNode.addEventListener('click', function(e) {
        if (e.target.classList.contains('remove-tag')) {
            const tagName = e.target.dataset.tag;
            selectedTags.delete(tagName);
            updateSelectedTagsDisplay();
            updateTagsDropdown();
        }
    });
    
    function populateTagsDropdown() {
        // Get all available tags from the page (you might need to pass this from Django)
        const availableTags = Array.from(document.querySelectorAll('.tag-badge')).map(tag => tag.dataset.tag);
        const uniqueTags = [...new Set(availableTags)];
        
        tagsDropdown.innerHTML = uniqueTags.map(tag => `
            <div class="tag-option" data-tag="${tag}" data-selected="false">
                <span class="tag-name">${tag}</span>
                <span class="tag-checkbox">
                    <i class="fas fa-check" style="display: none;"></i>
                </span>
            </div>
        `).join('');
    }
    
    // Function to refresh tags dropdown with current page tags
    function refreshTagsDropdown() {
        populateTagsDropdown();
        updateTagsDropdown();
    }
    
    // Make refreshTagsDropdown globally accessible
    window.refreshTagsDropdown = refreshTagsDropdown;
    
    function updateTagsDropdown() {
        const searchTerm = tagSearchInput.value.toLowerCase();
        const tagOptions = tagsDropdown.querySelectorAll('.tag-option');
        
        tagOptions.forEach(option => {
            const tagName = option.dataset.tag;
            const isSelected = selectedTags.has(tagName);
            const matchesSearch = tagName.toLowerCase().includes(searchTerm);
            
            option.style.display = matchesSearch ? 'block' : 'none';
            option.dataset.selected = isSelected.toString();
            const checkbox = option.querySelector('.tag-checkbox i');
            if (checkbox) {
                checkbox.style.display = isSelected ? 'inline' : 'none';
            }
        });
    }
    
    function updateSelectedTagsDisplay() {
        const tagsArray = Array.from(selectedTags);
        tagsInput.value = tagsArray.join(',');
        
        // Clear existing tag chips
        const existingChips = tagSearchInput.parentNode.querySelectorAll('.tag-chip');
        existingChips.forEach(chip => chip.remove());
        
        // Add tag chips before the input
        tagsArray.forEach(tag => {
            const tagChip = document.createElement('span');
            tagChip.className = 'tag-chip';
            tagChip.innerHTML = `
                ${tag}
                <button type="button" class="remove-tag" data-tag="${tag}">&times;</button>
            `;
            tagSearchInput.parentNode.insertBefore(tagChip, tagSearchInput);
        });
        
        // Update placeholder visibility
        if (tagsArray.length > 0) {
            tagSearchInput.placeholder = 'Add more tags...';
        } else {
            tagSearchInput.placeholder = 'Search tags...';
        }
    }
}

function initStatusFilterButtons() {
    const statusButtons = document.querySelectorAll('.status-filter-btn');
    
    // Initialize button states based on current filter values
    initializeStatusButtonStates();
    
    statusButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const filterKey = this.dataset.filter; // e.g., 'status_cbct' or legacy 'ios'
            const isReports = filterKey === 'reports';
            const isDynamic = filterKey.startsWith('status_');
            const currentValue = this.dataset.value || '';
            
            let newValue, newClass;
            
            // Reports filter: '' -> yes (green) -> '' (gray)
            if (isReports) {
                if (currentValue === '') { newValue = 'yes'; newClass = 'status-green'; }
                else { newValue = ''; newClass = 'status-gray'; }
            }
            // Dynamic filters cycle: '' -> processed (green) -> processing (yellow) -> failed (red) -> ''
            else if (isDynamic) {
                if (currentValue === '') { newValue = 'processed'; newClass = 'status-green'; }
                else if (currentValue === 'processed') { newValue = 'processing'; newClass = 'status-yellow'; }
                else if (currentValue === 'processing') { newValue = 'failed'; newClass = 'status-red'; }
                else { newValue = ''; newClass = 'status-gray'; }
            } else {
                // Legacy filters: '' -> yes (green) -> no (yellow) -> failed (red) -> ''
                if (currentValue === '') { newValue = 'yes'; newClass = 'status-green'; }
                else if (currentValue === 'yes') { newValue = 'no'; newClass = 'status-yellow'; }
                else if (currentValue === 'no') { newValue = 'failed'; newClass = 'status-red'; }
                else { newValue = ''; newClass = 'status-gray'; }
            }
            
            // Update button state
            this.dataset.value = newValue;
            this.className = `status-filter-btn ${newClass}`;
            
            // Update hidden input value
            const hiddenInput = document.getElementById(isDynamic ? `${filterKey}_value` : `${filterKey}FilterValue`);
            if (hiddenInput) {
                hiddenInput.value = newValue;
            }
            
            // Update button title
            updateButtonTitle(this, filterKey, newValue);
            
            // Update URL to reflect current filter state
            updateFilterURL();
        });
    });
}

function initializeStatusButtonStates() {
    // Legacy
    const legacyMappings = [
        { filter: 'ios', inputId: 'iosFilterValue', buttonSelector: '[data-filter="ios"]' },
        { filter: 'cbct', inputId: 'cbctFilterValue', buttonSelector: '[data-filter="cbct"]' },
        { filter: 'bite', inputId: 'biteFilterValue', buttonSelector: '[data-filter="bite"]' },
        { filter: 'voice', inputId: 'voiceFilterValue', buttonSelector: '[data-filter="voice"]' },
        { filter: 'reports', inputId: 'reportsFilterValue', buttonSelector: '[data-filter="reports"]' }
    ];
    legacyMappings.forEach(mapping => {
        const input = document.getElementById(mapping.inputId);
        const button = document.querySelector(mapping.buttonSelector);
        if (input && button) {
            const value = input.value;
            let className = 'status-gray';
            if (value === 'yes') className = 'status-green';
            else if (value === 'no') className = 'status-yellow';
            else if (value === 'failed') className = 'status-red';
            button.dataset.value = value;
            button.className = `status-filter-btn ${className}`;
            updateButtonTitle(button, mapping.filter, value);
        }
    });

    // Dynamic buttons
    document.querySelectorAll('input[id^="status_"][id$="_value"]').forEach(input => {
        const slug = input.id.replace(/^status_(.*)_value$/, '$1');
        const button = document.querySelector(`[data-filter="status_${slug}"]`);
        if (!button) return;
        const value = input.value;
        let className = 'status-gray';
        if (value === 'processed') className = 'status-green';
        else if (value === 'processing') className = 'status-yellow';
        else if (value === 'failed') className = 'status-red';
        button.dataset.value = value;
        button.className = `status-filter-btn ${className}`;
        updateButtonTitle(button, `status_${slug}`, value);
    });
}

function updateButtonTitle(button, filterKey, value) {
    if (filterKey.startsWith('status_')) {
        const name = button.textContent.trim() || filterKey.replace('status_', '').toUpperCase();
        const labels = { '': `All ${name} (no filter)`, 'processed': `${name} processed`, 'processing': `${name} processing`, 'failed': `${name} failed` };
        button.title = labels[value] || labels[''];
    } else {
        const filter = filterKey;
        const filterLabels = {
            'ios': { '': 'All IOS (no filter)', 'yes': 'Has IOS', 'no': 'No IOS', 'failed': 'IOS Failed' },
            'cbct': { '': 'All CBCT (no filter)', 'yes': 'Has CBCT', 'no': 'No CBCT', 'failed': 'CBCT Failed' },
            'bite': { '': 'All Bite (no filter)', 'yes': 'Has Bite Classification', 'no': 'No Bite Classification', 'failed': 'Bite Classification Failed' },
            'voice': { '': 'All Voice (no filter)', 'yes': 'Has Voice', 'no': 'No Voice', 'failed': 'Voice Failed' },
            'reports': { '': 'All Reports (no filter)', 'yes': 'Has Reports' }
        };
        const title = value ? (filterLabels[filter] && filterLabels[filter][value]) : (filterLabels[filter] && filterLabels[filter]['']);
        button.title = title || '';
    }
}

function initInlineTagRemoval() {
    document.addEventListener('click', function(e) {

        if (e.target.classList.contains('btn-remove-tag-inline')) {
            e.preventDefault();
            const scanId = e.target.dataset.scanId;
            const tag = e.target.dataset.tag;
            
            if (confirm(`Remove tag "${tag}" from this scan?`)) {
                secureFetch(`/${window.projectNamespace}/patient/${scanId}/tags/remove/`, {
                    method: 'POST',
                    body: JSON.stringify({ tag })
                }).then(r => r.json()).then(data => {
                    if (data.success) {
                        // Remove the tag badge from the UI
                        const tagBadge = e.target.closest('.tag-badge');
                        if (tagBadge) {
                            tagBadge.remove();
                        }
                        
                        // If no tags left, show the placeholder
                        const tagsCol = e.target.closest('.tags-col');
                        if (tagsCol && !tagsCol.querySelector('.tag-badge')) {
                            // Clear the column and add placeholder and button
                            tagsCol.innerHTML = '';
                            const placeholder = document.createElement('small');
                            placeholder.className = 'text-muted';
                            placeholder.textContent = '-';
                            tagsCol.appendChild(placeholder);
                            
                            // Re-add the add tag button
                            const addButton = document.createElement('button');
                            addButton.className = 'btn btn-sm btn-outline-secondary p-0 ms-1 btn-add-tag';
                            addButton.dataset.scanId = scanId;
                            addButton.title = 'Add tag';
                            addButton.innerHTML = '<i class="fas fa-plus" style="font-size: 0.65rem;"></i>';
                            tagsCol.appendChild(addButton);
                            // Re-initialize the add tag functionality
                            initTagAddInline();
                        }
                        
                        // Refresh the tags dropdown to reflect the removed tag
                        if (window.refreshTagsDropdown) {
                            window.refreshTagsDropdown();
                        }
                        
                        showNotification('success', 'Tag removed successfully');
                    } else {
                        showNotification('error', data.error || 'Failed to remove tag');
                    }
                }).catch(() => showNotification('error', 'Network error'));
            }
        }
    });
}

// Initialize everything
document.addEventListener('DOMContentLoaded', function() {
    autoExpandFilters();
    initListNameEditing();
    initAdminActions();
    initFilterRemoveButtons();
    initBulkSelection();
    initCreateFolder();
    initFolderContextMenu();
    initTagAddInline();
    initTagFilter();
    initStatusFilterButtons();
    initInlineTagRemoval();
    
    // Ensure tag dropdown is refreshed after all initialization
    setTimeout(() => {
        if (window.refreshTagsDropdown) {
            window.refreshTagsDropdown();
        }
    }, 100);
    
    // Add form submission handler to clean empty values
    const filterForm = document.getElementById('filterForm');
    if (filterForm) {
        filterForm.addEventListener('submit', cleanFormSubmission);
    }
    
    // Override per_page select onchange to use clean submission
    const perPageSelect = document.querySelector('select[name="per_page"]');
    if (perPageSelect) {
        perPageSelect.addEventListener('change', function() {
            handlePerPageChange(this);
        });
        // Remove the inline onchange to prevent double execution
        perPageSelect.removeAttribute('onchange');
    }
    
    // Add search input change handler to update URL
    const searchInput = document.querySelector('input[name="search"]');
    if (searchInput) {
        searchInput.addEventListener('input', function() {
            // Debounce the URL update to avoid too many updates
            clearTimeout(window.searchTimeout);
            window.searchTimeout = setTimeout(() => {
                updateFilterURL();
            }, 500);
        });
    }

    // Rotate chevrons on collapses
    function initChevronRotation(toggleId, collapseId) {
        const toggle = document.getElementById(toggleId);
        const collapseEl = document.getElementById(collapseId);
        if (!toggle || !collapseEl) return;
        const icon = toggle.querySelector('i');
        function syncIcon() {
            const isShown = collapseEl.classList.contains('show');
            icon.classList.toggle('fa-chevron-up', isShown);
            icon.classList.toggle('fa-chevron-down', !isShown);
        }
        collapseEl.addEventListener('shown.bs.collapse', syncIcon);
        collapseEl.addEventListener('hidden.bs.collapse', syncIcon);
        // initial state
        syncIcon();
    }

    initChevronRotation('filtersCollapseToggle', 'filtersCollapse');
    initChevronRotation('bulkCollapseToggle', 'bulkCollapse');

    // Ensure Filters and Bulk actions are expanded by default
    function ensureShown(collapseId) {
        const el = document.getElementById(collapseId);
        if (!el) return;
        if (!el.classList.contains('show') && window.bootstrap) {
            const instance = window.bootstrap.Collapse.getOrCreateInstance(el, { toggle: false });
            instance.show();
        }
    }
    ensureShown('filtersCollapse');
    ensureShown('bulkCollapse');
}); 
