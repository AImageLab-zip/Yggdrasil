/**
 * Entry point: CBCT + brain volume grid (roadmap Phase 3).
 *
 * Phase 1 builds this bundle; it does not wire it. No template loads this file yet
 * -- `templates/common/patient_detail.html` still loads NiiVue and viewer_grid.js.
 * The imports below are the real surface Phase 3 needs, and they are what pull the
 * three web workers and the ICRPolySeg wasm into the emitted tree, so
 * `npm run verify` has something to check.
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
};
