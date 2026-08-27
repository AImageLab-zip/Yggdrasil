/**
 * Entry point: photo stacks -- teleradiography and intraoral photos (roadmap Phases 4+5).
 *
 * The rule this surface exists to enforce: **no `pixelSpacing` unless it is actually
 * known.** Cornerstone then reports lengths in `px` and labels them uncalibrated, which
 * is the honest answer for a photograph. Fabricating 1 mm/px would report a fiction in
 * millimetres that nothing downstream could tell from a real measurement.
 *
 * This file is the only place Cornerstone and the surface's own modules meet. Everything
 * interesting lives under `imaging/photos/` and `imaging/loaders/`, where it is testable
 * without a GPU; what is here is wiring, and it is deliberately dull.
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    imageLoader,
    metaData,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    annotation as annotationApi,
    PanTool,
    ZoomTool,
    StackScrollTool,
    WindowLevelTool,
    LengthTool,
    AngleTool,
    CobbAngleTool,
    BidirectionalTool,
    RectangleROITool,
    EllipticalROITool,
    CircleROITool,
    ArrowAnnotateTool,
} from '@cornerstonejs/tools';

import { initImaging } from '../imaging/runtime/init.js';
import {
    WEB_IMAGE_SCHEME,
    createWebImageLoader,
} from '../imaging/loaders/webImageLoader.js';
import {
    PHOTO_METADATA_PRIORITY,
    createPhotoMetadataProvider,
} from '../imaging/photos/metadataProvider.js';
import {
    PHOTO_MEASUREMENT_TOOLS,
    createPhotoStack,
} from '../imaging/photos/stackViewport.js';
import { bootstrapPhotoStack } from '../imaging/photos/bootstrap.js';

export const SURFACE = 'photo-stack';

/**
 * The tools this surface binds, by the name `stackViewport.js` asks for them by.
 *
 * `ProbeTool` is deliberately absent: it reads an intensity, and on a photograph that is
 * an sRGB display value with no clinical meaning, so offering it would invite a reading
 * nobody should take. `ScaleOverlayTool` and `MagnifyTool` are absent for a duller
 * reason -- neither is wired to a control yet, and a tool nothing can reach is dead
 * weight in a 4 MB bundle.
 */
export const STACK_TOOLS = Object.freeze({
    Pan: PanTool,
    Zoom: ZoomTool,
    StackScroll: StackScrollTool,
    WindowLevel: WindowLevelTool,
    Length: LengthTool,
    Angle: AngleTool,
    CobbAngle: CobbAngleTool,
    Bidirectional: BidirectionalTool,
    RectangleROI: RectangleROITool,
    EllipticalROI: EllipticalROITool,
    CircleROI: CircleROITool,
    ArrowAnnotate: ArrowAnnotateTool,
});

/** Registered once per page, not once per mount. */
let registered = false;

/**
 * Register the loader and the metadata provider, and build the viewport.
 *
 * The registry is a live `Map` the bootstrap owns: a calibration writes into it and the
 * stack is then reset so Cornerstone re-reads the module. Handing the provider a snapshot
 * would leave a freshly calibrated image still reporting pixels until a reload.
 *
 * @param {object} options
 * @param {HTMLElement} options.element
 * @param {Map<string, object>} options.registry imageId -> image record.
 */
export async function mountPhotoStack({ element, registry }) {
    await initImaging();

    if (!registered) {
        imageLoader.registerImageLoader(
            WEB_IMAGE_SCHEME,
            createWebImageLoader({
                // Cornerstone's own, so the image's voxelManager is built the way
                // `ensureVoxelManager` would have built it -- see webImageLoader.js for
                // why the loader has to build it at all.
                voxelManagerFactory: coreUtilities.VoxelManager.createImageVoxelManager,
            })
        );
        metaData.addProvider(createPhotoMetadataProvider(registry), PHOTO_METADATA_PRIORITY);
        registered = true;
    }

    return createPhotoStack({
        cornerstone: {
            RenderingEngine,
            coreEnums,
            toolsEnums,
            addTool,
            ToolGroupManager,
            tools: STACK_TOOLS,
            annotationState: annotationApi.state,
            annotationVisibility: annotationApi.visibility,
        },
        element,
    });
}

/**
 * Start on import, and never throw into the page.
 *
 * A bootstrap that threw here would take the rest of the patient record with it -- the
 * tab this mounts into is one of several on a page a clinician needs the rest of.
 */
const started = bootstrapPhotoStack({ mount: mountPhotoStack }).catch((error) => {
    console.error('The photo stack failed to start:', error);
    return null;
});

export {
    started,
    bootstrapPhotoStack,
    createPhotoStack,
    mountPhotoStack as default,
    PHOTO_MEASUREMENT_TOOLS,
    WEB_IMAGE_SCHEME,
};
