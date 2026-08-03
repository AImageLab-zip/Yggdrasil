(function() {
    'use strict';

    function getViewerGrid() {
        if (window.ViewerGrid) {
            return window.ViewerGrid;
        }

        if (typeof ViewerGrid !== 'undefined') {
            return ViewerGrid;
        }

        return null;
    }

    function getViewerData() {
        const dataEl = document.getElementById('viewerGridData');
        if (!dataEl) {
            return {};
        }

        try {
            return JSON.parse(dataEl.textContent || '{}');
        } catch (e) {
            console.warn('Failed to parse viewerGridData:', e);
            return {};
        }
    }

    function prepareOptional3DWindow(viewerGrid, cbctFileId) {
        viewerGrid.clearWindow(3);
        const windowEl = document.querySelector('.viewer-window[data-window-index="3"]');
        if (!windowEl) {
            return;
        }

        windowEl.innerHTML = `
            <div class="viewer-optional-3d">
                <i class="fas fa-cube"></i>
                <p>3D volume rendering</p>
                <button type="button" class="btn btn-sm btn-outline-light viewer-load-3d-btn">
                    Load 3D
                </button>
            </div>
        `;

        const button = windowEl.querySelector('.viewer-load-3d-btn');
        if (!button) {
            return;
        }

        button.addEventListener('click', async function() {
            button.disabled = true;
            button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Loading';

            try {
                await viewerGrid.loadModalityInWindow(3, 'cbct', cbctFileId);
                const state = viewerGrid.windowStates && viewerGrid.windowStates[3];
                if (!state || !state.niivueInstance || !state.niivueInstance.isReady()) {
                    throw new Error('The optional 3D viewer could not be initialized');
                }
                viewerGrid.setWindowOrientation(3, 'render');

                const minRange = document.getElementById('windowMinRange');
                const maxRange = document.getElementById('windowMaxRange');
                if (window.CBCTViewer && minRange && maxRange) {
                    window.CBCTViewer.setWindowingFromPercent(
                        parseInt(minRange.value || '0', 10),
                        parseInt(maxRange.value || '100', 10)
                    );
                }
            } catch (error) {
                console.warn('Optional CBCT 3D viewer failed; keeping 2D views active:', error);
                prepareOptional3DWindow(viewerGrid, cbctFileId);
            }
        });
    }

    async function initFixedCbctGrid(cbctFileIdOverride) {
        var viewerGrid = getViewerGrid();
        if (!viewerGrid) {
            throw new Error('ViewerGrid is not available');
        }

        viewerGrid.init();

        const data = getViewerData();
        const cbctInfo = data.modalityFiles && data.modalityFiles.cbct;
        const cbctFileId = cbctFileIdOverride || (cbctInfo && cbctInfo.id);

        if (!cbctFileId) {
            throw new Error('CBCT file id not found for fixed grid initialization');
        }

        function loadAndOrient(windowIndex, orientation) {
            return viewerGrid.loadModalityInWindow(windowIndex, 'cbct', cbctFileId)
                .then(function() {
                    viewerGrid.setWindowOrientation(windowIndex, orientation);
                });
        }

        if (typeof viewerGrid.suspendSynchronization === 'function') {
            viewerGrid.suspendSynchronization();
        }

        try {
            await loadAndOrient(0, 'axial');
            await loadAndOrient(1, 'sagittal');
            await loadAndOrient(2, 'coronal');
        } finally {
            if (typeof viewerGrid.resumeSynchronization === 'function') {
                viewerGrid.resumeSynchronization();
            }
        }

        [0, 1, 2].forEach(function(windowIndex) {
            var state = viewerGrid.windowStates && viewerGrid.windowStates[windowIndex];
            if (!state || !state.niivueInstance || !state.niivueInstance.isReady() || !state.niivueInstance.nv) {
                return;
            }
            var pos = state.niivueInstance.nv.scene.crosshairPos;
            if (!pos || pos.length < 3) {
                return;
            }
            pos[0] = 0.5;
            pos[1] = 0.5;
            pos[2] = 0.5;
            state.niivueInstance.nv.drawScene();
        });

        try {
            prepareOptional3DWindow(viewerGrid, cbctFileId);
        } catch (error) {
            console.warn('Optional CBCT 3D controls failed; keeping 2D views active:', error);
        }

        return cbctFileId;
    }

    window.CBCTViewer = {
        initialized: false,
        loading: false,
        panoramicLoaded: false,
        controlsBound: false,
        activeFileId: null,
        _initGeneration: 0,

        init: function(modalitySlug) {
            if (modalitySlug && modalitySlug !== 'cbct') {
                return;
            }

            const data = getViewerData();
            const cbctInfo = data.modalityFiles && data.modalityFiles.cbct;
            const desiredFileId = cbctInfo && cbctInfo.id;

            if (this.initialized && (!desiredFileId || (this.activeFileId && String(this.activeFileId) === String(desiredFileId)))) {
                this.refreshAllViews();
                return;
            }

            if (this.loading) {
                return;
            }

            const initGeneration = ++this._initGeneration;
            this.loading = true;

            initFixedCbctGrid(desiredFileId)
                .then((activeFileId) => {
                    if (initGeneration !== this._initGeneration) {
                        return;
                    }
                    this.bindControls();
                    this.activeFileId = activeFileId;
                    this.initialized = true;
                    this.loading = false;
                })
                .catch((e) => {
                    if (initGeneration !== this._initGeneration) {
                        return;
                    }
                    this.initialized = false;
                    this.loading = false;
                    console.error('Failed to initialize fixed CBCT grid:', e);
                });
        },

        refreshAllViews: function() {
            var viewerGrid = getViewerGrid();
            if (!viewerGrid || !viewerGrid.windowStates) {
                return;
            }

            [0, 1, 2, 3].forEach((idx) => {
                const state = viewerGrid.windowStates[idx];
                if (state && state.niivueInstance && state.niivueInstance.isReady()) {
                    state.niivueInstance.redraw();
                }
            });
        },

        setWindowingFromPercent: function(percentMin, percentMax, options) {
            var viewerGrid = getViewerGrid();
            if (!viewerGrid || !viewerGrid.windowStates) {
                return;
            }

            const windowingOptions = options || {};
            const previewOnlyWindowIndex = Number.isInteger(windowingOptions.previewOnlyWindowIndex)
                ? windowingOptions.previewOnlyWindowIndex
                : null;

            [0, 1, 2, 3].forEach((idx) => {
                if (previewOnlyWindowIndex !== null && idx !== previewOnlyWindowIndex) {
                    return;
                }
                const state = viewerGrid.windowStates[idx];
                if (state && state.niivueInstance && state.niivueInstance.isReady()) {
                    state.niivueInstance.setWindowing(percentMin, percentMax, windowingOptions);
                }
            });
        },

        bindControls: function() {
            if (this.controlsBound) {
                return;
            }

            const minRange = document.getElementById('windowMinRange');
            const maxRange = document.getElementById('windowMaxRange');
            const minLabel = document.getElementById('windowMinValue');
            const maxLabel = document.getElementById('windowMaxValue');
            const resetButton = document.getElementById('resetCBCTView');

            const updateLabels = (minVal, maxVal) => {
                if (minLabel) {
                    minLabel.textContent = String(minVal);
                }
                if (maxLabel) {
                    maxLabel.textContent = String(maxVal);
                }
            };

            const targetFps = 15;
            const frameIntervalMs = 1000 / targetFps;
            let rafId = null;
            let lastAppliedTs = 0;
            let pendingMin = null;
            let pendingMax = null;
            let lastAppliedMin = null;
            let lastAppliedMax = null;
            let lastCommittedMin = null;
            let lastCommittedMax = null;
            let commitToken = 0;
            let pendingCommitFrameId = null;

            const cancelPendingCommitFrames = () => {
                if (pendingCommitFrameId !== null) {
                    cancelAnimationFrame(pendingCommitFrameId);
                    pendingCommitFrameId = null;
                }
            };

            const commitWindowingStaggered = (minVal, maxVal) => {
                if (minVal === lastCommittedMin && maxVal === lastCommittedMax) {
                    return;
                }

                cancelPendingCommitFrames();
                commitToken += 1;
                const currentCommitToken = commitToken;

                const viewerGrid = getViewerGrid();
                if (!viewerGrid || !viewerGrid.windowStates) {
                    return;
                }

                const windowOrder = [0, 1, 2, 3];

                const applyForWindow = (windowIndex) => {
                    if (currentCommitToken !== commitToken) {
                        return;
                    }

                    const state = viewerGrid.windowStates[windowIndex];
                    if (state && state.niivueInstance && state.niivueInstance.isReady()) {
                        state.niivueInstance.setWindowing(minVal, maxVal, { commit: true });
                    }
                };

                const run = (idx) => {
                    if (currentCommitToken !== commitToken) {
                        return;
                    }
                    if (idx >= windowOrder.length) {
                        lastCommittedMin = minVal;
                        lastCommittedMax = maxVal;
                        pendingCommitFrameId = null;
                        return;
                    }

                    applyForWindow(windowOrder[idx]);

                    pendingCommitFrameId = requestAnimationFrame(() => run(idx + 1));
                };

                run(0);
            };

            const applyWindowingNow = (minVal, maxVal, commit) => {
                if (!commit && minVal === lastAppliedMin && maxVal === lastAppliedMax) {
                    return;
                }

                if (commit) {
                    commitWindowingStaggered(minVal, maxVal);
                } else {
                    // Fast interactive preview on axial window only; apply to all on commit.
                    this.setWindowingFromPercent(minVal, maxVal, {
                        commit: false,
                        previewOnlyWindowIndex: 0
                    });
                }
                lastAppliedMin = minVal;
                lastAppliedMax = maxVal;
            };

            const scheduleWindowing = (minVal, maxVal) => {
                pendingMin = minVal;
                pendingMax = maxVal;

                if (rafId) {
                    return;
                }

                const tick = (ts) => {
                    if (ts - lastAppliedTs >= frameIntervalMs) {
                        if (pendingMin !== null && pendingMax !== null) {
                            applyWindowingNow(pendingMin, pendingMax, false);
                            lastAppliedTs = ts;
                        }
                        rafId = null;
                        return;
                    }
                    rafId = requestAnimationFrame(tick);
                };

                rafId = requestAnimationFrame(tick);
            };

            const flushWindowing = (minVal, maxVal) => {
                if (rafId) {
                    cancelAnimationFrame(rafId);
                    rafId = null;
                }

                pendingMin = null;
                pendingMax = null;
                applyWindowingNow(minVal, maxVal, true);
            };

            const applyWindowing = (commit) => {
                if (!minRange || !maxRange) {
                    return;
                }

                let minVal = parseInt(minRange.value || '0', 10);
                let maxVal = parseInt(maxRange.value || '100', 10);

                if (minVal > maxVal) {
                    maxVal = minVal;
                    maxRange.value = String(maxVal);
                }

                updateLabels(minVal, maxVal);

                if (commit) {
                    flushWindowing(minVal, maxVal);
                } else {
                    scheduleWindowing(minVal, maxVal);
                }
            };

            if (minRange) {
                minRange.addEventListener('input', () => applyWindowing(false));
                minRange.addEventListener('change', () => applyWindowing(true));
            }
            if (maxRange) {
                maxRange.addEventListener('input', () => applyWindowing(false));
                maxRange.addEventListener('change', () => applyWindowing(true));
            }

            if (resetButton) {
                resetButton.addEventListener('click', () => {
                    const viewerGrid = getViewerGrid();
                    if (!viewerGrid || !viewerGrid.windowStates) {
                        return;
                    }

                    [0, 1, 2, 3].forEach((idx) => {
                        const state = viewerGrid.windowStates[idx];
                        if (state && state.niivueInstance && state.niivueInstance.isReady() && state.niivueInstance.nv) {
                            const pos = state.niivueInstance.nv.scene.crosshairPos;
                            if (pos && pos.length >= 3) {
                                pos[0] = 0.5;
                                pos[1] = 0.5;
                                pos[2] = 0.5;
                            }
                            state.niivueInstance.nv.scene.pan2Dxyzmm = [0, 0, 0, 1];
                            state.niivueInstance.nv.drawScene();
                        }
                    });
                });
            }

            if (minRange && maxRange) {
                updateLabels(parseInt(minRange.value || '0', 10), parseInt(maxRange.value || '100', 10));
            }

            this.controlsBound = true;
        },

        loadPanoramicImage: function() {
            // Intentionally no-op in fixed CBCT NiiVue grid mode.
        },

        dispose: function() {
            this._initGeneration += 1;

            var viewerGrid = getViewerGrid();
            if (!viewerGrid) {
                this.initialized = false;
                this.loading = false;
                this.activeFileId = null;
                return;
            }

            [0, 1, 2, 3].forEach((idx) => {
                try {
                    viewerGrid.clearWindow(idx);
                } catch (e) {
                    console.warn('Error clearing viewer window', idx, e);
                }
            });

            this.initialized = false;
            this.loading = false;
            this.activeFileId = null;
        }
    };
})();
