// NIFTI Metadata Management
let currentMetadata = null;
let metadataLoaded = false;
let metadataLoadInFlight = null;

// CSRF token helper function
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
    // When CSRF_USE_SESSIONS = True, token is in hidden form, not cookies
    const csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (csrfInput) {
        return csrfInput.value;
    }
    
    // Fallback to cookie method for backwards compatibility
    const token = getCookie('csrftoken');
    if (!token) {
        console.error('SECURITY: CSRF token not found. This may indicate a security issue.');
        return null;
    }
    return token;
}

// Load NIFTI metadata on the first activation of the sidebar tab.
document.addEventListener('DOMContentLoaded', function() {
    const metadataTab = document.querySelector('.side-tab[data-tab-target="metadata"]');
    if (metadataTab) {
        metadataTab.addEventListener('click', function() {
            loadNiftiMetadata();
        });
        if (metadataTab.classList.contains('is-active')) {
            loadNiftiMetadata();
        }
    }
});

function loadNiftiMetadata() {
    if (metadataLoaded) {
        return Promise.resolve(currentMetadata);
    }
    if (metadataLoadInFlight) {
        return metadataLoadInFlight;
    }

    const scanId = JSON.parse(document.getElementById('django-data').textContent).scanId;
    const contentDiv = document.getElementById('niftiMetadataContent');
    const displayDiv = document.getElementById('niftiMetadataDisplay');
    const errorDiv = document.getElementById('niftiMetadataError');
    
    contentDiv.hidden = false;
    displayDiv.hidden = true;
    errorDiv.hidden = true;
    
    metadataLoadInFlight = fetch(`/${window.projectNamespace}/api/patient/${scanId}/nifti-metadata/`)
        .then(async response => {
            const data = await response.json();
            if (!response.ok || data.error) {
                throw new Error(data.error || `Request failed (${response.status})`);
            }
            if (!data || typeof data !== 'object') {
                throw new Error('Invalid metadata response format');
            }
            currentMetadata = data;
            displayMetadata(data);
            metadataLoaded = true;
            contentDiv.hidden = true;
            displayDiv.hidden = false;
            return data;
        })
        .catch(error => {
            console.error('NIFTI metadata fetch error:', error);
            showNiftiError('Failed to load NIFTI metadata: ' + error.message);
            return null;
        })
        .finally(() => {
            metadataLoadInFlight = null;
        });

    return metadataLoadInFlight;
}

function displayMetadata(metadata) {
    // Basic info with defensive programming
    document.getElementById('niftiOrientation').textContent = metadata.orientation || 'Unknown';
    document.getElementById('niftiDataType').textContent = metadata.data_type || 'Unknown';
    
    // Handle shape array safely
    if (metadata.shape && Array.isArray(metadata.shape)) {
        document.getElementById('niftiShape').textContent = metadata.shape.join(' × ');
    } else {
        document.getElementById('niftiShape').textContent = 'Unknown';
    }
    
    // Handle voxel dimensions safely
    if (metadata.voxel_dimensions && Array.isArray(metadata.voxel_dimensions)) {
        document.getElementById('niftiVoxelDims').textContent = 
            metadata.voxel_dimensions.map(d => d.toFixed(3)).join(' × ') + ' mm';
    } else {
        document.getElementById('niftiVoxelDims').textContent = 'Unknown';
    }
    
    // Affine matrix with improved formatting and defensive programming
    const affineTable = document.getElementById('affineTable');
    affineTable.innerHTML = '';
    
    const rowLabels = ['X-axis', 'Y-axis', 'Z-axis', 'Origin'];
    
    // Check if affine matrix exists and is valid
    if (!metadata.affine || !Array.isArray(metadata.affine) || metadata.affine.length !== 4) {
        // Show error in table
        const row = affineTable.insertRow();
        const cell = row.insertCell();
        cell.colSpan = 5;
        cell.textContent = 'Affine matrix data not available';
        cell.className = 'text-center text-muted';
        return;
    }
    
    for (let i = 0; i < 4; i++) {
        const row = affineTable.insertRow();
        
        // Add row label
        const labelCell = row.insertCell();
        labelCell.textContent = rowLabels[i];
        labelCell.className = 'fw-bold';
        
        // Add matrix values with defensive programming
        for (let j = 0; j < 4; j++) {
            const cell = row.insertCell();
            
            // Check if the row and value exist
            if (!metadata.affine[i] || !Array.isArray(metadata.affine[i]) || metadata.affine[i].length <= j) {
                cell.textContent = 'N/A';
                cell.className = 'text-muted';
                continue;
            }
            
            const value = metadata.affine[i][j];
            
            // Check if value is valid
            if (typeof value !== 'number' || isNaN(value)) {
                cell.textContent = 'N/A';
                cell.className = 'text-muted';
                continue;
            }
            
            // Format the value
            if (Math.abs(value) < 0.000001) {
                cell.textContent = '0.000000';
            } else {
                cell.textContent = value.toFixed(6);
            }
            
            // Highlight translation column (last column)
            if (j === 3) {
                cell.className = 'translation-column';
            }
        }
    }
}

