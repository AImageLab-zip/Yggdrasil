/**
 * Entry point: photo stacks -- teleradiography and intraoral photos (roadmap Phase 4).
 *
 * Phase 1 builds this bundle; it does not wire it.
 *
 * The calibration rule this surface exists to enforce (roadmap Phase 4): **no
 * `pixelSpacing` unless it is actually known.** Cornerstone then reports lengths in
 * `px` and labels them uncalibrated, which is the honest answer. Fabricating
 * 1 mm/px would report a fiction in millimetres.
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    imageLoader,
    metaData,
    cache,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    PanTool,
    ZoomTool,
    StackScrollTool,
    WindowLevelTool,
    LengthTool,
    AngleTool,
    CobbAngleTool,
    BidirectionalTool,
    ProbeTool,
    RectangleROITool,
    EllipticalROITool,
    CircleROITool,
    ArrowAnnotateTool,
    MagnifyTool,
    ScaleOverlayTool,
} from '@cornerstonejs/tools';

import { initImaging } from '../imaging/runtime/init.js';

export const SURFACE = 'photo-stack';

export const STACK_TOOLS = [
    PanTool,
    ZoomTool,
    StackScrollTool,
    WindowLevelTool,
    LengthTool,
    AngleTool,
    CobbAngleTool,
    BidirectionalTool,
    ProbeTool,
    RectangleROITool,
    EllipticalROITool,
    CircleROITool,
    ArrowAnnotateTool,
    MagnifyTool,
    ScaleOverlayTool,
];

export {
    RenderingEngine,
    coreEnums,
    toolsEnums,
    imageLoader,
    metaData,
    cache,
    coreUtilities,
    addTool,
    ToolGroupManager,
    initImaging,
};
