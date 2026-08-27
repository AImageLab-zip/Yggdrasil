/**
 * The Cornerstone-touching half of the volume grid.
 *
 * Everything with judgement in it lives in `layout.js`, `windowState.js` and `voi.js`,
 * which are pure and have 30 tests between them. What is left here is the part that
 * needs a GPU and therefore cannot be unit-tested: create a rendering engine, build
 * viewports, register tools, put a volume in. It is kept deliberately thin, and every
 * decision it makes is delegated.
 *
 * That split is the same one the validation harness uses, and for the same reason: if
 * the grid misbehaves in a browser, "is it the logic or the wiring?" should already be
 * answered.
 *
 * Three things this module is responsible for that are easy to get wrong:
 *
 *   - **Volumes are shared, viewports are not.** Two windows on the same file get one
 *     cached volume (`volumeIdFor` keys on the URL) and two viewports. Evicting the
 *     volume when one of those windows is cleared would blank the other, so eviction
 *     asks `activeVolumeIds`.
 *   - **The F2 warning has to reach the screen.** A volume whose header declares no
 *     orientation renders perfectly and may be mirrored. `loadVolumeIntoWindow`
 *     returns the warning and refuses to swallow it; the caller must show it.
 *   - **The VOI is set in stored units.** See `voi.js`. Passing a Hounsfield window
 *     straight to `setProperties` windows on air for two of the four rescale shapes.
 */

import {
    FIXED_CBCT_LAYOUT,
    ORIENTATIONS,
    assertEnumsMatch,
    isSliceOrientation,
    toolGroupIdFor,
    viewportId,
    viewportSpecFor,
    volumeIdFor,
} from './layout.js';
import {
    activeVolumeIds,
    beginLoad,
    completeLoad,
    createGridState,
    failLoad,
    setOrientation,
    windowAt,
} from './windowState.js';
import { modalityWindowFromVoiRange, openingVoi, unitFor } from './voi.js';
import { DEFAULT_RENDER_MODE, applyRenderMode } from './renderModes.js';
import { createOverlay, updateOverlay } from './viewportOverlay.js';
import { formatWindow } from './voi.js';
import { residualModalityLut } from '../metadata/modalityLutModule.js';
import { awaitVolumeLoad, readScalarData } from './volumeLoading.js';
import { describeGeometry } from '../geometry/orientation.js';
// The same list and filter a save uses: what is hidden, cleared and stored is one set.
import { MEASUREMENT_TOOLS, NAVIGATION_TOOL, measurementAnnotations } from './measurements.js';

/** The rendering engine id. Session-scoped, never persisted. */
export const RENDERING_ENGINE_ID = 'ygg-volume-grid';

/**
 * The volume render opens looking at the patient's front.
 *
 * Cornerstone's default for a 3D viewport looks down the superior axis -- straight at
 * the top of the head, which for a CBCT is a view of the skull vault and tells a
 * clinician nothing. These are Cornerstone's own `coronal` MPR vectors: the camera sits
 * anterior of the patient (`viewPlaneNormal` points from the focal point back toward
 * it) with superior up.
 */
export const RENDER_DEFAULT_CAMERA = Object.freeze({
    viewPlaneNormal: Object.freeze([0, -1, 0]),
    viewUp: Object.freeze([0, 0, 1]),
});

/**
 * Navigation that belongs to **both** tool groups.
 *
 * A tool has to be *added* to a group before it can be made active in it.
 * `setToolActive` on an unadded tool does not throw -- it logs "Tool Zoom not added to
 * toolGroup, can't set tool mode" and carries on, leaving the 3D view with no pan and
 * no zoom. That is how this shipped, and why the list is a named constant with a test
 * rather than a condition inside a loop.
 */
export const SHARED_NAVIGATION_TOOLS = Object.freeze(['Pan', 'Zoom']);

/**
 * The class the stylesheet keys the drop-hint off.
 *
 * `static/css/viewer_grid.css` has `.viewer-window.loaded .drop-hint { display: none }`.
 * Without the class the placeholder keeps its `height: 100%` and a loaded window shows
 * a grey icon over a black canvas -- indistinguishable from a viewer that failed.
 */
export const LOADED_CLASS = 'loaded';

/**
 * Tools bound to the left mouse button, one at a time, chosen from the toolbar.
 *
 * Everything else (pan on middle, zoom on right, scroll on wheel) stays bound
 * permanently, because a user who has picked the length tool still needs to navigate.
 */
export const PRIMARY_TOOLS = Object.freeze([
    'WindowLevel',
    'Crosshairs',
    'Length',
    'Height',
    'Angle',
    'CobbAngle',
    'Bidirectional',
    'Probe',
    'RectangleROI',
    'EllipticalROI',
    'CircleROI',
]);

