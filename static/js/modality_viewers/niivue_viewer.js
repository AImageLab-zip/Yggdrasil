/**
 * NiiVueViewer - A wrapper class for NiiVue single-view volume display
 *
 * Purpose: Provides a clean API for viewer_grid.js to use NiiVue for
 * medical volume visualization. Wraps the NiiVue library with methods
 * for initialization, orientation control, and slice navigation.
 *
 * Usage:
 *   const viewer = new NiiVueViewer('canvas-element-id');
 *   await viewer.init('t1', volumeBlob);
 *   viewer.setOrientation('sagittal');
 *   viewer.dispose();
 *
 * Dependencies: NiiVue library must be loaded (window.niivue)
 */

class NiiVueViewer {
    /**
     * Create a NiiVueViewer instance
     * @param {string} containerId - The ID of the canvas element to render into
     */
    constructor(containerId) {
        this.containerId = containerId;
        this.nv = null;
        this.initialized = false;
        this.currentOrientation = 'axial';
        this.modalitySlug = null;
        this.segmentationOverlayLoaded = false;
        this.onLocationChangeCallback = null;
    }

    async _payloadToArrayBuffer(filePayload) {
        if (filePayload instanceof ArrayBuffer) {
            return filePayload;
        }
        if (filePayload && typeof filePayload.arrayBuffer === 'function') {
            return filePayload.arrayBuffer();
        }
        throw new Error('Unsupported volume payload type. Expected Blob or ArrayBuffer.');
    }

    /**
     * Initialize the viewer with a volume
     * @param {string} modalitySlug - The modality identifier (e.g., 't1', 't2', 'flair')
     * @param {Blob|ArrayBuffer} fileBlob - NIfTI payload as Blob or ArrayBuffer
     * @returns {Promise<void>}
     */
    async init(modalitySlug, fileBlob) {
        if (this.initialized) {
            await this.dispose();
        }

        // Verify NiiVue is available
        if (typeof window.niivue === 'undefined' || typeof window.niivue.Niivue !== 'function') {
            throw new Error('NiiVue library not loaded. Ensure niivue.min.js is included before this script.');
        }

        this.modalitySlug = modalitySlug;

        // Create NiiVue instance with single-view mode (multiplanar: false)
        this.nv = new window.niivue.Niivue({
            backColor: [0, 0, 0, 1],       // Black background (medical imaging convention)
            show3Dcrosshair: false,         // No 3D crosshair in single view
            multiplanarForceRender: false,  // Single view mode
            isColorbar: false,              // No colorbar for simple viewing
            logging: false,                 // Disable console logging
            dragAndDropEnabled: false,      // Grid handles drag-drop, not NiiVue
            forceDevicePixelRatio: 1        // Keep CBCT viewer GPU footprint low
        });

        // Attach to canvas element
        const canvas = document.getElementById(this.containerId);
        if (!canvas) {
            throw new Error(`Canvas element with id '${this.containerId}' not found`);
        }

        await this.nv.attachToCanvas(canvas, false);

        // Load volume from pre-fetched blob data. loadFromArrayBuffer
        // parses the buffer directly without any HTTP request. The name
        // must end in .nii.gz so NiiVue selects the correct parser.
        let arrayBuffer = await this._payloadToArrayBuffer(fileBlob);
        await this.nv.loadFromArrayBuffer(arrayBuffer, modalitySlug + '.nii.gz');

        // Keep 2D crosshair behavior deterministic across viewers.
        if (this.nv.opts) {
            this.nv.opts.crosshairWidth = 2;
        }

        // Set default orientation to axial
        this.setOrientation('axial');

        this.initialized = true;
    }

