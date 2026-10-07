/**
 * Entry point: image segmentation on a video frame (the laparoscopy page's Annotation Mode).
 *
 * Wiring only, like `photo-stack.js`: this is the one place Cornerstone and the surface's
 * own modules meet. The surface is built when the annotator clicks Annotation Mode, not on
 * page load -- a rendering engine costs a WebGL context, and most visits to a patient page
 * are not annotating.
 *
 * The page already carries a `<video>`; this mounts nothing until asked, and a missing
 * probe or markup leaves that player exactly as it was.
 */

import {
    RenderingEngine,
    getEnabledElement,
    Enums as coreEnums,
    eventTarget,
    imageLoader,
    cache,
    metaData,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    segmentation,
    PanTool,
    ZoomTool,
    BrushTool,
    RectangleScissorsTool,
    drawing,
    cursors,
    utilities as toolsUtilities,
} from '@cornerstonejs/tools';
import BrushStrategy from '@cornerstonejs/tools/tools/segmentation/strategies/BrushStrategy';
import compositions from '@cornerstonejs/tools/tools/segmentation/strategies/compositions/index';
import { StrategyCallbacks } from '@cornerstonejs/tools/enums';

import { initImaging } from '../imaging/runtime/init.js';
import { enableTwoFingerNavigation } from '../imaging/runtime/touch.js';
import { askForText } from '../imaging/photos/dialog.js';
import {
    PHOTO_METADATA_PRIORITY,
    createPhotoMetadataProvider,
    registerPhotoRegistry,
} from '../imaging/photos/metadataProvider.js';
import { bootstrapSegmentation } from '../imaging/segment/bootstrap.js';
import {
    FRAME_IMAGE_SCHEME,
    createFrameImageLoader,
} from '../imaging/segment/frameImageLoader.js';
import { openSession } from '../imaging/segment/session.js';
import { createSegmentTools } from '../imaging/segment/tools.js';

/** Frames the loader can answer for, and the metadata the provider describes them with. */
const frames = new Map();
const records = new Map();

let instanceCounter = 0;
let prepared = null;

/**
 * One-time Cornerstone setup for the page.
 *
 * Do not set `overwriteMode` here: any value switches the brush to "lazy" editing, which
 * collects the whole stroke and paints it on mouse-up with a per-pixel test against every
 * point of the stroke -- a long stroke freezes the page. Each label is its own
 * segmentation, so nothing needs protecting from overwriting.
 */
function prepare() {
    prepared ??= (async () => {
        await initImaging();
        registerPhotoRegistry(records);
        imageLoader.registerImageLoader(
            FRAME_IMAGE_SCHEME,
            createFrameImageLoader({
                frames,
                voxelManagerFactory: coreUtilities.VoxelManager.createImageVoxelManager,
            })
        );
        metaData.addProvider(createPhotoMetadataProvider(), PHOTO_METADATA_PRIORITY);

        const { classes, configuration } = createSegmentTools({
            BrushTool,
            RectangleScissorsTool,
                    BrushStrategy,
            compositions,
            StrategyCallbacks,
            transformWorldToIndex: coreUtilities.transformWorldToIndex,
            getEnabledElement,
            drawing,
            resetElementCursor: cursors.elementCursor.resetElementCursor,
            triggerAnnotationRender: toolsUtilities.triggerAnnotationRenderForViewportIds,
        });
        for (const tool of [...classes, PanTool, ZoomTool]) {
            addTool(tool);
        }
        return {
            RenderingEngine,
            coreEnums,
            toolsEnums,
            eventTarget,
            imageLoader,
            cache,
            ToolGroupManager,
            segmentation,
            HistoryMemo: coreUtilities.HistoryMemo,
            Pan: PanTool,
            Zoom: ZoomTool,
            toolConfiguration: configuration,
            enableTwoFingerNavigation,
            setBrushSize: toolsUtilities.segmentation.setBrushSizeForToolGroup,
        };
    })();
    return prepared;
}

async function start() {
    try {
        await bootstrapSegmentation({
            doc: document,
            ask: askForText,
            async openSession(options) {
                const cornerstone = await prepare();
                instanceCounter += 1;
                return openSession({
                    ...options,
                    cornerstone,
                    instanceId: `s${instanceCounter}`,
                    frames,
                    records,
                });
            },
        });
    } catch (error) {
        console.error(`[ygg-segment] could not start: ${error.message}`);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
} else {
    start();
}