/**
 * Build the grid: one rendering engine, one viewport per window, two tool groups.
 *
 * @param {object} options
 * @param {object} options.cornerstone the imports the entry passes in -- see the entry
 *   for why they are injected rather than imported here.
 * @param {HTMLElement[]} options.elements one per window, in window order.
 * @param {object[]} [options.layout]
 * @returns {object} a grid handle.
 */
export function createVolumeGrid({ cornerstone, elements, layout = FIXED_CBCT_LAYOUT }) {
    const {
        RenderingEngine,
        coreEnums,
        toolsEnums,
        addTool,
        ToolGroupManager,
        tools,
        orientationUtilities,
        createVOISynchronizer,
        orientationMarkerUrl,
    } = cornerstone;

    // The inlined enum strings in layout.js are a copy; check it before anything is
    // built on it, so a version bump fails here rather than as a viewport that renders
    // nothing.
    assertEnumsMatch(coreEnums);

    const state = createGridState(layout);
    const renderingEngine = new RenderingEngine(RENDERING_ENGINE_ID);

    // Keyed by volume id, so two windows on one file share one entry. The header is
    // kept alongside the volume because everything that needs real values needs both,
    // and re-fetching the header per consumer would be a second round trip for bytes
    // already in hand.
    const volumeCache = new Map();

    const viewportInputs = layout
        .filter((entry) => !entry.lazy)
        .map((entry) => {
            const spec = viewportSpecFor(entry.orientation);
            return {
                viewportId: viewportId(entry.window),
                type: spec.type,
                element: elements[entry.window],
                defaultOptions: spec.orientation ? { orientation: spec.orientation } : {},
            };
        });
    renderingEngine.setViewports(viewportInputs);

    // One overlay per window, built with the viewports rather than after the volume
    // arrives: the panel name and the edge letters are properties of the *plane*, and
    // showing them immediately is what makes an empty grid look deliberate.
    const overlays = new Map();
    for (const entry of layout) {
        if (entry.lazy || !elements[entry.window]) {
            continue;
        }
        overlays.set(entry.window, createOverlay(elements[entry.window], { orientation: entry.orientation }));
    }

    const toolGroups = createToolGroups({
        addTool,
        ToolGroupManager,
        tools,
        toolsEnums,
        orientationMarkerUrl,
    });

    // Brightness applies to the whole study, not to one plane. Cornerstone's own
    // synchronizer, so a Shift+drag in any window moves them all -- including the
    // volume render, whose transfer function `setProperties({voiRange})` drives.
    const voiSynchronizer = createVOISynchronizer?.('ygg-grid-voi', { syncColormap: false });
    for (const entry of layout) {
        if (entry.lazy) {
            continue;
        }
        toolGroups[toolGroupIdFor(entry.orientation)].addViewport(
            viewportId(entry.window),
            RENDERING_ENGINE_ID
        );
        voiSynchronizer?.add({
            renderingEngineId: RENDERING_ENGINE_ID,
            viewportId: viewportId(entry.window),
        });
    }

    /**
     * Refresh one window's overlay from its viewport.
     *
     * Reads the camera each time rather than caching it: the edge letters follow the
     * camera, which is the whole reason they come from `getOrientationStringLPS` and
     * not from a per-plane table.
     */
    const refreshOverlay = (windowIndex) => {
        const nodes = overlays.get(windowIndex);
        const viewport = renderingEngine.getViewport(viewportId(windowIndex));
        if (!nodes || !viewport) {
            return;
        }
        const window = windowAt(state, windowIndex);
        let windowText = '';
        if (isSliceOrientation(window.orientation)) {
            const current = readWindow({ renderingEngine, state, windowIndex, volumeCache });
            windowText = current ? formatWindow(current, current.unit) : '';
        }
        updateOverlay(nodes, {
            camera: viewport.getCamera?.(),
            sliceIndex: viewport.getSliceIndex?.(),
            sliceCount: viewport.getImageIds?.()?.length,
            windowText,
            utilities: orientationUtilities,
        });
    };

    // Cornerstone dispatches these on the viewport's own element, so the overlay stays
    // in step with scrolling, crosshair drags, zoom and window/level without polling.
    for (const [windowIndex, ] of overlays) {
        const element = elements[windowIndex];
        for (const eventName of ['CORNERSTONE_CAMERA_MODIFIED', 'CORNERSTONE_VOI_MODIFIED']) {
            element.addEventListener(eventName, () => refreshOverlay(windowIndex));
        }
    }

    // Named rather than returned anonymously: `setAnnotationMode` composes two of the
    // handle's own operations, and calling them through the object is what keeps that
    // composition honest if either one grows.
    const grid = {
        state,
        renderingEngine,
        toolGroups,
        elements,
        overlays,
        refreshOverlay,
        refreshOverlays: () => overlays.forEach((unused, index) => refreshOverlay(index)),
        volumeCache,

        /** Load one volume into one or more windows. Returns the F2 warning, if any. */
        loadVolumeIntoWindows: (windowIndices, descriptor) =>
            loadVolumeIntoWindows({
                cornerstone,
                renderingEngine,
                state,
                windowIndices,
                descriptor,
                volumeCache,
                elements,
            }),

        /** Point a window at a different plane, rebuilding its viewport. */
        setWindowOrientation: (windowIndex, orientation) =>
            setWindowOrientation({ renderingEngine, state, windowIndex, orientation, elements, toolGroups }),

        /** Bind one primary tool to the left mouse button across the 2D group. */
        setPrimaryTool: (toolName) =>
            setPrimaryTool({ toolGroup: toolGroups[toolGroupIdFor(ORIENTATIONS.AXIAL)], toolsEnums, toolName }),

        /** Bring up the 3D window, which the layout leaves lazy. */
        enable3DWindow: (windowIndex, mode) =>
            enable3DWindow({ cornerstone, renderingEngine, state, elements, toolGroups, windowIndex, mode }),

        /** Switch the volume render between mip / amip / shaded. */
        setRenderMode: (windowIndex, mode) =>
            setRenderMode({ renderingEngine, windowIndex, mode }),

        /**
         * Re-fit every viewport to its element.
         *
         * Needed whenever the layout changes under Cornerstone -- expanding the 3D
         * view, showing a hidden tab -- because a viewport whose element changed size
         * keeps rendering at the old one until it is told.
         */
        resize: () => {
            renderingEngine.resize(true, true);
            overlays.forEach((unused, index) => refreshOverlay(index));
        },

        /** Reset the camera on one window, or on all of them. */
        resetCameras: (windowIndex) => resetCameras({ renderingEngine, state, windowIndex }),

        /** The window/level currently applied to a window, in modality units. */
        readWindow: (windowIndex) => readWindow({ renderingEngine, state, windowIndex, volumeCache }),

        /**
         * The header of whichever volume the grid is showing.
         *
         * Everything that needs real values needs it -- the residual LUT, the VOI, the
         * descriptor a measurement is saved against -- and the load already has it in
         * hand, so it is exposed rather than re-fetched.
         */
        currentHeader: () => {
            for (const window of state.windows) {
                const cached = window.volumeId ? volumeCache.get(window.volumeId) : null;
                if (cached?.header) {
                    return cached.header;
                }
            }
            return null;
        },

        /**
         * Every annotation currently drawn, as Cornerstone holds it.
         *
         * Handed to the server verbatim: the adapter there strips the runtime
         * identifiers and recomputes every number from the handles, so the client has
         * no business tidying it first.
         */
        readAnnotations: () => {
            const groups = cornerstone.annotationState?.getAllAnnotations?.() ?? [];
            return Array.isArray(groups) ? groups : Object.values(groups).flat();
        },

        /**
         * Draw the measurements a study already has.
         *
         * Added against a viewport element so Cornerstone files them under that
         * viewport's group; the annotations carry no `annotationUID` (it is stripped
         * before storage, being session-scoped) and `addAnnotation` mints a fresh one.
         */
        restoreAnnotations: (annotations) => {
            const add = cornerstone.annotationState?.addAnnotation;
            if (!add || !Array.isArray(annotations)) {
                return 0;
            }
            const element = elements[0];
            let restored = 0;
            for (const entry of annotations) {
                try {
                    const annotation = structuredClone(entry);
                    // **`isVisible` is not restored from storage.** The stored payload is
                    // the viewer's state verbatim, so an annotation saved while the
                    // measurements were hidden carries `isVisible: false` -- and
                    // `setAnnotationVisibility(uid, true)` will not undo it, because
                    // Cornerstone's `show()` only writes the flag for a UID that is in
                    // its own hidden set (`annotationVisibility.js`), which a freshly
                    // added annotation never is. That is the "I switch it on and see
                    // nothing" report: the annotation was restored permanently invisible
                    // and no toggle could reach it. Visibility belongs to the session,
                    // not to the record, so it starts true and
                    // {@link setAnnotationsVisible} decides from there.
                    annotation.isVisible = true;
                    add(annotation, element);
                    restored += 1;
                } catch (error) {
                    // One malformed stored annotation must not cost the others.
                    console.warn(`[ygg-grid] could not restore an annotation: ${error.message}`);
                }
            }
            renderingEngine.renderViewports(
                state.windows.filter((w) => w.volumeId).map((w) => viewportId(w.index))
            );
            return restored;
        },

        /**
         * Turn measuring on or off across the grid, as one operation.
         *
         * **The tool modes are the point, not the visibility.** `ToolGroup.addTool`
         * only *instantiates* a tool; it writes no entry in `toolOptions`, so a tool
         * that has never been given a mode is invisible to the annotation renderer --
         * `getToolsWithModesForElement` reads modes off `toolOptions` and skips
         * anything absent from it, and `AnnotationRenderingEngine._triggerRender` only
         * asks the tools that survive that filter to draw. So a Length annotation
         * restored on page load was **not drawn at all** until something put the Length
         * tool into a mode, no matter what its visibility said. That is why switching
         * annotations on showed nothing the first time and worked later: the first
         * click on any measurement button calls {@link setPrimaryTool}, which passives
         * every *other* primary tool and thereby gives them all a mode as a side
         * effect. This makes it the deliberate part of switching the mode on.
         *
         * Passive rather than Enabled: a measurement that is on screen should be
         * draggable by its handles. Drawing a *new* one still needs Active, which is
         * what the toolbar's tool buttons do.
         *
         * Order matters going the other way. `setPrimaryTool` passives the other
         * primary tools, so it has to run *before* the measurement tools are disabled
         * or it would hand them a mode back on the way out.
         */
        setAnnotationMode: (enabled) => {
            const on = Boolean(enabled);
            const toolGroup = toolGroups[toolGroupIdFor(ORIENTATIONS.AXIAL)];
            if (!on) {
                setPrimaryTool({ toolGroup, toolsEnums, toolName: NAVIGATION_TOOL });
            }
            setMeasurementToolModes({ toolGroup, enabled: on });
            return grid.setAnnotationsVisible(on);
        },

        /**
         * Delete every measurement currently drawn.
         *
         * **The viewer only.** Nothing is written: the server's revisions are
         * replace-the-whole-set, so what makes a clear permanent is the next save --
         * and until then a reload brings the measurements back, which is the only undo
         * there is. The caller says so in the notification.
         *
         * Measurements only, again: the crosshair's own annotation lives in the same
         * state and removing it would take the navigation reticle with it.
         *
         * @returns {number} how many were removed.
         */
        clearAnnotations: () => {
            const remove = cornerstone.annotationState?.removeAnnotation;
            const all = cornerstone.annotationState?.getAllAnnotations?.() ?? [];
            const measurements = measurementAnnotations(
                Array.isArray(all) ? all : Object.values(all).flat()
            );
            let removed = 0;
            // Copied first: removing from the live state while iterating it is how one
            // of these gets skipped.
            for (const entry of [...measurements]) {
                if (!entry?.annotationUID) {
                    continue;
                }
                try {
                    remove?.(entry.annotationUID);
                    removed += 1;
                } catch (error) {
                    console.warn(`[ygg-grid] could not remove an annotation: ${error.message}`);
                }
            }
            renderingEngine.renderViewports(
                state.windows.filter((w) => w.volumeId).map((w) => viewportId(w.index))
            );
            return removed;
        },

        /**
         * Show or hide every measurement at once.
         *
         * Visibility, not deletion: the annotations stay in Cornerstone's state, so
         * hiding and showing cannot lose work, and a save while hidden still writes
         * everything that is there.
         *
         * Two things it is careful about, both of which were wrong before:
         *
         * **Measurements only.** `getAllAnnotations()` returns everything Cornerstone is
         * holding, including the state tools keep for themselves -- and the crosshair is
         * one of them. Hiding "all annotations" therefore hid the crosshair too, so
         * switching measurements off took the navigation reticle with it.
         * {@link measurementAnnotations} is the same filter a save uses, so what gets
         * hidden and what gets stored are the same set by construction.
         *
         * **The flag is written as well as the set.** `setAnnotationVisibility` maintains
         * a module-level hidden-UID set and only touches `annotation.isVisible` when the
         * UID moves in or out of it, so calling it with `true` on an annotation that was
         * never in the set leaves a stale `isVisible: false` in place -- invisible, and
         * unreachable by any further toggling. Writing the flag here makes the call
         * idempotent, which is what lets the mode switch be re-asserted at will.
         */
        setAnnotationsVisible: (visible) => {
            const on = Boolean(visible);
            const visibility = cornerstone.annotationVisibility;
            const all = cornerstone.annotationState?.getAllAnnotations?.() ?? [];
            for (const entry of measurementAnnotations(Array.isArray(all) ? all : Object.values(all).flat())) {
                if (entry?.annotationUID) {
                    visibility?.setAnnotationVisibility(entry.annotationUID, on);
                }
                entry.isVisible = on;
            }
            renderingEngine.renderViewports(
                state.windows.filter((w) => w.volumeId).map((w) => viewportId(w.index))
            );
            return on;
        },

        /** Drop volumes no window is showing any more. */
        releaseUnusedVolumes: () => releaseUnusedVolumes({ cornerstone, state, volumeCache }),

        destroy() {
            for (const group of new Set(Object.values(toolGroups))) {
                ToolGroupManager.destroyToolGroup(group.id);
            }
            renderingEngine.destroy();
        },
    };

    return grid;
}

