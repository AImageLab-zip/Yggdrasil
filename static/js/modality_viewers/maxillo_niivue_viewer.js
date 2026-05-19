/**
 * MaxilloNiiVueViewer - Fixed 2x2 CBCT viewer using NiiVue
 *
 * Manages three NiiVue instances (Axial, Sagittal, Coronal) with:
 * - Synchronized crosshair positions across all views
 * - Percent-based windowing controls via adapter
 * - Integration with existing Maxillo template structure
 *
 * Usage (script-tag, no ES6 modules):
 *   var viewer = new window.MaxilloNiiVueViewer();
 *   viewer.init('cbct');  // Matches CBCTViewer.init() signature
 *   viewer.setWindowingFromPercent(10, 90);
 *   viewer.dispose();
 *
 * Dependencies:
 *   - NiiVue library (window.niivue)
 *   - NiiVueViewer class (window.NiiVueViewer)
 */

(function () {
    "use strict";

    /**
     * @constructor
     */
    function MaxilloNiiVueViewer() {
        this.initialized = false;
        this.loading = false;
        this.volumeBlob = null;
        this.targetModality = "cbct";
        this.containerPrefix = "";

        // NiiVue instances for each orientation
        this.viewers = {
            axial: null,
            sagittal: null,
            coronal: null,
        };

        // Windowing state (percent-based for UI compatibility)
        this.windowPercentMin = 0;
        this.windowPercentMax = 100;

        // Synchronization state
        this._isSyncing = false;
        this._syncRAF = null;
        this._pendingCrosshairPos = [0.5, 0.5, 0.5];
        this._pendingSourceOrientation = null;

        // Image modalities that should not use this viewer
        this._imageModalities = [
            "intraoral-photo",
            "teleradiography",
            "panoramic",
        ];
    }

    /**
     * Initialize the viewer with a modality
     * Entry point matching CBCTViewer.init() signature
     * @param {string} modalitySlug - The modality to display (e.g., 'cbct')
     */
    MaxilloNiiVueViewer.prototype.init = function (modalitySlug) {
        var self = this;

        // Skip for image modalities (these use different display methods)
        if (this._imageModalities.indexOf(modalitySlug) !== -1) {
            console.log(
                "MaxilloNiiVueViewer: Skipping image modality:",
                modalitySlug,
            );
            return;
        }

        if (this.loading) {
            console.warn(
                "MaxilloNiiVueViewer: Already loading, ignoring init call",
            );
            return;
        }

        this.targetModality = modalitySlug;
        this.loading = true;

        // Load volume data then initialize viewers
        this.loadVolumeData()
            .then(function () {
                return self.initializeViewers();
            })
            .then(function () {
                self.loading = false;
                self.initialized = true;
                console.log(
                    "MaxilloNiiVueViewer: Initialized successfully for",
                    modalitySlug,
                );
            })
            .catch(function (error) {
                self.loading = false;
                console.error(
                    "MaxilloNiiVueViewer: Initialization failed:",
                    error,
                );
            });
    };

    /**
     * Load volume data from API
     * Checks preload cache first, then fetches from appropriate endpoint
     * @returns {Promise<void>}
     */
    MaxilloNiiVueViewer.prototype.loadVolumeData = function () {
        var self = this;

        // Get scan ID and project namespace from Django template globals
        var scanId = window.scanId;
        var projectNamespace = window.projectNamespace || "maxillo";

        if (!scanId) {
            return Promise.reject(new Error("scanId not found in window"));
        }

        // Check preload cache first (populated by DOMContentLoaded preload)
        var cacheKey = scanId + ":" + this.targetModality;
        if (
            window._volumePreloadCache &&
            window._volumePreloadCache[cacheKey]
        ) {
            var cached = window._volumePreloadCache[cacheKey];
            // Handle promise (in-flight) or resolved blob
            if (cached instanceof Promise) {
                return cached.then(function (blob) {
                    self.volumeBlob = blob;
                });
            } else {
                self.volumeBlob = cached;
                return Promise.resolve();
            }
        }

        // Build API URL based on modality
        var url;
        if (this.targetModality === "cbct") {
            url = "/" + projectNamespace + "/api/patient/" + scanId + "/cbct/";
        } else {
            url =
                "/" +
                projectNamespace +
                "/api/patient/" +
                scanId +
                "/volume/" +
                this.targetModality +
                "/";
        }

        // Fetch volume data
        return fetch(url)
            .then(function (response) {
                if (!response.ok) {
                    throw new Error(
                        "HTTP " + response.status + ": " + response.statusText,
                    );
                }
                return response.blob();
            })
            .then(function (blob) {
                self.volumeBlob = blob;
                console.log(
                    "MaxilloNiiVueViewer: Volume loaded, size:",
                    blob.size,
                );
            });
    };

    /**
     * Initialize the three NiiVue viewer instances
     * Creates canvas elements and sets up each orientation view
     * @returns {Promise<void>}
     */
    MaxilloNiiVueViewer.prototype.initializeViewers = function () {
        var self = this;

        if (!this.volumeBlob) {
            return Promise.reject(new Error("No volume data loaded"));
        }

        // Check dependencies
        if (typeof window.NiiVueViewer !== "function") {
            return Promise.reject(new Error("NiiVueViewer class not loaded"));
        }

        // Container mapping for Maxillo template
        var containers = {
            axial: "axialView",
            sagittal: "sagittalView",
            coronal: "coronalView",
        };

        var orientations = ["axial", "sagittal", "coronal"];
        var initPromises = [];

        orientations.forEach(function (orientation) {
            var containerId = self.containerPrefix + containers[orientation];
            var container = document.getElementById(containerId);

            if (!container) {
                console.warn(
                    "MaxilloNiiVueViewer: Container not found:",
                    containerId,
                );
                return;
            }

            // Create canvas element for NiiVue
            var canvasId = "niivue-canvas-" + orientation;
            var existingCanvas = document.getElementById(canvasId);
            if (existingCanvas) {
                existingCanvas.remove();
            }

            var canvas = document.createElement("canvas");
            canvas.id = canvasId;
            canvas.className = "niivue-canvas";
            canvas.style.width = "100%";
            canvas.style.height = "100%";
            canvas.style.display = "block";

            // Clear container and add canvas
            container.innerHTML = "";
            container.appendChild(canvas);

            // Create NiiVueViewer instance
            var viewer = new window.NiiVueViewer(canvasId);
            self.viewers[orientation] = viewer;

            // Initialize viewer with volume and set orientation
            var initPromise = viewer
                .init(self.targetModality, self.volumeBlob)
                .then(function () {
                    viewer.setOrientation(orientation);

                    // Add slice counter element
                    var sliceCounter = document.createElement("div");
                    sliceCounter.className =
                        "slice-counter slice-counter-" + orientation;
                    sliceCounter.style.cssText =
                        "position: absolute; bottom: 5px; left: 5px; " +
                        "color: white; font-family: monospace; font-size: 12px; " +
                        "background: rgba(0,0,0,0.5); padding: 2px 5px; z-index: 10;";
                    container.style.position = "relative";
                    container.appendChild(sliceCounter);

                    // Update initial slice counter
                    self._updateSliceCounter(orientation);
                });

            initPromises.push(initPromise);
        });

        // After all viewers initialized, set up synchronization
        return Promise.all(initPromises).then(function () {
            self.setupSynchronization();
        });
    };

    /**
     * Set up cross-view synchronization
     * Uses rAF throttling to coalesce crosshair updates for performance
     */
    MaxilloNiiVueViewer.prototype.setupSynchronization = function () {
        var self = this;
        var orientations = ["axial", "sagittal", "coronal"];

        orientations.forEach(function (orientation) {
            var viewer = self.viewers[orientation];
            if (!viewer) return;

            viewer.onSliceChange(function (msg) {
                // Prevent infinite sync loops
                if (self._isSyncing) return;

                // Store pending sync data
                var crosshairPos = viewer.nv.scene.crosshairPos;
                self._pendingCrosshairPos[0] = crosshairPos[0];
                self._pendingCrosshairPos[1] = crosshairPos[1];
                self._pendingCrosshairPos[2] = crosshairPos[2];
                self._pendingSourceOrientation = orientation;

                // Coalesce updates with rAF
                if (!self._syncRAF) {
                    self._syncRAF = requestAnimationFrame(function () {
                        self._applyCrosshairSync();
                    });
                }
            });
        });
    };

    /**
     * Apply batched crosshair synchronization to all target views
     * Uses drawScene() for fast GPU-only redraw
     * @private
     */
    MaxilloNiiVueViewer.prototype._applyCrosshairSync = function () {
        this._syncRAF = null;
        this._isSyncing = true;

        var self = this;
        var sourceOrientation = this._pendingSourceOrientation;
        var crosshairPos = this._pendingCrosshairPos;
        var orientations = ["axial", "sagittal", "coronal"];

        orientations.forEach(function (orientation) {
            if (orientation === sourceOrientation) return;

            var viewer = self.viewers[orientation];
            if (viewer && viewer.isReady() && viewer.nv) {
                // Write directly into NiiVue's crosshairPos array
                var pos = viewer.nv.scene.crosshairPos;
                pos[0] = crosshairPos[0];
                pos[1] = crosshairPos[1];
                pos[2] = crosshairPos[2];

                // Fast GPU-only redraw (not updateGLVolume)
                viewer.nv.drawScene();
            }
        });

        // Update all slice counters
        this._updateAllSliceCounters();

        this._isSyncing = false;
    };

    /**
     * Update slice counter for a specific orientation
     * @param {string} orientation - 'axial', 'sagittal', or 'coronal'
     * @private
     */
    MaxilloNiiVueViewer.prototype._updateSliceCounter = function (orientation) {
        var viewer = this.viewers[orientation];
        if (!viewer || !viewer.isReady()) return;

        var counter = document.querySelector(".slice-counter-" + orientation);
        if (!counter) return;

        var currentSlice = viewer.getSliceIndex();
        var totalSlices = viewer.getSliceCount();
        counter.textContent = currentSlice + 1 + " / " + totalSlices;
    };

    /**
     * Update slice counters for all orientations
     * @private
     */
    MaxilloNiiVueViewer.prototype._updateAllSliceCounters = function () {
        this._updateSliceCounter("axial");
        this._updateSliceCounter("sagittal");
        this._updateSliceCounter("coronal");
    };

    /**
     * Set windowing using percent-based values
     * Updates all three viewers to maintain consistent appearance
     * @param {number} percentMin - Lower window percent (0-100)
     * @param {number} percentMax - Upper window percent (0-100)
     */
    MaxilloNiiVueViewer.prototype.setWindowingFromPercent = function (
        percentMin,
        percentMax,
    ) {
        this.windowPercentMin = percentMin;
        this.windowPercentMax = percentMax;

        var orientations = ["axial", "sagittal", "coronal"];
        var self = this;

        orientations.forEach(function (orientation) {
            var viewer = self.viewers[orientation];
            if (viewer && viewer.isReady()) {
                viewer.setWindowing(percentMin, percentMax);
            }
        });
    };

    /**
     * Get current windowing as percent values
     * @returns {{percentMin: number, percentMax: number}}
     */
    MaxilloNiiVueViewer.prototype.getWindowingPercent = function () {
        return {
            percentMin: this.windowPercentMin,
            percentMax: this.windowPercentMax,
        };
    };

    /**
     * Get the volume's data range (from first available viewer)
     * @returns {{min: number, max: number}}
     */
    MaxilloNiiVueViewer.prototype.getDataRange = function () {
        var viewer =
            this.viewers.axial || this.viewers.sagittal || this.viewers.coronal;
        if (viewer && viewer.isReady()) {
            return viewer.getDataRange();
        }
        return { min: 0, max: 1 };
    };

    /**
     * Force redraw of all views
     * Useful for tab switching or resize events
     */
    MaxilloNiiVueViewer.prototype.refreshAllViews = function () {
        var orientations = ["axial", "sagittal", "coronal"];
        var self = this;

        orientations.forEach(function (orientation) {
            var viewer = self.viewers[orientation];
            if (viewer && viewer.isReady()) {
                viewer.redraw();
            }
        });
    };

    /**
     * Dispose all viewers and clean up resources
     * Keeps volumeBlob cached for potential re-initialization
     */
    MaxilloNiiVueViewer.prototype.dispose = function () {
        // Cancel any pending sync
        if (this._syncRAF) {
            cancelAnimationFrame(this._syncRAF);
            this._syncRAF = null;
        }

        // Dispose all viewers
        var orientations = ["axial", "sagittal", "coronal"];
        var self = this;

        orientations.forEach(function (orientation) {
            var viewer = self.viewers[orientation];
            if (viewer) {
                viewer.dispose();
                self.viewers[orientation] = null;
            }
        });

        // Reset state (keep volumeBlob cached)
        this.initialized = false;
        this.loading = false;
        this._isSyncing = false;
        this._pendingCrosshairPos = [0.5, 0.5, 0.5];
        this._pendingSourceOrientation = null;
    };

    /**
     * Check if viewer is ready
     * @returns {boolean}
     */
    MaxilloNiiVueViewer.prototype.isReady = function () {
        return (
            this.initialized &&
            this.viewers.axial &&
            this.viewers.axial.isReady() &&
            this.viewers.sagittal &&
            this.viewers.sagittal.isReady() &&
            this.viewers.coronal &&
            this.viewers.coronal.isReady()
        );
    };

    // Expose globally (no ES6 modules - Django script-tag constraint)
    window.MaxilloNiiVueViewer = MaxilloNiiVueViewer;
})();
