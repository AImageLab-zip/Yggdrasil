/**
 * Volume Upload UI Logic (modality-agnostic)
 * - Works for any 3D volume modality blocks (DICOM/NIfTI/etc.)
 * - Backwards compatible with legacy CBCT-only markup/IDs
 */

document.addEventListener('DOMContentLoaded', function() {
    // Upload scan page functionality
    initUploadToggle();
    
    // Scan detail page functionality
    initDetailToggle();

    // Ensure only one cbct/cbct_folder_files input is active across blocks
    initExclusiveSelection();

    // Track which modality the user is actually uploading
    initModalitySelection();
});

function initUploadToggle() {
    // Multi-instance support: look for all containers first
    const containers = document.querySelectorAll('.volume-upload-container, .cbct-upload-container');
    if (containers.length > 0) {
        containers.forEach(container => {
            const fileRadio = container.querySelector('input[type="radio"][value="file"]');
            const folderRadio = container.querySelector('input[type="radio"][value="folder"]');
            const fileSection = container.querySelector('.volume-file-section') || container.querySelector('.cbct-file-section') || container.querySelector('[id$="_file_section"]');
            const folderSection = container.querySelector('.volume-folder-section') || container.querySelector('.cbct-folder-section') || container.querySelector('[id$="_folder_section"]');
            const groupName = container.getAttribute('data-group') || '';

            if (!fileRadio || !folderRadio || !fileSection || !folderSection) {
                return;
            }

            function setUploadTypeHidden(value) {
                // Prefer generic field if present, else legacy
                const generic = document.querySelector('input[name="volume_upload_type"]');
                if (generic) generic.value = value;
                const legacy = document.querySelector('input[name="cbct_upload_type"]');
                if (legacy) legacy.value = value;
            }

            function toggleSections() {
                if (fileRadio.checked) {
                    fileSection.style.display = 'block';
                    folderSection.style.display = 'none';
                    // Clear folder input when switching to file mode
                    const folderInput = (groupName && container.querySelector('#' + groupName + '_folder')) || container.querySelector('input[type="file"][webkitdirectory]');
                    if (folderInput) {
                        folderInput.value = '';
                    }
                    // Set upload type to file (hidden field)
                    setUploadTypeHidden('file');
                } else if (folderRadio.checked) {
                    fileSection.style.display = 'none';
                    folderSection.style.display = 'block';
                    // Clear file input when switching to folder mode
                    const fileInput = fileSection.querySelector('input[type="file"]');
                    if (fileInput) {
                        fileInput.value = '';
                    }
                    // Set upload type to folder (hidden field)
                    setUploadTypeHidden('folder');
                }
            }

            fileRadio.addEventListener('change', toggleSections);
            folderRadio.addEventListener('change', toggleSections);
            
            // Set initial state for this container
            toggleSections();
        });
        return;
    }

    // Legacy single-instance support
    const fileRadio = document.getElementById('cbct_file_upload');
    const folderRadio = document.getElementById('cbct_folder_upload');
    const fileSection = document.getElementById('cbct_file_section');
    const folderSection = document.getElementById('cbct_folder_section');
    
    if (!fileRadio || !folderRadio || !fileSection || !folderSection) {
        return; // Not on upload page
    }
    
    function toggleSections() {
        if (fileRadio.checked) {
            fileSection.style.display = 'block';
            folderSection.style.display = 'none';
            const folderInput = document.getElementById('cbct_folder');
            if (folderInput) {
                folderInput.value = '';
            }
            const hiddenField = document.querySelector('input[name="volume_upload_type"]') || document.querySelector('input[name="cbct_upload_type"]');
            if (hiddenField) {
                hiddenField.value = 'file';
            }
        } else if (folderRadio.checked) {
            fileSection.style.display = 'none';
            folderSection.style.display = 'block';
            const fileInput = fileSection.querySelector('input[type="file"]');
            if (fileInput) {
                fileInput.value = '';
            }
            const hiddenField = document.querySelector('input[name="volume_upload_type"]') || document.querySelector('input[name="cbct_upload_type"]');
            if (hiddenField) {
                hiddenField.value = 'folder';
            }
        }
    }
    
    fileRadio.addEventListener('change', toggleSections);
    folderRadio.addEventListener('change', toggleSections);
    toggleSections();
}