/**
 * Two tool groups: one for slices, one for the volume render.
 *
 * They are separate because the tools genuinely are: a length measurement has no
 * meaning in a volume render, and a trackball has none in a slice. One group with
 * everything in it would put tools on a toolbar that cannot act.
 */
function createToolGroups({ addTool, ToolGroupManager, tools, toolsEnums, orientationMarkerUrl }) {
    for (const tool of Object.values(tools)) {
        addTool(tool);
    }

    const twoD = ToolGroupManager.createToolGroup(toolGroupIdFor(ORIENTATIONS.AXIAL));
    const threeD = ToolGroupManager.createToolGroup(toolGroupIdFor(ORIENTATIONS.RENDER));

    // Navigation belongs to both groups. A tool must be *added* to a group before it
    // can be made active in it -- `setToolActive` on an unadded tool only warns
    // ("Tool Zoom not added to toolGroup, can't set tool mode") and leaves the 3D view
    // with no pan and no zoom. The measurement tools stay 2D-only, and the trackball
    // stays 3D-only, because neither means anything in the other.
    for (const [name, tool] of Object.entries(tools)) {
        if (name === 'TrackballRotate' || name === 'OrientationMarker') {
            threeD.addTool(tool.toolName);
            continue;
        }
        twoD.addTool(tool.toolName);
        if (SHARED_NAVIGATION_TOOLS.includes(name)) {
            threeD.addTool(tool.toolName);
        }
    }

    const { MouseBindings, KeyboardBindings } = toolsEnums;

    // Permanent bindings. A user who has picked the length tool still has to navigate,
    // so pan, zoom and scroll are never the thing the toolbar swaps.
    twoD.setToolActive(tools.Pan.toolName, {
        bindings: [{ mouseButton: MouseBindings.Auxiliary }],
    });
    twoD.setToolActive(tools.Zoom.toolName, {
        bindings: [{ mouseButton: MouseBindings.Secondary }],
    });
    twoD.setToolActive(tools.StackScroll.toolName, { bindings: [{ mouseButton: MouseBindings.Wheel }] });

    // Crosshairs is the opening primary tool, and it is the reason the three slice
    // views feel like one study rather than three pictures: it draws the coloured
    // reference lines showing where each plane cuts the others, and dragging them
    // navigates all three at once. `viewer_grid.js` synchronised NiiVue's crosshairPos
    // by hand to get this; Cornerstone ships it.
    twoD.setToolActive(tools.Crosshairs.toolName, {
        bindings: [{ mouseButton: MouseBindings.Primary }],
    });

    // Window/level keeps the primary button too, behind Shift. It was the default
    // before crosshairs took the button, and it is the second thing anybody does with
    // a new volume -- putting it behind a toolbar mode switch would be a regression.
    twoD.setToolActive(tools.WindowLevel.toolName, {
        bindings: [{ mouseButton: MouseBindings.Primary, modifierKey: KeyboardBindings.Shift }],
    });

    threeD.setToolActive(tools.TrackballRotate.toolName, {
        bindings: [{ mouseButton: MouseBindings.Primary }],
    });
    threeD.setToolActive(tools.Zoom.toolName, { bindings: [{ mouseButton: MouseBindings.Secondary }] });
    threeD.setToolActive(tools.Pan.toolName, { bindings: [{ mouseButton: MouseBindings.Auxiliary }] });

    // The orientation marker in the corner of the 3D view. `enabled` rather than
    // `active`: it is an indicator, not something to drag.
    //
    // The CUSTOM overlay type is the human figure -- Cornerstone's own default for that
    // type is 3D Slicer's `Human.vtp`, fetched from raw.githubusercontent.com at
    // runtime. The URL is overridden with our vendored copy, pinned to a Slicer commit
    // (see static/vendor/slicer/README.md). The objection is not that the request is
    // third-party -- CDNs are fine here -- but that raw.githubusercontent.com is a
    // source-browsing endpoint serving whatever the branch says today. Same analogy as
    // F5's itk-wasm default: a moving reference to an asset we need a fixed one of.
    if (tools.OrientationMarker) {
        const marker = tools.OrientationMarker;
        if (orientationMarkerUrl) {
            threeD.setToolConfiguration(marker.toolName, {
                overlayMarkerType: marker.OVERLAY_MARKER_TYPES.CUSTOM,
                overlayConfiguration: {
                    [marker.OVERLAY_MARKER_TYPES.CUSTOM]: { polyDataURL: orientationMarkerUrl },
                },
            });
        }
        threeD.setToolEnabled(marker.toolName);
    }

    return {
        [toolGroupIdFor(ORIENTATIONS.AXIAL)]: twoD,
        [toolGroupIdFor(ORIENTATIONS.RENDER)]: threeD,
    };
}

