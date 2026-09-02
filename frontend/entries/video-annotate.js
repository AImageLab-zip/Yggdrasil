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
    // Not an enum on `Enums`: it is exported from the package root in its own right, and
    // it is what selects the CanvasActor path for a labelmap on a video viewport. See
    // `video/editor.js:declareCpuImageRendering`.
    ActorRenderMode,
    imageLoader,
    cache,
    // The library's global event target, which is where `ANNOTATION_COMPLETED` is
    // announced -- it is a core export, not a tools one.
    eventTarget,
    metaData,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    annotation,
    segmentation,
    PanTool,
    ZoomTool,
    // Decision #14: the eraser is destructive here too. Only the mask is canonical;
    // revisions are the audit trail, not stroke replay.
    BrushTool,
    EraserTool,
    RectangleScissorsTool,
    CircleScissorsTool,
    // The plain freehand ROI, **not** `PlanarFreehandContourSegmentationTool`: the latter
    // refuses to create an annotation unless a Contour segmentation is active, and this
    // surface stores labelmaps only. The outline is rasterised into the mask and dropped
    // -- see `imaging/video/polygonFill.js`.
    PlanarFreehandROITool,
    LengthTool,
    ArrowAnnotateTool,
    utilities as toolsUtilities,
} from '@cornerstonejs/tools';

import { initImaging } from '../imaging/runtime/init.js';
import { createVideoEditor } from '../imaging/video/editor.js';
import {
    DATA_ELEMENT_ID,
    mountVideoAnnotator,
    readVideoData,
} from '../imaging/video/bootstrap.js';
import { createVideoMetadataProvider } from '../imaging/video/metadata.js';
import { bindVideoControls } from '../imaging/video/pageControls.js';

export const SURFACE = 'video-annotate';

/**
 * The tool classes registered with the library on this surface.
 *
 * Exactly the classes whose `toolName` appears in `VIDEO_TOOL_NAMES`, and no more.
 * `LivewireContourSegmentationTool` was registered here and added to no tool group and
 * named by no toolbar button -- a registration nothing could reach, which reads as a
 * live feature. `frontend/tests/videoToolNames.test.js` pins the two lists to each other.
 */
export const VIDEO_TOOLS = [
    PanTool,
    ZoomTool,
    BrushTool,
    EraserTool,
    RectangleScissorsTool,
    CircleScissorsTool,
    PlanarFreehandROITool,
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
                    ActorRenderMode,
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
                    // The brush's size is tool-group state, not viewport state, and the
                    // helper that writes it also invalidates the cursor -- setting the
                    // configuration by hand leaves the old circle on screen.
                    setBrushSizeForToolGroup: toolsUtilities.segmentation.setBrushSizeForToolGroup,
                    cache,
                    // `ANNOTATION_COMPLETED` is announced on the library's global event
                    // target, not on the element, and the finished outline has to be
                    // removed from the annotation store once it has been rasterised.
                    eventTarget,
                    annotationState: annotation.state,
                },
                options
            ),
    });
}

if (typeof window !== 'undefined') {
    // `surface` is published rather than returned so anything on the page can wait for
    // it without importing this module. It is `null` when the bootstrap declined, which
    // is a state the page has to distinguish from "not mounted yet".
    window.YggVideoAnnotate = { start, SURFACE, surface: undefined };
    const run = () =>
        start()
            .then((surface) => {
                window.YggVideoAnnotate.surface = surface;
                // Wired here rather than from a `<script type="module">` in the
                // template. The page used to poll `window.YggVideoAnnotate.surface`
                // every 50 ms for it; the entry has the surface the moment it exists
                // and the binder is a module a test can drive.
                if (surface) {
                    bindVideoControls({ surface, doc: document });
                }
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