function initDetailToggle() {
    const fileRadio = document.getElementById('cbct_file_upload_detail');
    const folderRadio = document.getElementById('cbct_folder_upload_detail');
    const fileSection = document.getElementById('cbct_file_section_detail');
    const folderSection = document.getElementById('cbct_folder_section_detail');
    
    if (!fileRadio || !folderRadio || !fileSection || !folderSection) {
        return; // Not on detail page
    }
    
    function toggleSections() {
        if (fileRadio.checked) {
            fileSection.style.display = 'block';
            folderSection.style.display = 'none';
            // Clear folder input when switching to file mode
            const folderInput = document.getElementById('cbct_folder_detail');
            if (folderInput) {
                folderInput.value = '';
            }
            // Set upload type to file
            const hiddenField = document.querySelector('input[name="volume_upload_type"]') || document.querySelector('input[name="cbct_upload_type"]');
            if (hiddenField) {
                hiddenField.value = 'file';
            }
        } else if (folderRadio.checked) {
            fileSection.style.display = 'none';
            folderSection.style.display = 'block';
            // Clear file input when switching to folder mode
            const fileInput = fileSection.querySelector('input[type="file"]');
            if (fileInput) {
                fileInput.value = '';
            }
            // Set upload type to folder
            const hiddenField = document.querySelector('input[name="volume_upload_type"]') || document.querySelector('input[name="cbct_upload_type"]');
            if (hiddenField) {
                hiddenField.value = 'folder';
            }
        }
    }
    
    fileRadio.addEventListener('change', toggleSections);
    folderRadio.addEventListener('change', toggleSections);
    
    // Set initial state
    toggleSections();
}

/**
 * Handle form submission validation (modality-agnostic)
 */
function handleFormSubmission() {
    const forms = document.querySelectorAll('form');
    
    forms.forEach(form => {
        // Only validate forms that contain file upload elements
        const hasFileInputs = form.querySelector('input[type="file"]') !== null;
        const hasUploadContainer = form.querySelector('.volume-upload-container, .cbct-upload-container') !== null;
        
        // Skip validation for forms that are not file upload forms (e.g., logout, search, etc.)
        if (!hasFileInputs && !hasUploadContainer) {
            return;
        }
        
        form.addEventListener('submit', function(e) {
            // Skip validation for scan management form (which only updates settings)
            const action = form.querySelector('input[name="action"]')?.value;
            if (action === 'update_management') {
                return true; // Allow scan management form to submit without file validation
            }
            
            // Check if at least one file is uploaded
            let hasAnyFile = false;
            const allFileInputs = form.querySelectorAll('input[type="file"]');
            allFileInputs.forEach(input => {
                if (input.files && input.files.length > 0) {
                    hasAnyFile = true;
                }
            });
            
            // If no files are being uploaded at all, show an error message
            if (!hasAnyFile) {
                e.preventDefault();
                if (typeof window.appNotify === 'function') {
                    window.appNotify('warning', 'Please upload at least one file.');
                }
                return false;
            }
            
            // Allow normal form submission
            return true;
        });
    });
}

// Initialize form submission handling
document.addEventListener('DOMContentLoaded', handleFormSubmission);

/**
 * Ensure exclusivity across duplicated inputs with the same name
 */
