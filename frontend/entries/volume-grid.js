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
    eventTarget,
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
    utilities as toolsUtilities,
    synchronizers,
    annotation as annotationApi,
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
    // The coloured axes in the corner of the 3D view.
    OrientationMarkerTool,
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

// Phase 8: a stored DICOM series renders through the same volume path as a NIfTI.
//
// `init` rather than the lighter `wadors/register`, deliberately: `register` installs
// the image loader and its metadata provider but **not** the decode web worker, and
// `imageLoader/decodeImageFrame.js` routes *every* transfer syntax through that worker
// -- uncompressed Explicit VR Little Endian included. With only `register`, a series
// downloads correctly and then never decodes, in the browser only.
//
// `metaDataManager` is how the application hands per-frame metadata to that decoder,
// because -- unlike the NIfTI loader -- the wadors loader never fetches it itself.
import { init as initDicomImageLoader } from '@cornerstonejs/dicom-image-loader';
import { metaDataManager as dicomMetaDataManager } from '@cornerstonejs/dicom-image-loader/wadors';

// Labelmap <-> contour <-> surface conversion (decision #11). Imported, exported and
// **not registered**: the import is what pulls the ICRPolySeg wasm into the emitted
// tree so `npm run verify` has something to check, which is why it was added in Phase
// 1. Registering it as a tools add-on is a separate act and nothing here needs one --
// see `mountVolumeGrid` below.
import * as polySeg from '@cornerstonejs/polymorphic-segmentation';

// Segmentation accelerator (roadmap Phase 5). Pulls the itk-wasm pipelines that F5
// would otherwise fetch from jsdelivr.
import { interpolate as interpolateLabelmap } from '@cornerstonejs/labelmap-interpolation';

import { initImaging } from '../imaging/runtime/init.js';
import { areaOnlyConfiguration } from '../imaging/annotations/roiTextLines.js';
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
    dicomMetaDataManager,
    initDicomImageLoader,
    polySeg,
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
    crosshairLinesOnly,
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
    OrientationMarker: OrientationMarkerTool,
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

    // **No polySeg add-on.** It was registered here for one release, to convert the
    // labelmap into a surface for the 3D window -- on the belief that a `volume3d`
    // viewport cannot render a labelmap. It can, so nothing in this grid needs a
    // representation conversion, and registering an add-on nothing asks for would spawn
    // a worker for no reason. `initImaging` still takes `addons`, because that is the
    // only hook tools offers and a later surface would need it. See
    // imaging/grid/segmentation.js.
    await initImaging();

    // The NIfTI loader is an **image** loader: it serves the per-frame
    // `nifti:<url>?frame=N` ids. Registering it as a *volume* loader routes volume
    // loading into it, where it looks up `imagePlaneModule` for an id that has no
    // per-frame metadata and dies on `const { rows, columns } = imagePlaneModule`.
    // The volume itself is built by the default streaming loader, which any
    // unregistered volume-id scheme falls through to -- hence VOLUME_ID_SCHEME.
    imageLoader.registerImageLoader(IMAGE_LOADER_SCHEME, cornerstoneNiftiImageLoader);

    // The `wadors` image loader, for stored DICOM series (Phase 8): an *image* loader
    // serving per-frame ids, with the volume itself still built by the default
    // streaming loader, exactly as above.
    //
    // Placement matters twice. After `initImaging()`, because it asks core for the web
    // worker manager. And before anything is cached, because `init` opens with
    // `cache.purgeCache()` -- calling it later would silently evict a loaded volume.
    initDicomImageLoader({
        // Where the codec wasm actually lives. Without this the decoders fall back to
        // `new URL('@cornerstonejs/codec-charls/decodewasm', import.meta.url)` -- a
        // bare package specifier inside `new URL`, which esbuild copies through
        // untouched and which resolves at runtime to a path that does not exist.
        // Resolved from this module's own URL, the same way every worker and the
        // orientation marker are, so it follows the build directory.
        wasmBasePath: new URL('../codec-wasm/', import.meta.url).href,
    });

    return createVolumeGrid({
        // The ROI tools print the area only. Mean/Max/Min/Std Dev on a CBCT are not
        // Hounsfield -- CBCT greyscale is vendor-dependent and uncalibrated -- and this
        // codebase already refuses to *store* those numbers from the client for exactly
        // that reason. Printing them in the overlay while refusing to store them would be
        // one claim made in two voices.
        // Per-tool configuration, merged from the two modules that own a piece of it:
        // the ROI tools' text lines, and the crosshair's handles.
        toolConfiguration: new Map([
            ...areaOnlyConfiguration(GRID_TOOLS, coreUtilities.roundNumber),
            [GRID_TOOLS.Crosshairs.toolName, crosshairLinesOnly()],
        ]),
        cornerstone: {
            RenderingEngine,
            // So the grid *reuses* an engine the panoramic surface may already have opened
            // under this id rather than displacing it -- see `createVolumeGrid`.
            getRenderingEngine,
            coreEnums,
            toolsEnums,
            // The segmentation overlay listens for SEGMENTATION_RENDERED, which is
            // dispatched globally rather than on an element.
            eventTarget,
            addTool,
            ToolGroupManager,
            tools: GRID_TOOLS,
            // `getOrientationStringLPS` / `invertOrientationStringLPS`: Cornerstone's
            // own, so the overlay letters are its answer and not our table.
            orientationUtilities: toolsUtilities.orientation,
            createVOISynchronizer: synchronizers.createVOISynchronizer,
            annotationState: annotationApi.state,
            annotationVisibility: annotationApi.visibility,
            // The vendored 3D Slicer human figure. Resolved from this module's own URL,
            // the same way the web workers resolve theirs, so it follows the build
            // directory and never reaches raw.githubusercontent.com. See
            // static/vendor/slicer/README.md.
            orientationMarkerUrl: new URL('../orientation/Human.vtp', import.meta.url).href,
            volumeLoader,
            createNiftiImageIdsAndCacheMetadata,
            // The segmentation overlay (brain classes, CBCT teeth). Exported from this
            // module since Phase 3 to pull its workers into the bundle, and injected
            // for the first time here -- `imaging/grid/segmentation.js` reads
            // `addSegmentations`, `addSegmentationRepresentations` and `config`.
            segmentation,
            // Registering a series' metadata is a step the DICOM path has to take
            // explicitly; see imaging/grid/dicomVolume.js.
            dicomMetaDataManager,
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