    /**
     * Load (first time) or show (subsequent calls) a semi-transparent segmentation mask.
     *
     * On the first call the NIfTI is parsed and uploaded to the GPU.
     * On later calls only the overlay opacity is restored — no re-parse, no GPU re-upload.
     *
     * @param {Blob|ArrayBuffer} fileBlob - NIfTI payload as Blob or ArrayBuffer
     * @param {{opacity?: number}} options
     * @returns {Promise<void>}
     */
    async setSegmentationOverlay(fileBlob, options = {}) {
        if (!this.nv || !this.initialized) {
            throw new Error('Cannot load segmentation overlay before base volume is initialized');
        }

        const opacity = typeof options.opacity === 'number' ? options.opacity : 0.5;

        // Fast path: overlay already loaded in GPU memory — just make it visible again.
        if (this.segmentationOverlayLoaded && this.nv.volumes && this.nv.volumes.length >= 2) {
            this._setOverlayOpacity(this.nv.volumes.length - 1, opacity);
            return;
        }

        // Slow path (first call only): parse + GPU upload.
        // Remove any stale overlay volumes first (handles edge cases only).
        this._unloadOverlayVolumes();

        const arrayBuffer = await this._payloadToArrayBuffer(fileBlob);
        const previousVolumeCount = this.nv.volumes ? this.nv.volumes.length : 0;

        await this.nv.loadFromArrayBuffer(arrayBuffer, 'braintumor-mri-seg.nii.gz');

        const overlayIndex = this.nv.volumes ? this.nv.volumes.length - 1 : -1;
        if (overlayIndex < previousVolumeCount || overlayIndex < 1) {
            throw new Error('Segmentation overlay volume was not loaded');
        }

        const overlay = this.nv.volumes[overlayIndex];

        // addColormap is confirmed public in NiiVue 0.67.
        // I=[0,85,170,255] evenly maps labels 0/1/2/3 across the 0-255 LUT range:
        //   voxel N → LUT index (N/3)*255  →  0→0, 1→85, 2→170, 3→255
        // cal_max is hardcoded to 3 — do NOT use global_max which NiiVue may
        // report as 1.0 for integer label files.
        try {
            this.nv.addColormap('segmentationMask', {
                R: [0,   0,   255, 0  ],
                G: [0,   255, 0,   0  ],
                B: [0,   0,   0,   255],
                A: [0,   255, 255, 255],
                I: [0,   85,  170, 255]
            });
            overlay.colormap = 'segmentationMask';
        } catch (e) {
            overlay.colormap = 'red';
        }

        this._setOverlayOpacity(overlayIndex, opacity);

        // Set cal range after the GPU update inside _setOverlayOpacity so it
        // is not overwritten by any internal reset, then flush to GPU.
        overlay.cal_min = 0;
        overlay.cal_max = 3;
        if (typeof this.nv.updateGLVolume === 'function') {
            this.nv.updateGLVolume();
        } else {
            this.nv.drawScene();
        }

        this.segmentationOverlayLoaded = true;
    }

    /**
     * Hide the segmentation overlay by setting its opacity to 0.
     * The GPU-resident volume is kept so re-showing it is instant.
     */
    removeSegmentationOverlay() {
        if (!this.nv || !this.nv.volumes || this.nv.volumes.length < 2) {
            this.segmentationOverlayLoaded = false;
            return;
        }

        // Hide via opacity — avoids GPU teardown and keeps the volume ready for re-show.
        this._setOverlayOpacity(this.nv.volumes.length - 1, 0);
        // Keep segmentationOverlayLoaded = true so the fast path is used next time.
    }

    /**
     * Actually unload overlay volumes from NiiVue (called on dispose / base-volume replace).
     * @private
     */
    _unloadOverlayVolumes() {
        if (!this.nv || !this.nv.volumes || this.nv.volumes.length < 2) {
            this.segmentationOverlayLoaded = false;
            return;
        }
        for (let i = this.nv.volumes.length - 1; i >= 1; i--) {
            this.nv.closeVolume(i);
        }
        this.segmentationOverlayLoaded = false;
        this.nv.drawScene();
    }

    /**
     * Set the opacity of an overlay volume and trigger a redraw.
     * @private
     */
    _setOverlayOpacity(overlayIndex, opacity) {
        const clamped = Math.max(0, Math.min(1, opacity));
        const overlay = this.nv.volumes[overlayIndex];
        overlay.opacity = clamped;
        if (typeof this.nv.setOpacity === 'function') {
            this.nv.setOpacity(overlayIndex, clamped);
        } else {
            this.nv.drawScene();
        }
    }

