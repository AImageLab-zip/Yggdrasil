/**
 * The axial slice the arch is drawn on.
 *
 * Replaces the Konva stage at `cbct_panorex_editor.js:383-508`: an image layer, a mask
 * layer, a tension-free polyline and ten hand-rolled draggable groups with their own
 * proximity grabbing, cursor handling and counter-scaling.
 *
 * What it becomes is an ordinary Cornerstone axial viewport on the volume the grid already
 * loaded, plus one open `SplineROITool` annotation whose control points *are* the arch. The
 * pan, the zoom and the handle dragging come with the tool; what stays here is the two
 * things that are specific to a panoramic.
 *
 * **The curve is the baker's.** `ArchSpline` -- registered under its own type key -- draws
 * the centripetal Catmull-Rom `seg2pano_core.js` fits through, not Cornerstone's uniform
 * one. See that module for the measurements.
 *
 * **The mask is an actor, not an overlay.** The reader needs to see the mandible the arch
 * was fitted to. Drawing it on a canvas positioned over the viewport would put the
 * mask's placement in one file and the viewport's transform in another -- the untested
 * interface Phase 5 shipped four defects through. It is a `vtkImageSlice` built from the
 * worker's own cleaned mask and placed by the volume's own affine, so there is one owner
 * of where it goes.
 */

import { ARCH_SPLINE, ArchSpline, archSplineConfiguration } from './archSpline.js';

/** The viewport this surface owns on the shared engine. */
export const VIEWPORT_ID = 'ygg-panoramic-axial';

/** Its tool group. Separate from the grid's: these tools are not those tools. */
export const TOOL_GROUP_ID = 'ygg-panoramic';

/** The actor uid for the mandible mask, so a Z change replaces rather than stacks. */
export const MASK_ACTOR_UID = 'panoramic-mask';

/** The mask's colour, matching the Konva editor's `rgba(91, 141, 239, 100)`. */
export const MASK_COLOR = Object.freeze([91 / 255, 141 / 255, 239 / 255]);
export const MASK_OPACITY = 100 / 255;

/**
 * Build the mask's image data: one slice, in the volume's own frame.
 *
 * The worker returns the *cleaned* mask -- closed, largest component, holes filled --
 * because that is what the polynomial was fitted through. Showing the raw labels instead
 * would show the reader a different thing from the one the arch answers to.
 *
 * @param {object} options
 * @param {Uint8Array} options.mask width*height, 0 or 1.
 * @param {object} options.descriptor the RAS descriptor.
 * @param {number} options.sliceIndex
 * @param {object} options.vtk `{vtkImageData, vtkDataArray}`.
 */
export function maskImageData({ mask, descriptor, sliceIndex, vtk }) {
    const { vtkImageData, vtkDataArray } = vtk;
    const { width, height } = descriptor.dimensions;
    const image = vtkImageData.newInstance();
    image.setDimensions(width, height, 1);
    // Spacing and origin from the affine's own columns, so the mask lands exactly where
    // the slice it was computed from is -- rather than at an origin this module invents.
    const affine = descriptor.affine;
    image.setSpacing(
        Math.hypot(affine[0][0], affine[1][0], affine[2][0]),
        Math.hypot(affine[0][1], affine[1][1], affine[2][1]),
        Math.hypot(affine[0][2], affine[1][2], affine[2][2])
    );
    image.setOrigin(
        -(affine[0][2] * sliceIndex + affine[0][3]),
        -(affine[1][2] * sliceIndex + affine[1][3]),
        affine[2][2] * sliceIndex + affine[2][3]
    );
    image.getPointData().setScalars(
        vtkDataArray.newInstance({ name: 'mandible', numberOfComponents: 1, values: mask })
    );
    return image;
}

/**
 * Mount the axial editor.
 *
 * @param {object} options
 * @param {HTMLElement} options.element
 * @param {object} options.cornerstone injected: `{renderingEngine, coreEnums, toolsEnums,
 *   addTool, ToolGroupManager, annotation, tools, setVolumesForViewports}`.
 * @param {object} options.vtk injected: `{vtkImageData, vtkDataArray, vtkImageMapper,
 *   vtkImageSlice}`.
 * @param {(controlPoints: number[][]) => void} [options.onArchEdited] fired on drag end.
 * @param {(controlPoints: number[][]) => void} [options.onArchDragged] fired continuously.
 */