function showNiftiError(message) {
    const contentDiv = document.getElementById('niftiMetadataContent');
    const errorDiv = document.getElementById('niftiMetadataError');
    const errorMessage = document.getElementById('niftiErrorMessage');
    
    contentDiv.hidden = true;
    errorMessage.textContent = message;
    errorDiv.hidden = false;
}

// Affine matrix editing functions
function editAffine() {
    if (!currentMetadata) return;
    
    document.getElementById('affineDisplay').hidden = true;
    document.getElementById('affineEdit').hidden = false;
    
    const editTable = document.getElementById('affineEditTable');
    editTable.innerHTML = '';
    
    const rowLabels = ['X-axis', 'Y-axis', 'Z-axis', 'Origin'];
    
    // Create edit table
    const table = document.createElement('table');
    table.className = 'affine-matrix-table';
    
    // Create header
    const thead = document.createElement('thead');
    const headerRow = thead.insertRow();
    headerRow.insertCell().textContent = '';
    headerRow.insertCell().textContent = 'X';
    headerRow.insertCell().textContent = 'Y';
    headerRow.insertCell().textContent = 'Z';
    headerRow.insertCell().textContent = 'Translation';
    table.appendChild(thead);
    
    // Create body
    const tbody = document.createElement('tbody');
    for (let i = 0; i < 4; i++) {
        const row = tbody.insertRow();
        
        // Add row label
        const labelCell = row.insertCell();
        labelCell.textContent = rowLabels[i];
        labelCell.className = 'fw-bold';
        
        // Add input cells
        for (let j = 0; j < 4; j++) {
            const cell = row.insertCell();
            const input = document.createElement('input');
            input.type = 'number';
            input.className = 'affine-input';
            input.step = '0.000001';
            input.value = currentMetadata.affine[i][j];
            input.dataset.row = i;
            input.dataset.col = j;
            
            if (j === 3) {
                cell.className = 'translation-column';
            }
            
            cell.appendChild(input);
        }
    }
    table.appendChild(tbody);
    editTable.appendChild(table);
}

function cancelAffineEdit() {
    document.getElementById('affineDisplay').hidden = false;
    document.getElementById('affineEdit').hidden = true;
}

function saveAffine() {
    const scanId = JSON.parse(document.getElementById('django-data').textContent).scanId;
    const newAffine = [];
    
    // Collect values from input fields
    for (let i = 0; i < 4; i++) {
        newAffine[i] = [];
        for (let j = 0; j < 4; j++) {
            const input = document.querySelector(`input[data-row="${i}"][data-col="${j}"]`);
            newAffine[i][j] = parseFloat(input.value);
        }
    }
    
    // Validate affine matrix
    if (!isValidAffineMatrix(newAffine)) {
        showNiftiError('Invalid affine matrix. Please check your values.');
        return;
    }
    
    // Send update request
    fetch(`/${window.projectNamespace}/api/patient/${scanId}/nifti-metadata/update/`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCSRFToken()
        },
        body: JSON.stringify({
            affine: newAffine
        })
    })
    .then(async response => {
        const data = await response.json();
        if (!response.ok || data.error) {
            throw new Error(data.error || `Request failed (${response.status})`);
        }
        return data;
    })
    .then(data => {
        // Use the persisted server representation, including recalculated
        // orientation and any other header-derived metadata.
        currentMetadata = data;
        displayMetadata(data);
        
        // Switch back to display mode
        cancelAffineEdit();
        
        // Show success message
        showSuccessMessage('Affine matrix updated. Reloading the CBCT viewer...');
        window.setTimeout(() => window.location.reload(), 600);
    })
    .catch(error => {
        showNiftiError('Failed to update affine matrix: ' + error.message);
    });
}

function isValidAffineMatrix(matrix) {
    // Basic validation: check if it's a 4x4 matrix with numeric values
    if (!Array.isArray(matrix) || matrix.length !== 4) {
        return false;
    }
    
    for (let i = 0; i < 4; i++) {
        if (!Array.isArray(matrix[i]) || matrix[i].length !== 4) {
            return false;
        }
        
        for (let j = 0; j < 4; j++) {
            if (typeof matrix[i][j] !== 'number' || isNaN(matrix[i][j])) {
                return false;
            }
        }
    }
    
    // Check if the 3x3 rotation/scale part is invertible (determinant != 0)
    const det = matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1]) -
                matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0]) +
                matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0]);
    
    return Math.abs(det) > 1e-10; // Small threshold for floating point precision
}

function showSuccessMessage(message) {
    // Create a temporary success message
    const successDiv = document.createElement('div');
    successDiv.className = 'alert alert-success alert-dismissible fade show';
    successDiv.innerHTML = `
        <i class="fas fa-check-circle me-1"></i>
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;
    
    // Insert at the top of the metadata box
    const metadataBox = document.querySelector('.nifti-metadata-box');
    metadataBox.insertBefore(successDiv, metadataBox.firstChild);
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
        if (successDiv.parentNode) {
            successDiv.remove();
        }
    }, 5000);
}
