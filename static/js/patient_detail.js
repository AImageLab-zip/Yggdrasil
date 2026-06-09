/**
 * Patient Detail Page - Main UI Controller
 * Handles common UI elements and modality viewer coordination
 */

// Revolutionary Classification UI Functions
function toggleDropdown(button) {
    if (!window.canEdit) {
        return; // Not editable for non-annotators
    }
    
    // Close all other dropdowns
    document.querySelectorAll('.value-dropdown.show').forEach(dropdown => {
        if (dropdown !== button.nextElementSibling) {
            dropdown.classList.remove('show');
        }
    });
    
    // Toggle this dropdown
    const dropdown = button.nextElementSibling;
    if (dropdown) {
        dropdown.classList.toggle('show');
        
        dropdown.querySelectorAll('.dropdown-option').forEach(option => {
            option.onclick = function() {
                updateClassification(button, option);
            };
        });
    }
}

function updateClassification(button, option) {
    const field = button.closest('.classification-value').dataset.field;
    const value = option.dataset.value;
    const displayText = option.textContent;
    
    // Update UI immediately
    button.textContent = displayText;
    button.classList.remove('ai-prediction');
    button.classList.add('manual-verified');
    
    // Hide dropdown
    button.nextElementSibling.classList.remove('show');
    
    // Save via AJAX
    postJson(`/${window.projectNamespace}/patient/${window.scanId}/update/`, {
        field: field,
        value: value
    })
    .then(data => {
        if (data.success) {
            showSavedIndicator();
            updatePageStatus();
        } else {
            console.error('Error saving classification:', data.error);
            button.classList.remove('manual-verified');
            button.classList.add('ai-prediction');
        }
    })
    .catch(error => {
        console.error('Network error:', error);
        button.classList.remove('manual-verified');
        button.classList.add('ai-prediction');
    });
}

function showSavedIndicator() {
    if (typeof window.appNotify === 'function') {
        window.appNotify('success', 'Saved');
        return;
    }

    const indicator = document.getElementById('savingIndicator');
    if (!indicator) {
        return;
    }
    indicator.style.display = 'block';
    setTimeout(() => {
        indicator.style.display = 'none';
    }, 2000);
}

function notify(type, message) {
    if (typeof window.appNotify === 'function') {
        window.appNotify(type, message);
        return;
    }
}

function updatePageStatus() {
    const statusBadge = document.querySelector('.status-badge');
    if (statusBadge && statusBadge.classList.contains('ai-pending')) {
        statusBadge.innerHTML = '<i class="fas fa-check-circle me-1"></i>VERIFIED';
        statusBadge.classList.remove('ai-pending');
        statusBadge.classList.add('manual-verified');
        
        const quickActions = document.querySelector('.quick-actions');
        if (quickActions) {
            quickActions.style.display = 'none';
        }
    }
}

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

function getCSRFToken() {
    const csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (csrfInput) {
        return csrfInput.value;
    }
    return getCookie('csrftoken');
}

function postJson(url, payload) {
    const headers = {
        'Content-Type': 'application/json'
    };
    const token = getCSRFToken();
    if (token) {
        headers['X-CSRFToken'] = token;
    }

    return fetch(url, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload)
    }).then(response => response.text().then(text => {
        let data = {};
        if (text) {
            try {
                data = JSON.parse(text);
            } catch (error) {
                data = {};
            }
        }
        if (!response.ok) {
            const message = data.error || `Request failed (${response.status})`;
            throw new Error(message);
        }
        return data;
    }));
}

function setScanNameDisplay(nameDisplay, value) {
    nameDisplay.innerHTML = `<strong>${escapeHtml(value)}</strong>`;
}

function syncManagementNameField(value) {
    const managementNameInput = document.querySelector('.scan-management-form input[name="name"]');
    if (managementNameInput) {
        managementNameInput.value = value;
    }
}

// Close dropdowns when clicking outside
document.addEventListener('click', function(event) {
    if (!event.target.closest('.classification-value')) {
        document.querySelectorAll('.value-dropdown.show').forEach(dropdown => {
            dropdown.classList.remove('show');
        });
    }
});