/**
 * Swap which tool the left mouse button drives.
 *
 * The previously-bound primary is set *passive*, not disabled: a passive tool keeps
 * its existing annotations visible and selectable, while a disabled one hides them.
 * Measurements disappearing when you pick a different tool is not acceptable.
 */
/**
 * Give every measurement tool a mode, or take it away.
 *
 * Extracted and exported because it is where the bug was and it needs no GPU: with no
 * mode at all, a measurement tool **does not draw its annotations**. `addTool` writes no
 * `toolOptions` entry, `getToolsWithModesForElement` reads modes off `toolOptions` and
 * skips whatever is missing from it, and the annotation rendering engine only asks the
 * surviving tools to draw. A study whose measurements were restored on load therefore
 * showed nothing until something gave those tools a mode -- which the first click on any
 * tool button did by accident, via `setPrimaryTool` passiving its neighbours.
 *
 * `Passive`, not `Enabled`: a measurement on screen should be draggable by its handles.
 * Creating a new one needs `Active`, which is the toolbar's job.
 *
 * @param {object} options
 * @param {object} options.toolGroup the 2D group.
 * @param {boolean} options.enabled
 * @returns {string[]} the tools whose mode was set, for the caller to assert on.
 */
export function setMeasurementToolModes({ toolGroup, enabled }) {
    const applied = [];
    for (const toolName of MEASUREMENT_TOOLS) {
        // Guarded: the list is shared with the save filter, and a tool named there but
        // never added to this group would otherwise only warn into the console.
        if (!toolGroup?.getToolInstance?.(toolName)) {
            continue;
        }
        if (enabled) {
            toolGroup.setToolPassive(toolName);
        } else {
            toolGroup.setToolDisabled(toolName);
        }
        applied.push(toolName);
    }
    return applied;
}

