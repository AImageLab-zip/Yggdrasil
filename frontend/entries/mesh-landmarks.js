/**
 * Entry point: IOS meshes and the landmark tool (roadmap Phase 6).
 *
 * Phase 1 builds this bundle; it does not wire it. Phase 6 deletes
 * `static/js/modality_viewers/ios.js` (1539 lines) and the three Three.js r128 CDN
 * tags at `templates/base.html:36-39`, replacing `THREE.TrackballControls` with
 * `TrackballRotateTool`.
 *
 * Coordinate-system note (roadmap Phase 2): IOS landmarks come from `worldToLocal`
 * against a mesh and have **no patient frame**. Their coordinate system is
 * `resource_local`; calling them `patient_world` would be false.
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    cornerstoneMeshLoader,
    geometryLoader,
    cache,
    utilities as coreUtilities,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    TrackballRotateTool,
    PanTool,
    ZoomTool,
    ProbeTool,
    LengthTool,
    OrientationMarkerTool,
} from '@cornerstonejs/tools';

import { initImaging } from '../imaging/runtime/init.js';

export const SURFACE = 'mesh-landmarks';

/** STL is the only mesh format the IOS pipeline produces today. */
export const MESH_TYPE = coreEnums.MeshType.STL;

export const MESH_TOOLS = [
    TrackballRotateTool,
    PanTool,
    ZoomTool,
    ProbeTool,
    LengthTool,
    OrientationMarkerTool,
];

export {
    RenderingEngine,
    coreEnums,
    toolsEnums,
    cornerstoneMeshLoader,
    geometryLoader,
    cache,
    coreUtilities,
    addTool,
    ToolGroupManager,
    initImaging,
};