// Inline name editing functionality
function initNameEditing() {
    const editBtn = document.querySelector('.btn-edit-name');
    const nameDisplay = document.querySelector('.scan-name-display');
    
    if (!editBtn || !nameDisplay) return;
    
    editBtn.addEventListener('click', function() {
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
        input.className = 'name-edit-input';
        input.style.width = '200px';
        
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
            
            postJson(`/${window.projectNamespace}/patient/${window.scanId}/update-name/`, {
                name: newName
            })
            .then(data => {
                if (data.success) {
                    setScanNameDisplay(nameDisplay, data.name);
                    syncManagementNameField(data.name);
                    if (input.parentNode) {
                        input.parentNode.replaceChild(nameDisplay, input);
                    }
                    showSavedIndicator();
                } else {
                    notify('error', 'Error saving name: ' + (data.error || 'Unknown error'));
                    if (input.parentNode) {
                        input.parentNode.replaceChild(nameDisplay, input);
                    }
                }
            })
            .catch(error => {
                console.error('Error:', error);
                notify('error', 'Error saving name: ' + (error.message || 'Unknown error'));
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
}

// Initialize confirm review functionality
function initConfirmReview() {
    const confirmBtn = document.getElementById('confirmReview');
    if (confirmBtn) {
        confirmBtn.addEventListener('click', function() {
            // Create form and submit to accept AI predictions
            const form = document.createElement('form');
            form.method = 'POST';
            form.style.display = 'none';
            
            const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
            const csrfInput = document.createElement('input');
            csrfInput.type = 'hidden';
            csrfInput.name = 'csrfmiddlewaretoken';
            csrfInput.value = csrfToken;
            
            const actionInput = document.createElement('input');
            actionInput.type = 'hidden';
            actionInput.name = 'action';
            actionInput.value = 'accept_ai';
            
            form.appendChild(csrfInput);
            form.appendChild(actionInput);
            document.body.appendChild(form);
            form.submit();
        });
    }
}

// Initialize viewer toggle functionality
function initViewerToggle() {
    const iosRadio = document.getElementById('iosViewer');
    const cbctRadio = document.getElementById('cbctViewer');
    const iosContainer = document.getElementById('ios-viewer');
    const cbctContainer = document.getElementById('cbct-viewer');
    const iosControls = document.getElementById('iosControls');
    const cbctControls = document.getElementById('cbctControls');
    const toggleGroup = document.getElementById('modalityToggleGroup');

    const ensureCbctViewerReady = function(modality) {
        if (typeof window.CBCTViewer === 'undefined') {
            return;
        }

        const targetModality = modality || 'cbct';
        if (targetModality !== 'cbct') {
            if (!window.CBCTViewer.loading) {
                window.CBCTViewer.init(targetModality);
            }
            return;
        }

        if (!window.CBCTViewer.loading) {
            window.CBCTViewer.init('cbct');
        }
    };

    const loadCbctInlinePanoramic = function() {
        if (typeof window.PanoramicViewer === 'undefined') {
            return;
        }
        if (typeof window.PanoramicViewer.loadInlineForCBCT !== 'function') {
            return;
        }
        window.PanoramicViewer.loadInlineForCBCT();
    };

    // Generic modality switching for dynamically rendered toggles
    if (toggleGroup) {
        toggleGroup.addEventListener('change', function(e) {
            const target = e.target;
            if (!target || target.type !== 'radio') return;
            const label = toggleGroup.querySelector(`label[for="${target.id}"]`);
            const modality = (label && label.dataset.modality) || (target.id && target.id.startsWith('modality_') ? target.id.substring('modality_'.length) : null);
            if (!modality) return;

            // Show relevant container
            if (modality === 'ios') {
                // Hide all image viewers
                const imageViewers = ['intraoral-viewer', 'teleradiography-viewer', 'panoramic-viewer'];
                imageViewers.forEach(viewerId => {
                    const viewer = document.getElementById(viewerId);
                    if (viewer) viewer.style.display = 'none';
                });
                
                if (iosContainer) iosContainer.style.display = 'block';
                if (cbctContainer) cbctContainer.style.display = 'none';
                if (iosControls) iosControls.style.display = 'block';
                if (cbctControls) cbctControls.style.display = 'none';
                
                // Initialize IOS viewer if not already done
                if (typeof window.IOSViewer !== 'undefined') {
                    window.IOSViewer.init();
                }
            } else if (modality === 'cbct') {
                // Hide all image viewers
                const imageViewers = ['intraoral-viewer', 'teleradiography-viewer', 'panoramic-viewer'];
                imageViewers.forEach(viewerId => {
                    const viewer = document.getElementById(viewerId);
                    if (viewer) viewer.style.display = 'none';
                });
                
                if (iosContainer) iosContainer.style.display = 'none';
                if (cbctContainer) cbctContainer.style.display = 'block';
                if (iosControls) iosControls.style.display = 'none';
                if (cbctControls) cbctControls.style.display = 'block';
                
                // Show cbct-viewer container
                const cbctViewer = document.getElementById('cbct-viewer');
                if (cbctViewer) cbctViewer.style.display = 'block';
                
                // Only initialize viewer if CBCT is processed
                if (window.isCBCTProcessed) {
                    setTimeout(() => {
                        ensureCbctViewerReady('cbct');
                        loadCbctInlinePanoramic();
                    }, 100);
                } else {
                    console.debug('CBCT not processed yet, skipping viewer initialization');
                }
            } else if (modality === 'intraoral' || modality === 'intraoral-photo') {
                // Handle intraoral photos viewer
                if (iosContainer) iosContainer.style.display = 'none';
                if (cbctContainer) cbctContainer.style.display = 'none';
                if (iosControls) iosControls.style.display = 'none';
                if (cbctControls) cbctControls.style.display = 'none';

                // Hide all viewer containers (but NOT scan-viewer which is inside ios-viewer)
                const allViewers = document.querySelectorAll('[id$="-viewer"]:not(#scan-viewer)');
                allViewers.forEach(el => el.style.display = 'none');
                
                const intraoralViewer = document.getElementById('intraoral-viewer');
                if (intraoralViewer) {
                    intraoralViewer.style.display = 'block';
                    if (typeof window.IntraoralViewer !== 'undefined') {
                        window.IntraoralViewer.load();
                    }
                }
            } else if (modality === 'teleradiography') {
                // Handle teleradiography viewer
                if (iosContainer) iosContainer.style.display = 'none';
                if (cbctContainer) cbctContainer.style.display = 'none';
                if (iosControls) iosControls.style.display = 'none';
                if (cbctControls) cbctControls.style.display = 'none';

                // Hide all viewer containers (but NOT scan-viewer which is inside ios-viewer)
                const allViewers = document.querySelectorAll('[id$="-viewer"]:not(#scan-viewer)');
                allViewers.forEach(el => el.style.display = 'none');
                
                const teleradiographyViewer = document.getElementById('teleradiography-viewer');
                if (teleradiographyViewer) {
                    teleradiographyViewer.style.display = 'block';
                    if (typeof window.TeleradiographyViewer !== 'undefined') {
                        window.TeleradiographyViewer.load();
                    }
                }
            } else if (modality === 'panoramic') {
                // Handle panoramic viewer
                if (iosContainer) iosContainer.style.display = 'none';
                if (cbctContainer) cbctContainer.style.display = 'none';
                if (iosControls) iosControls.style.display = 'none';
                if (cbctControls) cbctControls.style.display = 'none';

                // Hide all viewer containers (but NOT scan-viewer which is inside ios-viewer)
                const allViewers = document.querySelectorAll('[id$="-viewer"]:not(#scan-viewer)');
                allViewers.forEach(el => el.style.display = 'none');
                
                const panoramicViewer = document.getElementById('panoramic-viewer');
                if (panoramicViewer) {
                    panoramicViewer.style.display = 'block';
                    if (typeof window.PanoramicViewer !== 'undefined') {
                        window.PanoramicViewer.load();
                    }
                }
            } else {
                // Show generic container for other volume modalities (but not image modalities)
                // Image modalities are handled explicitly above
                const imageModalities = ['intraoral', 'intraoral-photo', 'teleradiography', 'panoramic'];
                
                if (imageModalities.includes(modality)) {
                    // This should not happen as image modalities are handled explicitly above
                    console.warn(`Image modality ${modality} should not reach generic volume handler`);
                    return;
                }
                
                // For actual volume modalities (like brain MRI), reuse CBCT controls (windowing/reset)
                if (iosContainer) iosContainer.style.display = 'none';
                if (cbctContainer) cbctContainer.style.display = 'none';
                if (iosControls) iosControls.style.display = 'none';
                if (cbctControls) cbctControls.style.display = 'block';

                const generic = document.getElementById(`${modality}-viewer`);
                const allGeneric = document.querySelectorAll('[id$="-viewer"]:not(#scan-viewer)');
                if (allGeneric && allGeneric.length) {
                    allGeneric.forEach(el => {
                        if (el && el.id !== 'ios-viewer' && el.id !== 'cbct-viewer' && 
                            el.id !== 'intraoral-viewer' && el.id !== 'teleradiography-viewer' && 
                            el.id !== 'panoramic-viewer') {
                            el.style.display = 'none';
                        }
                    });
                }
                if (generic) {
                    generic.style.display = 'block';
                    // Initialize volume viewer for this modality using CBCT viewer backend
                    ensureCbctViewerReady(modality);
                }
            }
        });

        // Ensure a default selection is applied if radios rendered without checked
        const anyChecked = toggleGroup.querySelector('input[type="radio"][name="viewerType"]:checked');
        if (!anyChecked) {
            const preferredSlug = window.defaultModality || (window.hasIOS ? 'ios' : (window.hasCBCT ? 'cbct' : null));
            if (preferredSlug) {
                const preferredInput = document.getElementById(`modality_${preferredSlug}`);
                if (preferredInput) {
                    preferredInput.checked = true;
                    // If the element or its label is hidden on initial layout, delay dispatch
                    setTimeout(() => {
                        preferredInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }, 0);
                }
            }
        } else {
            // Ensure initial viewer initialization even if radio was pre-checked by server
            setTimeout(() => {
                anyChecked.dispatchEvent(new Event('change', { bubbles: true }));
            }, 0);
        }
    }

    // IOS-only case
    if (iosRadio && !cbctRadio) {
        if (iosContainer) iosContainer.style.display = 'block';
        if (cbctContainer) cbctContainer.style.display = 'none';
        if (iosControls) iosControls.style.display = 'block';
        if (cbctControls) cbctControls.style.display = 'none';
        return;
    }

    // CBCT-only case
    if (!iosRadio && cbctRadio) {
        if (iosContainer) iosContainer.style.display = 'none';
        if (cbctContainer) cbctContainer.style.display = 'block';
        if (iosControls) iosControls.style.display = 'none';
        if (cbctControls) cbctControls.style.display = 'block';
        setTimeout(() => {
            ensureCbctViewerReady('cbct');
            loadCbctInlinePanoramic();
        }, 100);
        return;
    }

    // Both toggles exist
    if (cbctRadio && typeof window.hasCBCT !== 'undefined' && !window.hasCBCT) {
        cbctRadio.disabled = true;
        if (cbctRadio.parentElement) {
            cbctRadio.parentElement.classList.add('disabled');
            cbctRadio.parentElement.title = 'No CBCT data available';
        }
    }

    // Handle initial state based on which radio button is checked
    if (cbctRadio && cbctRadio.checked && window.hasCBCT && window.isCBCTProcessed) {
        ensureCbctViewerReady('cbct');
        loadCbctInlinePanoramic();
    }

    if (iosRadio) {
        iosRadio.addEventListener('change', function() {
            if (this.checked) {
                if (iosContainer) iosContainer.style.display = 'block';
                if (cbctContainer) cbctContainer.style.display = 'none';
                if (iosControls) iosControls.style.display = 'block';
                if (cbctControls) cbctControls.style.display = 'none';
                
                // Initialize IOS viewer if not already done
                if (typeof window.IOSViewer !== 'undefined') {
                    window.IOSViewer.init();
                }
            }
        });
    }

    if (cbctRadio) {
        cbctRadio.addEventListener('change', function() {
            if (this.checked && window.hasCBCT) {
                if (iosContainer) iosContainer.style.display = 'none';
                if (cbctContainer) cbctContainer.style.display = 'block';
                if (iosControls) iosControls.style.display = 'none';
                if (cbctControls) cbctControls.style.display = 'block';

                // Only initialize viewer if CBCT is processed
                if (window.isCBCTProcessed) {
                    // Handle CBCT viewer state with a delay to ensure containers are visible
                    setTimeout(() => {
                        ensureCbctViewerReady('cbct');
                        loadCbctInlinePanoramic();
                    }, 100); // 100ms delay to ensure containers are visible and sized
                } else {
                    console.debug('CBCT not processed yet, skipping viewer initialization');
                }
            }
        });
    }
} 

// Tag management
function initTagManagement() {
    const chips = document.getElementById('tagChips');
    const addBtn = document.getElementById('btnAddTag');
    const input = document.getElementById('newTagInput');
    if (!chips || !addBtn || !input) return;
    
    addBtn.addEventListener('click', () => addTag(input, chips));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            addTag(input, chips);
        }
    });
    chips.addEventListener('click', (e) => {
        const btn = e.target.closest('.btn-remove-tag');
        if (!btn) return;
        const tag = btn.dataset.tag;
        postJson(`/${window.projectNamespace}/patient/${window.scanId}/tags/remove/`, {
            tag: tag
        }).then(data => {
            if (data.success) {
                const toRemove = chips.querySelector(`[data-tag="${CSS.escape(tag)}"]`);
                if (toRemove) toRemove.remove();
                showSavedIndicator();
            } else {
                notify('error', data.error || 'Failed to remove tag');
            }
        }).catch(error => notify('error', error.message || 'Network error'));
    });
}

function addTag(input, chips) {
    const tag = (input.value || '').trim();
    if (!tag) return;
    postJson(`/${window.projectNamespace}/patient/${window.scanId}/tags/add/`, {
        tag: tag
    }).then(data => {
        if (data.success) {
            // add chip if not already present
            if (!chips.querySelector(`[data-tag="${CSS.escape(tag)}"]`)) {
                const span = document.createElement('span');
                span.className = 'badge rounded-pill bg-light text-dark border';
                span.setAttribute('data-tag', tag);
                span.innerHTML = `${escapeHtml(tag)} <button type="button" class="btn btn-sm btn-link text-danger p-0 ms-1 btn-remove-tag" data-tag="${escapeHtml(tag)}"><i class="fas fa-times"></i></button>`;
                chips.appendChild(span);
            }
            input.value = '';
            showSavedIndicator();
        } else {
            notify('error', data.error || 'Failed to add tag');
        }
    }).catch(error => notify('error', error.message || 'Network error'));
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.innerText = text;
    return div.innerHTML;
} 

function initFileManagement() {
    const addBtn = document.getElementById('addRawFileBtn');
    const fileTypeSelect = document.getElementById('rawFileTypeSelect');
    const fileInput = document.getElementById('rawFileInput');

    if (addBtn && fileTypeSelect && fileInput) {
        addBtn.addEventListener('click', () => {
            const fileType = (fileTypeSelect.value || '').trim();
            const file = fileInput.files && fileInput.files[0];
            if (!fileType) {
                notify('error', 'Select a raw file type');
                return;
            }
            if (!file) {
                notify('error', 'Select a file to upload');
                return;
            }

            const formData = new FormData();
            formData.append('file_type', fileType);
            formData.append('file', file);

            const headers = {};
            const token = getCSRFToken();
            if (token) {
                headers['X-CSRFToken'] = token;
            }

            fetch(`/${window.projectNamespace}/patient/${window.scanId}/files/raw/add/`, {
                method: 'POST',
                headers,
                body: formData,
            })
                .then(response => response.json().then(data => ({ ok: response.ok, data })))
                .then(({ ok, data }) => {
                    if (!ok || !data.success) {
                        throw new Error(data.error || 'Failed to add raw file');
                    }
                    showSavedIndicator();
                    window.location.reload();
                })
                .catch(error => notify('error', error.message || 'Network error'));
        });
    }

    document.querySelectorAll('.btn-delete-raw-file').forEach((btn) => {
        btn.addEventListener('click', () => {
            const fileId = btn.dataset.fileId;
            const fileName = btn.dataset.fileName || 'this file';
            if (!fileId) return;
            if (!window.confirm(`Delete raw file "${fileName}"? Related processed files will be removed and the job will be marked failed.`)) {
                return;
            }

            postJson(`/${window.projectNamespace}/patient/${window.scanId}/files/raw/${fileId}/delete/`, {})
                .then((data) => {
                    if (!data.success) {
                        throw new Error(data.error || 'Failed to remove raw file');
                    }
                    showSavedIndicator();
                    window.location.reload();
                })
                .catch((error) => notify('error', error.message || 'Network error'));
        });
    });
}

// Initialize everything when page loads
document.addEventListener('DOMContentLoaded', function() {
    console.debug('DOM Content Loaded - initializing...');
    
    // Get Django data
    const djangoData = JSON.parse(document.getElementById('django-data').textContent);
    window.canEdit = djangoData.canEdit;
    window.scanId = djangoData.scanId;
    window.hasIOS = djangoData.hasIOS;
    window.hasCBCT = djangoData.hasCBCT;
    window.isCBCTProcessed = djangoData.isCBCTProcessed;
    window.modalities = Array.isArray(djangoData.modalities) ? djangoData.modalities : [];
    window.defaultModality = djangoData.defaultModality || null;
    
    console.debug('Can edit:', window.canEdit);
    console.debug('Scan ID:', window.scanId);
    console.debug('Has CBCT:', window.hasCBCT);
    console.debug('Is CBCT processed:', window.isCBCTProcessed);

    // Preload CBCT volume in background only for legacy CBCT pipeline.
    // Fixed NiiVue grid has its own fetch/cache path and preloading here would duplicate work.
    let useLegacyVolumePreload = true;
    const viewerGridDataEl = document.getElementById('viewerGridData');
    if (viewerGridDataEl) {
        try {
            const viewerGridData = JSON.parse(viewerGridDataEl.textContent || '{}');
            if (viewerGridData.fixedMode) {
                useLegacyVolumePreload = false;
            }
        } catch (e) {
            console.warn('Unable to parse viewerGridData for preload gating:', e);
        }
    }

    if (useLegacyVolumePreload && window.hasCBCT && window.isCBCTProcessed && typeof window.VolumeLoader !== 'undefined') {
        window.VolumeLoader.preload('cbct');
    }

    // Initialize modality viewers
    if (window.hasIOS && typeof window.IOSViewer !== 'undefined') {
        console.debug('Initializing IOS viewer');
        window.IOSViewer.init();
    }
    
    // Initialize image modality viewers
    if (typeof window.IntraoralViewer !== 'undefined') {
        window.IntraoralViewer.init(window.scanId);
    }
    if (typeof window.TeleradiographyViewer !== 'undefined') {
        window.TeleradiographyViewer.init(window.scanId);
    }
    if (typeof window.PanoramicViewer !== 'undefined') {
        window.PanoramicViewer.init(window.scanId);
    }
    
    // Initialize other UI components
    initNameEditing();
    initConfirmReview();
    initViewerToggle();
    initTagManagement();
    initFileManagement();
});
