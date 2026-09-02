/**
 * Patient Detail Page - Main UI Controller
 * Handles common UI elements and modality viewer coordination
 */

function closeClassificationDropdown(dropdown, restoreFocus) {
    if (!dropdown) return;

    dropdown.classList.remove('show');
    dropdown.classList.remove('open-up');
    dropdown.style.position = '';
    dropdown.style.left = '';
    dropdown.style.top = '';
    dropdown.style.bottom = '';
    dropdown.style.width = '';
    const button = dropdown.previousElementSibling;
    if (button) {
        button.setAttribute('aria-expanded', 'false');
        if (restoreFocus) button.focus();
    }
}

function setSelectedClassificationOption(button, dropdown) {
    const selectedText = button.textContent.trim();
    dropdown.querySelectorAll('.dropdown-option').forEach(option => {
        option.setAttribute('aria-selected', String(option.textContent.trim() === selectedText));
    });
}

function focusClassificationOption(dropdown, position) {
    const options = Array.from(dropdown.querySelectorAll('.dropdown-option'));
    if (!options.length) return;

    const selectedIndex = options.findIndex(option => option.getAttribute('aria-selected') === 'true');
    const index = position === 'last' ? options.length - 1 : Math.max(selectedIndex, 0);
    options[index].focus();
}

// Bite classification dropdowns
function toggleDropdown(button) {
    if (!window.canEdit) {
        return; // Not editable for non-annotators
    }
    
    // Close all other dropdowns
    document.querySelectorAll('.value-dropdown.show').forEach(dropdown => {
        if (dropdown !== button.nextElementSibling) {
            closeClassificationDropdown(dropdown, false);
        }
    });
    
    // Toggle this dropdown
    const dropdown = button.nextElementSibling;
    if (dropdown) {
        const willShow = !dropdown.classList.contains('show');
        dropdown.classList.toggle('show', willShow);
        button.setAttribute('aria-expanded', String(willShow));

        if (!willShow) return;

        setSelectedClassificationOption(button, dropdown);

        // The side-panel card clips absolutely-positioned dropdowns (overflow
        // hidden/auto), so pin the menu to the viewport at the button's rect and
        // flip it upward when there is no room below.
        const rect = button.getBoundingClientRect();
        const menuWidth = dropdown.offsetWidth || Math.max(rect.width, 160);
        const menuHeight = dropdown.offsetHeight || dropdown.scrollHeight || 0;
        const gap = 6;
        const spaceBelow = window.innerHeight - rect.bottom;
        const openUp = menuHeight > 0 && spaceBelow < menuHeight + gap && rect.top > menuHeight + gap + 12;
        const left = Math.max(8, Math.min(rect.left, window.innerWidth - menuWidth - 8));
        dropdown.classList.toggle('open-up', openUp);
        dropdown.style.position = 'fixed';
        dropdown.style.left = left + 'px';
        dropdown.style.width = Math.max(rect.width, Math.min(menuWidth, window.innerWidth - 16)) + 'px';
        if (openUp) {
            dropdown.style.top = 'auto';
            dropdown.style.bottom = (window.innerHeight - rect.top + gap) + 'px';
        } else {
            dropdown.style.top = (rect.bottom + gap) + 'px';
            dropdown.style.bottom = 'auto';
        }

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
    option.parentElement.querySelectorAll('.dropdown-option').forEach(item => {
        item.setAttribute('aria-selected', String(item === option));
    });
    closeClassificationDropdown(button.nextElementSibling, true);
    
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
            closeClassificationDropdown(dropdown, false);
        });
    }
});