export function createArchViewport({
    element, cornerstone, vtk, onArchEdited = () => {}, onArchDragged = () => {},
}) {
    const {
        renderingEngine, coreEnums, toolsEnums, addTool, ToolGroupManager, annotation,
        tools, setVolumesForViewports,
    } = cornerstone;

    renderingEngine.enableElement({
        viewportId: VIEWPORT_ID,
        type: coreEnums.ViewportType.ORTHOGRAPHIC,
        element,
        defaultOptions: { orientation: coreEnums.OrientationAxis.AXIAL, background: [0, 0, 0] },
    });
    const viewport = renderingEngine.getViewport(VIEWPORT_ID);

    let toolGroup = null;
    let archUid = null;
    let maskActor = null;

    /**
     * Bind the tools.
     *
     * `SplineROITool` is configured for **open** splines only. An arch runs condyle to
     * condyle; closing it draws a loop through the tongue, and the endpoint refuses the
     * result anyway.
     */
    function bindTools() {
        if (toolGroup) {
            return toolGroup;
        }
        for (const tool of Object.values(tools)) {
            addTool(tool);
        }
        toolGroup = ToolGroupManager.createToolGroup(TOOL_GROUP_ID);
        toolGroup.addViewport(VIEWPORT_ID, renderingEngine.id);
        toolGroup.addTool(tools.PanTool.toolName);
        toolGroup.addTool(tools.ZoomTool.toolName);
        toolGroup.addTool(tools.SplineROITool.toolName, {
            spline: archSplineConfiguration(),
        });
        toolGroup.setToolActive(tools.SplineROITool.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Primary }],
        });
        toolGroup.setToolActive(tools.PanTool.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Auxiliary }],
        });
        toolGroup.setToolActive(tools.ZoomTool.toolName, {
            bindings: [{ mouseButton: toolsEnums.MouseBindings.Secondary }],
        });
        return toolGroup;
    }

    async function setVolume(volumeId) {
        await setVolumesForViewports(renderingEngine, [{ volumeId }], [VIEWPORT_ID]);
        bindTools();
    }

    /**
     * Move to the slice the arch is on.
     *
     * `setViewReference({sliceIndex})`, which is the supported way -- there is no
     * `setSliceIndex` on a volume viewport in 5.8.2, and calling one optionally would have
     * left the arch drawn for one slice while the image showed another, silently.
     */
    function showSlice(sliceIndex) {
        viewport.setViewReference({ sliceIndex });
        viewport.render();
    }

    /**
     * Draw the arch, replacing whatever was there.
     *
     * The annotation is rebuilt rather than mutated: the tool caches a spline instance per
     * annotation and keyed on its control points, and editing the array in place leaves
     * the drawn curve one generation behind the handles.
     */
    function setArch(controlPoints, worldFor) {
        if (archUid) {
            annotation.state.removeAnnotation(archUid);
            archUid = null;
        }
        const points = controlPoints.map(worldFor);
        const drawn = {
            highlighted: true,
            invalidated: true,
            metadata: {
                toolName: tools.SplineROITool.toolName,
                viewPlaneNormal: viewport.getCamera().viewPlaneNormal,
                viewUp: viewport.getCamera().viewUp,
                referencedImageId: undefined,
            },
            data: {
                handles: { points, activeHandleIndex: null, textBox: { hasMoved: false } },
                // Open, and drawn by the arch spline -- see `archSpline.js`.
                spline: { type: ARCH_SPLINE, instance: new ArchSpline({ controlPoints: points }) },
                contour: { closed: false },
                label: '',
                cachedStats: {},
            },
        };
        archUid = annotation.state.addAnnotation(drawn, element);
        viewport.render();
        return archUid;
    }

    /** Put the cleaned mandible mask on the slice, replacing the previous one. */
    function setMask(mask, descriptor, sliceIndex) {
        const { vtkImageMapper, vtkImageSlice } = vtk;
        if (maskActor) {
            viewport.removeActors?.([MASK_ACTOR_UID]);
            maskActor = null;
        }
        if (!mask) {
            viewport.render();
            return;
        }
        const mapper = vtkImageMapper.newInstance();
        mapper.setInputData(maskImageData({ mask, descriptor, sliceIndex, vtk }));
        maskActor = vtkImageSlice.newInstance();
        maskActor.setMapper(mapper);
        const property = maskActor.getProperty();
        property.setColorWindow(1);
        property.setColorLevel(0.5);
        property.setOpacity(MASK_OPACITY);
        property.setRGBTransferFunction?.(null);
        viewport.addActor({ uid: MASK_ACTOR_UID, actor: maskActor });
        viewport.render();
    }

    /**
     * The arch as it stands, in the slice pixels the baker speaks.
     *
     * @param {(world: number[]) => number[]} indexFor world -> RAS index.
     */
    function readArch(indexFor) {
        if (!archUid) {
            return null;
        }
        const drawn = annotation.state.getAnnotation(archUid);
        return (drawn?.data?.handles?.points ?? []).map((point) => {
            const [x, y] = indexFor(point);
            return [x, y];
        });
    }

    /**
     * Report edits.
     *
     * Two events, because the surface does two different things with them: a drag *moves*
     * the live CPR, and a drag *release* re-fits the arch and re-bakes. Cornerstone fires
     * `ANNOTATION_MODIFIED` throughout and `ANNOTATION_COMPLETED` at the end.
     */
    function bindEditing(indexFor) {
        element.addEventListener(toolsEnums.Events.ANNOTATION_MODIFIED, () => {
            const arch = readArch(indexFor);
            if (arch) {
                onArchDragged(arch);
            }
        });
        for (const name of [toolsEnums.Events.ANNOTATION_COMPLETED, 'mouseup', 'touchend']) {
            element.addEventListener(name, () => {
                const arch = readArch(indexFor);
                if (arch) {
                    onArchEdited(arch);
                }
            });
        }
    }

    return {
        viewport,
        setVolume,
        setArch,
        setMask,
        showSlice,
        readArch,
        bindEditing,
        toolGroup: () => toolGroup,
        destroy() {
            if (archUid) {
                annotation.state.removeAnnotation(archUid);
                archUid = null;
            }
            ToolGroupManager.destroyToolGroup?.(TOOL_GROUP_ID);
            renderingEngine.disableElement?.(VIEWPORT_ID);
        },
    };
}
