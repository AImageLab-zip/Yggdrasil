/**
 * Viewer Grid - Drag-drop interaction for multi-window MRI viewer
 *
 * Manages state for 4 viewer windows and drag-drop loading of modalities.
 * Each window gets its own NiiVueViewer instance for true multi-window support.
 */

const ViewerGrid = (function() {
    'use strict';

    let isInitialized = false;

    // Cache for fetched volume ArrayBuffers (keyed by fileId).
    // Note: Cache persists across window clears for network optimization.
    const volumeCache = {};
    const volumeFetchPromises = {};

    // Per-window load generation counters used to invalidate stale async loads.
    // If a window is cleared or reloaded while an older load is still running,
    // only the newest generation is allowed to update UI/state.
    const windowLoadGenerations = {
        0: 0,
        1: 0,
        2: 0,
        3: 0
    };

    // Window state for 4 grid positions
    const windowStates = {
        0: { modality: null, loading: false, error: null, fileId: null, niivueInstance: null, currentOrientation: 'axial' },
        1: { modality: null, loading: false, error: null, fileId: null, niivueInstance: null, currentOrientation: 'axial' },
        2: { modality: null, loading: false, error: null, fileId: null, niivueInstance: null, currentOrientation: 'axial' },
        3: { modality: null, loading: false, error: null, fileId: null, niivueInstance: null, currentOrientation: 'axial' }
    };

    function beginWindowLoad(windowIndex) {
        if (!Object.prototype.hasOwnProperty.call(windowLoadGenerations, windowIndex)) {
            windowLoadGenerations[windowIndex] = 0;
        }
        windowLoadGenerations[windowIndex] += 1;
        return windowLoadGenerations[windowIndex];
    }

    function isWindowLoadCurrent(windowIndex, generation) {
        return windowLoadGenerations[windowIndex] === generation;
    }

    // Synchronization groups - windows viewing the same orientation scroll together
    const synchronizationGroups = {
        'axial': [],
        'sagittal': [],
        'coronal': []
    };

    // Free scroll state - tracks which windows have free-scroll enabled
    const freeScrollWindows = {
        0: false,
        1: false,
        2: false,
        3: false
    };

    // rAF-throttled synchronization state — coalesces multiple crosshair
    // updates per frame into a single sync pass using drawScene() instead
    // of the heavier updateGLVolume().
    let _syncRAF = null;
    let _isSyncing = false;
    let _syncSourceWindow = -1;
    const _syncCrosshairPos = [0, 0, 0];
    let _syncSuspended = false;
    const VISIBLE_CROSSHAIR_WIDTH = 2;

    function clampCrosshairCoord(value) {
        if (typeof value !== 'number' || !Number.isFinite(value)) {
            return 0.5;
        }
        if (value < 0) return 0;
        if (value > 1) return 1;
        return value;
    }

    const TOOL_IDS = {
        NONE: 'none',
        MEASURE: 'measure'
    };

    const toolRegistry = {};
    let activeTool = TOOL_IDS.NONE;

    const measurementState = {
        pendingStartPoint: null,
        measurements: [],
        nextId: 1
    };

    const measurementOverlayState = {
        0: { projected: [], hover: null },
        1: { projected: [], hover: null },
        2: { projected: [], hover: null },
        3: { projected: [], hover: null }
    };

    // Global data from Django template
    let djangoData = {
        scanId: null,
        projectNamespace: null,
        modalityFiles: {},
        fixedMode: false,
        enableDragDrop: true,
        enableContextMenu: true,
        allowClearWindow: true
    };

    /**
     * Initialize the viewer grid system
     * Called on DOMContentLoaded for brain project pages
     */
    function init() {
        if (isInitialized) {
            return;
        }

        // Load Django data from template script
        loadDjangoData();

        // Populate file IDs on modality chips
        populateChipFileIds();

        // Initialize drag-drop interaction (disabled in fixed layouts)
        if (djangoData.enableDragDrop) {
            initDragDrop();
        }

        // Initialize context menu lifecycle handlers (outside-click close, etc.)
        initContextMenus();

        // Initialize synchronization system
        initSynchronization();

        // Initialize tool system (measurement and future tools)
        initToolSystem();

        window.addEventListener('resize', () => {
            renderMeasurementOverlays();
        });

        isInitialized = true;
        console.log('ViewerGrid initialized', { djangoData, windowStates });
    }

    function initToolSystem() {
        registerMeasurementTool();
        initGlobalToolbar();
        updateToolbarState();
    }

    function registerTool(toolId, toolDefinition) {
        toolRegistry[toolId] = toolDefinition;
    }

    function setActiveTool(toolId) {
        const nextTool = (toolId && toolRegistry[toolId]) ? toolId : TOOL_IDS.NONE;
        if (nextTool === activeTool) {
            if (toolRegistry[activeTool] && typeof toolRegistry[activeTool].onDeactivate === 'function') {
                toolRegistry[activeTool].onDeactivate();
            }
            activeTool = TOOL_IDS.NONE;
        } else {
            if (toolRegistry[activeTool] && typeof toolRegistry[activeTool].onDeactivate === 'function') {
                toolRegistry[activeTool].onDeactivate();
            }
            activeTool = nextTool;
        }

        updateToolbarState();
        updateToolCursors();
        renderMeasurementOverlays();
    }

    function updateToolbarState() {
        const measureButtons = document.querySelectorAll('.viewer-tool-btn[data-tool="measure"]');
        measureButtons.forEach((button) => {
            button.classList.toggle('active', activeTool === TOOL_IDS.MEASURE);
            button.setAttribute('aria-pressed', activeTool === TOOL_IDS.MEASURE ? 'true' : 'false');
        });

        const statusEls = document.querySelectorAll('.viewer-tool-status');
        let statusText = 'Tool: None';
        if (activeTool === TOOL_IDS.MEASURE) {
            statusText = measurementState.pendingStartPoint
                ? 'Measure: select second point'
                : 'Measure: select first point';
        }
        statusEls.forEach((el) => {
            el.textContent = statusText;
        });
    }

    function updateToolCursors() {
        const windows = document.querySelectorAll('.viewer-window');
        windows.forEach((windowEl) => {
            windowEl.classList.toggle('measure-tool-active', activeTool === TOOL_IDS.MEASURE);
        });
    }

    function initGlobalToolbar() {
        const grids = document.querySelectorAll('.viewer-grid');
        grids.forEach((gridEl) => {
            if (!gridEl || gridEl.previousElementSibling && gridEl.previousElementSibling.classList.contains('viewer-tools-bar')) {
                return;
            }

            const toolbar = document.createElement('div');
            toolbar.className = 'viewer-tools-bar';
            toolbar.innerHTML = `
                <div class="viewer-tools-left">
                    <button type="button" class="viewer-tool-btn" data-tool="measure" title="Distance measurement">
                        <i class="fas fa-ruler"></i>
                    </button>
                    <button type="button" class="viewer-tool-btn" data-tool-action="clear-measurements" title="Clear measurements">
                        <i class="fas fa-eraser"></i>
                    </button>
                </div>
                <div class="viewer-tool-status">Tool: None</div>
            `;

            const measureBtn = toolbar.querySelector('[data-tool="measure"]');
            if (measureBtn) {
                measureBtn.addEventListener('click', () => {
                    setActiveTool(TOOL_IDS.MEASURE);
                });
            }

            const clearBtn = toolbar.querySelector('[data-tool-action="clear-measurements"]');
            if (clearBtn) {
                clearBtn.addEventListener('click', () => {
                    measurementState.pendingStartPoint = null;
                    measurementState.measurements = [];
                    for (let i = 0; i < 4; i++) {
                        measurementOverlayState[i].hover = null;
                    }
                    updateToolbarState();
                    renderMeasurementOverlays();
                });
            }

            gridEl.parentNode.insertBefore(toolbar, gridEl);
        });
    }

    function orientationToSliceType(viewer, orientation) {
        if (!viewer || !viewer.nv) {
            return null;
        }
        if (orientation === 'axial') {
            return viewer.nv.sliceTypeAxial;
        }
        if (orientation === 'sagittal') {
            return viewer.nv.sliceTypeSagittal;
        }
        return viewer.nv.sliceTypeCoronal;
    }

    function getCanvasPixelPosition(event, canvas, viewer) {
        if (!canvas || !viewer || !viewer.nv) {
            return null;
        }
        const rect = canvas.getBoundingClientRect();
        const dpr = (viewer.nv.uiData && viewer.nv.uiData.dpr) ? viewer.nv.uiData.dpr : (window.devicePixelRatio || 1);
        return [
            (event.clientX - rect.left) * dpr,
            (event.clientY - rect.top) * dpr
        ];
    }

    function getWorldMMAtEvent(event, canvas, viewer) {
        if (!viewer || !viewer.nv) {
            return null;
        }

        const pixelPosition = getCanvasPixelPosition(event, canvas, viewer);
        if (!pixelPosition) {
            return null;
        }

        const frac = viewer.nv.canvasPos2frac(pixelPosition);
        if (!frac || frac[0] < 0 || frac[1] < 0 || frac[2] < 0) {
            return null;
        }

        const mm = viewer.nv.frac2mm(frac);
        if (!mm || Number.isNaN(mm[0]) || Number.isNaN(mm[1]) || Number.isNaN(mm[2])) {
            return null;
        }

        return [Number(mm[0]), Number(mm[1]), Number(mm[2])];
    }

    function projectMMToCanvas(windowIndex, viewer, pointMM) {
        if (!viewer || !viewer.nv || !pointMM) {
            return null;
        }

        const preferredSliceType = orientationToSliceType(viewer, windowStates[windowIndex].currentOrientation);
        const frac = viewer.nv.mm2frac(pointMM);
        const canvasPosition = viewer.nv.frac2canvasPosWithTile(frac, preferredSliceType);

        if (!canvasPosition || !canvasPosition.pos) {
            return null;
        }

        return [canvasPosition.pos[0], canvasPosition.pos[1]];
    }

    function isPrimaryUnmodifiedClick(event) {
        return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey;
    }

    function registerMeasurementTool() {
        registerTool(TOOL_IDS.MEASURE, {
            onPrimaryClick: ({ windowIndex, event, canvas, viewer }) => {
                if (!viewer || !viewer.nv) {
                    return false;
                }

                const pixelPosition = getCanvasPixelPosition(event, canvas, viewer);
                if (!pixelPosition) {
                    return false;
                }

                const frac = viewer.nv.canvasPos2frac(pixelPosition);
                if (!frac || frac[0] < 0 || frac[1] < 0 || frac[2] < 0) {
                    return false;
                }

                const mm = viewer.nv.frac2mm(frac);
                const currentPoint = {
                    windowIndex: windowIndex,
                    pointMM: [Number(mm[0]), Number(mm[1]), Number(mm[2])]
                };

                if (!measurementState.pendingStartPoint) {
                    measurementState.pendingStartPoint = currentPoint;
                    updateToolbarState();
                    renderMeasurementOverlays();
                    return true;
                }

                const start = measurementState.pendingStartPoint.pointMM;
                const end = currentPoint.pointMM;
                const dx = end[0] - start[0];
                const dy = end[1] - start[1];
                const dz = end[2] - start[2];
                const distance = Math.sqrt(dx * dx + dy * dy + dz * dz);

                measurementState.measurements.push({
                    id: measurementState.nextId++,
                    startMM: start,
                    endMM: end,
                    distanceMM: distance,
                    createdAt: Date.now()
                });

                measurementState.pendingStartPoint = null;
                updateToolbarState();
                renderMeasurementOverlays();
                return true;
            },
            onDeactivate: () => {
                measurementState.pendingStartPoint = null;
                updateToolbarState();
            }
        });
    }

    function drawDottedLine(ctx, x1, y1, x2, y2, dpr) {
        ctx.save();
        ctx.strokeStyle = 'rgba(255, 230, 120, 0.95)';
        ctx.lineWidth = Math.max(1.5, 2 * dpr);
        ctx.setLineDash([6 * dpr, 6 * dpr]);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.restore();
    }

    function drawMeasurementPoint(ctx, x, y, dpr, highlighted) {
        ctx.save();
        ctx.fillStyle = highlighted ? 'rgba(255, 249, 160, 1)' : 'rgba(255, 230, 120, 0.95)';
        ctx.strokeStyle = 'rgba(20, 20, 20, 0.95)';
        ctx.lineWidth = Math.max(1, dpr);
        ctx.beginPath();
        ctx.arc(x, y, Math.max(3, 3.5 * dpr), 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.restore();
    }

    function drawTooltip(ctx, text, x, y, dpr) {
        ctx.save();
        ctx.font = `${Math.max(11, 12 * dpr)}px monospace`;
        const paddingX = 6 * dpr;
        const paddingY = 4 * dpr;
        const textWidth = ctx.measureText(text).width;
        const boxWidth = textWidth + paddingX * 2;
        const boxHeight = 18 * dpr;
        const boxX = x + 10 * dpr;
        const boxY = y - 10 * dpr - boxHeight;

        ctx.fillStyle = 'rgba(0, 0, 0, 0.82)';
        ctx.fillRect(boxX, boxY, boxWidth, boxHeight);
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.22)';
        ctx.lineWidth = Math.max(1, dpr * 0.8);
        ctx.strokeRect(boxX, boxY, boxWidth, boxHeight);

        ctx.fillStyle = 'rgba(255, 255, 255, 0.96)';
        ctx.fillText(text, boxX + paddingX, boxY + boxHeight - paddingY - 2 * dpr);
        ctx.restore();
    }

    function formatDistance(distanceMM) {
        if (distanceMM >= 100) {
            return `${distanceMM.toFixed(0)} mm`;
        }
        if (distanceMM >= 10) {
            return `${distanceMM.toFixed(1)} mm`;
        }
        return `${distanceMM.toFixed(2)} mm`;
    }

    function distancePointToSegment(px, py, x1, y1, x2, y2) {
        const dx = x2 - x1;
        const dy = y2 - y1;
        if (dx === 0 && dy === 0) {
            const ddx = px - x1;
            const ddy = py - y1;
            return Math.sqrt(ddx * ddx + ddy * ddy);
        }
        const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)));
        const projX = x1 + t * dx;
        const projY = y1 + t * dy;
        const sx = px - projX;
        const sy = py - projY;
        return Math.sqrt(sx * sx + sy * sy);
    }

    function projectMeasurementToWindow(windowIndex, viewer, measurement) {
        if (!viewer || !viewer.nv) {
            return null;
        }
        const preferredSliceType = orientationToSliceType(viewer, windowStates[windowIndex].currentOrientation);
        const startFrac = viewer.nv.mm2frac(measurement.startMM);
        const endFrac = viewer.nv.mm2frac(measurement.endMM);
        const startCanvas = viewer.nv.frac2canvasPosWithTile(startFrac, preferredSliceType);
        const endCanvas = viewer.nv.frac2canvasPosWithTile(endFrac, preferredSliceType);

        if (!startCanvas || !endCanvas || !startCanvas.pos || !endCanvas.pos) {
            return null;
        }

        return {
            id: measurement.id,
            distanceMM: measurement.distanceMM,
            startPx: [startCanvas.pos[0], startCanvas.pos[1]],
            endPx: [endCanvas.pos[0], endCanvas.pos[1]]
        };
    }

    function renderMeasurementOverlayForWindow(windowIndex) {
        const state = windowStates[windowIndex];
        if (!state || !state.niivueInstance || !state.niivueInstance.isReady()) {
            return;
        }

        const windowEl = document.querySelector(`.viewer-window[data-window-index="${windowIndex}"]`);
        if (!windowEl) {
            return;
        }

        const baseCanvas = windowEl.querySelector('.niivue-canvas');
        const overlayCanvas = windowEl.querySelector('.measurement-overlay');
        if (!baseCanvas || !overlayCanvas) {
            return;
        }

        if (overlayCanvas.width !== baseCanvas.width || overlayCanvas.height !== baseCanvas.height) {
            overlayCanvas.width = baseCanvas.width;
            overlayCanvas.height = baseCanvas.height;
        }

        const ctx = overlayCanvas.getContext('2d');
        if (!ctx) {
            return;
        }

        ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

        const dpr = (state.niivueInstance.nv.uiData && state.niivueInstance.nv.uiData.dpr)
            ? state.niivueInstance.nv.uiData.dpr
            : (window.devicePixelRatio || 1);

        const projected = [];
        measurementState.measurements.forEach((measurement) => {
            const projectedMeasurement = projectMeasurementToWindow(windowIndex, state.niivueInstance, measurement);
            if (!projectedMeasurement) {
                return;
            }

            const hover = measurementOverlayState[windowIndex].hover;
            const isHoveredMeasurement = hover && hover.measurementId === projectedMeasurement.id;
            const highlightStart = isHoveredMeasurement && hover.part === 'start';
            const highlightEnd = isHoveredMeasurement && hover.part === 'end';

            drawDottedLine(
                ctx,
                projectedMeasurement.startPx[0],
                projectedMeasurement.startPx[1],
                projectedMeasurement.endPx[0],
                projectedMeasurement.endPx[1],
                dpr
            );
            drawMeasurementPoint(ctx, projectedMeasurement.startPx[0], projectedMeasurement.startPx[1], dpr, highlightStart);
            drawMeasurementPoint(ctx, projectedMeasurement.endPx[0], projectedMeasurement.endPx[1], dpr, highlightEnd);
            projected.push(projectedMeasurement);
        });

        measurementOverlayState[windowIndex].projected = projected;

        if (measurementState.pendingStartPoint) {
            const pendingMeasurement = {
                id: -1,
                startMM: measurementState.pendingStartPoint.pointMM,
                endMM: measurementState.pendingStartPoint.pointMM,
                distanceMM: 0
            };
            const projectedPending = projectMeasurementToWindow(windowIndex, state.niivueInstance, pendingMeasurement);
            if (projectedPending) {
                drawMeasurementPoint(ctx, projectedPending.startPx[0], projectedPending.startPx[1], dpr, true);
            }
        }

        const hover = measurementOverlayState[windowIndex].hover;
        if (hover) {
            drawTooltip(ctx, hover.text, hover.x, hover.y, dpr);
        }
    }

    function renderMeasurementOverlays() {
        for (let i = 0; i < 4; i++) {
            renderMeasurementOverlayForWindow(i);
        }
    }

    function updateMeasurementHover(windowIndex, event, canvas, viewer) {
        if (!viewer || !viewer.nv) {
            return;
        }

        const position = getCanvasPixelPosition(event, canvas, viewer);
        if (!position) {
            return;
        }

        const [px, py] = position;
        const projected = measurementOverlayState[windowIndex].projected || [];
        const dpr = (viewer.nv.uiData && viewer.nv.uiData.dpr) ? viewer.nv.uiData.dpr : (window.devicePixelRatio || 1);
        const endpointThreshold = Math.max(6, 7 * dpr);
        const lineThreshold = Math.max(5, 6 * dpr);

        let nextHover = null;
        for (let i = 0; i < projected.length; i++) {
            const measurement = projected[i];
            const ds = Math.hypot(px - measurement.startPx[0], py - measurement.startPx[1]);
            if (ds <= endpointThreshold) {
                nextHover = {
                    measurementId: measurement.id,
                    part: 'start',
                    x: px,
                    y: py,
                    text: formatDistance(measurement.distanceMM)
                };
                break;
            }

            const de = Math.hypot(px - measurement.endPx[0], py - measurement.endPx[1]);
            if (de <= endpointThreshold) {
                nextHover = {
                    measurementId: measurement.id,
                    part: 'end',
                    x: px,
                    y: py,
                    text: formatDistance(measurement.distanceMM)
                };
                break;
            }

            const dl = distancePointToSegment(
                px,
                py,
                measurement.startPx[0],
                measurement.startPx[1],
                measurement.endPx[0],
                measurement.endPx[1]
            );
            if (dl <= lineThreshold) {
                nextHover = {
                    measurementId: measurement.id,
                    part: 'line',
                    x: px,
                    y: py,
                    text: formatDistance(measurement.distanceMM)
                };
                break;
            }
        }

        const currentHover = measurementOverlayState[windowIndex].hover;
        let changed = false;
        if (!currentHover && !nextHover) {
            changed = false;
        } else if (!currentHover || !nextHover) {
            changed = true;
        } else {
            changed =
                currentHover.measurementId !== nextHover.measurementId ||
                currentHover.part !== nextHover.part ||
                Math.abs(currentHover.x - nextHover.x) > 1 ||
                Math.abs(currentHover.y - nextHover.y) > 1;
        }

        measurementOverlayState[windowIndex].hover = nextHover;
        if (changed) {
            renderMeasurementOverlayForWindow(windowIndex);
        }
    }

    /**
     * Initialize synchronization event system.
     * Uses requestAnimationFrame to coalesce multiple crosshair updates
     * per frame into a single sync pass, and drawScene() instead of
     * updateGLVolume() since only the crosshair position changed.
     */
    function initSynchronization() {
        window.addEventListener('sliceIndexChanged', (event) => {
            if (_syncSuspended) return;

            // Ignore events triggered by sync itself to prevent cascade
            if (_isSyncing) return;

            const { windowIndex, crosshairPos } = event.detail;

            // Skip if source window has free-scroll enabled
            if (freeScrollWindows[windowIndex]) return;
            if (!crosshairPos) return;

            // Store pending sync data (last writer wins within a frame)
            _syncSourceWindow = windowIndex;
            _syncCrosshairPos[0] = clampCrosshairCoord(crosshairPos[0]);
            _syncCrosshairPos[1] = clampCrosshairCoord(crosshairPos[1]);
            _syncCrosshairPos[2] = clampCrosshairCoord(crosshairPos[2]);

            // Coalesce into single rAF — multiple scroll ticks within one
            // frame result in only one sync pass.
            if (!_syncRAF) {
                _syncRAF = requestAnimationFrame(applyCrosshairSync);
            }
        });
    }

    function emitSliceChanged(windowIndex, viewer, windowEl) {
        if (!viewer) {
            return;
        }

        const currentSliceIndex = viewer.getSliceIndex();
        const currentOrientation = windowStates[windowIndex].currentOrientation;
        const total = viewer.getSliceCount();

        const counterEl = windowEl ? windowEl.querySelector('.slice-counter') : null;
        if (counterEl) {
            counterEl.textContent = `${currentSliceIndex + 1} / ${total}`;
        }

        window.dispatchEvent(new CustomEvent('sliceIndexChanged', {
            detail: {
                windowIndex: windowIndex,
                sliceIndex: currentSliceIndex,
                orientation: currentOrientation,
                crosshairPos: viewer.nv ? [...viewer.nv.scene.crosshairPos] : null
            }
        }));

        renderMeasurementOverlayForWindow(windowIndex);
    }

    /**
     * Apply batched crosshair synchronization to all target windows.
     * Uses drawScene() instead of updateGLVolume() — the crosshair
     * position is sampled by the shader from the existing 3D texture,
     * so no volume texture recalculation is needed.
     */
    function applyCrosshairSync() {
        _syncRAF = null;
        if (_syncSuspended) {
            return;
        }
        _isSyncing = true;

        const sourceIdx = _syncSourceWindow;

        for (let targetIdx = 0; targetIdx < 4; targetIdx++) {
            if (targetIdx === sourceIdx) continue;
            if (freeScrollWindows[targetIdx]) continue;

            const targetViewer = windowStates[targetIdx].niivueInstance;
            if (targetViewer && targetViewer.isReady() && targetViewer.nv) {
                // Write directly into NiiVue's crosshairPos array — avoids
                // allocating a new array on every sync.
                const pos = targetViewer.nv.scene.crosshairPos;
                pos[0] = clampCrosshairCoord(_syncCrosshairPos[0]);
                pos[1] = clampCrosshairCoord(_syncCrosshairPos[1]);
                pos[2] = clampCrosshairCoord(_syncCrosshairPos[2]);
                targetViewer.nv.drawScene();

                // Update target's slice counter
                const targetEl = document.querySelector(`.viewer-window[data-window-index="${targetIdx}"]`);
                const targetCounter = targetEl ? targetEl.querySelector('.slice-counter') : null;
                if (targetCounter) {
                    const total = targetViewer.getSliceCount();
                    const idx = targetViewer.getSliceIndex();
                    targetCounter.textContent = `${idx + 1} / ${total}`;
                }

                renderMeasurementOverlayForWindow(targetIdx);
            }
        }

        _isSyncing = false;
    }

    /**
     * Update synchronization group membership for a window
     * Removes from old group and adds to new group
     * @param {number} windowIndex - 0-3 for grid position
     * @param {string} newOrientation - 'axial', 'sagittal', or 'coronal'
     */
    function updateOrientationGroup(windowIndex, newOrientation) {
        // Remove from all groups
        for (const orientation in synchronizationGroups) {
            const index = synchronizationGroups[orientation].indexOf(windowIndex);
            if (index > -1) {
                synchronizationGroups[orientation].splice(index, 1);
            }
        }

        // Add to new group
        if (synchronizationGroups[newOrientation]) {
            synchronizationGroups[newOrientation].push(windowIndex);
            console.log(`Window ${windowIndex} joined ${newOrientation} group:`, synchronizationGroups[newOrientation]);
        }
    }

    /**
     * Get consensus slice index for an orientation group
     * Returns the slice index from the first ready viewer in the group
     * @param {string} orientation - 'axial', 'sagittal', or 'coronal'
     * @returns {number} Slice index, or 0 if no viewers ready
     */
    function getGroupConsensusSlice(orientation) {
        const group = synchronizationGroups[orientation];
        if (!group || group.length === 0) {
            return 0;
        }

        // Find first ready viewer in group
        for (const windowIndex of group) {
            const viewer = windowStates[windowIndex].niivueInstance;
            if (viewer && viewer.isReady() && !freeScrollWindows[windowIndex]) {
                return viewer.getSliceIndex();
            }
        }

        return 0;
    }

    /**
     * Load Django data from script tag
     */
    function loadDjangoData() {
        const dataEl = document.getElementById('viewerGridData');
        if (dataEl) {
            try {
                const data = JSON.parse(dataEl.textContent);
                djangoData = {
                    scanId: data.scanId,
                    projectNamespace: data.projectNamespace,
                    modalityFiles: data.modalityFiles || {},
                    fixedMode: !!data.fixedMode,
                    enableDragDrop: data.enableDragDrop !== false,
                    enableContextMenu: data.enableContextMenu !== false,
                    allowClearWindow: data.allowClearWindow !== false
                };
            } catch (e) {
                console.error('Error parsing Django data:', e);
            }
        }
    }

    /**
     * Populate file IDs on modality chips from Django data
     */
    function populateChipFileIds() {
        const chips = document.querySelectorAll('.modality-chip');
        chips.forEach(chip => {
            const modality = chip.dataset.modality;
            const fileInfo = djangoData.modalityFiles[modality];
            if (fileInfo && fileInfo.id) {
                chip.dataset.fileId = fileInfo.id;
            }
        });
    }

    function buildFileServeUrl(fileId, fileKey) {
        const namespace = djangoData.projectNamespace || 'maxillo';
        let url = `/${namespace}/api/processing/files/serve/${fileId}/`;
        if (fileKey) {
            url += `?file_key=${encodeURIComponent(fileKey)}`;
        }
        return url;
    }

    /**
     * Initialize drag-drop handlers
     */
    function initDragDrop() {
        const chips = document.querySelectorAll('.modality-chip');
        const windows = document.querySelectorAll('.viewer-window');

        // Make modality chips draggable
        chips.forEach(chip => {
            chip.addEventListener('dragstart', handleDragStart);
            chip.addEventListener('dragend', handleDragEnd);
        });

        // Make viewer windows drop zones
        windows.forEach(window => {
            window.addEventListener('dragover', handleDragOver, true);
            window.addEventListener('dragleave', handleDragLeave, true);
            window.addEventListener('drop', handleDrop, true);
        });
    }

    function resolveWindowDropTarget(target) {
        if (!target || typeof target.closest !== 'function') {
            return null;
        }
        return target.closest('.viewer-window');
    }

    /**
     * Handle drag start from modality chip
     */
    function handleDragStart(e) {
        const modality = e.currentTarget.dataset.modality;
        const fileId = e.currentTarget.dataset.fileId;

        // Store modality and file ID in dataTransfer
        e.dataTransfer.setData('text/plain', modality);
        e.dataTransfer.setData('application/json', JSON.stringify({
            modality: modality,
            fileId: fileId
        }));

        e.dataTransfer.effectAllowed = 'copy';

        // Visual feedback
        e.currentTarget.style.opacity = '0.5';
    }

    /**
     * Handle drag end
     */
    function handleDragEnd(e) {
        e.currentTarget.style.opacity = '1';
    }

    /**
     * Handle drag over window (for drop zone highlighting)
     */
    function handleDragOver(e) {
        const windowEl = resolveWindowDropTarget(e.target) || e.currentTarget;
        if (!windowEl || !windowEl.classList || !windowEl.classList.contains('viewer-window')) {
            return;
        }

        e.preventDefault(); // Required to allow drop
        e.dataTransfer.dropEffect = 'copy';

        // Highlight drop zone
        windowEl.classList.add('drag-over');
    }

    /**
     * Handle drag leave window
     */
    function handleDragLeave(e) {
        const windowEl = resolveWindowDropTarget(e.target) || e.currentTarget;
        if (!windowEl || !windowEl.classList || !windowEl.classList.contains('viewer-window')) {
            return;
        }

        // Keep highlight while moving between child elements within the same window.
        const nextTarget = e.relatedTarget;
        if (nextTarget && windowEl.contains(nextTarget)) {
            return;
        }

        windowEl.classList.remove('drag-over');
    }

    /**
     * Handle drop into window
     */
    function handleDrop(e) {
        const windowEl = resolveWindowDropTarget(e.target) || e.currentTarget;
        if (!windowEl || !windowEl.classList || !windowEl.classList.contains('viewer-window')) {
            return;
        }

        e.preventDefault();
        windowEl.classList.remove('drag-over');

        // Parse dropped data
        let modalityData;
        try {
            modalityData = JSON.parse(e.dataTransfer.getData('application/json'));
        } catch (err) {
            // Fallback to plain text
            const modality = e.dataTransfer.getData('text/plain');
            modalityData = {
                modality: modality,
                fileId: null
            };
        }

        // Get window index
        const windowIndex = parseInt(windowEl.dataset.windowIndex, 10);
        if (Number.isNaN(windowIndex)) {
            return;
        }

        // Load modality in this window
        loadModalityInWindow(windowIndex, modalityData.modality, modalityData.fileId);
    }

    /**
     * Load a modality into a specific window
     * Each window gets its own NiiVueViewer instance
     * @param {number} windowIndex - 0-3 for grid position
     * @param {string} modality - Modality slug (e.g. 'braintumor-mri-t1')
     * @param {string|null} fileId - FileRegistry ID for this modality
     */
    async function loadModalityInWindow(windowIndex, modality, fileId) {
        console.log(`Loading ${modality} (fileId: ${fileId}) in window ${windowIndex}`);

        const windowEl = document.querySelector(`.viewer-window[data-window-index="${windowIndex}"]`);
        if (!windowEl) {
            console.error(`Window element not found for index ${windowIndex}`);
            return;
        }

        const loadGeneration = beginWindowLoad(windowIndex);

        // Dispose existing viewer if present
        const existingState = windowStates[windowIndex];
        if (existingState.niivueInstance) {
            console.log(`Disposing previous NiiVue viewer in window ${windowIndex}`);
            try {
                existingState.niivueInstance.dispose();
            } catch (e) {
                console.warn('Error disposing previous viewer:', e);
            }
        }

        // Update state to loading
        windowStates[windowIndex] = {
            modality: modality,
            loading: true,
            error: null,
            fileId: fileId,
            niivueInstance: null,
            currentOrientation: 'axial'
        };

        // Create viewer container structure with canvas and orientation menu
        const canvasId = `niivue-canvas-${windowIndex}`;
        const viewerHTML = `
            <div class="niivue-viewer-container" style="width: 100%; height: 100%; position: relative;">
                <div class="niivue-loading" style="position: absolute; top: 0; left: 0; right: 0; bottom: 0; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,0.8); z-index: 10;">
                    <div class="spinner-border text-primary" role="status">
                        <span class="visually-hidden">Loading...</span>
                    </div>
                </div>
                <canvas id="${canvasId}" class="niivue-canvas"></canvas>
                <canvas class="measurement-overlay"></canvas>
                <div class="orientation-menu">
                    <button class="orientation-btn active" data-orientation="axial">A</button>
                    <button class="orientation-btn" data-orientation="sagittal">S</button>
                    <button class="orientation-btn" data-orientation="coronal">C</button>
                    <button class="free-scroll-btn" title="Toggle free scroll">
                        <i class="fas fa-link"></i>
                    </button>
                    <button class="reset-view-btn" title="Reset zoom and pan">
                        <i class="fas fa-compress-arrows-alt"></i>
                    </button>
                    <button class="crosshair-toggle-btn" title="Toggle crosshair">
                        <i class="fas fa-crosshairs"></i>
                    </button>
                </div>
            </div>
        `;

        // Clear window content (except drop hint)
        const dropHint = windowEl.querySelector('.drop-hint');
        windowEl.innerHTML = '';
        if (dropHint) {
            windowEl.appendChild(dropHint);
            dropHint.style.display = 'none';
        }

        // Add viewer container
        const viewerContainer = document.createElement('div');
        viewerContainer.innerHTML = viewerHTML;
        windowEl.appendChild(viewerContainer.firstElementChild);

        // Add window label
        const label = document.createElement('div');
        label.className = 'window-label';
        label.textContent = modality.toUpperCase();
        windowEl.appendChild(label);

        // Check if NiiVueViewer class is available
        if (!window.NiiVueViewer) {
            console.error('NiiVueViewer not loaded');
            windowStates[windowIndex].loading = false;
            windowStates[windowIndex].error = 'NiiVueViewer not loaded';
            updateWindowUI(windowIndex);
            return;
        }

        // Fetch file ArrayBuffer from API (with caching)
        try {
            let fileArrayBuffer;
            if (volumeCache[fileId]) {
                console.log(`Using cached ArrayBuffer for fileId ${fileId}`);
                fileArrayBuffer = volumeCache[fileId];
            } else if (volumeFetchPromises[fileId]) {
                console.log(`Awaiting in-flight ArrayBuffer fetch for fileId ${fileId}`);
                fileArrayBuffer = await volumeFetchPromises[fileId];
            } else {
                console.log(`Fetching ArrayBuffer for fileId ${fileId}`);
                volumeFetchPromises[fileId] = (async () => {
                    const response = await fetch(buildFileServeUrl(fileId, 'primary'));
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                    }
                    return response.arrayBuffer();
                })();

                try {
                    fileArrayBuffer = await volumeFetchPromises[fileId];
                    volumeCache[fileId] = fileArrayBuffer;
                } finally {
                    delete volumeFetchPromises[fileId];
                }
                console.log(`File ArrayBuffer received and cached: ${fileArrayBuffer.byteLength} bytes`);
            }

            if (!isWindowLoadCurrent(windowIndex, loadGeneration)) {
                return;
            }

            // Create new NiiVueViewer instance for this window
            const viewer = new window.NiiVueViewer(canvasId);

            // Initialize viewer with modality and ArrayBuffer
            await viewer.init(modality, fileArrayBuffer);

            if (!isWindowLoadCurrent(windowIndex, loadGeneration)) {
                try {
                    viewer.dispose();
                } catch (e) {
                    console.warn('Error disposing stale viewer:', e);
                }
                return;
            }

            // Store instance in state
            windowStates[windowIndex].niivueInstance = viewer;
            windowStates[windowIndex].loading = false;
            windowStates[windowIndex].error = null;

            // Hide loading spinner
            const loadingDiv = windowEl.querySelector('.niivue-loading');
            if (loadingDiv) {
                loadingDiv.style.display = 'none';
            }

            // Attach slice change callback for synchronization and slice counter
            viewer.onSliceChange(() => {
                emitSliceChanged(windowIndex, viewer, windowEl);
            });

            // Add to synchronization group and adopt crosshair from any existing window
            updateOrientationGroup(windowIndex, windowStates[windowIndex].currentOrientation);
            if (!freeScrollWindows[windowIndex]) {
                // Find any other ready viewer and copy its full 3D crosshair position
                for (let i = 0; i < 4; i++) {
                    if (i === windowIndex) continue;
                    const other = windowStates[i].niivueInstance;
                    if (other && other.isReady() && other.nv && !freeScrollWindows[i]) {
                        const sourcePos = other.nv.scene.crosshairPos;
                        const targetPos = viewer.nv.scene.crosshairPos;
                        targetPos[0] = clampCrosshairCoord(sourcePos[0]);
                        targetPos[1] = clampCrosshairCoord(sourcePos[1]);
                        targetPos[2] = clampCrosshairCoord(sourcePos[2]);
                        viewer.nv.drawScene();
                        break;
                    }
                }
            }

            // Add slice counter element
            const sliceCounter = document.createElement('div');
            sliceCounter.className = 'slice-counter';
            const currentSlice = viewer.getSliceIndex();
            const totalSlices = viewer.getSliceCount();
            sliceCounter.textContent = `${currentSlice + 1} / ${totalSlices}`;
            windowEl.querySelector('.niivue-viewer-container').appendChild(sliceCounter);

            renderMeasurementOverlayForWindow(windowIndex);

            // Attach orientation menu event handlers
            const menuBtns = windowEl.querySelectorAll('.orientation-btn');
            menuBtns.forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation(); // Prevent click from reaching NiiVue canvas
                    const orientation = btn.dataset.orientation;
                    viewer.setOrientation(orientation);
                    menuBtns.forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    windowStates[windowIndex].currentOrientation = orientation;

                    // Update synchronization group
                    updateOrientationGroup(windowIndex, orientation);

                    // Sync to group consensus slice (unless free-scrolling)
                    if (!freeScrollWindows[windowIndex]) {
                        const consensusSlice = getGroupConsensusSlice(orientation);
                        viewer.setSliceIndex(consensusSlice);
                    }

                    // Update slice counter for new orientation
                    const counter = windowEl.querySelector('.slice-counter');
                    if (counter) {
                        const idx = viewer.getSliceIndex();
                        const total = viewer.getSliceCount();
                        counter.textContent = `${idx + 1} / ${total}`;
                    }

                    renderMeasurementOverlayForWindow(windowIndex);
                });
            });

            // Attach Free Scroll button handler
            const freeScrollBtn = windowEl.querySelector('.free-scroll-btn');
            if (freeScrollBtn) {
                freeScrollBtn.addEventListener('click', (e) => {
                    e.stopPropagation(); // Prevent click from reaching NiiVue canvas

                    // Toggle free-scroll state
                    freeScrollWindows[windowIndex] = !freeScrollWindows[windowIndex];

                    // Update button appearance
                    const icon = freeScrollBtn.querySelector('i');
                    if (freeScrollWindows[windowIndex]) {
                        // Free-scroll enabled (unlinked)
                        freeScrollBtn.classList.add('free-scroll-active');
                        icon.classList.remove('fa-link');
                        icon.classList.add('fa-link-slash');
                        freeScrollBtn.title = 'Re-sync scrolling';
                    } else {
                        // Free-scroll disabled (re-sync)
                        freeScrollBtn.classList.remove('free-scroll-active');
                        icon.classList.remove('fa-link-slash');
                        icon.classList.add('fa-link');
                        freeScrollBtn.title = 'Toggle free scroll';

                        // Re-sync to group consensus slice
                        const currentOrientation = windowStates[windowIndex].currentOrientation;
                        const consensusSlice = getGroupConsensusSlice(currentOrientation);
                        viewer.setSliceIndex(consensusSlice);
                    }

                    console.log(`Window ${windowIndex} free-scroll: ${freeScrollWindows[windowIndex]}`);
                    renderMeasurementOverlayForWindow(windowIndex);
                });
            }

            // Attach Reset View button handler
            const resetViewBtn = windowEl.querySelector('.reset-view-btn');
            if (resetViewBtn) {
                resetViewBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (viewer.nv) {
                        viewer.nv.scene.pan2Dxyzmm = [0, 0, 0, 1];
                        viewer.nv.drawScene();
                        renderMeasurementOverlayForWindow(windowIndex);
                    }
                });
            }

            // Attach Crosshair Toggle button handler
            const crosshairBtn = windowEl.querySelector('.crosshair-toggle-btn');
            if (crosshairBtn) {
                crosshairBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (viewer.nv) {
                        const isVisible = viewer.nv.opts.crosshairWidth > 0;
                        viewer.nv.opts.crosshairWidth = isVisible ? 0 : VISIBLE_CROSSHAIR_WIDTH;
                        viewer.nv.drawScene();
                        crosshairBtn.classList.toggle('crosshair-hidden', isVisible);
                        renderMeasurementOverlayForWindow(windowIndex);
                    }
                });
            }

            // Custom scroll/zoom/pan handlers on canvas.
            // Use capture phase so we intercept before NiiVue's own handlers.
            const canvas = document.getElementById(canvasId);
            if (canvas) {
                // Track Alt+right-click for intensity adjustment (window/level)
                let isRightClickIntensity = false;

                // Intercept right-click mousedown BEFORE NiiVue processes it.
                // Without Alt: block NiiVue's drag (intensity square).
                // With Alt: let NiiVue handle it for window/level adjustment.
                canvas.addEventListener('mousedown', (e) => {
                    if (activeTool === TOOL_IDS.MEASURE && isPrimaryUnmodifiedClick(e)) {
                        const tool = toolRegistry[activeTool];
                        const handled = tool && typeof tool.onPrimaryClick === 'function'
                            ? tool.onPrimaryClick({ windowIndex, event: e, canvas, viewer })
                            : false;
                        if (handled) {
                            e.preventDefault();
                            e.stopImmediatePropagation();
                            return;
                        }
                    }

                    if (e.button === 2) {
                        if (e.altKey) {
                            isRightClickIntensity = true;
                        } else {
                            e.stopImmediatePropagation();
                        }
                    }
                }, { capture: true });

                canvas.addEventListener('mouseup', (e) => {
                    if (e.button === 2 && isRightClickIntensity) {
                        isRightClickIntensity = false;
                    }
                });

                // Handle contextmenu: always prevent browser menu,
                // show custom menu only on regular right-click (not Alt+right-click)
                canvas.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    if (isRightClickIntensity) {
                        isRightClickIntensity = false;
                    } else {
                        e.stopImmediatePropagation();
                        showViewerContextMenu(e.clientX, e.clientY, windowIndex, viewer);
                    }
                }, { capture: true });

                // Shift+scroll: fast navigation (5 slices per step)
                // Ctrl+scroll: zoom in/out anchored at mouse position
                canvas.addEventListener('wheel', (e) => {
                    if (e.ctrlKey) {
                        e.preventDefault();
                        e.stopImmediatePropagation();

                        const nv = viewer.nv;
                        if (!nv) {
                            return;
                        }

                        const pan = nv.scene.pan2Dxyzmm;
                        const currentZoom = pan[3] || 1;
                        const primaryDelta = e.deltaY !== 0 ? e.deltaY : e.deltaX;
                        if (primaryDelta === 0) {
                            return;
                        }
                        const zoomFactor = primaryDelta > 0 ? 0.9 : 1.1;
                        const newZoom = Math.max(1, Math.min(5, currentZoom * zoomFactor));

                        const beforeMM = getWorldMMAtEvent(e, canvas, viewer);
                        nv.scene.pan2Dxyzmm = [pan[0], pan[1], pan[2], newZoom];
                        nv.drawScene();

                        if (beforeMM) {
                            const mousePx = getCanvasPixelPosition(e, canvas, viewer);
                            const anchorPx = projectMMToCanvas(windowIndex, viewer, beforeMM);
                            if (mousePx && anchorPx && typeof nv.dragForPanZoom === 'function' && nv.uiData) {
                                const basePan = [
                                    nv.scene.pan2Dxyzmm[0],
                                    nv.scene.pan2Dxyzmm[1],
                                    nv.scene.pan2Dxyzmm[2],
                                    nv.scene.pan2Dxyzmm[3]
                                ];

                                const evaluateCandidate = (startPx, endPx) => {
                                    nv.scene.pan2Dxyzmm = [basePan[0], basePan[1], basePan[2], basePan[3]];
                                    nv.uiData.pan2DxyzmmAtMouseDown = [basePan[0], basePan[1], basePan[2], basePan[3]];
                                    nv.dragForPanZoom([startPx[0], startPx[1], endPx[0], endPx[1]]);
                                    nv.drawScene();

                                    const projected = projectMMToCanvas(windowIndex, viewer, beforeMM);
                                    if (!projected) {
                                        return {
                                            score: Number.POSITIVE_INFINITY,
                                            pan: [
                                                nv.scene.pan2Dxyzmm[0],
                                                nv.scene.pan2Dxyzmm[1],
                                                nv.scene.pan2Dxyzmm[2],
                                                nv.scene.pan2Dxyzmm[3]
                                            ]
                                        };
                                    }

                                    return {
                                        score: Math.hypot(projected[0] - mousePx[0], projected[1] - mousePx[1]),
                                        pan: [
                                            nv.scene.pan2Dxyzmm[0],
                                            nv.scene.pan2Dxyzmm[1],
                                            nv.scene.pan2Dxyzmm[2],
                                            nv.scene.pan2Dxyzmm[3]
                                        ]
                                    };
                                };

                                const candidateAnchorToMouse = evaluateCandidate(anchorPx, mousePx);
                                const candidateMouseToAnchor = evaluateCandidate(mousePx, anchorPx);
                                const bestCandidate = candidateAnchorToMouse.score <= candidateMouseToAnchor.score
                                    ? candidateAnchorToMouse
                                    : candidateMouseToAnchor;

                                nv.scene.pan2Dxyzmm = [
                                    bestCandidate.pan[0],
                                    bestCandidate.pan[1],
                                    bestCandidate.pan[2],
                                    bestCandidate.pan[3]
                                ];
                            }
                        }

                        nv.drawScene();
                        renderMeasurementOverlayForWindow(windowIndex);
                    } else if (e.shiftKey) {
                        e.preventDefault();
                        e.stopImmediatePropagation();
                        const primaryDelta = e.deltaY !== 0 ? e.deltaY : e.deltaX;
                        if (primaryDelta === 0) {
                            return;
                        }
                        const step = primaryDelta > 0 ? 5 : -5;
                        const current = viewer.getSliceIndex();
                        const total = viewer.getSliceCount();
                        const next = Math.max(0, Math.min(total - 1, current + step));
                        viewer.setSliceIndex(next);
                        emitSliceChanged(windowIndex, viewer, windowEl);
                    }
                }, { capture: true, passive: false });

                // Ctrl+drag: pan using NiiVue native drag pan logic
                let isPanning = false;
                let panStartPx = null;
                const panSensitivity = 1.5;

                canvas.addEventListener('mousedown', (e) => {
                    if (e.ctrlKey && e.button === 0) {
                        const startPx = getCanvasPixelPosition(e, canvas, viewer);
                        if (!startPx || !viewer.nv) {
                            return;
                        }

                        isPanning = true;
                        panStartPx = [startPx[0], startPx[1]];
                        viewer.nv.uiData.pan2DxyzmmAtMouseDown = [
                            viewer.nv.scene.pan2Dxyzmm[0],
                            viewer.nv.scene.pan2Dxyzmm[1],
                            viewer.nv.scene.pan2Dxyzmm[2],
                            viewer.nv.scene.pan2Dxyzmm[3]
                        ];
                        e.preventDefault();
                        e.stopImmediatePropagation();
                        canvas.style.cursor = 'grabbing';
                    }
                }, { capture: true });

                canvas.addEventListener('mousemove', (e) => {
                    updateMeasurementHover(windowIndex, e, canvas, viewer);

                    if (!isPanning) return;
                    e.preventDefault();
                    e.stopImmediatePropagation();

                    if (!panStartPx || !viewer.nv || typeof viewer.nv.dragForPanZoom !== 'function') {
                        return;
                    }

                    const currentPx = getCanvasPixelPosition(e, canvas, viewer);
                    if (!currentPx) {
                        return;
                    }

                    const nv = viewer.nv;
                    const scaledEndX = panStartPx[0] + (currentPx[0] - panStartPx[0]) * panSensitivity;
                    const scaledEndY = panStartPx[1] + (currentPx[1] - panStartPx[1]) * panSensitivity;
                    nv.dragForPanZoom([panStartPx[0], panStartPx[1], scaledEndX, scaledEndY]);
                    nv.drawScene();
                    renderMeasurementOverlayForWindow(windowIndex);
                }, { capture: true });

                const stopPan = () => {
                    if (isPanning) {
                        isPanning = false;
                        panStartPx = null;
                        canvas.style.cursor = '';
                    }
                };
                canvas.addEventListener('mouseup', stopPan);
                canvas.addEventListener('mouseleave', stopPan);
                canvas.addEventListener('mouseleave', () => {
                    if (measurementOverlayState[windowIndex].hover) {
                        measurementOverlayState[windowIndex].hover = null;
                        renderMeasurementOverlayForWindow(windowIndex);
                    }
                });

            }

            // Mark window as loaded
            windowEl.classList.add('loaded');

            console.log(`Successfully loaded ${modality} in window ${windowIndex} using NiiVue`);

        } catch (error) {
            if (!isWindowLoadCurrent(windowIndex, loadGeneration)) {
                return;
            }
            console.error(`Error loading ${modality} in window ${windowIndex}:`, error);

            // Determine user-friendly message
            let userMessage = 'Failed to load volume';
            if (error.message.includes('HTTP 404')) {
                userMessage = 'Volume file not found';
            } else if (error.message.includes('HTTP 403')) {
                userMessage = 'Access denied to volume';
            } else if (error.message.includes('network') || error.message.includes('fetch') || error.message.includes('Failed to fetch')) {
                userMessage = 'Network error - check connection';
            }

            windowStates[windowIndex].loading = false;
            windowStates[windowIndex].error = userMessage;

            // Show error UI with retry button
            const loadingDiv = windowEl.querySelector('.niivue-loading');
            if (loadingDiv) {
                loadingDiv.style.display = 'none';
            }

            // Remove existing viewer container and replace with error
            const viewerContainerEl = windowEl.querySelector('.niivue-viewer-container');
            if (viewerContainerEl) {
                viewerContainerEl.innerHTML = `
                    <div class="viewer-error">
                        <i class="fas fa-exclamation-triangle"></i>
                        <p>${userMessage}</p>
                        <button class="btn btn-sm btn-outline-light retry-btn"
                                data-window="${windowIndex}"
                                data-modality="${modality}"
                                data-file-id="${fileId}">
                            <i class="fas fa-redo me-1"></i>Retry
                        </button>
                    </div>
                `;

                // Attach retry handler
                const retryBtn = viewerContainerEl.querySelector('.retry-btn');
                if (retryBtn) {
                    retryBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        const w = parseInt(e.currentTarget.dataset.window, 10);
                        const m = e.currentTarget.dataset.modality;
                        const f = e.currentTarget.dataset.fileId;
                        loadModalityInWindow(w, m, f);
                    });
                }
            }
        }
    }

    /**
     * Update window UI based on state
     * @param {number} windowIndex - 0-3 for grid position
     */
    function updateWindowUI(windowIndex) {
        const state = windowStates[windowIndex];
        const windowEl = document.querySelector(`.viewer-window[data-window-index="${windowIndex}"]`);

        if (!windowEl) return;

        // Empty state
        if (!state.modality) {
            // Clear all content except drop hint
            const dropHint = windowEl.querySelector('.drop-hint');
            windowEl.innerHTML = '';
            if (dropHint) {
                windowEl.appendChild(dropHint);
                dropHint.style.display = 'flex';
            } else {
                // Create drop hint if it doesn't exist
                const newDropHint = document.createElement('div');
                newDropHint.className = 'drop-hint';
                newDropHint.innerHTML = '<i class="fas fa-arrow-down"></i><p>Drop modality here</p>';
                windowEl.appendChild(newDropHint);
            }
            windowEl.classList.remove('loaded');
            return;
        }

        // Hide drop hint if present
        const dropHint = windowEl.querySelector('.drop-hint');
        if (dropHint) {
            dropHint.style.display = 'none';
        }

        // Error state
        if (state.error) {
            // Hide loading spinner if present
            const loadingDiv = windowEl.querySelector('.niivue-loading');
            if (loadingDiv) {
                loadingDiv.style.display = 'none';
            }

            // Find or create error container
            let errorDiv = windowEl.querySelector('.error-message');
            if (!errorDiv) {
                errorDiv = document.createElement('div');
                errorDiv.className = 'error-message';
                errorDiv.style.cssText = 'position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: #dc3545; text-align: center; z-index: 100;';
                windowEl.appendChild(errorDiv);
            }
            errorDiv.innerHTML = `<i class="fas fa-exclamation-triangle"></i><br>${state.error}`;
            return;
        }

        // Loading and loaded states are handled by loadModalityInWindow
        // which creates the viewer container HTML directly
    }

    /**
     * Initialize context menus for clearing windows
     */
    function initContextMenus() {
        // Close context menu on click elsewhere
        document.addEventListener('click', () => {
            const existingMenu = document.getElementById('viewerContextMenu');
            if (existingMenu) {
                existingMenu.remove();
            }
        });

        // Ensure left click outside closes menu even when other handlers stop propagation.
        document.addEventListener('mousedown', (event) => {
            if (event.button !== 0) {
                return;
            }

            const existingMenu = document.getElementById('viewerContextMenu');
            if (!existingMenu) {
                return;
            }

            if (existingMenu.contains(event.target)) {
                return;
            }

            existingMenu.remove();
        }, true);
    }

    /**
     * Show context menu for viewer window at cursor position
     */
    function showViewerContextMenu(x, y, windowIndex, viewer) {
        if (!djangoData.enableContextMenu) {
            return;
        }

        // Remove existing menu
        const existingMenu = document.getElementById('viewerContextMenu');
        if (existingMenu) {
            existingMenu.remove();
        }

        const windowEl = document.querySelector(`.viewer-window[data-window-index="${windowIndex}"]`);
        const currentOrientation = windowStates[windowIndex].currentOrientation;
        const isFreeScroll = freeScrollWindows[windowIndex];

        // Create menu
        const menu = document.createElement('div');
        menu.id = 'viewerContextMenu';
        menu.className = 'viewer-context-menu';
        menu.style.top = `${y}px`;
        menu.style.left = `${x}px`;

        // Orientation section
        const orientSection = document.createElement('div');
        orientSection.className = 'context-menu-section';
        orientSection.innerHTML = '<div class="context-menu-label">Orientation</div>';

        const orientButtons = document.createElement('div');
        orientButtons.className = 'context-menu-orientation-buttons';
        ['axial', 'sagittal', 'coronal'].forEach(orient => {
            const btn = document.createElement('button');
            btn.textContent = orient[0].toUpperCase();
            btn.className = 'context-menu-orient-btn' + (orient === currentOrientation ? ' active' : '');
            btn.onclick = () => {
                viewer.setOrientation(orient);
                const menuBtns = windowEl.querySelectorAll('.orientation-btn');
                menuBtns.forEach(b => b.classList.remove('active'));
                const targetBtn = windowEl.querySelector(`.orientation-btn[data-orientation="${orient}"]`);
                if (targetBtn) targetBtn.classList.add('active');
                windowStates[windowIndex].currentOrientation = orient;
                updateOrientationGroup(windowIndex, orient);
                if (!freeScrollWindows[windowIndex]) {
                    const consensusSlice = getGroupConsensusSlice(orient);
                    viewer.setSliceIndex(consensusSlice);
                }
                renderMeasurementOverlayForWindow(windowIndex);
                menu.remove();
            };
            orientButtons.appendChild(btn);
        });
        orientSection.appendChild(orientButtons);
        menu.appendChild(orientSection);

        // Actions section
        const actionsSection = document.createElement('div');
        actionsSection.className = 'context-menu-section';

        // Reset view option
        const resetOption = createMenuOption(
            'compress-arrows-alt',
            'Reset View',
            () => {
                if (viewer.nv) {
                    viewer.nv.scene.pan2Dxyzmm = [0, 0, 0, 1];
                    viewer.nv.drawScene();
                    renderMeasurementOverlayForWindow(windowIndex);
                }
                menu.remove();
            }
        );
        actionsSection.appendChild(resetOption);

        // Toggle crosshair option
        const crosshairVisible = viewer.nv ? viewer.nv.opts.crosshairWidth > 0 : true;
        const crosshairOption = createMenuOption(
            'crosshairs',
            crosshairVisible ? 'Hide Crosshair' : 'Show Crosshair',
            () => {
                if (viewer.nv) {
                    viewer.nv.opts.crosshairWidth = crosshairVisible ? 0 : VISIBLE_CROSSHAIR_WIDTH;
                    viewer.nv.drawScene();
                    const crosshairBtn = windowEl.querySelector('.crosshair-toggle-btn');
                    if (crosshairBtn) {
                        crosshairBtn.classList.toggle('crosshair-hidden', crosshairVisible);
                    }
                    renderMeasurementOverlayForWindow(windowIndex);
                }
                menu.remove();
            }
        );
        actionsSection.appendChild(crosshairOption);

        // Unlink/sync option
        const unlinkOption = createMenuOption(
            isFreeScroll ? 'link' : 'link-slash',
            isFreeScroll ? 'Re-sync Scrolling' : 'Unlink (Free Scroll)',
            () => {
                freeScrollWindows[windowIndex] = !freeScrollWindows[windowIndex];
                const freeScrollBtn = windowEl.querySelector('.free-scroll-btn');
                if (freeScrollBtn) {
                    const icon = freeScrollBtn.querySelector('i');
                    if (freeScrollWindows[windowIndex]) {
                        freeScrollBtn.classList.add('free-scroll-active');
                        icon.classList.remove('fa-link');
                        icon.classList.add('fa-link-slash');
                    } else {
                        freeScrollBtn.classList.remove('free-scroll-active');
                        icon.classList.remove('fa-link-slash');
                        icon.classList.add('fa-link');
                        const consensusSlice = getGroupConsensusSlice(windowStates[windowIndex].currentOrientation);
                        viewer.setSliceIndex(consensusSlice);
                    }
                }
                renderMeasurementOverlayForWindow(windowIndex);
                menu.remove();
            }
        );
        actionsSection.appendChild(unlinkOption);

        // Clear window option
        if (djangoData.allowClearWindow) {
            const clearOption = createMenuOption(
                'times',
                'Clear Window',
                () => {
                    clearWindow(windowIndex);
                    menu.remove();
                }
            );
            actionsSection.appendChild(clearOption);
        }

        menu.appendChild(actionsSection);
        document.body.appendChild(menu);

        // Position menu to stay on screen
        const rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) {
            menu.style.left = `${x - rect.width}px`;
        }
        if (rect.bottom > window.innerHeight) {
            menu.style.top = `${y - rect.height}px`;
        }
    }

    /**
     * Helper to create context menu option
     */
    function createMenuOption(iconClass, text, onClick) {
        const option = document.createElement('div');
        option.className = 'context-menu-option';
        option.innerHTML = `<i class="fas fa-${iconClass} me-2"></i>${text}`;
        option.onclick = onClick;
        return option;
    }

    /**
     * Clear a window (reset to empty state)
     * @param {number} windowIndex - 0-3 for grid position
     */
    function clearWindow(windowIndex) {
        beginWindowLoad(windowIndex);

        // Dispose NiiVue viewer if present
        const state = windowStates[windowIndex];
        if (state.niivueInstance) {
            console.log(`Disposing NiiVue viewer in window ${windowIndex}`);
            try {
                state.niivueInstance.dispose();
            } catch (e) {
                console.warn('Error disposing viewer:', e);
            }
        }

        // Remove from synchronization groups
        for (const orientation in synchronizationGroups) {
            const index = synchronizationGroups[orientation].indexOf(windowIndex);
            if (index > -1) {
                synchronizationGroups[orientation].splice(index, 1);
            }
        }

        // Reset free scroll state
        freeScrollWindows[windowIndex] = false;
        measurementOverlayState[windowIndex].projected = [];
        measurementOverlayState[windowIndex].hover = null;

        // Reset state
        windowStates[windowIndex] = {
            modality: null,
            loading: false,
            error: null,
            fileId: null,
            niivueInstance: null,
            currentOrientation: 'axial'
        };

        updateWindowUI(windowIndex);
        renderMeasurementOverlays();
        console.log(`Cleared window ${windowIndex}`);
    }

    function setWindowOrientation(windowIndex, orientation) {
        const state = windowStates[windowIndex];
        if (!state || !state.niivueInstance) {
            return;
        }

        const viewer = state.niivueInstance;
        viewer.setOrientation(orientation);
        windowStates[windowIndex].currentOrientation = orientation;
        updateOrientationGroup(windowIndex, orientation);

        const windowEl = document.querySelector(`.viewer-window[data-window-index="${windowIndex}"]`);
        if (windowEl) {
            const menuBtns = windowEl.querySelectorAll('.orientation-btn');
            menuBtns.forEach(btn => {
                btn.classList.toggle('active', btn.dataset.orientation === orientation);
            });
        }

        const counter = windowEl ? windowEl.querySelector('.slice-counter') : null;
        if (counter) {
            const idx = viewer.getSliceIndex();
            const total = viewer.getSliceCount();
            counter.textContent = `${idx + 1} / ${total}`;
        }

        renderMeasurementOverlayForWindow(windowIndex);
    }

    function suspendSynchronization() {
        _syncSuspended = true;
        if (_syncRAF) {
            cancelAnimationFrame(_syncRAF);
            _syncRAF = null;
        }
    }

    function resumeSynchronization() {
        _syncSuspended = false;
    }

    // Public API
    return {
        init: init,
        windowStates: windowStates,
        loadModalityInWindow: loadModalityInWindow,
        clearWindow: clearWindow,
        setWindowOrientation: setWindowOrientation,
        suspendSynchronization: suspendSynchronization,
        resumeSynchronization: resumeSynchronization
    };
})();

// Expose globally for adapters/integration scripts.
window.ViewerGrid = ViewerGrid;

// Initialize on DOMContentLoaded for brain project pages
document.addEventListener('DOMContentLoaded', function() {
    // Check if we're on a brain patient detail page (has viewer grid)
    if (document.querySelector('.viewer-grid')) {
        ViewerGrid.init();
    }
});