document.addEventListener('keydown', function(event) {
    const button = event.target.closest('.value-button');
    if (button && event.key === 'Escape') {
        const dropdown = button.nextElementSibling;
        if (dropdown && dropdown.classList.contains('show')) {
            event.preventDefault();
            closeClassificationDropdown(dropdown, true);
        }
        return;
    }

    if (button && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        const dropdown = button.nextElementSibling;
        if (!dropdown) return;
        if (!dropdown.classList.contains('show')) toggleDropdown(button);
        focusClassificationOption(dropdown, event.key === 'ArrowUp' ? 'last' : 'selected');
        return;
    }

    const option = event.target.closest('.dropdown-option');
    if (!option) return;

    const dropdown = option.parentElement;
    const options = Array.from(dropdown.querySelectorAll('.dropdown-option'));
    const currentIndex = options.indexOf(option);

    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const offset = event.key === 'ArrowDown' ? 1 : -1;
        options[(currentIndex + offset + options.length) % options.length].focus();
    } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        options[event.key === 'Home' ? 0 : options.length - 1].focus();
    } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        updateClassification(dropdown.previousElementSibling, option);
    } else if (event.key === 'Escape') {
        event.preventDefault();
        closeClassificationDropdown(dropdown, true);
    } else if (event.key === 'Tab') {
        closeClassificationDropdown(dropdown, false);
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

    // Image modalities (panoramic/intraoral/teleradiography) hide both control
    // groups; collapse the toolbar bar so it does not render as an empty strip
    // between the modality selector and the viewer.
    const updateToolbarVisibility = function() {
        const toolbar = document.querySelector('.viewer-toolbar');
        if (!toolbar) return;
        const anyVisible = [iosControls, cbctControls].some(function(group) {
            return group && group.style.display !== 'none';
        });
        toolbar.style.display = anyVisible ? '' : 'none';
    };

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
        // **A CBCT patient with no panoramic yet is not an error, so it must not be
        // fetched as one.** The panoramic pane is offered for every CBCT (one can be
        // generated from the volume), and asking `?meta=1` before one exists is a
        // request the server can only answer 404 -- which the browser logs as a failed
        // GET before any handler runs, so no amount of catching quiets it. The page is
        // told at render time whether a panoramic file exists; where none does, show
        // the pane's own empty state and make no request.
        if (window.hasPanoramicImage === false) {
            window.PanoramicViewer.showInlineEmptyForCBCT();
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
                if (iosControls) iosControls.style.display = 'flex';
                if (cbctControls) cbctControls.style.display = 'none';
                
                // The IOS mesh viewer is a Cornerstone module that starts itself, like
                // teleradiography and intraoral. Nothing to initialise on a tab switch.
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
                if (cbctControls) cbctControls.style.display = 'flex';
                
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
                    // Same as teleradiography below: the Cornerstone photo stack mounts
                    // itself on import and sizes itself from a ResizeObserver, so showing
                    // the tab is all this has to do. There is no load() to call and no
                    // window global to call it on -- the bundle is an ES module.
                    intraoralViewer.style.display = 'block';
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
                    // The Cornerstone photo stack mounts itself on import and sizes
                    // itself from a ResizeObserver, so showing the tab is all this has
                    // to do. There is no load() to call -- and no window global to call
                    // it on: the bundle is an ES module.
                    teleradiographyViewer.style.display = 'block';
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
                        // Same reasoning as `loadCbctInlinePanoramic`: the standalone
                        // pane is offered for every CBCT, and asking for a panoramic
                        // that does not exist is a 404 the browser logs on our behalf.
                        if (window.hasPanoramicImage === false) {
                            window.PanoramicViewer.showEmpty(
                                window.PanoramicViewer.targets.standalone
                            );
                        } else {
                            window.PanoramicViewer.load();
                        }
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
                if (cbctControls) cbctControls.style.display = 'flex';

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
            updateToolbarVisibility();
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
        if (iosControls) iosControls.style.display = 'flex';
        if (cbctControls) cbctControls.style.display = 'none';
        updateToolbarVisibility();
        return;
    }

    // CBCT-only case
    if (!iosRadio && cbctRadio) {
        if (iosContainer) iosContainer.style.display = 'none';
        if (cbctContainer) cbctContainer.style.display = 'block';
        if (iosControls) iosControls.style.display = 'none';
        if (cbctControls) cbctControls.style.display = 'flex';
        setTimeout(() => {
            ensureCbctViewerReady('cbct');
            loadCbctInlinePanoramic();
        }, 100);
        updateToolbarVisibility();
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
                if (iosControls) iosControls.style.display = 'flex';
                if (cbctControls) cbctControls.style.display = 'none';
                
                // The IOS mesh viewer is a Cornerstone module that starts itself, like
                // teleradiography and intraoral. Nothing to initialise on a tab switch.
            }
        });
    }

    if (cbctRadio) {
        cbctRadio.addEventListener('change', function() {
            if (this.checked && window.hasCBCT) {
                if (iosContainer) iosContainer.style.display = 'none';
                if (cbctContainer) cbctContainer.style.display = 'block';
                if (iosControls) iosControls.style.display = 'none';
                if (cbctControls) cbctControls.style.display = 'flex';

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
    window.hasPanoramicImage = djangoData.hasPanoramicImage;
    window.isCBCTProcessed = djangoData.isCBCTProcessed;
    window.modalities = Array.isArray(djangoData.modalities) ? djangoData.modalities : [];
    window.defaultModality = djangoData.defaultModality || null;
    
    console.debug('Can edit:', window.canEdit);
    console.debug('Scan ID:', window.scanId);
    console.debug('Has CBCT:', window.hasCBCT);
    console.debug('Is CBCT processed:', window.isCBCTProcessed);

    // Initialize image modality viewers. IOS and intraoral are absent on purpose -- both
    // are Cornerstone modules that start themselves, like teleradiography.
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
