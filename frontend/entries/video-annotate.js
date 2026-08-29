/**
 * Entry point: laparoscopy video annotation (roadmap Phase 10).
 *
 * Wired in Phase 10. Two contracts this surface does not break:
 *
 *   - **Decision #9**: the Magic Tool keeps its WebSocket GPU worker untouched
 *     (`laparoscopy_annotator_worker.js` and the Django proxies). Only the *sink*
 *     changes -- a Cornerstone labelmap instead of a Konva polygon, in
 *     `imaging/video/magicSink.js`. `@cornerstonejs/ai` stays deferred, see
 *     `docs/cornerstone-future-work.md` #1.
 *   - **Decision #15**: NPZ export stays byte-compatible, regenerated from labelmaps
 *     rather than replayed through PIL. That half is server-side and already shipped.
 *
 * `VideoViewport` renders the video and carries the labelmaps -- `addImages` builds a
 * `CanvasActor` per labelmap, which is exactly the path the tools' stack labelmap plan
 * takes. `imaging/video/metadata.js` records the wrong turn taken on the way to that,
 * and `frontend/tests/videoLabelmapSupport.test.js` pins the facts it rests on.
 *
 * Times are integer milliseconds throughout (roadmap Phase 2, `AnnotationSelector`).
 */

import {
    RenderingEngine,
    Enums as coreEnums,
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
    // Decision #14: the eraser is destructive here too. Only the mask is canonical;
    // revisions are the audit trail, not stroke replay.
    BrushTool,
    EraserTool,
    RectangleScissorsTool,
    CircleScissorsTool,
    PlanarFreehandContourSegmentationTool,
    LivewireContourSegmentationTool,
    LengthTool,
    ArrowAnnotateTool,
} from '@cornerstonejs/tools';

import { initImaging } from '../imaging/runtime/init.js';
import { createVideoEditor } from '../imaging/video/editor.js';
import {
    DATA_ELEMENT_ID,
    mountVideoAnnotator,
    readVideoData,
} from '../imaging/video/bootstrap.js';
import { createVideoMetadataProvider } from '../imaging/video/metadata.js';

export const SURFACE = 'video-annotate';

export const VIDEO_TOOLS = [
    PanTool,
    ZoomTool,
    BrushTool,
    EraserTool,
    RectangleScissorsTool,
    CircleScissorsTool,
    PlanarFreehandContourSegmentationTool,
    LivewireContourSegmentationTool,
    LengthTool,
    ArrowAnnotateTool,
];

/**
 * Register the metadata provider and mount the annotator.
 *
 * The provider answers **synchronously** -- `loadVideoStreamMetadata` calls it inline --
 * so everything it needs comes off the page payload the server already rendered. It is
 * registered before the viewport is created, because the viewport asks the moment it is
 * handed an imageId.
 */
export async function start() {
    await initImaging();

    const data = readVideoData(document, DATA_ELEMENT_ID);
    if (!data) {
        return mountVideoAnnotator({ createEditor: null });
    }

    metaData.addProvider(
        createVideoMetadataProvider((url) =>
            url === data.videoUrl || url.endsWith(data.videoUrl)
                ? {
                      url: data.videoUrl,
                      width: data.width,
                      height: data.height,
                      fps: data.fps,
                      numberOfFrames: data.frameCount,
                  }
                : null
        ),
        // Above the default providers: nothing else knows this scheme, and a lower
        // priority would let a generic provider answer `undefined` first.
        10000
    );

    return mountVideoAnnotator({
        createEditor: (options) =>
            createVideoEditor(
                {
                    RenderingEngine,
                    coreEnums,
                    toolsEnums,
                    segmentation,
                    SegmentationRepresentations: toolsEnums.SegmentationRepresentations,
                    ToolGroupManager,
                    addTool,
                    tools: VIDEO_TOOLS,
                    // Reached through the namespace: the helper is exported from
                    // `loaders/imageLoader.js` but not re-exported by name at the
                    // package root, and esbuild is right to refuse the named import.
                    createAndCacheDerivedLabelmapImages:
                        imageLoader.createAndCacheDerivedLabelmapImages,
                    cache,
                },
                options
            ),
    });
}

if (typeof window !== 'undefined') {
    // `surface` is published rather than returned so the page's glue can wait for it
    // without importing this module. It is `null` when the bootstrap declined, which is
    // a state the page has to distinguish from "not mounted yet".
    window.YggVideoAnnotate = { start, SURFACE, surface: undefined };
    const run = () =>
        start()
            .then((surface) => {
                window.YggVideoAnnotate.surface = surface;
            })
            .catch((error) => {
                // Never throw into the page. A blank viewer that reports nothing is
                // indistinguishable from one that failed -- the lesson the grid's first
                // version taught.
                console.error('[ygg-video] failed to mount', error);
                window.YggVideoAnnotate.surface = null;
            });
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', run);
    } else {
        run();
    }
}

export {
    RenderingEngine,
    coreEnums,
    toolsEnums,
    imageLoader,
    cache,
    coreUtilities,
    segmentation,
    addTool,
    ToolGroupManager,
    initImaging,
};
