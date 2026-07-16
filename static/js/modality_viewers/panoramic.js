/**
 * Panoramic Viewer
 * Handles display and interaction with panoramic images
 */

window.PanoramicViewer = {
    initialized: false,
    patientId: null,
    selectedVariant: null,
    variants: [],
    
    init: function(patientId) {
        this.patientId = patientId;
        this.selectedVariant = null;
        this.variants = [];
        this.initialized = true;
        this.bindVariantControls();
        console.debug('Panoramic Viewer initialized for patient', patientId);
    },
    
    getApiUrl: function() {
        const namespace = window.projectNamespace || 'maxillo';
        return `/${namespace}/api/patient/${this.patientId}/panoramic/`;
    },

    getMetaUrl: function() {
        const params = new URLSearchParams({ meta: '1' });
        if (this.selectedVariant && this.variants.length) {
            params.set('variant', this.selectedVariant);
        }
        return `${this.getApiUrl()}?${params}`;
    },

    bindVariantControls: function() {
        const controls = document.getElementById('panoramicVariantControls');
        if (!controls || controls.dataset.bound) return;
        controls.dataset.bound = 'true';
        controls.addEventListener('click', (event) => {
            const button = event.target.closest('[data-panoramic-variant]');
            if (!button || !this.variants.some((variant) => variant.id === button.dataset.panoramicVariant)) return;
            this.selectedVariant = button.dataset.panoramicVariant;
            this.updateVariantControls();
            this.load();
        });
    },

    updateVariantControls: function() {
        const controls = document.getElementById('panoramicVariantControls');
        if (!controls) return;
        controls.style.display = this.variants.length ? 'inline-flex' : 'none';
        controls.querySelectorAll('[data-panoramic-variant]').forEach((button) => {
            const available = this.variants.some((variant) => variant.id === button.dataset.panoramicVariant);
            button.style.display = available ? '' : 'none';
            button.classList.toggle('active', available && button.dataset.panoramicVariant === this.selectedVariant);
        });
    },

    loadInto: function(config) {
        if (!this.patientId) {
            console.error('No patient ID set for panoramic viewer');
            return;
        }

        const loading = document.getElementById(config.loadingId);
        const content = document.getElementById(config.contentId);
        const error = document.getElementById(config.errorId);
        const img = document.getElementById(config.imageId);
        
        if (!img) {
            console.debug('Panoramic image element not found for target:', config.imageId);
            return;
        }
        
        // Show loading state
        if (loading) loading.style.display = 'block';
        if (content) content.style.display = 'none';
        if (error) error.style.display = 'none';
        
        fetch(this.getMetaUrl())
            .then(response => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json();
            })
            .then(data => {
                this.variants = Array.isArray(data.variants) ? data.variants : [];
                this.selectedVariant = data.selected_variant || null;
                this.updateVariantControls();
                img.addEventListener('load', () => {
                    console.debug('Panoramic image loaded successfully');
                    if (loading) loading.style.display = 'none';
                    if (content) content.style.display = 'block';
                    if (window.RGBImageEditor && data.source_file_id) {
                        const container = img.parentElement;
                        if (container) {
                            container.querySelectorAll('.rgb-edit-toolbar').forEach((el) => el.remove());
                        }
                        delete img.dataset.rgbEditorMounted;
                        window.RGBImageEditor.attachToImage(img, {
                            patientId: this.patientId,
                            modalitySlug: 'panoramic',
                            sourceFileId: data.source_file_id,
                            rawUrl: data.raw_url,
                            container,
                        });
                    }
                }, { once: true });

                img.onerror = () => {
                    console.error('Failed to load panoramic image');
                    if (loading) loading.style.display = 'none';
                    if (error) error.style.display = 'block';
                };

                img.onclick = null;

                img.src = data.url;
            })
            .catch(() => {
                if (loading) loading.style.display = 'none';
                if (error) error.style.display = 'block';
            });
    },

    load: function() {
        this.loadInto({
            loadingId: 'panoramicLoading',
            contentId: 'panoramicContent',
            errorId: 'panoramicError',
            imageId: 'panoramicStandaloneImage',
            title: 'Panoramic'
        });
    },

    loadInlineForCBCT: function() {
        this.loadInto({
            loadingId: 'cbctPanoramicLoading',
            contentId: 'cbctPanoramicContent',
            errorId: 'cbctPanoramicError',
            imageId: 'cbctPanoramicImage',
            title: 'CBCT Panoramic'
        });
    },
    
    showFullscreenImage: function() {}
};
