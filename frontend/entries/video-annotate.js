/**
 * Entry point: laparoscopy video annotation (roadmap Phase 10).
 *
 * Phase 1 builds this bundle; it does not wire it. Phase 10 migrates last, and
 * removes both Konva CDN tags (`templates/common/patient_detail.html:52`,
 * `templates/laparoscopy/patient_detail_content.html:371`).
 *
 * Two contracts this surface must not break:
 *   - Decision #9: the Magic Tool keeps its WebSocket GPU worker untouched
 *     (`laparoscopy_annotator_worker.js` and the Django proxies). Only the *sink*
 *     changes -- Cornerstone labelmap state instead of Konva polygons. `@cornerstonejs/ai`
 *     stays deferred, see docs/cornerstone-future-work.md #1.
 *   - Decision #15: NPZ export stays byte-compatible, but is regenerated from
 *     labelmaps rather than replayed through PIL.
 *
 * Times are integer milliseconds throughout (roadmap Phase 2, `AnnotationSelector`).
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    imageLoader,
    cache,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    segmentation,
    PanTool,
    ZoomTool,
    // Decision #14: the eraser becomes destructive here too. Only the mask is
    // canonical; revisions are the audit trail, not stroke replay.
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
