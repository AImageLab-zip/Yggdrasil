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
        this.renderController = null;
        this.volumeMetadata = null;
        this.segmentationMetadata = null;
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

        // Inspect the file's orientation metadata before displaying it.
        // NiiVue reorients to RAS from the header; when the header carries
        // no valid qform/sform it silently assumes RAS storage order, so
        // the result is recorded for the caller to act on (warning banner).
        this.volumeMetadata = window.VolumeMetadata
            ? window.VolumeMetadata.parseNiftiMetadata(arrayBuffer)
            : null;

        await this.nv.loadFromArrayBuffer(arrayBuffer, modalitySlug + '.nii.gz');

        if (window.NiiVueRenderModes && typeof window.NiiVueRenderModes.createController === 'function') {
            try {
                this.renderController = window.NiiVueRenderModes.createController(this.nv);
            } catch (error) {
                console.warn('NiiVueViewer: enhanced CBCT rendering is unavailable:', error);
                this.renderController = null;
            }
        }

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
        const labelMax = Number.isInteger(options.labelMax) && options.labelMax > 3
            ? options.labelMax
            : 3;

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

        this.segmentationMetadata = window.VolumeMetadata
            ? window.VolumeMetadata.parseNiftiMetadata(arrayBuffer)
            : null;

        await this.nv.loadFromArrayBuffer(arrayBuffer, 'braintumor-mri-seg.nii.gz');

        const overlayIndex = this.nv.volumes ? this.nv.volumes.length - 1 : -1;
        if (overlayIndex < previousVolumeCount || overlayIndex < 1) {
            throw new Error('Segmentation overlay volume was not loaded');
        }

        const overlay = this.nv.volumes[overlayIndex];

        try {
            if (labelMax > 3) {
                const colormap = { R: [], G: [], B: [], A: [], I: [] };
                for (let value = 0; value <= labelMax; value++) {
                    const hue = (value * 0.61803398875) % 1;
                    const sector = Math.floor(hue * 6);
                    const fraction = hue * 6 - sector;
                    const p = 0.25;
                    const q = 1 - fraction * 0.75;
                    const t = 0.25 + fraction * 0.75;
                    const rgb = [
                        [1, t, p], [q, 1, p], [p, 1, t],
                        [p, q, 1], [t, p, 1], [1, p, q]
                    ][sector % 6];
                    colormap.R.push(value === 0 ? 0 : Math.round(rgb[0] * 255));
                    colormap.G.push(value === 0 ? 0 : Math.round(rgb[1] * 255));
                    colormap.B.push(value === 0 ? 0 : Math.round(rgb[2] * 255));
                    colormap.A.push(value === 0 ? 0 : 255);
                    colormap.I.push(Math.round(value * 255 / labelMax));
                }
                this.nv.addColormap('cbctSegmentationMask', colormap);
                overlay.colormap = 'cbctSegmentationMask';
            } else {
                this.nv.addColormap('segmentationMask', {
                    R: [0,   0,   255, 0  ],
                    G: [0,   255, 0,   0  ],
                    B: [0,   0,   0,   255],
                    A: [0,   255, 255, 255],
                    I: [0,   85,  170, 255]
                });
                overlay.colormap = 'segmentationMask';
            }
        } catch (e) {
            overlay.colormap = 'red';
        }

        this._setOverlayOpacity(overlayIndex, opacity);

        // Set cal range after the GPU update inside _setOverlayOpacity so it
        // is not overwritten by any internal reset, then flush to GPU.
        overlay.cal_min = 0;
        overlay.cal_max = labelMax;
        if (typeof this.nv.updateGLVolume === 'function') {
            this.nv.updateGLVolume();
        } else {
            this.nv.drawScene();
        }
        if (this.renderController) {
            this.renderController.reapply();
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
            if (typeof this.nv.removeVolumeByIndex === 'function') {
                this.nv.removeVolumeByIndex(i);
            } else if (typeof this.nv.removeVolume === 'function') {
                this.nv.removeVolume(this.nv.volumes[i]);
            }
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

    setRenderMode(mode = 'amip') {
        if (!this.nv) {
            throw new Error('Cannot enable render mode before initialization');
        }
        const renderSliceType = this.nv.sliceTypeRender !== undefined
            ? this.nv.sliceTypeRender
            : 4;
        if (this.nv.opts) {
            this.nv.opts.gradientOpacity = 0.6;
            this.nv.opts.gradientAmount = 0.5;
            this.nv.opts.crosshairWidth = 0;
        }
        this.nv.setSliceType(renderSliceType);
        this.currentOrientation = 'render';
        if (this.nv.scene) {
            this.nv.scene.volScaleMultiplier = 1.2;
        }
        if (typeof this.nv.setRenderAzimuthElevation === 'function') {
            this.nv.setRenderAzimuthElevation(180, 15);
        }
        const nativeShaded = typeof this.nv.setVolumeRenderIllumination === 'function';
        let modeResult = {
            available: mode === 'shaded' && nativeShaded,
            custom: false,
            fallback: mode === 'shaded' && nativeShaded,
            message: mode === 'shaded' && nativeShaded
                ? 'Using NiiVue native shading.'
                : 'Custom render shaders are unavailable.'
        };
        if (this.renderController) {
            modeResult = this.renderController.setMode(mode);
        } else if (mode === 'shaded' && nativeShaded) {
            modeResult.pending = true;
            try {
                const operation = this.nv.setVolumeRenderIllumination(0.5);
                modeResult.ready = Promise.resolve(operation).then(() => {
                    modeResult.pending = false;
                    return modeResult;
                }, (error) => {
                    console.warn('NiiVueViewer: native shaded rendering failed:', error);
                    modeResult.available = false;
                    modeResult.fallback = false;
                    modeResult.pending = false;
                    modeResult.message = 'NiiVue native shaded rendering failed on this GPU.';
                    return modeResult;
                });
            } catch (error) {
                console.warn('NiiVueViewer: native shaded rendering failed:', error);
                modeResult.available = false;
                modeResult.fallback = false;
                modeResult.pending = false;
                modeResult.message = 'NiiVue native shaded rendering failed on this GPU.';
                modeResult.ready = Promise.resolve(modeResult);
            }
        }
        this.nv.drawScene();
        return modeResult;
    }

    getRenderModeAvailability() {
        if (this.renderController) {
            return this.renderController.getModeAvailability();
        }
        const nativeShaded = !!(this.nv && typeof this.nv.setVolumeRenderIllumination === 'function');
        return {
            mip: { available: false, custom: false, fallback: false },
            amip: { available: false, custom: false, fallback: false },
            shaded: { available: nativeShaded, custom: false, fallback: nativeShaded }
        };
    }

    resetRenderCamera() {
        if (!this.nv) {
            return;
        }
        if (this.nv.scene) {
            this.nv.scene.volScaleMultiplier = 1.2;
        }
        if (typeof this.nv.setRenderAzimuthElevation === 'function') {
            this.nv.setRenderAzimuthElevation(180, 15);
        }
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
     * Orientation metadata parsed from the loaded volume file, or null when
     * the metadata reader is unavailable. `hasMetadata` is false when the
     * file declares no valid qform/sform and the display therefore assumes
     * RAS storage order.
     * @returns {{ok: boolean, hasMetadata: boolean, orientation: (string|null), issues: string[]}|null}
     */
    getVolumeMetadata() {
        return this.volumeMetadata;
    }

    /**
     * Orientation metadata of the segmentation overlay (null until the
     * overlay is loaded for the first time).
     */
    getSegmentationMetadata() {
        return this.segmentationMetadata;
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
     * Apply medical Level/Window controls expressed as percentages of the full
     * volume range. Enhanced shaders update uniforms only; their controller owns
     * the safe cal_min/cal_max fallback when custom shaders are unavailable.
     */
    setLevelWindow(level, width) {
        if (!this.nv || !this.initialized) {
            return;
        }
        if (this.renderController) {
            this.renderController.setWindow(level, width);
            return;
        }

        const normalizedWidth = Math.max(1, Math.min(100, Number(width) || 100));
        const normalizedLevel = Math.max(0, Math.min(100, Number(level) || 0));
        const percentMin = normalizedLevel - normalizedWidth / 2;
        const percentMax = normalizedLevel + normalizedWidth / 2;
        this.setWindowing(percentMin, percentMax);
    }

    getInitialLevelWindow() {
        if (this.renderController) {
            return this.renderController.getInitialWindow();
        }
        const volumes = this.nv && this.nv.volumes;
        const volume = volumes && volumes[0];
        const min = Number.isFinite(volume && volume.global_min) ? volume.global_min : 0;
        const max = Number.isFinite(volume && volume.global_max) && volume.global_max > min
            ? volume.global_max
            : min + 1;
        const robustMin = Number.isFinite(volume && volume.robust_min) ? volume.robust_min : min;
        const robustMax = Number.isFinite(volume && volume.robust_max) ? volume.robust_max : max;
        const span = max - min;
        const low = Math.max(0, Math.min(1, (robustMin - min) / span));
        const high = Math.max(low, Math.min(1, (robustMax - min) / span));
        return {
            level: Math.round((low + high) * 50),
            window: Math.max(1, Math.round((high - low) * 100)),
            range: { min, max, robustMin, robustMax }
        };
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
     * Expose NiiVue's native-order CPU voxel array without copying it.
     * Consumers must treat the returned data as read-only.
     */
    getNativeVolumeDescriptor() {
        if (!this.nv || !this.initialized || !this.nv.volumes || !this.nv.volumes[0]) {
            return null;
        }

        const volume = this.nv.volumes[0];
        const header = volume.hdr || {};
        const dims = header.dims || volume.dims;
        const data = volume.img;
        if (!dims || dims.length < 4 || !ArrayBuffer.isView(data)) {
            return null;
        }

        const dimensions = {
            width: Number(dims[1]),
            height: Number(dims[2]),
            depth: Number(dims[3])
        };
        if (data.length < dimensions.width * dimensions.height * dimensions.depth) {
            return null;
        }

        let affine = header.affine || null;
        if (affine && !Array.isArray(affine[0]) && affine.length >= 16) {
            affine = [
                Array.from(affine.slice(0, 4)),
                Array.from(affine.slice(4, 8)),
                Array.from(affine.slice(8, 12)),
                Array.from(affine.slice(12, 16))
            ];
        }

        return {
            data,
            dimensions,
            affine,
            flipZ: Boolean(affine && affine[2] && Number(affine[2][2]) > 0),
            slope: Number(header.scl_slope) || 1,
            intercept: Number(header.scl_inter) || 0,
            datatype: header.datatypeCode,
            fileName: volume.name || null
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

            if (typeof this.nv.cleanup === 'function') this.nv.cleanup();
            this.nv = null;
        }

        this.initialized = false;
        this.currentOrientation = 'axial';
        this.modalitySlug = null;
        this.segmentationOverlayLoaded = false;
        this.renderController = null;
        this.volumeMetadata = null;
        this.segmentationMetadata = null;
    }
}

// Expose as global for viewer_grid.js to use
window.NiiVueViewer = NiiVueViewer;