function setPrimaryTool({ toolGroup, toolsEnums, toolName }) {
    if (!PRIMARY_TOOLS.includes(toolName)) {
        throw new Error(`'${toolName}' is not a primary tool; it cannot take the left button.`);
    }
    for (const name of PRIMARY_TOOLS) {
        if (name !== toolName) {
            toolGroup.setToolPassive(name);
        }
    }
    toolGroup.setToolActive(toolName, {
        bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
    });
}

/**
 * Load one volume and show it in every window that wants it.
 *
 * **Plural on purpose.** The maxillo grid puts one CBCT in three windows, and doing
 * that a window at a time is not merely inelegant: `getCompleteScalarDataArray()`
 * allocates a *fresh copy of the whole volume* on every call
 * (`VoxelManager.js:649`), so computing the opening VOI three times meant three
 * 200 MB allocations for one 60-million-voxel study. That is most of the "it takes a
 * while to load" this replaced.
 *
 * The header is read separately from the volume because the loader does not surface
 * it, and everything Phase 3 does with real values -- the residual LUT, the VOI, the
 * F2 gate -- needs it. One extra request for a few hundred bytes; the browser cache
 * serves it from the same entry as the volume fetch that follows.
 *
 * @returns {Promise<object>} one result, describing all the windows.
 */