    /**
     * Set the viewing orientation
     * @param {string} orientation - 'axial', 'sagittal', or 'coronal'
     */
    setOrientation(orientation) {
        if (!this.nv) {
            console.warn('NiiVueViewer: Cannot set orientation - viewer not initialized');
            return;
        }

        const normalizedOrientation = orientation.toLowerCase();

        // Map orientation names to NiiVue slice type constants
        // NiiVue uses: sliceTypeAxial=2, sliceTypeSagittal=1, sliceTypeCoronal=0
        let sliceType;
        let actualOrientation = normalizedOrientation;
        switch (normalizedOrientation) {
            case 'axial':
                sliceType = this.nv.sliceTypeAxial;
                break;
            case 'sagittal':
                sliceType = this.nv.sliceTypeSagittal;
                break;
            case 'coronal':
                sliceType = this.nv.sliceTypeCoronal;
                break;
            default:
                console.warn(`NiiVueViewer: Unknown orientation '${orientation}', defaulting to axial`);
                sliceType = this.nv.sliceTypeAxial;
                actualOrientation = 'axial';
        }

        this.nv.setSliceType(sliceType);
        this.currentOrientation = actualOrientation;
        this.nv.drawScene();
    }

    /**
     * Get the current slice index (for Phase 5 synchronization)
     * @returns {number} The current slice index, or -1 if not initialized
     */
    getSliceIndex() {
        if (!this.nv || !this.initialized) {
            return -1;
        }

        // NiiVue stores crosshair position as fraction [0-1] for each axis
        // Convert to slice index based on current orientation
        const crosshair = this.nv.scene.crosshairPos;
        const volumes = this.nv.volumes;

        if (!volumes || volumes.length === 0) {
            return -1;
        }

        const dims = volumes[0].dimsRAS;

        switch (this.currentOrientation) {
            case 'axial':
                // Z axis (dim 3)
                return Math.round(crosshair[2] * (dims[3] - 1));
            case 'sagittal':
                // X axis (dim 1)
                return Math.round(crosshair[0] * (dims[1] - 1));
            case 'coronal':
                // Y axis (dim 2)
                return Math.round(crosshair[1] * (dims[2] - 1));
            default:
                return -1;
        }
    }

    /**
     * Set the current slice index (for Phase 5 synchronization)
     * @param {number} index - The slice index to navigate to
     */
    setSliceIndex(index) {
        if (!this.nv || !this.initialized) {
            console.warn('NiiVueViewer: Cannot set slice index - viewer not initialized');
            return;
        }

        const volumes = this.nv.volumes;
        if (!volumes || volumes.length === 0) {
            return;
        }

        const dims = volumes[0].dimsRAS;
        const crosshair = this.nv.scene.crosshairPos;
        if (!crosshair || crosshair.length < 3) {
            return;
        }

        switch (this.currentOrientation) {
            case 'axial':
                // Z axis (dim 3)
                crosshair[2] = Math.min(Math.max(index / (dims[3] - 1), 0), 1);
                break;
            case 'sagittal':
                // X axis (dim 1)
                crosshair[0] = Math.min(Math.max(index / (dims[1] - 1), 0), 1);
                break;
            case 'coronal':
                // Y axis (dim 2)
                crosshair[1] = Math.min(Math.max(index / (dims[2] - 1), 0), 1);
                break;
        }

        this.nv.drawScene();
    }

    /**
     * Get the total number of slices in the current orientation
     * @returns {number} The total slice count, or 0 if not initialized
     */
    getSliceCount() {
        if (!this.nv || !this.initialized) {
            return 0;
        }

        const volumes = this.nv.volumes;
        if (!volumes || volumes.length === 0) {
            return 0;
        }

        const dims = volumes[0].dimsRAS;

        switch (this.currentOrientation) {
            case 'axial':
                return dims[3];
            case 'sagittal':
                return dims[1];
            case 'coronal':
                return dims[2];
            default:
                return 0;
        }
    }

    /**
     * Check if the viewer is initialized and ready
     * @returns {boolean}
     */
    isReady() {
        return this.initialized && this.nv !== null;
    }

    /**
     * Get the current orientation
     * @returns {string} 'axial', 'sagittal', or 'coronal'
     */
    getOrientation() {
        return this.currentOrientation;
    }

