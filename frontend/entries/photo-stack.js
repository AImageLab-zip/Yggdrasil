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
    eventTarget,
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
    SplineROITool,
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
import {
    INTRAORAL_CONTROL_IDS,
    PHOTO_CONTROL_IDS,
} from '../imaging/photos/controls.js';
import { areaOnlyConfiguration } from '../imaging/annotations/roiTextLines.js';
import { askForText } from '../imaging/photos/dialog.js';
import {
    KONVA_TENSION,
    toothSplineConfiguration,
} from '../imaging/annotations/tensionSpline.js';

export const SURFACE = 'photo-stack';

/**
 * The tooth-outline tool: `SplineROITool` drawing Konva's tension curve.
 *
 * Subclassed only to give it its own `toolName`. Sharing `'SplineROI'` with a general
 * spline tool would mean one entry in `toolOptions`, so binding the measurement spline
 * would bind tooth outlining with it -- and every stored contour would come back under a
 * name the segmentation reader does not recognise. Upstream's own
 * `CatmullRomSplineROI`/`CardinalSplineROI` names exist for the same reason.
 */
class ToothOutlineTool extends SplineROITool {}
ToothOutlineTool.toolName = 'ToothOutline';

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
    ToothOutline: ToothOutlineTool,
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

    configuration.set(ToothOutlineTool.toolName, {
        spline: toothSplineConfiguration(),
        // A tooth outline is a segmentation, not a measurement. Its area is a number about
        // a photograph -- meaningless without calibration and misleading with it, since a
        // crown's outline is not its cross-section. Turning stats off is also what makes
        // the label below the only text, rather than an area line above it.
        calculateStats: false,
        // A ring, always. An open tooth outline is not a shape the model can store: the
        // adapter writes `closed=True` unconditionally, so allowing one here would let a
        // user draw something the server silently closes for them.
        allowOpenSplines: false,
        /**
         * The overlay text: the FDI code, and nothing else.
         *
         * Upstream's `defaultGetTextLines` destructures `data.cachedStats[targetId]`
         * without a guard, which with `calculateStats: false` is `undefined` -- a
         * `TypeError` on the first render rather than an empty label. So this is required,
         * not a preference.
         */
        getTextLines: (data) => (data?.label ? [data.label] : []),
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
export async function mountPhotoStack({ element, registry, instanceId = 'stack' }) {
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
            imageToWorld: coreUtilities.imageToWorldCoords,
            uuid: coreUtilities.uuidv4,
        },
        element,
        instanceId,
    });

    // Cornerstone's own converters, handed out rather than re-implemented. They are exact
    // inverses derived from the same `imagePlaneModule` the viewport renders from; a third
    // implementation would be the one that disagrees, silently, by half a pixel -- both of
    // these offset by half a spacing and it is easy not to notice.
    return {
        stack,
        worldToImage: coreUtilities.worldToImageCoords,
        imageToWorld: coreUtilities.imageToWorldCoords,
        /** Everything `toothSegmentation.js` needs, so the bootstrap holds no Cornerstone. */
        segmentation: {
            toolName: ToothOutlineTool.toolName,
            splineType: KONVA_TENSION,
            worldToImage: coreUtilities.worldToImageCoords,
            imageToWorld: coreUtilities.imageToWorldCoords,
            readAnnotations: () => stack.readAnnotations(),
            removeAnnotation: (uid) => annotationApi.state.removeAnnotation(uid),
            /**
             * Subscribe to the outline events, and return the unsubscriber.
             *
             * `ANNOTATION_COMPLETED` and `ANNOTATION_MODIFIED` are both routed to the same
             * handler because the editor's response is the same for both: derive the action,
             * re-read the map, queue a save. Distinguishing them would duplicate that.
             */
            onAnnotationChange: ({ onChange, onRemoved }) => {
                const changed = (event) => onChange?.(event?.detail?.annotation);
                const removed = (event) => onRemoved?.(event?.detail?.annotation);
                eventTarget.addEventListener(toolsEnums.Events.ANNOTATION_COMPLETED, changed);
                eventTarget.addEventListener(toolsEnums.Events.ANNOTATION_MODIFIED, changed);
                eventTarget.addEventListener(toolsEnums.Events.ANNOTATION_REMOVED, removed);
                return () => {
                    eventTarget.removeEventListener(toolsEnums.Events.ANNOTATION_COMPLETED, changed);
                    eventTarget.removeEventListener(toolsEnums.Events.ANNOTATION_MODIFIED, changed);
                    eventTarget.removeEventListener(toolsEnums.Events.ANNOTATION_REMOVED, removed);
                };
            },
        },
    };
}

/**
 * Start on import, and never throw into the page.
 *
 * A bootstrap that threw here would take the rest of the patient record with it -- the
 * tab this mounts into is one of several on a page a clinician needs the rest of.
 */
/**
 * The surfaces this entry mounts, one per payload the page provides.
 *
 * Two of them, and they are the same code: teleradiography, and the intraoral photographs
 * with tooth segmentation switched on. Each gets its own DOM ids, its own rendering engine
 * and its own tool group, because a patient-detail page carries both at once -- only one is
 * visible, but both mount, so hidden-tab state is right the moment it is shown.
 *
 * A page with neither payload mounts nothing and says so.
 */
export const SURFACES = Object.freeze([
    { dataElementId: 'photoStackData', ids: PHOTO_CONTROL_IDS, instanceId: 'stack' },
    { dataElementId: 'intraoralStackData', ids: INTRAORAL_CONTROL_IDS, instanceId: 'intraoral' },
]);

/**
 * Start on import, and never throw into the page.
 *
 * A bootstrap that threw here would take the rest of the patient record with it -- the tab
 * this mounts into is one of several on a page a clinician needs the rest of. Each surface
 * is caught separately, so a broken intraoral payload cannot take teleradiography down.
 */
const started = Promise.all(
    SURFACES.map((surface) =>
        bootstrapPhotoStack({ mount: mountPhotoStack, ...surface }).catch((error) => {
            console.error(`The ${surface.instanceId} photo stack failed to start:`, error);
            return null;
        })
    )
);

export {
    started,
    bootstrapPhotoStack,
    createPhotoStack,
    mountPhotoStack as default,
    PHOTO_MEASUREMENT_TOOLS,
    WEB_IMAGE_SCHEME,
};
