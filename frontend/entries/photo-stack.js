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
    LabelTool,
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
import { areaOnlyConfiguration } from '../imaging/annotations/roiTextLines.js';
import { askForText } from '../imaging/photos/dialog.js';

export const SURFACE = 'photo-stack';

/**
 * The tools this surface binds, by the name `stackViewport.js` asks for them by.
 *
 * `ProbeTool` is deliberately absent: it reads an intensity, and on a photograph that is
 * an sRGB display value with no clinical meaning, so offering it would invite a reading
 * nobody should take. `ScaleOverlayTool` and `MagnifyTool` are absent for a duller
 * reason -- neither is wired to a control yet, and a tool nothing can reach is dead
 * weight in a 4 MB bundle.
 *
 * `LabelTool` replaces `ArrowAnnotateTool`: a named point says *what* it is pointing at,
 * where an arrow leaves the reader inferring. It is also the mapped one -- an arrow had no
 * entry in `annotations/adapters/cornerstone.py`, so a single one made the adapter refuse
 * the entire save.
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
    Label: LabelTool,
});

/**
 * Per-tool configuration, built once.
 *
 * Two things, both about what the overlay says rather than what is stored:
 *
 * - The ROI tools print the **area only**. Upstream's default adds Mean, Max, Min and Std
 *   Dev, which on a photograph are statistics about the JPEG and on a CBCT are not
 *   Hounsfield -- and which this codebase already refuses to *store* from the client for
 *   that reason. Printing them while refusing to store them is one claim in two voices.
 * - `LabelTool` asks for its text through the app's own dialog. Its default
 *   `getTextCallback` calls `prompt()`, which is unstyled, sits outside the page, and can
 *   be permanently suppressed by the browser -- after which naming a point silently stops
 *   working with no error to explain it.
 */
function toolConfiguration() {
    const configuration = areaOnlyConfiguration(STACK_TOOLS, coreUtilities.roundNumber);
    const getTextCallback = (done) =>
        askForText({
            title: 'Name this point',
            message: 'What is this point? The name is what the marker shows.',
            placeholder: 'e.g. Nasion',
        }).then((text) => done(text ?? undefined));

    configuration.set(LabelTool.toolName, {
        getTextCallback,
        // Double-clicking an existing marker renames it, through the same dialog.
        changeTextCallback: (data, event, done) =>
            askForText({
                title: 'Rename this point',
                message: 'What is this point?',
                initial: data?.label ?? '',
            }).then((text) => done(text ?? undefined)),
    });
    return configuration;
}

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

    const stack = createPhotoStack({
        toolConfiguration: toolConfiguration(),
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

    // Cornerstone's own converters, handed out rather than re-implemented. They are exact
    // inverses derived from the same `imagePlaneModule` the viewport renders from; a third
    // implementation would be the one that disagrees, silently, by half a pixel -- both of
    // these offset by half a spacing and it is easy not to notice.
    return {
        stack,
        worldToImage: coreUtilities.worldToImageCoords,
        imageToWorld: coreUtilities.imageToWorldCoords,
    };
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