    /**
     * Attach a callback for slice changes (wraps NiiVue's onLocationChange)
     * @param {Function} callback - Function called when slice position changes
     */
    onSliceChange(callback) {
        if (!this.nv) {
            console.warn('NiiVueViewer: Cannot attach onSliceChange - viewer not initialized');
            return;
        }

        this.onLocationChangeCallback = callback;
        this.nv.onLocationChange = (msg) => {
            if (this.onLocationChangeCallback) {
                this.onLocationChangeCallback(msg);
            }
        };
    }

    /**
     * Force a redraw of the viewer
     */
    redraw() {
        if (this.nv) {
            this.nv.drawScene();
        }
    }

    /**
     * Set windowing using percent-based values (0-100%)
     * Maps percent range to NiiVue's calMin/calMax based on volume's data range.
     * @param {number} percentMin - Lower window percent (0-100)
     * @param {number} percentMax - Upper window percent (0-100)
     */
    setWindowing(percentMin, percentMax, options = {}) {
        if (!this.nv || !this.initialized) {
            console.warn('NiiVueViewer: Cannot set windowing - viewer not initialized');
            return;
        }

        const volumes = this.nv.volumes;
        if (!volumes || volumes.length === 0) {
            console.warn('NiiVueViewer: Cannot set windowing - no volume loaded');
            return;
        }

        const volume = volumes[0];
        const dataMin = volume.global_min;
        const dataMax = volume.global_max;

        // Clamp and order percent values
        const pMin = Math.max(0, Math.min(100, percentMin));
        const pMax = Math.max(0, Math.min(100, percentMax));
        const lowP = Math.min(pMin, pMax);
        const highP = Math.max(pMin, pMax);

        // Map percent to absolute data values
        volume.cal_min = dataMin + (dataMax - dataMin) * (lowP / 100);
        volume.cal_max = dataMin + (dataMax - dataMin) * (highP / 100);

        // NiiVue needs updateGLVolume() to apply cal_min/cal_max updates.
        // Interactive smoothness is handled by throttling callers.
        this.nv.updateGLVolume();
    }

    /**
     * Get current windowing as percent values
     * @returns {{percentMin: number, percentMax: number}} Current windowing in percent
     */
    getWindowing() {
        if (!this.nv || !this.initialized) {
            return { percentMin: 0, percentMax: 100 };
        }

        const volumes = this.nv.volumes;
        if (!volumes || volumes.length === 0) {
            return { percentMin: 0, percentMax: 100 };
        }

        const volume = volumes[0];
        const dataMin = volume.global_min;
        const dataMax = volume.global_max;
        const dataRange = dataMax - dataMin;

        if (dataRange <= 0) {
            return { percentMin: 0, percentMax: 100 };
        }

        // Map calMin/calMax back to percent
        const percentMin = ((volume.cal_min - dataMin) / dataRange) * 100;
        const percentMax = ((volume.cal_max - dataMin) / dataRange) * 100;

        return {
            percentMin: Math.max(0, Math.min(100, percentMin)),
            percentMax: Math.max(0, Math.min(100, percentMax))
        };
    }

    /**
     * Get the volume's actual data range
     * @returns {{min: number, max: number}} Volume's global min and max values
     */
    getDataRange() {
        if (!this.nv || !this.initialized) {
            return { min: 0, max: 1 };
        }

        const volumes = this.nv.volumes;
        if (!volumes || volumes.length === 0) {
            return { min: 0, max: 1 };
        }

        const volume = volumes[0];
        return {
            min: volume.global_min,
            max: volume.global_max
        };
    }

    /**
     * Dispose of the viewer and clean up resources
     */
    dispose() {
        if (this.nv) {
            // Clean up onLocationChange callback
            if (this.onLocationChangeCallback) {
                this.nv.onLocationChange = null;
                this.onLocationChangeCallback = null;
            }

            // Clear all volumes
            if (this.nv.volumes && this.nv.volumes.length > 0) {
                for (let i = this.nv.volumes.length - 1; i >= 0; i--) {
                    this.nv.closeVolume(i);
                }
            }
            this.nv = null;
        }

        this.initialized = false;
        this.currentOrientation = 'axial';
        this.modalitySlug = null;
        this.segmentationOverlayLoaded = false;
    }
}

// Expose as global for viewer_grid.js to use
window.NiiVueViewer = NiiVueViewer;
