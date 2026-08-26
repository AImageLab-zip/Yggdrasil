/**
 * Entry point: CBCT + brain volume grid (roadmap Phase 3).
 *
 * **Not yet wired.** `templates/common/patient_detail.html` still loads NiiVue and
 * `viewer_grid.js`; the swap and the deletions are gated on a green validation-harness
 * run across the maxillo *and* brain corpora, which is the roadmap's own condition for
 * this phase. What is here is the replacement, built and testable ahead of that gate.
 *
 * The imports below are also what pull the three web workers and the ICRPolySeg wasm
 * into the emitted tree, so `npm run verify` has something to check.
 *
 * Cornerstone is imported *here* and injected into `createVolumeGrid` rather than
 * imported by `imaging/grid/viewportManager.js` directly. That keeps every module
 * under `imaging/grid/` importable under `node --test`, which is what lets the layout,
 * the window state machine and the VOI conversions carry 30 tests between them while
 * the part that genuinely needs a GPU stays one thin file.
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    volumeLoader,
    imageLoader,
    metaData,
    cache,
    setVolumesForViewports,
    utilities as coreUtilities,
    getRenderingEngine,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    segmentation,
    // Navigation.
    PanTool,
    ZoomTool,
    StackScrollTool,
    WindowLevelTool,
    CrosshairsTool,
    // The 3D window's primary. Phase 6 uses the same tool for IOS meshes, where it
    // replaces THREE.TrackballControls; the volume render needs it first.
    TrackballRotateTool,
    // Measurement -- the whole point of the migration. Today there is exactly one
    // tool (static/js/viewer_grid.js:88-91) and it is never persisted.
    LengthTool,
    HeightTool,
    AngleTool,
    CobbAngleTool,
    BidirectionalTool,
    ProbeTool,
    RectangleROITool,
    EllipticalROITool,
    CircleROITool,
    // Segmentation editing. Decision #14: brush and eraser mutate voxels.
    BrushTool,
    EraserTool,
    RectangleScissorsTool,
    CircleScissorsTool,
    SphereScissorsTool,
    PaintFillTool,
} from '@cornerstonejs/tools';

import {
    cornerstoneNiftiImageLoader,
    createNiftiImageIdsAndCacheMetadata,
    Enums as niftiEnums,
} from '@cornerstonejs/nifti-volume-loader';

// Labelmap <-> contour <-> surface conversion (decision #11). Used in memory only:
// per docs/cornerstone-future-work.md #7, labelmap is the one canonical form at rest.
import { init as polySegInit } from '@cornerstonejs/polymorphic-segmentation';

// Segmentation accelerator (roadmap Phase 5). Pulls the itk-wasm pipelines that F5
// would otherwise fetch from jsdelivr.
import { interpolate as interpolateLabelmap } from '@cornerstonejs/labelmap-interpolation';

import { initImaging } from '../imaging/runtime/init.js';
import { GRID_VIEWPORT_COUNT } from '../imaging/runtime/config.js';

// F3: the loader throws on a relative URL and reads `.gz` off the pathname only.
import { niftiVolumeImageId, volumeUrl, upgradeLegacyServeUrl } from '../imaging/ids/imageIds.js';

// F1: `modalityScaleNifti` skips the rescale whenever either factor is neutral, so HU
// would be silently wrong. Decision #5 (real modality values, no percent-of-range)
// rests on deriving the LUT ourselves.
import { modalityLutModule, applyModalityLut } from '../imaging/metadata/modalityLutModule.js';

export const SURFACE = 'volume-grid';

export const NAVIGATION_TOOLS = [
    PanTool,
    ZoomTool,
    StackScrollTool,
    WindowLevelTool,
    CrosshairsTool,
];

export const MEASUREMENT_TOOLS = [
    LengthTool,
    HeightTool,
    AngleTool,
    CobbAngleTool,
    BidirectionalTool,
    ProbeTool,
    RectangleROITool,
    EllipticalROITool,
    CircleROITool,
];

export const SEGMENTATION_TOOLS = [
    BrushTool,
    EraserTool,
    RectangleScissorsTool,
    CircleScissorsTool,
    SphereScissorsTool,
    PaintFillTool,
];

export {
    RenderingEngine,
    getRenderingEngine,
    coreEnums,
    toolsEnums,
    niftiEnums,
    volumeLoader,
    imageLoader,
    metaData,
    cache,
    segmentation,
    setVolumesForViewports,
    coreUtilities,
    addTool,
    ToolGroupManager,
    cornerstoneNiftiImageLoader,
    createNiftiImageIdsAndCacheMetadata,
    polySegInit,
    interpolateLabelmap,
    initImaging,
    GRID_VIEWPORT_COUNT,
    niftiVolumeImageId,
    volumeUrl,
    upgradeLegacyServeUrl,
    modalityLutModule,
    applyModalityLut,
};

import { createVolumeGrid, RENDERING_ENGINE_ID, PRIMARY_TOOLS } from '../imaging/grid/viewportManager.js';
import { bootstrapVolumeGrid } from '../imaging/grid/bootstrap.js';
import {
    FIXED_CBCT_LAYOUT,
    FREE_LAYOUT,
    GRID_WINDOWS,
    IMAGE_LOADER_SCHEME,
    ORIENTATIONS,
    VOLUME_ID_SCHEME,
} from '../imaging/grid/layout.js';
import { formatWindow, modalityWindowFromVoiRange, openingVoi, unitFor } from '../imaging/grid/voi.js';
import {
    createGridState,
    setFreeScroll,
    syncTargets,
    windowAt,
} from '../imaging/grid/windowState.js';

/**
 * The tools the grid registers, by the name the tool group knows them under.
 *
 * A map rather than the arrays above because `viewportManager` needs to pick
 * `TrackballRotate` out for the 3D group and bind `Pan`/`Zoom`/`StackScroll`
 * permanently; addressing those by array index would be unreadable and fragile.
 */