function initExclusiveSelection() {
    const forms = document.querySelectorAll('form');
    forms.forEach(form => {
        // Per-container exclusivity for generic/modern blocks
        const containers = form.querySelectorAll('.volume-upload-container, .cbct-upload-container');
        containers.forEach(container => {
            const groupName = container.getAttribute('data-group') || '';
            const fileInput = container.querySelector('input[type="file"]:not([webkitdirectory])');
            const folderInput = container.querySelector('input[type="file"][webkitdirectory]');

            const setUploadTypeHidden = (value) => {
                const generic = form.querySelector('input[name="volume_upload_type"]');
                if (generic) generic.value = value;
                const legacy = form.querySelector('input[name="cbct_upload_type"]');
                if (legacy) legacy.value = value;
            };
            // Selected modality field deprecated - modalities are now inferred from uploaded files

            if (fileInput) fileInput.addEventListener('change', () => {
                if (fileInput.files && fileInput.files.length > 0) {
                    if (folderInput) folderInput.value = '';
                    setUploadTypeHidden('file');
                }
            });
            if (folderInput) folderInput.addEventListener('change', () => {
                if (folderInput.files && folderInput.files.length > 0) {
                    if (fileInput) fileInput.value = '';
                    setUploadTypeHidden('folder');
                }
            });
        });

        // Legacy CBCT-only blocks (outside containers)
        const legacyFileInputs = form.querySelectorAll('input[type="file"][name="cbct"]');
        legacyFileInputs.forEach(input => {
            input.addEventListener('change', () => {
                if (input.files && input.files.length > 0) {
                    form.querySelectorAll('input[type="file"][name="cbct"]').forEach(other => { if (other !== input) other.value = ''; });
                    form.querySelectorAll('input[type="file"][name="cbct_folder_files"]').forEach(other => { other.value = ''; });
                    const hiddenField = form.querySelector('input[name="volume_upload_type"]') || form.querySelector('input[name="cbct_upload_type"]');
                    if (hiddenField) hiddenField.value = 'file';
                    // Selected modality field deprecated - modalities are now inferred from uploaded files
                }
            });
        });
        const legacyFolderInputs = form.querySelectorAll('input[type="file"][name="cbct_folder_files"]');
        legacyFolderInputs.forEach(input => {
            input.addEventListener('change', () => {
                if (input.files && input.files.length > 0) {
                    form.querySelectorAll('input[type="file"][name="cbct_folder_files"]').forEach(other => { if (other !== input) other.value = ''; });
                    form.querySelectorAll('input[type="file"][name="cbct"]').forEach(other => { other.value = ''; });
                    const hiddenField = form.querySelector('input[name="volume_upload_type"]') || form.querySelector('input[name="cbct_upload_type"]');
                    if (hiddenField) hiddenField.value = 'folder';
                    // Selected modality field deprecated - modalities are now inferred from uploaded files
                }
            });
        });
    });
}

/**
 * Track modality selection for IOS and per-modality blocks (agnostic)
 */
function initModalitySelection() {
    const forms = document.querySelectorAll('form');
    forms.forEach(form => {
        // Selected modality field deprecated - modalities are now inferred from uploaded files on the backend

        // IOS inputs (keeping file validation)
        const upper = form.querySelector('input[type="file"][name="upper_scan"]');
        const lower = form.querySelector('input[type="file"][name="lower_scan"]');

        // Per-modality blocks (with data-group); listen to inputs named by slug or slug_folder_files
        const containers = form.querySelectorAll('.volume-upload-container, .cbct-upload-container');
        containers.forEach(container => {
            const groupName = container.getAttribute('data-group');
            if (!groupName) return;
            const fileInput = container.querySelector(`input[type="file"][name="${groupName}"]`);
            const folderInputA = container.querySelector(`input[type="file"][name="${groupName}_folder_files"]`);
            const folderInputB = container.querySelector(`input[type="file"][name="${groupName}-folder_files"]`);
            // File input event listeners removed - modality inference handled by backend
        });

        // Selected modality field deprecated - backend now infers modalities from uploaded files
    });
}
