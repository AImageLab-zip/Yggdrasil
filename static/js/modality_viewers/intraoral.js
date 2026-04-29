/**
 * Intraoral Photos Viewer
 * Handles display and interaction with intraoral photos
 */

window.IntraoralViewer = {
    initialized: false,
    patientId: null,
    
    init: function(patientId) {
        this.patientId = patientId;
        this.initialized = true;
        console.debug('Intraoral Photos Viewer initialized for patient', patientId);
    },
    
    load: function() {
        if (!this.patientId) {
            console.error('No patient ID set for intraoral photos viewer');
            return;
        }
        
        const loading = document.getElementById('intraoralLoading');
        const content = document.getElementById('intraoralContent');
        const error = document.getElementById('intraoralError');
        const grid = document.getElementById('intraoralGrid');
        
        // Show loading state
        if (loading) loading.style.display = 'block';
        if (content) content.style.display = 'none';
        if (error) error.style.display = 'none';
        
        // Make API call to get intraoral photos
        fetch(`/maxillo/api/patient/${this.patientId}/intraoral/`)
            .then(response => response.json())
            .then(data => {
                if (loading) loading.style.display = 'none';
                
                if (data.error) {
                    console.error('Error loading intraoral photos:', data.error);
                    if (error) error.style.display = 'block';
                    return;
                }
                
                if (!data.images || data.images.length === 0) {
                    if (error) error.style.display = 'block';
                    return;
                }
                
                if (grid) {
                    grid.innerHTML = '';
                    if (window.IntraoralSegmentation && typeof window.IntraoralSegmentation.mount === 'function') {
                        window.IntraoralSegmentation.mount(grid, data.images);
                    } else {
                        this.renderFallbackGrid(grid, data.images);
                    }
                }
                
                if (content) content.style.display = 'block';
            })
            .catch(error => {
                console.error('Error fetching intraoral photos:', error);
                if (loading) loading.style.display = 'none';
                if (error) error.style.display = 'block';
            });
    },
    
    renderFallbackGrid: function(grid, images) {
        images.forEach((image) => {
            const col = document.createElement('div');
            col.className = 'col-lg-3 col-md-4 col-sm-6';

            const card = document.createElement('div');
            card.className = 'card h-100';
            card.style.cursor = 'pointer';

            const img = document.createElement('img');
            img.src = image.url;
            img.className = 'card-img-top';
            img.style.height = '200px';
            img.style.objectFit = 'cover';
            img.alt = image.original_filename || `Intraoral photo ${image.index}`;

            const cardBody = document.createElement('div');
            cardBody.className = 'card-body p-2';

            const cardText = document.createElement('small');
            cardText.className = 'text-muted';
            cardText.textContent = image.original_filename || '';

            if (image.original_filename) cardBody.appendChild(cardText);

            card.appendChild(img);
            card.appendChild(cardBody);
            col.appendChild(card);
            card.addEventListener('click', () => this.showFullscreenImage(image.url, `Intraoral Photo ${image.index}`));
            grid.appendChild(col);
        });
    },

    showFullscreenImage: function(src, title) {
        const modal = document.getElementById('fullscreenImageModal');
        const modalTitle = document.getElementById('fullscreenImageModalLabel');
        const fullscreenImg = document.getElementById('fullscreenImage');
        
        if (modalTitle) modalTitle.textContent = title || 'Image Viewer';
        if (fullscreenImg) fullscreenImg.src = src;
        
        if (modal) {
            const bsModal = new bootstrap.Modal(modal);
            bsModal.show();
        }
    }
};


