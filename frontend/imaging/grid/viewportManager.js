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
import { residualModalityLut } from '../metadata/modalityLutModule.js';
import { awaitVolumeLoad, readScalarData } from './volumeLoading.js';
import { describeGeometry } from '../geometry/orientation.js';

/** The rendering engine id. Session-scoped, never persisted. */
export const RENDERING_ENGINE_ID = 'ygg-volume-grid';

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

    const toolGroups = createToolGroups({ addTool, ToolGroupManager, tools, toolsEnums });
    for (const entry of layout) {
        if (entry.lazy) {
            continue;
        }
        toolGroups[toolGroupIdFor(entry.orientation)].addViewport(
            viewportId(entry.window),
            RENDERING_ENGINE_ID
        );
    }

    return {
        state,
        renderingEngine,
        toolGroups,
        elements,
        volumeCache,

        /** Load one volume into one window. Returns the F2 warning, if any. */
        loadVolumeIntoWindow: (windowIndex, descriptor) =>
            loadVolumeIntoWindow({
                cornerstone,
                renderingEngine,
                state,
                windowIndex,
                descriptor,
                volumeCache,
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

        /** Reset the camera on one window, or on all of them. */
        resetCameras: (windowIndex) => resetCameras({ renderingEngine, state, windowIndex }),

        /** The window/level currently applied to a window, in modality units. */
        readWindow: (windowIndex) => readWindow({ renderingEngine, state, windowIndex, volumeCache }),

        /** Drop volumes no window is showing any more. */
        releaseUnusedVolumes: () => releaseUnusedVolumes({ cornerstone, state, volumeCache }),

        destroy() {
            for (const group of new Set(Object.values(toolGroups))) {
                ToolGroupManager.destroyToolGroup(group.id);
            }
            renderingEngine.destroy();
        },
    };
}

/**
 * Two tool groups: one for slices, one for the volume render.
 *
 * They are separate because the tools genuinely are: a length measurement has no
 * meaning in a volume render, and a trackball has none in a slice. One group with
 * everything in it would put tools on a toolbar that cannot act.
 */
function createToolGroups({ addTool, ToolGroupManager, tools, toolsEnums }) {
    for (const tool of Object.values(tools)) {
        addTool(tool);
    }

    const twoD = ToolGroupManager.createToolGroup(toolGroupIdFor(ORIENTATIONS.AXIAL));
    const threeD = ToolGroupManager.createToolGroup(toolGroupIdFor(ORIENTATIONS.RENDER));

    for (const [name, tool] of Object.entries(tools)) {
        if (name === 'TrackballRotate') {
            threeD.addTool(tool.toolName);
            continue;
        }
        twoD.addTool(tool.toolName);
    }

    const { MouseBindings } = toolsEnums;

    // Permanent bindings. A user who has picked the length tool still has to navigate,
    // so pan, zoom and scroll are never the thing the toolbar swaps.
    twoD.setToolActive(tools.Pan.toolName, {
        bindings: [{ mouseButton: MouseBindings.Auxiliary }],
    });
    twoD.setToolActive(tools.Zoom.toolName, {
        bindings: [{ mouseButton: MouseBindings.Secondary }],
    });
    twoD.setToolActive(tools.StackScroll.toolName, { bindings: [{ mouseButton: MouseBindings.Wheel }] });

    // The opening primary tool. Window/level rather than a measurement: the first
    // thing anyone does with a new volume is find the tissue.
    twoD.setToolActive(tools.WindowLevel.toolName, {
        bindings: [{ mouseButton: MouseBindings.Primary }],
    });

    threeD.setToolActive(tools.TrackballRotate.toolName, {
        bindings: [{ mouseButton: MouseBindings.Primary }],
    });
    threeD.setToolActive(tools.Zoom.toolName, { bindings: [{ mouseButton: MouseBindings.Secondary }] });
    threeD.setToolActive(tools.Pan.toolName, { bindings: [{ mouseButton: MouseBindings.Auxiliary }] });

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
 * Fetch a volume's header, load it, put it in a viewport, and set the opening window.
 *
 * The header is read separately from the volume because the loader does not surface
 * it, and everything Phase 3 does with real values -- the residual LUT, the VOI, the
 * F2 gate -- needs it. One extra request for a few hundred bytes; the browser cache
 * serves it from the same entry as the volume fetch that follows.
 *
 * @returns {Promise<{orientationWarning: string|null, voi: object, geometry: object}>}
 */
async function loadVolumeIntoWindow({
    cornerstone,
    renderingEngine,
    state,
    windowIndex,
    descriptor,
    volumeCache,
}) {
    const { volumeLoader, createNiftiImageIdsAndCacheMetadata, setVolumesForViewports } = cornerstone;
    const { url, modality, fileId } = descriptor;
    const volumeId = volumeIdFor(url);
    const generation = beginLoad(state, windowIndex, { modality, fileId, volumeId });

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
        volumeCache?.set(volumeId, { volume, header });

        // The window may have been cleared or reloaded while that was in flight.
        if (!completeLoad(state, windowIndex, generation, {
            orientationWarning: geometry.declared ? null : orientationWarningFor(geometry),
        })) {
            return { superseded: true, orientationWarning: null, voi: null, geometry };
        }

        const id = viewportId(windowIndex);
        await setVolumesForViewports(renderingEngine, [{ volumeId }], [id]);

        const voi = openingVoi({
            scalarData: readScalarData(volume),
            header,
            modality,
        });
        renderingEngine.getViewport(id).setProperties({ voiRange: voi.range });
        renderingEngine.renderViewports([id]);

        return {
            superseded: false,
            orientationWarning: windowAt(state, windowIndex).orientationWarning,
            voi,
            unit: unitFor(modality),
            geometry,
        };
    } catch (error) {
        failLoad(state, windowIndex, generation, error.message);
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
