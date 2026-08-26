/**
 * Entry point: live panoramic CPR (roadmap Phase 7, decision #8).
 *
 * Phase 1 builds this bundle; it does not wire it.
 *
 * Scope boundary that matters: only the **interactive** layer moves to Cornerstone +
 * vtk.js `ImageCPRMapper`. The **baking** layer stays verbatim --
 * `static/js/seg2pano_core.js` and `static/js/worker/seg2pano_worker.js` are
 * untouched, so the derived PNGs collected by `common/export_catalog.py:232-241`
 * keep their exact bytes and stay exportable.
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    volumeLoader,
    cache,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    PanTool,
    ZoomTool,
    WindowLevelTool,
    LengthTool,
    ProbeTool,
    PlanarFreehandROITool,
    SplineROITool,
} from '@cornerstonejs/tools';

// The CPR mapper itself. Imported by path because vtk.js ships one module per class.
import vtkImageCPRMapper from '@kitware/vtk.js/Rendering/Core/ImageCPRMapper.js';
import vtkImageSlice from '@kitware/vtk.js/Rendering/Core/ImageSlice.js';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData.js';

import { initImaging } from '../imaging/runtime/init.js';

export const SURFACE = 'panoramic-cpr';

/** The centreline is authored as a spline; these are the tools that edit it. */
export const CPR_TOOLS = [
    PanTool,
    ZoomTool,
    WindowLevelTool,
    LengthTool,
    ProbeTool,
    PlanarFreehandROITool,
    SplineROITool,
];

export {
    RenderingEngine,
    coreEnums,
    toolsEnums,
    volumeLoader,
    cache,
    coreUtilities,
    addTool,
    ToolGroupManager,
    vtkImageCPRMapper,
    vtkImageSlice,
    vtkPolyData,
    initImaging,
};
