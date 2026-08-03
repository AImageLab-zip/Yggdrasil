/**
 * Panoramic Viewer
 * Handles display and interaction with panoramic images
 */

window.PanoramicViewer = {
    initialized: false,
    patientId: null,
    selectedVariant: null,
    variants: [],
    targets: {
        standalone: {
            loadingId: 'panoramicLoading',
            contentId: 'panoramicContent',
            errorId: 'panoramicError',
            imageId: 'panoramicStandaloneImage'
        },
        inline: {
            loadingId: 'cbctPanoramicLoading',
            contentId: 'cbctPanoramicContent',
            errorId: 'cbctPanoramicError',
            imageId: 'cbctPanoramicImage'
        }
    },
    
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

    variantId: function(offset, mode) {
        const z = offset > 0 ? `zplus${offset}` : offset < 0 ? `zminus${Math.abs(offset)}` : 'z0';
        return `${z}_${mode}`;
    },

    variantSelection: function(variantId) {
        const match = /^(zplus(\d+)|zminus(\d+)|z0)_(mean|raysum)$/.exec(variantId || '');
        if (!match) return null;
        const offset = match[2] ? Number(match[2]) : match[3] ? -Number(match[3]) : 0;
        return { offset, mode: match[4] };
    },

    formatOffset: function(offset) {
        return offset > 0 ? `+${offset}` : `${offset}`;
    },

    selectVariant: function(controls, offset, mode) {
        const target = controls.dataset.panoramicTarget;
        const variantId = this.variantId(offset, mode);
        if (!this.targets[target] || !this.variants.some((variant) => variant.id === variantId)) return;
        this.selectedVariant = variantId;
        this.updateVariantControls();
        this.loadInto(this.targets[target]);
    },

    bindVariantControls: function() {
        document.querySelectorAll('[data-panoramic-variant-controls]').forEach((controls) => {
            if (controls.dataset.bound) return;
            controls.dataset.bound = 'true';
            controls.addEventListener('click', (event) => {
                const button = event.target.closest('[data-panoramic-mode]');
                if (!button) return;
                const slider = controls.querySelector('[data-panoramic-z-slider]');
                this.selectVariant(controls, Number(slider.value), button.dataset.panoramicMode);
            });
            const slider = controls.querySelector('[data-panoramic-z-slider]');
            if (slider) {
                slider.addEventListener('input', () => {
                    const label = controls.querySelector('[data-panoramic-z-label]');
                    if (label) label.textContent = this.formatOffset(Number(slider.value));
                });
                slider.addEventListener('change', () => {
                    const selection = this.variantSelection(this.selectedVariant) || { mode: 'mean' };
                    this.selectVariant(controls, Number(slider.value), selection.mode);
                });
            }
        });
    },

    updateVariantControls: function() {
        const selection = this.variantSelection(this.selectedVariant) || { offset: 0, mode: 'mean' };
        document.querySelectorAll('[data-panoramic-variant-controls]').forEach((controls) => {
            controls.hidden = this.variants.length === 0;
            controls.querySelectorAll('[data-panoramic-mode]').forEach((button) => {
                button.classList.toggle('active', button.dataset.panoramicMode === selection.mode);
                button.setAttribute('aria-pressed', button.dataset.panoramicMode === selection.mode ? 'true' : 'false');
            });
            const slider = controls.querySelector('[data-panoramic-z-slider]');
            const label = controls.querySelector('[data-panoramic-z-label]');
            if (slider) slider.value = selection.offset;
            if (label) label.textContent = this.formatOffset(selection.offset);
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
                const container = img.parentElement;
                if (container) {
                    container.querySelectorAll('.rgb-edit-toolbar, .rgb-crop-layer').forEach((el) => el.remove());
                    container.classList.remove('rgb-edit-host');
                }
                delete img.dataset.rgbEditorMounted;
                img.addEventListener('load', () => {
                    console.debug('Panoramic image loaded successfully');
                    if (loading) loading.style.display = 'none';
                    if (content) content.style.display = 'block';
                    if (data.editable !== false && window.RGBImageEditor && data.source_file_id) {
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
        this.loadInto(this.targets.standalone);
    },

    loadInlineForCBCT: function() {
        this.loadInto(this.targets.inline);
    },
    
    showFullscreenImage: function() {}
};
