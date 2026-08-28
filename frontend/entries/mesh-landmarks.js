/**
 * Entry point: IOS meshes and the landmark tool (roadmap Phase 6).
 *
 * Replaces `static/js/modality_viewers/ios.js` (1539 lines) and, with it, the last of the
 * four frontend stacks this migration exists to remove: the three Three.js r128 CDN tags
 * at `templates/base.html` had exactly one consumer, and it was that file.
 *
 * This is the only module on the surface that imports Cornerstone or vtk.js. Everything
 * interesting lives under `imaging/mesh/`, where it is testable without a GPU; what is
 * here is wiring, and it is deliberately dull.
 *
 * ## The coordinate story, because it is the whole compatibility risk
 *
 * A landmark is stored in `resource_local` coordinates -- one STL's own object space.
 * The legacy viewer rotated each jaw 180 degrees about Y and translated both by the
 * negated centre of their combined bounding box, but stored
 * `mesh.worldToLocal(hit.point)`, which inverts the *full* world matrix -- so the stored
 * numbers are raw STL vertex coordinates and always were.
 *
 * `@cornerstonejs/core`'s `Mesh` applies **no transform** to an STL actor. So as long as
 * nothing here transforms the actors either, a picked world position *is* the value to
 * store and a stored value *is* where the marker goes. There is no conversion in this
 * surface, and there must not be one. `frontend/tests/meshLandmarks.test.js` asserts both
 * halves, and asserts that the upstream class still applies no transform -- a 5.9 bump
 * that started centring meshes would otherwise move every landmark on every historical
 * study, silently, and look entirely fine doing it.
 */

import {
    RenderingEngine,
    Enums as coreEnums,
    cache,
    geometryLoader,
} from '@cornerstonejs/core';

import {
    addTool,
    ToolGroupManager,
    Enums as toolsEnums,
    TrackballRotateTool,
    PanTool,
    ZoomTool,
} from '@cornerstonejs/tools';

import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor.js';
import vtkAxesActor from '@kitware/vtk.js/Rendering/Core/AxesActor.js';
import vtkCellPicker from '@kitware/vtk.js/Rendering/Core/CellPicker.js';
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper.js';
import vtkSphereSource from '@kitware/vtk.js/Filters/Sources/SphereSource.js';

import { initImaging } from '../imaging/runtime/init.js';
import { createMeshViewport } from '../imaging/mesh/meshViewport.js';
import { bootstrapMeshLandmarks } from '../imaging/mesh/bootstrap.js';

export const SURFACE = 'mesh-landmarks';

/** STL is the only mesh format the IOS pipeline produces today. */
export const MESH_TYPE = coreEnums.MeshType.STL;

/**
 * The tools this surface binds.
 *
 * Three, and the Phase 1 stub's list is shorter for it. `ProbeTool` is planar and reads an
 * intensity a surface mesh does not have; `LengthTool` measures in the plane of a camera
 * that is free to rotate, which on a 3D scan is a number with no defensible meaning; and
 * `OrientationMarkerTool` was never wired to a control. A tool nothing can reach is dead
 * weight in a bundle this size, and one that can be reached and answers wrongly is worse.
 *
 * Landmark placement is deliberately *not* a tool. There is no 3D point tool in
 * Cornerstone -- picking a surface is `vtkCellPicker` against the renderer, and the
 * markers are Yggdrasil's own actors, which is what decision #4 requires anyway.
 */
export const MESH_TOOLS = Object.freeze({
    TrackballRotate: TrackballRotateTool,
    Pan: PanTool,
    Zoom: ZoomTool,
});

/**
 * Build the viewport, handing the pure modules everything they need.
 *
 * The two injected bags are the seam: `imaging/mesh/` holds no import of either package,
 * so `meshViewport.js` runs against a fake in `node --test` and the bootstrap runs against
 * a fake viewport.
 */
export async function mountMeshViewport(options) {
    return createMeshViewport({
        ...options,
        cornerstone: {
            RenderingEngine,
            coreEnums,
            toolsEnums,
            geometryLoader,
            cache,
            addTool,
            ToolGroupManager,
            tools: MESH_TOOLS,
        },
        vtk: { vtkCellPicker, vtkSphereSource, vtkMapper, vtkActor, vtkAxesActor },
    });
}

/**
 * Start on import, and never throw into the page.
 *
 * A bootstrap that threw here would take the rest of the patient record with it -- the tab
 * this mounts into is one of several on a page a clinician needs the rest of.
 */
const started = initImaging()
    .then(() => bootstrapMeshLandmarks({ mount: mountMeshViewport }))
    .catch((error) => {
        console.error('The IOS mesh viewer failed to start:', error);
        return null;
    });

export {
    started,
    bootstrapMeshLandmarks,
    createMeshViewport,
    initImaging,
    mountMeshViewport as default,
};