export const GRID_TOOLS = {
    Pan: PanTool,
    Zoom: ZoomTool,
    StackScroll: StackScrollTool,
    WindowLevel: WindowLevelTool,
    Crosshairs: CrosshairsTool,
    Length: LengthTool,
    Height: HeightTool,
    Angle: AngleTool,
    CobbAngle: CobbAngleTool,
    Bidirectional: BidirectionalTool,
    Probe: ProbeTool,
    RectangleROI: RectangleROITool,
    EllipticalROI: EllipticalROITool,
    CircleROI: CircleROITool,
    TrackballRotate: TrackballRotateTool,
};

/**
 * Build a volume grid over four DOM elements.
 *
 * @param {object} options
 * @param {HTMLElement[]} options.elements one per window, in window order.
 * @param {object[]} [options.layout] {@link FIXED_CBCT_LAYOUT} or {@link FREE_LAYOUT}.
 * @returns {Promise<object>} the grid handle from `createVolumeGrid`.
 */
export async function mountVolumeGrid({ elements, layout = FIXED_CBCT_LAYOUT }) {
    if (!Array.isArray(elements) || elements.length !== GRID_VIEWPORT_COUNT) {
        throw new Error(
            `The grid needs exactly ${GRID_VIEWPORT_COUNT} elements, got ${elements?.length}.`
        );
    }

    await initImaging();
    // The NIfTI loader is an **image** loader: it serves the per-frame
    // `nifti:<url>?frame=N` ids. Registering it as a *volume* loader routes volume
    // loading into it, where it looks up `imagePlaneModule` for an id that has no
    // per-frame metadata and dies on `const { rows, columns } = imagePlaneModule`.
    // The volume itself is built by the default streaming loader, which any
    // unregistered volume-id scheme falls through to -- hence VOLUME_ID_SCHEME.
    imageLoader.registerImageLoader(IMAGE_LOADER_SCHEME, cornerstoneNiftiImageLoader);

    return createVolumeGrid({
        cornerstone: {
            RenderingEngine,
            coreEnums,
            toolsEnums,
            addTool,
            ToolGroupManager,
            tools: GRID_TOOLS,
            volumeLoader,
            createNiftiImageIdsAndCacheMetadata,
            setVolumesForViewports,
            cache,
        },
        elements,
        layout,
    });
}

/**
 * Start on page load.
 *
 * The `{% cornerstone_entry %}` tag loads this module for its side effects, so the
 * self-start is the side effect. `bootstrapVolumeGrid` returns null on any page without
 * a grid, which is most of them, and reports a real failure into the grid rather than
 * throwing -- a bootstrap that throws here would take the rest of the patient record
 * with it.
 */
const started = bootstrapVolumeGrid({ mount: mountVolumeGrid }).catch((error) => {
    // Belt and braces: bootstrapVolumeGrid already catches its own failures, so
    // reaching here means a bug in the bootstrap itself.
    console.error('The volume grid failed to start:', error);
    return null;
});

export {
    createVolumeGrid,
    bootstrapVolumeGrid,
    started,
    mountVolumeGrid as default,
    RENDERING_ENGINE_ID,
    PRIMARY_TOOLS,
    FIXED_CBCT_LAYOUT,
    FREE_LAYOUT,
    ORIENTATIONS,
    GRID_WINDOWS,
    createGridState,
    setFreeScroll,
    syncTargets,
    windowAt,
    formatWindow,
    modalityWindowFromVoiRange,
    openingVoi,
    unitFor,
};
