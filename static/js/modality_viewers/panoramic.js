/**
 * Panoramic Viewer
 * Handles display and interaction with panoramic images
 */

window.PanoramicViewer = {
    initialized: false,
    patientId: null,
    selectedVariant: null,
    variants: [],
    refreshRevision: null,
    targets: {
        standalone: {
            loadingId: 'panoramicLoading',
            contentId: 'panoramicContent',
            errorId: 'panoramicError',
            imageId: 'panoramicStandaloneImage',
            requestToken: 0,
            abortController: null
        },
        inline: {
            loadingId: 'cbctPanoramicLoading',
            contentId: 'cbctPanoramicContent',
            errorId: 'cbctPanoramicError',
            imageId: 'cbctPanoramicImage',
            requestToken: 0,
            abortController: null
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
        if (variantId === 'mip' || variantId === 'mean' || variantId === 'raysum') {
            return { offset: 0, mode: variantId };
        }
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
        if (mode === 'mean' && this.variants.some((variant) => variant.id === 'mip')) {
            mode = 'mip';
        }
        const legacyId = this.variantId(offset, mode);
        const variantId = this.variants.some((variant) => variant.id === legacyId)
            ? legacyId
            : mode;
        if (!this.targets[target] || !this.variants.some((variant) => variant.id === variantId)) return;
        this.selectedVariant = variantId;
        this.updateVariantControls();
        this.loadInto(this.targets[target]);
    },

    bindVariantControls: function() {
        const editButton = document.getElementById('editSavedPanoramic');
        if (editButton && !editButton.dataset.bound) {
            editButton.dataset.bound = 'true';
            editButton.addEventListener('click', () => {
                if (
                    window.CBCTPanorexEditor &&
                    typeof window.CBCTPanorexEditor.enterEditMode === 'function'
                ) {
                    window.CBCTPanorexEditor.enterEditMode();
                }
            });
        }
        document.querySelectorAll('[data-panoramic-variant-controls]').forEach((controls) => {
            if (controls.dataset.bound) return;
            controls.dataset.bound = 'true';
            controls.addEventListener('click', (event) => {
                const button = event.target.closest('[data-panoramic-mode]');
                if (!button) return;
                const slider = controls.querySelector('[data-panoramic-z-slider]');
                this.selectVariant(controls, slider ? Number(slider.value) : 0, button.dataset.panoramicMode);
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
                if (button.dataset.panoramicMode === 'mean') {
                    button.textContent = this.variants.some((variant) => variant.id === 'mip') ? 'MIP' : 'Average';
                }
                const buttonMode = button.dataset.panoramicMode === 'mean' && selection.mode === 'mip'
                    ? 'mip'
                    : button.dataset.panoramicMode;
                button.classList.toggle('active', buttonMode === selection.mode);
                button.setAttribute('aria-pressed', buttonMode === selection.mode ? 'true' : 'false');
            });
            const slider = controls.querySelector('[data-panoramic-z-slider]');
            const label = controls.querySelector('[data-panoramic-z-label]');
            const zControl = controls.querySelector('.panoramic-z-control');
            const hasLegacySweep = this.variants.some((variant) => /^z(?:plus\d+|minus\d+|0)_(?:mean|raysum)$/.test(variant.id));
            if (zControl) zControl.hidden = !hasLegacySweep;
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
        const editButton = document.getElementById('editSavedPanoramic');
        
        if (!img) {
            console.debug('Panoramic image element not found for target:', config.imageId);
            return;
        }
        
        const requestToken = ++config.requestToken;
        if (config.abortController) config.abortController.abort();
        config.abortController = typeof AbortController === 'function' ? new AbortController() : null;

        // Show loading state
        if (loading) loading.style.display = 'block';
        if (content) content.style.display = 'none';
        if (error) error.style.display = 'none';
        if (config === this.targets.inline && editButton) editButton.hidden = true;
        
        fetch(this.getMetaUrl(), config.abortController ? { signal: config.abortController.signal } : undefined)
            .then(response => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json();
            })
            .then(data => {
                if (requestToken !== config.requestToken) return;
                this.variants = Array.isArray(data.variants) ? data.variants : [];
                this.selectedVariant = data.selected_variant || null;
                this.updateVariantControls();
                if (config === this.targets.inline && editButton) {
                    // Independent of whether a panoramic exists yet: the editor is
                    // only ever opened from this button, so hiding it for a patient
                    // with no panoramic would leave no way in at all.
                    editButton.hidden = !(
                        window.canEdit && document.getElementById('cbctPanorexEditor')
                    );
                }
                const container = img.parentElement;
                if (container) {
                    container.querySelectorAll('.rgb-edit-toolbar, .rgb-crop-layer').forEach((el) => el.remove());
                    container.classList.remove('rgb-edit-host');
                }
                delete img.dataset.rgbEditorMounted;
                img.onload = () => {
                    if (requestToken !== config.requestToken) return;
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
                };

                img.onerror = () => {
                    console.error('Failed to load panoramic image');
                    if (loading) loading.style.display = 'none';
                    if (error) error.style.display = 'block';
                };

                img.onclick = () => {
                    if (requestToken === config.requestToken) {
                        this.showFullscreenImage(img.src, 'Panoramic');
                    }
                };

                const cacheToken = data.generation_uuid || (
                    data.revision !== undefined ? data.revision : this.refreshRevision
                );
                img.src = cacheToken === null || cacheToken === undefined
                    ? data.url
                    : data.url + (data.url.includes('?') ? '&' : '?') + 'generation=' + encodeURIComponent(cacheToken);
            })
            .catch((fetchError) => {
                if (fetchError && fetchError.name === 'AbortError') return;
                if (requestToken !== config.requestToken) return;
                if (loading) loading.style.display = 'none';
                if (error) error.style.display = 'block';
                if (config === this.targets.inline && editButton) {
                    editButton.hidden = !(
                        window.canEdit && document.getElementById('cbctPanorexEditor')
                    );
                }
            });
    },

    load: function() {
        this.loadInto(this.targets.standalone);
    },

    loadInlineForCBCT: function() {
        this.loadInto(this.targets.inline);
    },

    refreshAfterSave: function(data) {
        data = data || {};
        this.refreshRevision = data.revision !== undefined ? data.revision : Date.now();
        this.variants = Array.isArray(data.variants) ? data.variants : [
            { id: 'mip', label: 'MIP' },
            { id: 'raysum', label: 'X-ray' }
        ];
        const preferred = data.selected_variant || data.variant || data.default_mode || 'mip';
        this.selectedVariant = preferred;
        this.updateVariantControls();
        Object.keys(this.targets).forEach((key) => {
            if (document.getElementById(this.targets[key].imageId)) this.loadInto(this.targets[key]);
        });
    },
    
    showFullscreenImage: function(src, title) {
        const modal = document.getElementById('fullscreenImageModal');
        const modalTitle = document.getElementById('fullscreenImageModalLabel');
        const fullscreenImg = document.getElementById('fullscreenImage');

        if (modalTitle) modalTitle.textContent = title || 'Image Viewer';

        if (modal) {
            const bsModal = new bootstrap.Modal(modal);
            bsModal.show();
            if (fullscreenImg) {
                fullscreenImg.onload = null;
                fullscreenImg.src = src;
            }
        }
    }
};