async function loadVolumeIntoWindows({
    cornerstone,
    renderingEngine,
    state,
    windowIndices,
    descriptor,
    volumeCache,
    elements,
}) {
    const { volumeLoader, createNiftiImageIdsAndCacheMetadata, setVolumesForViewports } = cornerstone;
    const { url, modality, fileId } = descriptor;
    const volumeId = volumeIdFor(url);
    const generations = new Map(
        windowIndices.map((index) => [index, beginLoad(state, index, { modality, fileId, volumeId })])
    );

    try {
        const header = await fetchHeader(url);
        const geometry = describeGeometry(header);

        const imageIds = await createNiftiImageIdsAndCacheMetadata({ url });
        if (!imageIds?.length) {
            throw new Error('The loader produced no imageIds for this volume.');
        }
        const volume = await volumeLoader.createAndCacheVolume(volumeId, { imageIds });
        // NOT `await volume.load()`: `ImageVolume.load` returns undefined, so awaiting
        // it resolves on the next microtask with no frames loaded. See volumeLoading.js.
        await awaitVolumeLoad(volume);

        // Read once and keep it. Every later consumer -- the VOI here, the panoramic
        // bridge, the ROI readout -- would otherwise re-materialise the whole volume.
        const scalarData = readScalarData(volume);
        volumeCache?.set(volumeId, { volume, header, scalarData });

        const orientationWarning = geometry.declared ? null : orientationWarningFor(geometry);
        const live = windowIndices.filter((index) =>
            completeLoad(state, index, generations.get(index), { orientationWarning })
        );
        if (live.length === 0) {
            return { superseded: true, orientationWarning: null, voi: null, geometry, windows: [] };
        }

        const ids = live.map(viewportId);
        await setVolumesForViewports(renderingEngine, [{ volumeId }], ids);

        const voi = openingVoi({ scalarData, header, modality });
        for (const index of live) {
            const id = viewportId(index);
            if (isSliceOrientation(windowAt(state, index).orientation)) {
                // A slice viewport clips against the scalar data, so the VOI is what
                // decides what is visible.
                renderingEngine.getViewport(id)?.setProperties({ voiRange: voi.range });
                continue;
            }
            // A volume render has no VOI: what it shows is decided by the blend mode
            // and the transfer function. Setting `voiRange` on it does nothing, and
            // leaving it unconfigured renders an opaque block.
            try {
                setRenderMode({ renderingEngine, windowIndex: index, mode: DEFAULT_RENDER_MODE });
                // Frontal, not superior. Set after the volume so `resetCamera` has real
                // bounds to fit, and before the render so the first frame is already
                // the right way round.
                const render = renderingEngine.getViewport(id);
                render?.setCamera?.({
                    viewPlaneNormal: [...RENDER_DEFAULT_CAMERA.viewPlaneNormal],
                    viewUp: [...RENDER_DEFAULT_CAMERA.viewUp],
                });
                render?.resetCamera?.({ resetPan: true, resetZoom: true });
            } catch (error) {
                // A 3D view that will not configure must not cost the slice views.
                console.warn(`[ygg-grid] 3D render mode: ${error.message}`);
            }
        }
        renderingEngine.renderViewports(ids);

        // The template ships a `.drop-hint` placeholder in every window, and the
        // stylesheet hides it behind `.viewer-window.loaded`
        // (`static/css/viewer_grid.css:252`). Without the class the hint keeps its
        // `height: 100%` and the window shows a grey icon over a black canvas -- which
        // is exactly what a viewer that failed to load looks like.
        for (const index of live) {
            elements?.[index]?.classList?.add(LOADED_CLASS);
        }

        return {
            superseded: false,
            orientationWarning,
            voi,
            unit: unitFor(modality),
            geometry,
            windows: live,
        };
    } catch (error) {
        for (const [index, generation] of generations) {
            failLoad(state, index, generation, error.message);
        }
        throw error;
    }
}

