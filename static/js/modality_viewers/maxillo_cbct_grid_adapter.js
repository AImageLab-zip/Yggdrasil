(function() {
    'use strict';

    function getViewerGrid() {
        return window.ViewerGrid || (typeof ViewerGrid !== 'undefined' ? ViewerGrid : null);
    }

    function getViewerData() {
        const dataEl = document.getElementById('viewerGridData');
        if (!dataEl) {
            return {};
        }
        try {
            return JSON.parse(dataEl.textContent || '{}');
        } catch (error) {
            console.warn('Failed to parse viewerGridData:', error);
            return {};
        }
    }

    function readyViewer(viewerGrid, windowIndex) {
        const state = viewerGrid && viewerGrid.windowStates && viewerGrid.windowStates[windowIndex];
        return state && state.niivueInstance && state.niivueInstance.isReady()
            ? state.niivueInstance
            : null;
    }

    function prepareOptional3DWindow(viewerGrid) {
        viewerGrid.clearWindow(3);
        const windowEl = document.querySelector('.viewer-window[data-window-index="3"]');
        if (!windowEl) {
            return;
        }
        windowEl.innerHTML = `
            <div class="viewer-optional-3d">
                <i class="fas fa-cube" aria-hidden="true"></i>
                <p>3D rendering loads only when requested.</p>
                <button type="button" class="viewer-load-3d-btn">Load 3D</button>
            </div>
        `;
        const button = windowEl.querySelector('.viewer-load-3d-btn');
        button.addEventListener('click', function() {
            window.CBCTViewer.loadRenderWindow(button);
        });
    }

    async function initFixedCbctGrid(cbctFileIdOverride) {
        const viewerGrid = getViewerGrid();
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
            return viewerGrid.loadModalityInWindow(windowIndex, 'cbct', cbctFileId).then(function() {
                viewerGrid.setWindowOrientation(windowIndex, orientation);
            });
        }

        viewerGrid.suspendSynchronization();
        try {
            await loadAndOrient(0, 'axial');
            await loadAndOrient(1, 'sagittal');
            await loadAndOrient(2, 'coronal');
        } finally {
            viewerGrid.resumeSynchronization();
        }

        [0, 1, 2].forEach(function(windowIndex) {
            const viewer = readyViewer(viewerGrid, windowIndex);
            const position = viewer && viewer.nv && viewer.nv.scene.crosshairPos;
            if (position && position.length >= 3) {
                position[0] = 0.5;
                position[1] = 0.5;
                position[2] = 0.5;
                viewer.nv.drawScene();
            }
        });
        prepareOptional3DWindow(viewerGrid);
        return cbctFileId;
    }

    window.CBCTViewer = {
        initialized: false,
        loading: false,
        panoramicLoaded: false,
        controlsBound: false,
        activeFileId: null,
        renderLoadPromise: null,
        renderFocused: false,
        level: 50,
        windowWidth: 100,
        renderMode: 'amip',
        _initGeneration: 0,

        init: function(modalitySlug) {
            if (modalitySlug && modalitySlug !== 'cbct') {
                return;
            }
            const data = getViewerData();
            const cbctInfo = data.modalityFiles && data.modalityFiles.cbct;
            const desiredFileId = cbctInfo && cbctInfo.id;
            if (this.initialized && (!desiredFileId || String(this.activeFileId) === String(desiredFileId))) {
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
                    this.initializeWindowControls();
                    this.initialized = true;
                    this.loading = false;
                })
                .catch((error) => {
                    if (initGeneration !== this._initGeneration) {
                        return;
                    }
                    this.initialized = false;
                    this.loading = false;
                    this.setRenderStatus('CBCT viewer failed to initialize.');
                    console.error('Failed to initialize fixed CBCT grid:', error);
                });
        },

        refreshAllViews: function() {
            const viewerGrid = getViewerGrid();
            [0, 1, 2, 3].forEach(function(windowIndex) {
                const viewer = readyViewer(viewerGrid, windowIndex);
                if (viewer) {
                    viewer.redraw();
                }
            });
        },

        initializeWindowControls: function() {
            const viewer = readyViewer(getViewerGrid(), 0);
            if (viewer && typeof viewer.getInitialLevelWindow === 'function') {
                const initial = viewer.getInitialLevelWindow();
                this.level = initial.level;
                this.windowWidth = initial.window;
            }
            this.syncWindowControls();
            this.setLevelWindow(this.level, this.windowWidth);
        },

        syncWindowControls: function() {
            const levelRange = document.getElementById('windowLevelRange');
            const windowRange = document.getElementById('windowWidthRange');
            const levelValue = document.getElementById('windowLevelValue');
            const windowValue = document.getElementById('windowWidthValue');
            if (levelRange) levelRange.value = String(this.level);
            if (windowRange) windowRange.value = String(this.windowWidth);
            if (levelValue) levelValue.textContent = String(this.level);
            if (windowValue) windowValue.textContent = String(this.windowWidth);
        },

        setLevelWindow: function(level, width) {
            this.level = Math.max(0, Math.min(100, Number(level) || 0));
            this.windowWidth = Math.max(1, Math.min(100, Number(width) || 1));
            this.syncWindowControls();
            const viewerGrid = getViewerGrid();
            [0, 1, 2, 3].forEach((windowIndex) => {
                const viewer = readyViewer(viewerGrid, windowIndex);
                if (viewer && typeof viewer.setLevelWindow === 'function') {
                    viewer.setLevelWindow(this.level, this.windowWidth);
                }
            });
        },

        setRenderStatus: function(message, isError) {
            const status = document.getElementById('cbctRenderStatus');
            if (!status) {
                return;
            }
            status.textContent = message || '';
            status.classList.toggle('is-error', !!isError);
        },

        updateModeAvailability: function(viewer) {
            const select = document.getElementById('cbctRenderMode');
            if (!select || !viewer || typeof viewer.getRenderModeAvailability !== 'function') {
                return;
            }
            const availability = viewer.getRenderModeAvailability();
            Array.from(select.options).forEach(function(option) {
                const capability = availability[option.value];
                option.disabled = !capability || !capability.available;
            });
        },

        resolveRenderModeResult: function(result) {
            return result && result.ready ? result.ready : Promise.resolve(result);
        },

        applyRenderMode: async function(mode) {
            const viewerGrid = getViewerGrid();
            const viewer = readyViewer(viewerGrid, 3);
            if (!viewer) {
                return { available: false, message: 'Load the 3D viewer to select a render mode.' };
            }
            const result = await this.resolveRenderModeResult(viewerGrid.setWindowRenderMode(3, mode));
            this.updateModeAvailability(viewer);
            if (result.available) {
                this.renderMode = mode;
                const select = document.getElementById('cbctRenderMode');
                if (select) select.value = mode;
            }
            this.setRenderStatus(result.message || '', !result.available);
            return result;
        },

        ensureRenderViewer: function() {
            const viewerGrid = getViewerGrid();
            const existing = readyViewer(viewerGrid, 3);
            if (existing) {
                return Promise.resolve(existing);
            }
            if (this.renderLoadPromise) {
                return this.renderLoadPromise;
            }

            this.setRenderStatus('Loading 3D volume...');
            const loadPromise = (async () => {
                await viewerGrid.loadModalityInWindow(3, 'cbct', this.activeFileId);
                const viewer = readyViewer(viewerGrid, 3);
                if (!viewer) {
                    throw new Error('The 3D viewer could not be initialized');
                }
                viewer.setLevelWindow(this.level, this.windowWidth);
                let result = await this.resolveRenderModeResult(viewerGrid.setWindowRenderMode(3, this.renderMode));
                this.updateModeAvailability(viewer);
                if (!result.available && this.renderMode !== 'shaded') {
                    const unavailableMessage = result.message;
                    result = await this.resolveRenderModeResult(viewerGrid.setWindowRenderMode(3, 'shaded'));
                    if (result.available) {
                        this.renderMode = 'shaded';
                        const select = document.getElementById('cbctRenderMode');
                        if (select) select.value = 'shaded';
                        result.message = unavailableMessage + ' ' + (result.message || 'Using Shaded Volume instead.');
                    }
                }
                this.updateModeAvailability(viewer);
                this.setRenderStatus(result.message || '', !result.available);
                return viewer;
            })();
            const trackedPromise = loadPromise.catch((error) => {
                if (this.renderLoadPromise === trackedPromise) {
                    this.renderLoadPromise = null;
                }
                this.setRenderStatus('3D rendering is unavailable: ' + error.message, true);
                throw error;
            });
            this.renderLoadPromise = trackedPromise;
            return this.renderLoadPromise;
        },

        setRenderFocus: function(enabled) {
            const grid = document.querySelector('.viewer-grid');
            const toggle = document.getElementById('toggleCBCT3DOnly');
            const exit = document.getElementById('exitCBCT3DFocus');
            const tools = grid && grid.previousElementSibling && grid.previousElementSibling.classList.contains('viewer-tools-bar')
                ? grid.previousElementSibling
                : null;
            this.renderFocused = !!enabled;
            if (grid) grid.classList.toggle('viewer-grid--render-focus', this.renderFocused);
            if (tools) tools.classList.toggle('viewer-tools-bar--render-focus', this.renderFocused);
            if (toggle) {
                toggle.classList.toggle('active', this.renderFocused);
                toggle.setAttribute('aria-pressed', this.renderFocused ? 'true' : 'false');
            }
            if (exit) exit.hidden = !this.renderFocused;
            requestAnimationFrame(() => this.refreshAllViews());
        },

        enterRenderFocus: async function(sourceButton) {
            if (this.renderFocused) {
                this.setRenderFocus(false);
                return;
            }
            if (sourceButton) sourceButton.disabled = true;
            const toggle = document.getElementById('toggleCBCT3DOnly');
            if (toggle) toggle.disabled = true;
            try {
                await this.ensureRenderViewer();
                this.setRenderFocus(true);
            } catch (error) {
                console.warn('Optional CBCT 3D viewer failed; slice views remain active:', error);
            } finally {
                if (sourceButton) sourceButton.disabled = false;
                if (toggle) toggle.disabled = false;
            }
        },

        loadRenderWindow: async function(sourceButton) {
            if (sourceButton) sourceButton.disabled = true;
            try {
                await this.ensureRenderViewer();
                this.refreshAllViews();
            } catch (error) {
                console.warn('Optional CBCT 3D viewer failed; slice views remain active:', error);
            } finally {
                if (sourceButton && sourceButton.isConnected) sourceButton.disabled = false;
            }
        },

        resetViews: function() {
            const viewerGrid = getViewerGrid();
            [0, 1, 2].forEach(function(windowIndex) {
                const viewer = readyViewer(viewerGrid, windowIndex);
                if (!viewer || !viewer.nv) return;
                const position = viewer.nv.scene.crosshairPos;
                if (position && position.length >= 3) {
                    position[0] = 0.5;
                    position[1] = 0.5;
                    position[2] = 0.5;
                }
                viewer.nv.scene.pan2Dxyzmm = [0, 0, 0, 1];
                viewer.nv.drawScene();
            });
            const renderViewer = readyViewer(viewerGrid, 3);
            if (renderViewer) renderViewer.resetRenderCamera();
        },

        bindControls: function() {
            if (this.controlsBound) {
                return;
            }
            const levelRange = document.getElementById('windowLevelRange');
            const windowRange = document.getElementById('windowWidthRange');
            const resetButton = document.getElementById('resetCBCTView');
            const toggle3D = document.getElementById('toggleCBCT3DOnly');
            const renderMode = document.getElementById('cbctRenderMode');
            const reset3D = document.getElementById('resetCBCT3DCamera');
            const exit3D = document.getElementById('exitCBCT3DFocus');

            const applyRanges = () => {
                this.setLevelWindow(
                    parseInt(levelRange ? levelRange.value : String(this.level), 10),
                    parseInt(windowRange ? windowRange.value : String(this.windowWidth), 10)
                );
            };
            if (levelRange) levelRange.addEventListener('input', applyRanges);
            if (windowRange) windowRange.addEventListener('input', applyRanges);
            if (resetButton) resetButton.addEventListener('click', () => this.resetViews());
            if (toggle3D) toggle3D.addEventListener('click', () => this.enterRenderFocus(toggle3D));
            if (renderMode) {
                renderMode.addEventListener('change', async () => {
                    try {
                        await this.ensureRenderViewer();
                        const result = await this.applyRenderMode(renderMode.value);
                        if (!result.available) renderMode.value = this.renderMode;
                    } catch (error) {
                        renderMode.value = this.renderMode;
                    }
                });
            }
            if (reset3D) {
                reset3D.addEventListener('click', () => {
                    const viewer = readyViewer(getViewerGrid(), 3);
                    if (viewer) viewer.resetRenderCamera();
                });
            }
            if (exit3D) exit3D.addEventListener('click', () => this.setRenderFocus(false));
            document.addEventListener('keydown', (event) => {
                if (event.key === 'Escape' && this.renderFocused) {
                    this.setRenderFocus(false);
                }
            });
            window.addEventListener('viewergridvolumeready', (event) => {
                const detail = event.detail || {};
                const viewer = readyViewer(getViewerGrid(), detail.windowIndex);
                if (viewer) viewer.setLevelWindow(this.level, this.windowWidth);
            });
            this.controlsBound = true;
        },

        loadPanoramicImage: function() {
            // Intentionally no-op in fixed CBCT NiiVue grid mode.
        },

        dispose: function() {
            this._initGeneration += 1;
            this.setRenderFocus(false);
            const viewerGrid = getViewerGrid();
            if (viewerGrid) {
                [0, 1, 2, 3].forEach(function(windowIndex) {
                    try {
                        viewerGrid.clearWindow(windowIndex);
                    } catch (error) {
                        console.warn('Error clearing viewer window', windowIndex, error);
                    }
                });
            }
            this.initialized = false;
            this.loading = false;
            this.activeFileId = null;
            this.renderLoadPromise = null;
        }
    };
})();
