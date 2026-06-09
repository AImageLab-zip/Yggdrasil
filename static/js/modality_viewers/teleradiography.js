/**
 * Teleradiography Viewer
 * Handles display and interaction with teleradiography images
 */

window.TeleradiographyViewer = {
    initialized: false,
    patientId: null,
    
    init: function(patientId) {
        this.patientId = patientId;
        this.initialized = true;
        console.debug('Teleradiography Viewer initialized for patient', patientId);
    },
    
    load: function() {
        if (!this.patientId) {
            console.error('No patient ID set for teleradiography viewer');
            return;
        }
        
        const loading = document.getElementById('teleradiographyLoading');
        const content = document.getElementById('teleradiographyContent');
        const error = document.getElementById('teleradiographyError');
        const img = document.getElementById('teleradiographyImage');
        
        // Show loading state
        if (loading) loading.style.display = 'block';
        if (content) content.style.display = 'none';
        if (error) error.style.display = 'none';
        
        const namespace = window.projectNamespace || 'maxillo';
        // Make API call to get teleradiography image metadata
        fetch(`/${namespace}/api/patient/${this.patientId}/teleradiography/?meta=1`)
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                if (loading) loading.style.display = 'none';
                if (img) {
                    img.src = data.url;
                    img.addEventListener('load', () => {
                        if (content) content.style.display = 'block';
                        if (window.RGBImageEditor && data.source_file_id) {
                            const container = img.parentElement;
                            if (container) {
                                container.querySelectorAll('.rgb-edit-toolbar').forEach((el) => el.remove());
                            }
                            delete img.dataset.rgbEditorMounted;
                            window.RGBImageEditor.attachToImage(img, {
                                patientId: this.patientId,
                                modalitySlug: 'teleradiography',
                                sourceFileId: data.source_file_id,
                                rawUrl: data.raw_url,
                                container,
                            });
                        }
                    }, { once: true });
                    img.onerror = () => {
                        if (error) error.style.display = 'block';
                    };
                    
                    // Keep image inline; no fullscreen modal for RGB editing workflow.
                    img.onclick = null;
                }
            })
            .catch(error => {
                console.error('Error loading teleradiography:', error);
                if (loading) loading.style.display = 'none';
                if (error) error.style.display = 'block';
            });
    },
    
    showFullscreenImage: function() {}
};