/**
 * The message a volume with no declared orientation must show.
 *
 * Finding F2. `nifti-reader-js` fabricates a diagonal affine from `pixDims` and
 * asserts RAS storage order with no evidence; Cornerstone then renders it without
 * complaint. The volume is perfectly usable for anything relative -- a distance, a
 * ratio -- and is not trustworthy for anything about *sides*, which is exactly the
 * distinction the message has to draw.
 */
function orientationWarningFor(geometry) {
    return (
        'This volume declares no orientation (qform_code = sform_code = 0). Left and ' +
        `right are inferred from pixel dimensions and may be reversed; the image is ` +
        `being displayed as ${geometry.axcodes ?? 'RAS'}. Measurements are unaffected.`
    );
}

async function fetchHeader(url) {
    const reader = globalThis.nifti;
    if (!reader) {
        throw new Error('The vendored nifti-reader is not loaded; orientation cannot be checked.');
    }
    // Range-request the header. `serve_file` advertises byte ranges only for audio and
    // video, so a server that ignores the header hands back the whole volume -- which
    // is correct, just larger, and the browser cache absorbs it for the load below.
    const response = await fetch(url, { credentials: 'same-origin', headers: { Range: 'bytes=0-1023' } });
    if (!response.ok && response.status !== 206) {
        throw new Error(`HTTP ${response.status} fetching the volume header.`);
    }
    const buffer = await response.arrayBuffer();
    const decompressed = reader.isCompressed(buffer) ? reader.decompress(buffer) : buffer;
    return reader.readHeader(decompressed);
}


/**
 * Rebuild one window's viewport for a different plane.
 *
 * `setViewports` rather than mutating the existing viewport: an orthographic and a
 * volume3d viewport are different objects, so switching between a slice and the 3D
 * render is a rebuild whichever way it is expressed. Doing it uniformly means the
 * slice-to-slice case cannot drift from the slice-to-3D one.
 */
function setWindowOrientation({ renderingEngine, state, windowIndex, orientation, elements, toolGroups }) {
    const previous = windowAt(state, windowIndex).orientation;
    if (previous === orientation) {
        return;
    }
    setOrientation(state, windowIndex, orientation);

    const id = viewportId(windowIndex);
    const spec = viewportSpecFor(orientation);
    renderingEngine.enableElement({
        viewportId: id,
        type: spec.type,
        element: elements[windowIndex],
        defaultOptions: spec.orientation ? { orientation: spec.orientation } : {},
    });

    // The viewport may have moved between tool groups -- 2D tools do not belong to a
    // volume render, and vice versa.
    const previousGroup = toolGroups[toolGroupIdFor(previous)];
    const nextGroup = toolGroups[toolGroupIdFor(orientation)];
    if (previousGroup !== nextGroup) {
        previousGroup.removeViewports(RENDERING_ENGINE_ID, id);
        nextGroup.addViewport(id, RENDERING_ENGINE_ID);
    }

    const { volumeId } = windowAt(state, windowIndex);
    if (volumeId) {
        renderingEngine.renderViewports([id]);
    }
    return isSliceOrientation(orientation);
}

/**
 * Evict volumes no window is showing.
 *
 * Asks `activeVolumeIds`, which deduplicates: clearing one of three windows on a
 * shared CBCT must not blank the other two.
 */
function releaseUnusedVolumes({ cornerstone, state, volumeCache }) {
    const keep = new Set(activeVolumeIds(state));
    const released = [];
    for (const volume of cornerstone.cache.getVolumes()) {
        if (!keep.has(volume.volumeId)) {
            cornerstone.cache.removeVolumeLoadObject(volume.volumeId);
            // Dropped here too, or this map pins the scalar data Cornerstone just
            // released and the eviction frees nothing.
            volumeCache?.delete(volume.volumeId);
            released.push(volume.volumeId);
        }
    }
    return released;
}


/**
 * Bring up the lazy 3D window.
 *
 * The layout marks window 3 `lazy` and `createVolumeGrid` skips it, because a volume
 * render of a full CBCT is the most expensive thing the page can do and most visits
 * never ask for it. This is what happens when somebody does.
 */
async function enable3DWindow({
    cornerstone,
    renderingEngine,
    state,
    elements,
    toolGroups,
    windowIndex,
    mode = DEFAULT_RENDER_MODE,
}) {
    const { setVolumesForViewports } = cornerstone;
    const id = viewportId(windowIndex);
    const spec = viewportSpecFor(ORIENTATIONS.RENDER);

    // The volume to show is whatever the 2D windows are already showing; loading a
    // second copy for the 3D view would double a CBCT in GPU memory.
    const source = state.windows.find((entry) => entry.volumeId && !entry.loading);
    if (!source) {
        throw new Error('There is no loaded volume to render in 3D yet.');
    }

    renderingEngine.enableElement({
        viewportId: id,
        type: spec.type,
        element: elements[windowIndex],
        defaultOptions: {},
    });
    toolGroups[toolGroupIdFor(ORIENTATIONS.RENDER)].addViewport(id, RENDERING_ENGINE_ID);

    setOrientation(state, windowIndex, ORIENTATIONS.RENDER);
    const window = windowAt(state, windowIndex);
    window.volumeId = source.volumeId;
    window.modality = source.modality;
    window.fileId = source.fileId;

    await setVolumesForViewports(renderingEngine, [{ volumeId: source.volumeId }], [id]);
    setRenderMode({ renderingEngine, windowIndex, mode });
    renderingEngine.renderViewports([id]);
    return window;
}

/**
 * Apply a render mode to the 3D viewport.
 *
 * Reaches for the actor rather than the viewport's own helpers because the blend mode
 * and the shader replacements both live on the mapper, and `renderModes.js` owns the
 * order they have to be set in.
 */
function setRenderMode({ renderingEngine, windowIndex, mode }) {
    const viewport = renderingEngine.getViewport(viewportId(windowIndex));
    if (!viewport) {
        throw new Error(`Window ${windowIndex} has no viewport to render into.`);
    }
    const actor = viewport.getActors?.()?.[0]?.actor;
    if (!actor?.getMapper) {
        throw new Error('The 3D viewport has no volume actor yet.');
    }
    const spec = applyRenderMode(actor, mode);
    viewport.render();
    return spec;
}

/** Reset the camera on one window, or every loaded one. */
function resetCameras({ renderingEngine, state, windowIndex = null }) {
    const targets =
        windowIndex === null
            ? state.windows.filter((entry) => entry.volumeId).map((entry) => entry.index)
            : [windowIndex];

    for (const index of targets) {
        const viewport = renderingEngine.getViewport(viewportId(index));
        viewport?.resetCamera?.();
    }
    renderingEngine.renderViewports(targets.map(viewportId));
    return targets;
}

/**
 * The window currently applied to a viewport, in modality units.
 *
 * Converted back out of stored units on the way, because that is what the readout has
 * to show -- see `voi.js`. Returns null when there is nothing to report rather than a
 * zero window, which would read as a real setting.
 */
function readWindow({ renderingEngine, state, windowIndex, volumeCache }) {
    const window = windowAt(state, windowIndex);
    const viewport = renderingEngine.getViewport(viewportId(windowIndex));
    const range = viewport?.getProperties?.()?.voiRange;
    const cached = window.volumeId ? volumeCache?.get(window.volumeId) : null;

    if (!range || !cached?.header) {
        return null;
    }
    return {
        ...modalityWindowFromVoiRange(range, residualModalityLut(cached.header)),
        unit: unitFor(window.modality),
    };
}
